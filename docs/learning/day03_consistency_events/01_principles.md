# Day 3 原理：状态机、幂等、乐观锁与审计事件

## 1. 今天解决的真实问题

售后 API 面对的请求不是理想的单线程调用：

- 手机网络超时后，客户端会重试创建请求；
- 网关或任务 Worker 也可能自动重试；
- 两名客服可能同时处理同一工单；
- Agent 和人工操作可能基于不同时间读取的状态；
- 数据库写入一半失败时，工单和审计不能互相矛盾。

Day 3 用四项确定性后端能力解决这些问题：

- 状态机：限制业务允许的状态边；
- 幂等键：把同一业务意图的重试识别为同一次操作；
- 乐观锁：拒绝基于旧版本的覆盖；
- Unit of Work：让工单、事件和幂等记录原子提交。

这些能力以后会约束 Agent 的动作，但今天没有实现 Agent Runtime。不能把“建了 Agent 以后要用的表”说成 Agent 已经具备状态管理能力。

## 2. 创建工单的完整流程

```text
客户端 POST /api/v1/tickets
  + JSON 业务字段
  + Idempotency-Key
→ Middleware 生成本次 HTTP request_id
→ Schema 校验业务字段，Header 校验幂等键格式
→ Router 翻译为 CreateTicketCommand
→ Service 对业务字段生成规范化 SHA-256 指纹
→ Unit of Work 创建一个短生命周期 Session
→ 查询 (operation=create_ticket, key)
```

如果记录已经存在：

```text
指纹相同
→ 读取第一次创建的 Ticket
→ 返回同一个 ticket_id
→ Idempotency-Replayed: true
→ 不新增 Ticket，不新增 Event
```

```text
指纹不同
→ 409 idempotency_conflict
→ 不覆盖第一次请求，也不创建第二张工单
```

如果记录不存在：

```text
构造 Ticket(version=1, status=open)
→ add Ticket
→ flush Ticket，使外键目标在当前事务中存在
→ add ticket_created Event
→ add IdempotencyRecord 并 flush
→ commit
→ 返回 201 与 Idempotency-Replayed: false
```

Service 的“先查询”只能加速普通重试，不能独立解决并发。两个请求可能同时查询到不存在，所以 PostgreSQL 的复合主键 `(operation, key)` 才是最终裁决者。竞争失败的一方回滚自己的 Ticket 和 Event，再读取胜者提交的结果。

## 3. 为什么幂等键必须同时绑定请求指纹

只保存 `key → ticket_id` 会产生危险歧义：

1. 用户第一次用 key `abc` 创建“包裹未收到”。
2. 客户端错误地再次用 `abc` 创建“商品损坏”。
3. 如果系统直接返回旧 ticket_id，调用者会误以为第二个业务请求已经成功。

因此幂等记录保存：

```text
operation + key + request_hash + resource_id
```

相同 key 只有在 operation 和请求指纹都相同时才是合法 replay。请求指纹只覆盖决定业务结果的字段，不包含每次重试都会变化的 request_id。

## 4. 工单状态机

当前状态边：

```text
open
→ investigating

investigating
→ waiting_for_customer | pending_approval | resolved

waiting_for_customer
→ investigating

pending_approval
→ investigating | resolved

resolved
→ investigating | closed

closed
→ 无后继状态
```

状态机属于 Domain，因为它表达的是“业务允许什么”，与 HTTP、SQLAlchemy 和 PostgreSQL 无关。

Schema 只会确认 `target_status` 是系统认识的枚举值。它不能确认 `open → resolved` 是否符合流程；这必须由 Domain 根据当前状态判断。

## 5. 乐观锁的完整执行过程

客户端更新状态时必须提交 `expected_version`：

```text
读取 Ticket(status=open, version=1)
→ 请求 target_status=investigating, expected_version=1
→ Service 快速比较当前 version
→ Domain 校验状态边
→ 构造 Ticket(status=investigating, version=2)
→ Repository 执行条件更新
```

