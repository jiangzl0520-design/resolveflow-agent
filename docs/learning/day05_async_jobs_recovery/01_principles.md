# Day 5 原理：异步任务、重试、取消与恢复

## 1. 今天解决的真实问题

售后调查可能需要查询多个外部系统、等待模型、检索证据，甚至暂停等待人工。HTTP 请求不能一直占着连接：

- 网关、浏览器或反向代理可能先超时；
- API 进程重启会丢失进程内任务；
- Worker 可能被杀死、断网或执行到一半崩溃；
- Redis 可能暂时不可用；
- Celery 可能把同一消息投递多次；
- 用户可能在任务执行期间请求取消；
- 重试如果再次执行副作用，未来可能造成重复退款。

Day 5 的目标不是让 Celery “尽量跑”，而是让任务状态可查询、失败可解释、中断可恢复、重复投递不重复产生业务副作用。

今天实现的是确定性的后端执行底座，不是 Agent Runtime。它会在后续承载 Agent Run，但当前没有模型决策、Observation、计划修订或 AgentState。

## 2. 完整操作流程

```text
客户端
  POST /tickets/{ticket_id}/investigation-jobs
  Authorization + Idempotency-Key
→ Middleware 生成 request_id 和 trace_id
→ Router/Schema 校验 HTTP 输入
→ InvestigationJobService
     检查权限和 tenant/resource
     根据 ticket_id + job_type 计算请求摘要
     PostgreSQL 事务写入 Job(queued) + job_created
→ 事务提交后尝试发送 Celery 消息
     消息正文只含 job_id
     headers 只传 request_id/trace_id
→ 成功：记录 dispatched_at + job_dispatched
→ Redis 失败：记录 job_dispatch_failed，HTTP 仍返回 202 + Dispatch-Pending:true
→ Celery Worker 收到 job_id
→ system-only locator 从 PostgreSQL 找到可信 tenant_id
→ tenant-scoped Unit of Work 读取 Job
→ 原子竞争 lease_token + version
→ 执行可重试的调查准备步骤
→ 再检查取消和租约所有权
→ 一个数据库事务写入：
     Ticket open → investigating
     ticket_status_changed
     Job running → succeeded
     job_succeeded
→ Worker 完成后才确认消息
```

如果投递第一次失败：

```text
PostgreSQL 中 Job 仍存在且 dispatched_at 为空
→ Celery Beat 每 30 秒触发补偿任务
→ 扫描待投递 Job
→ 再次发送
→ 成功后记录 dispatched_at
```

如果 Worker 崩溃：

```text
Job 保持 running，lease_expires_at 仍在 PostgreSQL
→ Celery 未收到 ACK，消息在 visibility timeout 后重新出现
→ 新 Worker 发现旧租约已过期
→ 获取新 lease_token，version 增加
→ 记录 job_lease_recovered
→ 从持久化状态继续执行
```

## 3. API 为什么只“受理”，不等待完成

提交接口返回 `202 Accepted`，表示任务已经被系统持久化受理，不表示调查完成。

这样拆分后：

- API 延迟不再等于任务执行时间；
- API 和 Worker 可以独立扩缩容；
- 客户端通过 Job 查询接口观察状态；
- API 重启不会删除 PostgreSQL 中的 Job；
- Worker 失败不会让原 HTTP 连接承担恢复责任。

错误做法是使用 FastAPI `BackgroundTasks` 保存关键长任务。它适合进程内、短小、允许随进程丢失的附加工作；不能单独提供跨进程队列、重新投递、租约恢复和持久化状态机。

## 4. Celery 提供“至少一次”，不是“恰好一次”

当前关键配置：

- `task_acks_late=True`：任务执行结束后再 ACK；
- `task_reject_on_worker_lost=True`：Worker 子进程异常丢失时重新入队；
- `worker_prefetch_multiplier=1`：每个 Worker 尽量只提前取一个任务；
- Redis `visibility_timeout=120`：未确认消息多久后重新可见；
- `task_ignore_result=True`：不用 Celery result backend 保存业务结果。

这些配置提高了崩溃后的重新投递能力，但也意味着同一任务可能执行多次。例如：

```text
Worker 已完成业务写入
→ ACK 之前进程崩溃
→ 消息重新投递
```

因此不能把 Celery 的 ACK 当成业务幂等。正确关系是：

