# Day 5 测试与运行证据

## 1. 环境

- 日期：2026-07-24
- Python：3.12.13
- Celery：5.6.3
- SQLAlchemy：2.0.51
- Alembic：1.18.5
- PostgreSQL：18.3-alpine，Docker healthcheck healthy
- Redis：8.2-alpine，Docker healthcheck healthy
- Alembic head：`20260724_0004`
- 操作系统：Windows 11

Celery 官方不支持 Windows Worker，所以这里不声称完成了 Linux 生产进程运行证明。已真实运行的是领域 Worker、Celery task wrapper、真实 PostgreSQL 并发测试、真实 Redis broker 连接和全部 API；Linux 容器化 Worker 的进程级验收留在部署阶段。

## 2. 依赖与基础设施

新增依赖：

```text
celery[redis]==5.6.3
```

最终检查：

```text
pip install -e . --no-build-isolation
→ Successfully installed resolveflow-agent-0.1.0

pip check
→ No broken requirements found.
```

Redis 运行配置实测：

```text
maxmemory-policy
noeviction
appendonly
yes
```

Celery 使用真实 Redis 建立 broker 连接：

```text
broker_connected=True
transport=redis
```

## 3. 数据库迁移

迁移：

```text
20260724_0003
→ 20260724_0004 Add durable investigation jobs and transition events
```

新增表：

- `investigation_jobs`
- `investigation_job_events`

迁移自动化检查覆盖：

- upgrade 创建表；
- Ticket/Job/Event 外键；
- tenant_id 非空；
- Job version/lease/retry/cancel/trace 字段；
- tenant + job type + idempotency key 唯一约束；
- downgrade；
- Alembic 与 ORM metadata 同步。

开发 PostgreSQL 已实际升级到 0004，测试 PostgreSQL 每次集成测试自动升级到 head。

## 4. Day 5 专项场景

`tests/test_investigation_jobs.py` 覆盖：

1. API 以 202 持久化受理 Job，返回 Location、trace 和投递状态。
2. 相同 key/相同输入 replay 同一 Job，只做一次顺序投递。
3. 相同 key/不同 Ticket 返回 409。
4. Redis 投递失败后 Job 保持 pending，补偿扫描成功重发。
5. Worker 把 Ticket 与 Job 结果原子提交。
6. 临时错误按退避等待后成功，不重复 Ticket 副作用。
7. 三次失败后进入 terminal failed。
8. queued 任务取消后 Worker 是 no-op。
9. running 任务收到取消后，旧 lease 不能提交。
10. 模拟 Worker 崩溃，过期租约被新 Worker 恢复。
11. 新 lease 获胜后，旧 Worker 的 version update 被拒绝。
12. customer 不能提交调查 Job。
13. 跨 tenant Job 隐藏为 404。

`tests/test_celery_worker.py` 覆盖：

- late ACK、worker-lost requeue、prefetch、visibility 和 route 配置；
- timeout/lease/visibility 错误顺序启动失败；
- 消息只包含 job_id 与 trace headers；
- Redis/Kombu 异常映射为可补偿应用错误；
- Celery process task 实际委托 WorkerService 并关闭数据库 runtime；
- Beat recovery task 实际执行 pending scan 并关闭 runtime。

## 5. 真实 PostgreSQL 专项

命令：

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test"
.\.venv\Scripts\python.exe -m pytest -W error -m postgres
```

结果：

```text
6 passed, 49 deselected in 0.62s
```

Day 5 新增的真实数据库场景让两个线程同时运行同一 Job：

- 最终 Job `succeeded`；
- `attempts == 1`；
- Ticket 只从 open 变为 investigating 一次；
- 只产生一个携带该 job_id 的状态副作用；
- 两个 Worker 中只有一个返回 succeeded。

这证明防重不是 SQLite 单线程假象，PostgreSQL 条件 UPDATE 和 version fencing 真实参与了竞争。

## 6. 全量回归

命令：

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test"
.\.venv\Scripts\python.exe -m pytest -W error
```

最终结果：

```text
58 passed
```

`-W error` 下没有 warning。Day 1–4 的 HTTP、迁移、幂等、并发、认证、RBAC 和多租户能力没有回归。

## 7. 可重复量化实验

数据集：

`evaluation/datasets/day05_duplicate_delivery_v1.json`

运行：

```powershell
.\.venv\Scripts\python.exe evaluation\run_day05_reliability.py `
  --output evaluation\reports\day05_reliability_v1.json
```

实验输入明确标记为合成数据：

- 25 个唯一 Job；
- 每个 Job 交付 3 次；
- 总交付 75 次；
- 其中 5 个 Job 预置过期租约，模拟 Worker 崩溃后的遗留状态。

原始报告：

`evaluation/reports/day05_reliability_v1.json`

本次结果：

| 指标 | 无幂等 claim 基线 | ResolveFlow |
|---|---:|---:|
| 总交付 | 75 | 75 |
| 业务副作用 | 75 | 25 |
| 重复副作用 | 50 | 0 |
| 恰好一个副作用的 Job | 不适用 | 25/25 |
| 过期租约恢复 | 不适用 | 5/5 |
| 本地 P50 单次交付延迟 | 未作为目标 | 0.932 ms |
| 本地 P95 单次交付延迟 | 未作为目标 | 11.814 ms |

解释边界：

- 这是合成可靠性实验，不是生产收入、节省金额或真实客户成功率；
- baseline 明确定义为“至少一次交付但没有数据库幂等 claim 的消费者”；
- 延迟来自本机临时 SQLite，只能证明脚本可测，不能外推生产 SLO；
- PostgreSQL 的并发正确性由上一节的真实数据库测试单独证明。

`tests/test_day05_evaluation.py` 对报告核心不变量做回归断言，避免只保存一次好看的 JSON 后代码退化。

## 8. 实现过程中发现并解决的问题

### 8.1 Celery 依赖安装两次超时

第一次 editable install 和随后单独安装都因下载时间超过命令限制而退出。没有把 timeout 当成功；扩大允许时长后完成安装，再用 editable metadata refresh 与 `pip check` 验证。

### 8.2 Docker CLI 不在当前 Codex 终端 PATH

Docker Desktop 已运行，但当前终端继承的是安装前 PATH。通过正在运行的进程定位到：

```text
C:\Users\16696\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe
```

直接使用绝对路径完成 compose/Redis 核验，没有要求用户再次重启。

### 8.3 投递恢复最初只有服务方法

最初测试能够手动调用 `recover_pending_dispatches()`，但工程运行仍依赖人工。复核后增加 Celery Beat 每 30 秒触发的 recovery task，并添加 task wrapper 测试。

### 8.4 不伪造 exactly-once

Celery 配置不能证明恰好一次。最终验收改为：

- 接受消息重复；
- 数据库 claim 只允许一个 Worker 获得执行权；
- Ticket 副作用、Ticket event、Job success、Job event 同一事务；
- 用 75 次合成交付和 PostgreSQL 双 Worker 竞争证明重复副作用为 0。

## 9. 当前没有过度宣称的能力

Day 5 尚未证明：

- Linux 容器内 Celery Worker/Beat 的持续运行；
- 真实云 Redis 故障切换；
- 跨区域消息复制；
- transactional outbox 或 CDC；
- 外部退款 API 的端到端 exactly-once；
- 长任务 lease heartbeat；
- Agent checkpoint 和节点级恢复；
- OpenTelemetry 分布式 trace；
- 生产吞吐、P95/P99 或业务收益。

这些分别属于后续部署、工具执行、Agent Runtime 和可观测性阶段。
