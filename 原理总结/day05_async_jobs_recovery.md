# Day 05：异步任务、重试、取消与恢复

## 完成内容

把耗时调查从同步HTTP请求拆成持久化异步Job，引入Celery、Redis传输、PostgreSQL任务状态、租约、fencing、重试预算、取消和投递补偿。

## 完整流程

```text
客户端提交调查
→ API校验并在PostgreSQL创建Job
→ API返回202和job_id
→ Dispatcher把job_id发送到Redis/Celery
→ Worker领取Job并获得lease/version
→ 执行调查步骤
→ 在数据库事务中保存业务结果和最终Job状态
→ 客户端按job_id查询状态
```

API只负责“受理”，不保持HTTP连接等待长任务。PostgreSQL是Job状态和结果的事实源，Redis只负责消息传输；消息丢失时Beat扫描未成功投递的数据库记录重新发送。

Celery提供至少一次投递，不提供业务上的Exactly-once。消息可能重复，因此Worker必须使用Job状态、租约、version fencing和幂等业务写入阻止重复副作用。

## 状态与恢复

Job经历pending、running、retrying、succeeded、failed或cancelled。Worker领取任务时写入lease owner和过期时间；旧Worker即使在超时后恢复，也必须因version/fencing不匹配而无法覆盖新Worker结果。

依赖超时、限流等临时错误进入有界重试和指数退避；参数、权限、资源不存在等确定性错误不应盲目重试。取消是协作式：API写入取消请求，Worker在安全检查点停止，不能假设线程或外部请求可以瞬间强杀。

业务结果与Job最终状态必须同一事务提交，避免“结果已写但任务仍显示失败”或“任务显示成功但结果不存在”。

## 测试证据

全量58项测试通过，真实PostgreSQL异步专项6项通过，覆盖重复投递、租约过期、旧Worker fencing、重试、取消和投递补偿。

## 与Agent的关系

后续Agent运行同样可能跨进程、暂停和恢复。Day05建立的事实源、至少一次、幂等、租约和错误分类，是LangGraph checkpoint和高风险工具可靠执行的后端基础。