核心 SQL 语义：

```sql
UPDATE tickets
SET status = 'investigating', version = 2
WHERE id = :ticket_id AND version = 1;
```

- 影响 1 行：调用者看到的 version 仍有效；
- 影响 0 行：读取与更新之间已有其他事务修改，返回 409；
- 不能在冲突后自动覆盖，因为系统不知道哪个业务决定更正确。

Service 的读取后比较无法消除竞态窗口。数据库 `WHERE version = expected_version` 才能防止“比较结束后、UPDATE 之前”发生的并发覆盖。

## 6. Unit of Work、flush、commit 和 rollback

### Unit of Work

一次业务用例共享一个 Session，使三个 Repository 属于同一事务：

```text
TicketRepository
TicketEventRepository
IdempotencyRepository
          ↓
同一个 Session / Transaction
```

### flush

`flush` 把当前待处理变更发送到数据库，让数据库执行 SQL 和约束检查，但不提交事务。其他事务仍看不到这些未提交数据。

真实 PostgreSQL 测试证明：没有 ORM relationship 时，不能假设 SQLAlchemy 会按我们期望的父子顺序插入不同 Mapper。创建流程因此明确先单独 flush Ticket，再添加引用 Ticket 的 Event 和 IdempotencyRecord。

### commit

`commit` 才让全部变更永久可见。它成功后，工单、事件和幂等记录同时成立。

### rollback

任意 flush、约束或 commit 失败，Unit of Work rollback。即使 Ticket 已经在前一个 flush 中发送给 PostgreSQL，只要尚未 commit，仍会被完整撤销。

## 7. 审计事件与普通日志的区别

`ticket_events` 保存业务事实：

- `ticket_created`；
- `ticket_status_changed`，包含前后状态和前后版本。

业务事件与 Ticket 在同一事务中提交，因此不会出现“状态已变但事件缺失”或“事件说变了但状态回滚”。

普通日志主要用于排障，可能被采样、轮转或集中收集；审计事件属于业务数据，需要稳定结构、外键和事务一致性。二者不能互相替代。

## 8. 错误语义

- 缺少或格式错误的 Idempotency-Key：422，协议输入不合法。
- 相同 key、不同请求指纹：409 `idempotency_conflict`。
- 非法状态边：409 `invalid_ticket_transition`。
- expected_version 过期或并发 UPDATE 失败：409 `concurrent_ticket_update`。
- 工单不存在：404 `ticket_not_found`。
- 数据库无法完成查询、flush 或 commit：503 `storage_unavailable`。

这些冲突不能全部变成 500，也不能全部重试。幂等冲突、状态冲突和并发冲突需要调用者修正输入或重新读取最新状态；存储临时故障才可能按退避策略重试。

## 9. 测试分层和证据关系

```text
Domain/API + SQLite
→ 快速验证状态规则、HTTP 契约、多表事务和错误映射

真实 PostgreSQL
→ 验证 UUID/JSONB/外键、唯一键竞争、行级条件更新和并发结果

全量回归
→ 确认 Day 1、Day 2 功能没有被破坏
```

真实 PostgreSQL 测试同时释放两个线程：

- 同一幂等创建最终一个资源、一个审计事件；
- 同一 version 的两个更新最终一个成功、一个冲突。

最终证据是 PostgreSQL 专项 `3 passed`、完整测试 `23 passed`、`alembic check` 无遗漏迁移。

## 10. 官方资料

- [SQLAlchemy Versioning](https://docs.sqlalchemy.org/en/20/orm/versioning.html)
- [SQLAlchemy Transactions and Connection Management](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html)
- [PostgreSQL Constraints](https://www.postgresql.org/docs/current/ddl-constraints.html)
- [PostgreSQL Transaction Isolation](https://www.postgresql.org/docs/current/transaction-iso.html)
- [FastAPI Header Parameters](https://fastapi.tiangolo.com/tutorial/header-params/)