```text
Celery：保证消息最终有机会再次到达
PostgreSQL Job 状态机：判断这次到达是否还能执行
数据库事务与 version/lease：阻止两个 Worker 同时提交
幂等业务状态转换：阻止重复副作用
```

“消息最多消费一次”容易丢任务；“消息至少消费一次”不会轻易丢，但要求业务自己防重。分布式系统不能只靠队列配置获得通用的恰好一次业务语义。

## 5. 为什么 PostgreSQL 是事实源，Redis 只是传输层

Redis 中的消息只带 `job_id`。完整的任务事实保存在 PostgreSQL：

- tenant、actor、原始 request_id/trace_id；
- Job 类型和当前状态；
- attempts/max_attempts；
- lease_token/lease_expires_at；
- next_attempt_at；
- cancel_requested_at；
- dispatch 状态；
- last error；
- 全部 Job transition events。

这样做有三个原因：

1. Redis 消息不能成为权限和租户事实来源，消息可能重复、陈旧或被错误构造。
2. Celery result backend 只描述任务框架结果，不等于业务 Ticket 与 Job 的一致状态。
3. Worker 恢复时必须从数据库读取最新状态，不能继续使用消息里的旧快照。

Redis 开启 AOF 和 `noeviction` 是额外保护，但不能替代数据库状态机。

## 6. 数据库提交与消息发送的双写问题

数据库事务和 Redis publish 不能放进同一个普通 ACID 事务。两种顺序都有失败窗口。

如果先发消息、后写 Job：

```text
消息已被 Worker 收到
→ 数据库提交失败
→ Worker 找不到 Job
```

所以当前选择：

```text
先提交 Job
→ 再发消息
→ 再记录投递结果
```

这会留下另一个窗口：

```text
Job 已提交
→ Redis 暂时失败
```

解决方法不是回滚已经受理的 Job，而是把它标记为 `dispatch_pending`，由 Beat 补偿扫描重新发送。

还有一种窗口：

```text
Redis 已接受消息
→ 记录 dispatched_at 前进程崩溃
→ 补偿任务再次发送
```

这会重复投递，但不会重复副作用，因为 Worker 必须先竞争数据库租约。更严格的未来替代方案是 transactional outbox；Day 5 先实现了可测试的持久化补偿模式。

## 7. Job 状态机

```text
queued
  ├─ claim → running
  └─ cancel → cancelled

running
  ├─ success → succeeded
  ├─ retryable failure → retry_scheduled
  ├─ permanent/max-attempt failure → failed
  └─ cancel request → cancel_requested → cancelled

retry_scheduled
  ├─ 到 next_attempt_at 后 claim → running
  └─ cancel → cancelled

running + expired lease
  └─ new claim → running(attempt+1, new lease, new version)
```

终态只有 `succeeded`、`failed`、`cancelled`。终态收到重复消息时不再执行副作用。

状态机的价值是把“现在允许做什么”写成确定性规则，而不是让每个 Worker 凭 if/else 猜测。

## 8. 租约、乐观锁和 fencing 的关系

租约表示某个 Worker 在有限时间内拥有执行权：

- `lease_token`：本次占有权的随机标识；
- `lease_expires_at`：占有权过期时间；
- `version`：每次状态变化递增。

Worker 完成时必须同时满足：

```text
status == running
current.lease_token == claimed.lease_token
current.version == claimed.version
```

如果旧 Worker 卡住，租约过期后新 Worker 会获得新 token 和新 version。旧 Worker 即使后来恢复，也无法使用旧 version 更新记录。这就是 fencing：不只是判断“时间是否过期”，还用不可复用的所有权标识阻止过期持有者提交。

租约不是数据库锁。数据库锁持续时间很短，只保护一次 claim/update；租约跨越外部调用，但允许超时恢复。

## 9. 重试、指数退避和错误分类

只有可恢复错误才重试：

- 临时网络错误；
- 依赖限流或短暂不可用；
- soft timeout；
- Worker 竞争失败。

永久错误直接失败：

- Ticket 不存在；
- Ticket 状态不允许调查；
- 输入或业务事实永久不合法。

指数退避：

```text
delay = min(base × 2^(attempt-1), max_delay)
```

当前默认是 2、4、8……秒，上限 60 秒，最大 3 次实际 claim。数据库保存 `next_attempt_at`，所以过早到达的重复消息只会得到“尚未到重试时间”，不会提前执行。

最大尝试次数必须由 PostgreSQL 控制。只配置 Celery 无限 retry 会让系统性错误永久循环。

