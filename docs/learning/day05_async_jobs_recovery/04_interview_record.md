# Day 5 面试记录

## 结论

Day 5 新增的是后端异步执行基础设施：

- 持久化 Job 状态机；
- Redis/Celery 投递；
- late acknowledgement 和 worker-lost requeue；
- 数据库租约、version fencing 与副作用幂等；
- 指数退避、最大尝试次数和 timeout；
- 协作式取消；
- pending dispatch 自动补偿；
- API/Worker request_id 与 trace_id 传播；
- PostgreSQL 并发与合成故障评估。

这些能力将承载未来 Agent Run，但今天仍没有新增：

- LLM 决策；
- Agent Loop；
- AgentState；
- Observation；
- 动态工具选择；
- 计划修订；
- Context/Memory/RAG；
- Agent checkpoint；
- 人机审批节点；
- Agent 质量评估。

特别需要区分：

```text
Job retry
  = 后端再次调度一次执行机会

Agent plan revision
  = Agent 根据新的 Observation 改变下一步行动
```

以及：

```text
Job state
  = queued/running/retry/cancel/success/failure

AgentState
  = 目标、事实、证据、计划、工具结果、下一节点和终止条件
```

按照项目 `AGENTS.md`，异步任务、数据库、事务、Celery、部署和普通错误恢复属于后端知识，只直接讲解，不作为面试题。不能为了完成“每天提问”而用后端题目冒充 Agent 面试。

因此 Day 5 不安排面试问题。Day 5 的原理、关键代码、失败过程和量化证据已经记录在本目录另外三份文档中。Day 6 开始模型网关和结构化输出后，如果出现新的 Agent 相关知识，再恢复只问 Agent 的面试流程。
