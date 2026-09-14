# Day 5 关键代码

## 1. 代码在整体架构中的位置

```text
HTTP
  app/api/routes/investigation_jobs.py
  app/schemas/investigation_job.py
        ↓
Application Service
  app/services/investigation_job_service.py
        ↓
Domain
  app/domain/investigation_job.py
        ↓
Repository / Unit of Work / PostgreSQL
  app/repositories/sqlalchemy_investigation_job_repository.py
  app/repositories/sqlalchemy_investigation_job_event_repository.py
  app/db/models.py
        ↓
Message Adapter
  app/worker/celery_dispatcher.py
        ↓
Redis / Celery
        ↓
Worker Adapter
  app/worker/tasks.py
        ↓
Worker Application Service
  app/services/investigation_worker_service.py
        ↓
Ticket + Job 同一数据库事务
```

这条链路仍然遵守协议适配、业务编排、领域规则和基础设施分离。Router 不实现重试，Celery Task 不实现 Ticket 业务规则，Repository 不决定状态转移。

## 2. `InvestigationJob` 领域状态机

文件：`app/domain/investigation_job.py`

### 输入和输出

- 输入：当前不可变 Job、当前时间、lease token、lease duration 或目标状态需要的数据。
- 输出：一个新的不可变 Job。
- 异常：当前状态不允许操作时抛出 `JobNotClaimableError` 或 `JobStateTransitionError`。

### 实际执行

`claim()` 检查终态、取消、重试时间、最大次数和已有租约，然后生成：

```text
status=running
attempts + 1
version + 1
new lease_token
new lease_expires_at
```

`schedule_retry()` 清除租约、保存 `next_attempt_at` 和 `last_error_code`；达到最大尝试次数时直接进入 `failed`。

### 为什么这样写

状态变化集中在 Domain 后，API、Worker 和测试不能随意写字符串状态。不可变对象让“修改前”和“修改后”可以明确比较，也便于 Repository 使用 expected version。

### 状态和异常

非法状态不会被静默纠正。例如 succeeded Job 再次 claim 会得到 terminal，而不是再次执行。过早到达的 retry 会携带还需等待的秒数。

## 3. 数据模型和迁移

文件：

- `app/db/models.py`
- `migrations/versions/20260724_0004_add_durable_investigation_jobs.py`

新增：

- `investigation_jobs`：当前可恢复状态；
- `investigation_job_events`：不可变状态转换轨迹。

关键约束：

```text
UNIQUE(tenant_id, job_type, idempotency_key)
investigation_jobs.ticket_id → tickets.id
investigation_job_events.job_id → investigation_jobs.id
```

输入是 Domain Job/Event，输出是数据库记录。Repository 负责 enum、JSON roles 和 UTC 时间之间的转换。

唯一约束是并发幂等的最终防线：两个 API 线程同时查不到现有 Job 时，数据库仍只允许一个提交成功。

## 4. 提交服务 `InvestigationJobService.submit`

文件：`app/services/investigation_job_service.py`

### 输入

- 已认证 `AuthenticatedActor`；
- `ticket_id`；
- `Idempotency-Key`；
- `request_id/trace_id`。

### 输出

`SubmitInvestigationJobResult`：

- Job 当前快照；
- 是否为幂等 replay；
- 是否仍待投递。

### 执行流程

1. tenant-scoped 查询 Ticket。
2. 检查 `INVESTIGATION_JOB_SUBMIT` 和资源边界。
3. 计算 `ticket_id + job_type` 的稳定摘要。
4. 同事务写入 queued Job 和 created event。
5. 提交后调用 dispatcher。
6. 记录 dispatch success/failure。
7. 返回 202 所需快照。

### 为什么数据库提交必须在消息发送前

Worker 收到消息时必须已经能读到 Job。发送失败不会删除已经受理的任务，而是变成可补偿的 pending dispatch。

### 异常

- 同 key、不同 ticket：409 idempotency conflict；
- 无权限：403；
- 跨 tenant 或 Ticket 不存在：404；
- Redis 不可用：不是提交失败；Job 保留并返回 dispatch pending；
- 并发唯一键冲突：加载数据库中的胜者并返回 replay。

## 5. Celery dispatcher

文件：`app/worker/celery_dispatcher.py`

关键消息契约：

```python
send_task(
    "resolveflow.process_investigation_job",
    args=[str(job.id)],
    task_id=str(job.id),
    headers={
        "request_id": job.request_id,
        "trace_id": job.trace_id,
    },
    retry=False,
)
```

输入是已持久化 Job，输出没有业务返回值。`retry=False` 表示 dispatcher 本身不在调用栈中无限等待 Redis；失败被统一翻译成 `BrokerUnavailableError`，由 PostgreSQL pending 状态和 Beat 补偿。

