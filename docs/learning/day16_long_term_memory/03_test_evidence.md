# Day 16 测试、评估与运行证据

## 1. 自动化测试覆盖

### 正常路径

- 用户明确语言偏好写入后，可在同一客户的两个不同 Ticket/Agent Run 中召回；
- 同值重复写入返回 `unchanged`，不创建重复版本；
- 严格更新的不同值使旧版本 `superseded`、新版本 `active`；
- Supervisor 删除后所有版本原文清空，重复删除按幂等键重放；
- API 创建、查询、删除和响应 Schema 正常；
- SQLite SQL Repository 和真实 PostgreSQL 都完成写入、Ticket 绑定召回、删除与审计。

### 失败路径

- `model_inference` 返回 `confirmation_required` 且不写数据库；
- 非白名单业务事实、低置信度、非法 value、超最大 TTL 返回拒绝/422；
- Agent 没有删除权限，客户不能访问其他 subject；
- 相同幂等键修改业务参数返回 409；
- Memory Repository 读取失败时，Agent 安全调查继续，Context Trace 记录 `dependency_unavailable`；
- SQLAlchemy 异常统一转换成 Memory 存储/读取错误，API 返回 503 而不是伪装成空记忆。

### 边界路径

- 不同租户相同 subject/key 严格隔离；
- 过期 active 记录不被召回；
- 同时间或更旧的不同值使相关版本全部 `conflicted`，不再召回；
- 冲突后严格更新的可信候选可以恢复单一 active 版本；
- 删除 active、superseded、conflicted、expired 的全部原文，只保留哈希；
- `request_id/trace_id` 改变不破坏业务幂等，value 改变必须冲突；
- SQLite 迁移 upgrade/downgrade 和 ORM metadata 同步检查通过；
- 固定时钟下审计事件时间相同，测试不再错误使用随机 UUID 推断先后顺序。

## 2. Day16 专项测试

```powershell
$env:TEST_DATABASE_URL = `
  'postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test'
.\.venv\Scripts\python.exe -m pytest `
  tests\test_long_term_memory.py `
  tests\test_agent_memory_context.py `
  tests\test_memories_api.py `
  tests\test_day16_evaluation.py `
  tests\integration\test_memory_repository.py `
  tests\integration\test_memory_postgres.py `
  tests\integration\test_migrations.py -q `
  --basetemp=.pytest_tmp_day16_retry
```

结果：

```text
20 passed
```

这 20 个专项测试同时覆盖领域策略、授权、多租户、幂等、冲突/过期/删除生命周期、API、Agent Context、SQL Repository、真实 PostgreSQL、Alembic 和固定数据集评估。

## 3. 固定数据集评估设计

数据集：`evaluation/datasets/day16_long_term_memory_v1.json`

脚本：`evaluation/run_day16_long_term_memory.py`

原始报告：`evaluation/reports/day16_long_term_memory_v1_report.json`

数据集版本为 `day16-long-term-memory-v1`，明确标记 `synthetic: true`，共 80 例：

- 20 例明确、稳定且允许写入的用户偏好；
- 20 例未经确认的模型推断；
- 20 例不允许进入长期记忆的业务事实；
- 20 例生命周期问题：过期、冲突、删除、正常更新各 5 例。

对照基线 `SaveEverythingBaseline` 模拟常见错误方案：保存全部原始聊天，忽略 key 白名单、来源、TTL、冲突和删除，召回最后追加内容。最终方案运行真实 `MemoryService`。两者使用相同输入，不调用付费模型，也不调用 embedding。

## 4. 评估真实输出

```text
dataset: day16-long-term-memory-v1
synthetic cases: 80
paid model calls: 0
embedding calls: 0

save-everything baseline:
  correct handling: 31.25%
  unsafe writes: 40
  unsafe recalls: 15
  average memory payload: 2631.38 characters

policy-controlled memory:
  correct handling: 100.00%
  unsafe writes: 0
  unsafe recalls: 0
  average memory payload: 3.56 characters

measured change:
  correct handling: +68.75 percentage points
  unsafe writes blocked: 40
  unsafe recalls blocked: 15
  memory payload reduction: 99.86%
```

复现命令：

```powershell
.\.venv\Scripts\python.exe `
  evaluation\run_day16_long_term_memory.py `
  --output evaluation\reports\day16_long_term_memory_v1_report.json
```

## 5. 如何正确理解这些数据

100% 是当前固定合成案例上的确定性策略处理率，不是线上客服任务成功率。99.86% 是与“保存整段聊天”这一特定错误基线相比的持久化 value 字符数变化，不等于真实模型 Token、延迟或成本下降。该评估可以证明白名单、来源门槛、生命周期和最小化存储按设计工作，但不能证明生产用户满意度。

后续要形成线上价值证据，还需要使用脱敏真实失败样本，测量错误个性化率、人工确认率、冲突率、删除完成率、Memory 对 Context Token 的贡献、任务完成率和用户满意度。

## 6. 全量回归

```powershell
$env:TEST_DATABASE_URL = `
  'postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test'
.\.venv\Scripts\python.exe -m pytest -q `
  --basetemp=.pytest_tmp_day16_full_retry
```

结果：

```text
219 passed
```

全量回归覆盖 Day1–Day16 的 HTTP、事务、认证、多租户、异步任务、模型网关、工具、Agent Loop、LangGraph checkpoint、退款审批、MCP、知识入库、混合检索、引用门禁、Context Builder 和长期记忆。

## 7. 数据库与工程检查

```text
alembic heads:   20260805_0011 (head)
alembic current: 20260805_0011 (head)
alembic check:   No new upgrade operations detected.
pip check:       No broken requirements found.
compileall:      passed
PostgreSQL:      healthy
Redis:           healthy
```

开发 PostgreSQL 已从 `20260804_0010` 实际升级到 `20260805_0011`。真实 PostgreSQL 集成测试连接专用 `resolveflow_test` 数据库，不对开发数据执行清空操作。

## 8. 当前明确边界

- 过期记录在读取时立即过滤，但只有下次同 key 变更时才把状态持久化为 `expired`，尚无后台清理任务；
- API 尚无分页、批量确认和面向客户的 consent UI；
- 当前 key 数量很少，刻意不使用 embedding 或向量检索；
- 首次创建同一全新 key 的极端并发由唯一约束保证不重复，失败方返回可重试存储错误，尚未自动重新读取后重算；
- 评估使用合成数据和确定性逻辑，没有真实模型质量、Provider 延迟或线上业务收益结论；
- Day17 才继续覆盖外部内容 prompt injection、敏感信息脱敏和更完整的 Context 安全测试。
