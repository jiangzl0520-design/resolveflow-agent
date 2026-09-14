# Day 3 测试证据

## 1. 环境和迁移

- WSL：2.7.10.0
- Docker Desktop：4.83.0
- Docker Engine：29.6.2
- PostgreSQL：18.3-alpine，healthy
- Redis：8.2-alpine，healthy
- Alembic head：`20260724_0002`

迁移新增：

- `tickets.version INTEGER NOT NULL DEFAULT 1`；
- `idempotency_keys`；
- 复合主键 `pk_idempotency_keys(operation, key)`；
- 指向 `tickets.id` 的外键；
- created_at 清理索引。

## 2. 快速测试

未配置真实 PostgreSQL 时：

```text
20 passed, 1 skipped in 1.28s
```

跳过的是带 `postgres` marker 的真实并发测试，不计作 PostgreSQL 通过证据。

## 3. 真实 PostgreSQL 首次失败证据

第一次运行发现：

```text
ForeignKeyViolation:
idempotency_keys.resource_id 引用的 ticket 尚不存在
```

原因：同一个 Session 保证事务相同，但在没有 ORM relationship 时，没有保证不同 Mapper 按业务期望的父子顺序 INSERT。

第一次修复把 Ticket 和 Event 一起 flush，真实测试又发现：

```text
ForeignKeyViolation:
ticket_events.ticket_id 引用的 ticket 尚不存在
```

最终写入顺序改为：

```text
add Ticket
→ flush Ticket
→ add Event
→ add IdempotencyRecord 并 flush
→ commit
```

flush 没有 commit，后续失败仍能完整 rollback。没有为了让测试通过而移除外键。

## 4. PostgreSQL 专项测试

命令：

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test"
.\.venv\Scripts\python.exe -m pytest -W error -m postgres
```

最终结果：

```text
3 passed, 20 deselected in 0.31s
```

覆盖：

1. 真实迁移和 Ticket INSERT/SELECT 往返。
2. 两线程并发相同幂等创建：同一个 ticket_id，一个 replay，只有一条 created Event。
3. 两线程并发消费 version 1：一个成功、一个 ConcurrentTicketUpdateError，最终 version 2，只有一条状态变化 Event。

## 5. 完整回归

命令：

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test"
.\.venv\Scripts\python.exe -m pytest -W error
```

最终结果：

```text
....................... [100%]
23 passed in 1.74s
```

没有 skip，没有 warning。

## 6. 正常、失败和边界覆盖

### 正常路径

- 首次幂等创建返回 201 和 replayed=false。
- 相同输入重试返回同一 ticket_id 和 replayed=true。
- 合法 `open → investigating`，version 从 1 变为 2。
- created 和 status_changed 审计事件按时间读取。

### 失败路径

- 存储异常安全映射为 503，不泄漏底层文本。
- flush 外键失败会回滚完整事务。
- 并发唯一键竞争只有一个创建者。
- 并发版本竞争只有一个更新者。

### 边界路径

- 缺少 Idempotency-Key 返回 422。
- 同 key 不同输入返回 409。
- `open → resolved` 返回 409，Ticket 和 Event 均不变化。
- stale expected_version 返回 409，不覆盖新状态。
- migration downgrade 删除新增幂等表和 version 列。

## 7. 静态和迁移同步

```text
python -m compileall -q app migrations tests  → exit 0
alembic check → No new upgrade operations detected.
```

## 8. 尚未覆盖

- 没有做高并发压力测试，不能声称具体吞吐量。
- 没有幂等记录过期和清理 Worker。
- 没有 Day 4 的身份认证，状态事件 actor 暂用 system/customer 类型，尚未绑定真实员工身份。
- 没有 Agent 动作；这些能力只是未来 Agent 的确定性后端边界。
