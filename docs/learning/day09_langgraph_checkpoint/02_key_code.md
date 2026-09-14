# Day 9 关键代码

## 1. `app/agent/workflow.py`

### 输入与输出

- 输入：`AgentRunCommand`、稳定`run_id`和可选暂停节点。
- 输出：`DurableAgentRunView`，包含Day8 `AgentRunState`、是否暂停、下一节点、中断载荷和checkpoint_id。

### 实际执行流程

`DurableAgentWorkflow`构建四个业务节点：

```text
control
plan
execute_tool
verify
```

START先进入control。plan的结构化Decision通过条件边进入execute_tool或verify；工具和未通过验证的finish都回到control。

### 为什么这样写

Agent的每个可恢复边界必须足够粗，避免把每一行代码都变成节点；也必须足够细，确保模型调用、工具调用和验证之间可以形成独立checkpoint。

### 架构位置

它位于Agent Runtime层：

```text
业务服务/Worker
→ DurableAgentWorkflow
→ Planner / Verifier / ToolExecutor
→ LangGraph Checkpointer
```

### 状态变化

- plan增加step、Token、Decision计数和pending decision；
- execute_tool增加Observation和Tool Trace；
- verify增加验证结果或最终Outcome；
- control处理暂停、恢复与取消，并从执行预算中扣除人工等待时间；
- 每个节点输出由LangGraph写入同一thread的下一checkpoint。

### 异常处理

- Planner错误转为明确terminal error code；
- checkpoint缺少pending decision时拒绝恢复；
- 工具版本缺失时拒绝反序列化旧Observation；
- 未声明的resume action失败关闭；
- 权限和契约错误沿用Day8的安全终止策略。

## 2. `app/agent/checkpointing.py`

### 输入与输出

- 输入：SQLAlchemy格式PostgreSQL URL。
- 输出：带受限serializer的`PostgresSaver`上下文。

### 执行流程

1. 把`postgresql+psycopg://`转换为Psycopg可识别URL。
2. 建立`autocommit=True`、`prepare_threshold=0`连接。
3. 创建`PostgresSaver`。
4. 首次部署时调用`saver.setup()`。
5. 离开上下文时关闭连接。

### 写法原因

官方`saver.setup()`需要可提交DDL的连接。显式管理连接还能给serializer施加限制，避免默认反序列化任意项目模块。

### 架构位置

它是基础设施适配层。Agent图只依赖Checkpointer接口，不依赖连接字符串、Psycopg或具体表。

### 异常处理

非PostgreSQL URL直接拒绝。连接、DDL和读写失败由Psycopg/LangGraph抛出，调用方不能在没有checkpoint的情况下假装恢复成功。

## 3. `app/agent/setup_checkpoints.py`

这是部署入口，不是业务请求路径：

```powershell
python -m app.agent.setup_checkpoints
```

它只负责让LangGraph自管表达到当前checkpointer版本。它不创建ResolveFlow业务表，也不能替代`alembic upgrade head`。

## 4. `app/agent/models.py`

Day9新增：

- `AgentRunStatus.CANCELLED`
- `AgentTerminationReason.CANCELLED`

取消是独立终态，不能错误记录为failed或completed。这样监控、恢复和后续评估能够区分用户主动停止与系统执行失败。

## 5. `tests/test_agent_workflow.py`

关键测试证明：

- 图在正常Observation下完整结束；
- checkpoint暂停后恢复不重复第一个工具；
- 暂停时取消不会调用后续工具；
- 非法resume action失败关闭；
- 相同run_id不能覆盖已有thread；
- 缺失旧工具版本时拒绝恢复；
- 总时间预算跨Runtime继续生效；
- 人工暂停时长不消耗执行预算，恢复后的活跃时间仍受限制；
- PostgreSQL连接关闭后，用新Workflow实例恢复同一run并跳过已成功工具。

## 6. `evaluation/run_day09_checkpoint_recovery.py`

同一30条合成数据比较：

```text
无checkpoint：重启后从头执行
有checkpoint：新Runtime从保存位置继续
```

报告分别记录Outcome、精确恢复数、总工具调用和重复调用，不能只用“能恢复”这种无法量化的描述。