## 10. soft timeout、hard timeout、lease 与 visibility 的顺序

当前配置强制：

```text
soft timeout 45s
< hard timeout 60s
< DB lease 90s
< Redis visibility timeout 120s
```

关系如下：

- soft timeout 先给任务抛出可处理异常，允许记录 retry；
- hard timeout 处理无法正常退出的任务；
- lease 比 hard timeout 长，正常任务不会在仍运行时被新 Worker 抢走；
- visibility 比 lease 长，消息重新出现时旧数据库租约已经过期，新 Worker 可以恢复。

hard timeout 可能直接杀死进程，无法保证 finally 或异常处理代码运行。因此真正恢复仍依赖 late ACK、消息重新投递和数据库租约，而不是依赖被杀死的 Worker 自己收尾。

当前没有租约 heartbeat，原因是单任务 hard limit 小于 lease。未来如果允许长时间 Agent Run，必须续租，或把运行拆为 checkpoint 化短步骤。

## 11. 取消为什么必须是协作式

取消不是删除消息，也不是直接杀进程。

- `queued/retry_scheduled`：还没执行，可以立即变为 `cancelled`；
- `running`：先变为 `cancel_requested`；
- Worker 在外部调用前后、最终提交前读取数据库；
- 看到取消后转为 `cancelled`，不提交 Ticket 副作用。

直接 hard revoke 有两个问题：

1. 不知道 Worker 正执行到副作用之前还是之后；
2. 被杀进程无法可靠写入最终状态。

协作取消能保证状态可审计。它不能自动撤销已经提交的外部副作用；未来退款工具还需要幂等键、执行后验证和必要的补偿动作。

## 12. 最终状态为什么与业务副作用放在同一事务

成功路径把以下内容放入一个 Unit of Work：

- Ticket `open → investigating`；
- `ticket_status_changed`；
- Job `running → succeeded`；
- `job_succeeded`。

如果事务失败，四者一起回滚。如果先把 Job 标成成功、再更新 Ticket，可能出现“任务显示成功，但工单没变化”；反过来也可能出现“工单已变化，任务仍显示失败并被重复执行”。

外部退款 API 无法加入本地数据库事务。后续需要使用幂等键、操作记录、执行后查询验证和补偿，而不是假装跨系统有一个本地事务。

## 13. trace 与安全上下文如何跨进程

API 生成：

- `request_id`：定位一次 HTTP 请求；
- `trace_id`：串联 API、队列和 Worker。

Job 持久化两者，响应返回 `X-Request-ID` 和 `X-Trace-ID`。Celery header 也传递它们，方便未来 OpenTelemetry 接入。

队列正文只包含 `job_id`，不相信消息里的 tenant、role 或审批结论。Worker 用 system-only locator 找到 Job 的可信 tenant，再创建 tenant-scoped Unit of Work。这样队列消息不会成为绕过多租户边界的输入通道。

## 14. 与未来 Agent Runtime 的关系

Day 5 的 Job 状态回答：

> 这个后台任务是否已受理、由谁执行、是否要重试/取消、Worker 崩溃后谁能接管？

未来 AgentState/checkpoint 回答：

> Agent 当前目标是什么、已经获得哪些 Observation、下一节点是什么、哪些证据和计划仍有效？

两者不能混为一谈：

```text
Celery/Job envelope
  承载一次 Agent Run
    Agent checkpoint
      承载节点、工具调用和推理状态
```

Celery retry 只表示“再次调度执行”，不等于 Agent 根据新 Observation 修订计划。Day 9 才会实现 Agent checkpoint 与节点恢复。

## 15. 官方资料

- [Celery Tasks：重试、late acknowledgement、幂等和 worker lost](https://docs.celeryq.dev/en/stable/userguide/tasks.html)
- [Celery Redis：visibility timeout](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html)
- [Celery Configuration](https://docs.celeryq.dev/en/stable/userguide/configuration.html)
- [Celery Introduction：平台支持范围](https://docs.celeryq.dev/en/stable/getting-started/introduction.html)
- [FastAPI Background Tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/)

Celery 官方说明不支持 Windows。当前 Windows 自动化测试验证领域状态机、数据库、任务适配和真实 Redis 连接；生产 Worker 与最终跨进程验收必须运行在 Linux、WSL 或 Linux 容器中，不能把 Windows 单机测试写成生产运行证明。