消息不携带 tenant、roles、Ticket 快照和审批结论。Worker 必须回数据库读取可信事实。

## 6. Celery 配置

文件：`app/worker/celery_app.py`

关键配置：

```text
JSON serializer only
late ACK
reject on worker lost
prefetch = 1
ignore result backend
soft/hard time limit
Redis visibility timeout
30 秒 pending-dispatch Beat schedule
```

Celery 解决投递、调度、Worker 生命周期和 timeout；它不替代 Job 领域状态机，也不保存最终业务结果。

## 7. Worker claim 和 fencing

文件：`app/services/investigation_worker_service.py`

### 输入和输出

- 输入：`job_id` 和 `worker_id`；
- 输出：`WorkerOutcome`，类型为 succeeded、retry、terminal 或 missing；
- retry 还携带 countdown 和 reason。

Worker 先用 `SqlAlchemyInvestigationJobLocator` 找可信 tenant，再在 tenant UoW 中读取 Job。

最关键的提交前检查：

```python
return (
    current.status is InvestigationJobStatus.RUNNING
    and current.lease_token == claimed.lease_token
    and current.version == claimed.version
)
```

这段代码不是普通“状态判断”。它是 fencing：只允许仍拥有当前租约的 Worker 提交。新 Worker 接管后，旧 token/version 会失效。

### 异常处理

- claim race：1 秒后重试；
- retry 尚未到时间：按数据库给出的剩余时间重试；
- soft timeout/临时错误：指数退避；
- permanent error：failed；
- max attempts：failed；
- Worker 已失去租约：观察数据库现状，不再提交；
- cancel requested：转 cancelled；
- 未知异常：按可恢复错误处理，但仍受最大次数限制。

## 8. 最终副作用的原子提交

文件：`app/services/investigation_worker_service.py` 的 `_complete()`

一个 Unit of Work 同时执行：

```text
Ticket open → investigating
新增 Ticket status event
Job running → succeeded
新增 Job succeeded event
COMMIT
```

输入是仍拥有租约的 claimed Job 和 executor preparation，输出是 succeeded outcome。

如果 version、租约或 Ticket version 竞争失败，事务不产生半完成结果。重复消息发现 Ticket 已经 investigating、Job 已终态时不会再新增同一副作用。

## 9. 取消实现

文件：

- `app/services/investigation_job_service.py`
- `app/services/investigation_worker_service.py`

API 对 queued/retry 任务立即写 cancelled；对 running 任务写 cancel_requested。Worker 在 executor 前、executor 后、最终事务前检查数据库。

输入是已授权 actor 和 job_id，输出是最新 Job。取消不是删除记录，全部状态变化都有事件。

## 10. 投递补偿

文件：

- `app/worker/recovery_runtime.py`
- `app/worker/tasks.py`

`recover_pending_investigation_jobs` 每 30 秒调用：

```text
locator.list_pending_dispatch(now, limit)
→ tenant-scoped load
→ dispatcher.dispatch
→ record dispatch result + event
```

多个补偿任务并发时可能重复发送，但数据库 claim 保证只有一个 Worker 能提交副作用。这里选择“允许重复投递、禁止重复业务结果”，而不是依赖脆弱的进程锁。

## 11. API 与 Schema

文件：

- `app/api/routes/investigation_jobs.py`
- `app/schemas/investigation_job.py`
- `app/api/dependencies.py`
- `app/main.py`

Router 负责：

- 路径/方法匹配；
- 读取 `Idempotency-Key`；
- 把 HTTP 输入转为 Command；
- 返回 202、Location、Idempotency-Replayed、Dispatch-Pending；
- 序列化 Job/Event。

Router 不计算退避时间、不决定租约、不直接写 Repository。

Middleware 同时生成/传播 request_id 和 trace_id，并写入响应头。

## 12. 配置顺序门禁

文件：`app/core/config.py`

Settings 启动时强制：

```text
soft time limit
< hard time limit
< job lease
< broker visibility timeout
```

错误配置会在应用启动时失败，不会等线上任务运行后才暴露。输入来自环境变量，输出是不可变 Settings。

## 13. 可重复评估代码

文件：

- `evaluation/datasets/day05_duplicate_delivery_v1.json`
- `evaluation/run_day05_reliability.py`
- `evaluation/reports/day05_reliability_v1.json`

评估对同一批 25 个合成任务各投递 3 次，并给 5 个任务写入过期租约。

基线消费者每次投递都直接产生副作用；ResolveFlow 使用真实 Job Domain、SQLAlchemy Repository、迁移和 WorkerService 执行。测试断言报告中的重复副作用和恢复数，防止报告与代码脱节。

报告中的延迟只代表本地 SQLite 实验，不能作为生产 SLO。
