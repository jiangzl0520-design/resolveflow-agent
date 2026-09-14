# Day 9 原理：LangGraph状态图、checkpoint与可恢复执行

## 1. 今天解决的真实问题

Day8的`AgentRunner`已经能够根据Observation动态选择下一步，但整个`AgentRunState`只存在当前Python进程里。进程关闭、Worker重启或服务部署后，系统不知道：

- 已经完成了哪些模型决策和工具调用；
- 当前应该从哪个节点继续；
- 哪些工具结果已经成功，不能重复调用；
- 运行是在执行、暂停、取消还是终止；
- 重启前消耗的步骤、时间和Token预算是多少。

Day9把同一套Agent语义映射为LangGraph状态图，并使用checkpoint持久化每个节点边界的状态。

## 2. 完整操作流程

### 2.1 创建运行

1. 上层构造`AgentRunCommand`，其中actor、tenant、ticket、order、目标、request_id和trace_id已经来自可信后端上下文。
2. `DurableAgentWorkflow.start()`生成或接收稳定的`run_id`。
3. `run_id`同时作为LangGraph的`thread_id`。以后查询、暂停和恢复必须使用同一个ID。
4. Command和预算被转换为只含字符串、数字、布尔值、列表和字典的`AgentGraphState`。
5. 图从`START`进入`control`节点。

### 2.2 control节点

`control`是每轮Agent动作之前的确定性控制门。

- `cancel_requested=true`：直接进入`cancelled`终态，不再调用模型或工具。
- `pause_requested=true`：调用LangGraph `interrupt()`，框架保存checkpoint并把暂停信息返回调用方。
- 恢复输入为`continue`：清除暂停标记并进入`plan`。
- 恢复输入为`cancel`：进入取消终态。
- 任何未声明的恢复动作：失败关闭，不能通过伪造动作跳过控制。

`interrupt()`所在节点恢复时会从节点开头重新执行，因此在`interrupt()`之前不能写数据库、退款或调用外部副作用。当前control节点在中断前只读取状态和构造JSON载荷。

状态还保存`paused_at`和累计暂停秒数。最大执行时间会扣除显式等待时间：人工第二天继续不会因为“等待了一夜”自动失败，但恢复后的实际执行时间仍然受到原预算约束。

### 2.3 plan节点

1. 先检查跨进程保存的最大步骤和总时间预算。
2. 从Graph State重建`AuthenticatedActor`和Day8的`AgentRunState`只读视图。
3. Registry只向Planner暴露当前actor有权使用的工具。
4. Planner输出结构化`AgentDecision`。
5. 累加Token和step，更新相同Decision计数。
6. Token超限、重复Decision或Planner异常时确定性终止。
7. `call_tool`路由到`execute_tool`；`finish`路由到`verify`；`escalate`进入人工升级终态。

Decision与本次模型Token被保存为`pending_planner`。这样进程在Decision之后重启时，不需要重新调用模型才能知道下一步。

### 2.4 execute_tool节点

1. 从checkpoint读取`pending_planner`并重新校验为`AgentDecision`。
2. tenant、actor、request、trace、run和step继续由可信状态注入。
3. ToolExecutor再次执行权限、参数、超时、输出契约和记录检查。
4. Tool Observation被转换为JSON安全文档并追加到Graph State。
5. Step Trace同时记录模型调用、工具调用、状态和错误类型。
6. 权限失败转人工；内部错误和输出契约错误失败关闭；可修复错误回到control，再交给Planner修正。

如果设置`pause_after_step=1`，第一个工具节点成功并完成checkpoint后，下一次control会中断。恢复后框架从control继续，不会重新执行已经完成的工具节点。

### 2.5 verify节点

1. 把持久化Observation按工具名和锁定版本重建为Pydantic输出对象。
2. Day8 Verifier继续检查当前order/category对应的证据。
3. 验证通过才写入最终Outcome和Summary并进入completed。
4. 验证失败会记录失败码，然后回到control和plan补充证据。

### 2.6 checkpoint与进程重启

LangGraph在输入和每个节点边界形成checkpoint。每个thread形成有版本的状态历史，至少保存：

- channel values，也就是当前Graph State；
- 下一节点；
- checkpoint父子关系；
- 节点写入和中断信息；
- 当前step等框架元数据。

PostgreSQL checkpointer关闭后，新进程重新创建Planner、Registry、Executor和Graph，使用相同`thread_id`读取最后checkpoint，再调用resume即可继续。

## 3. 状态图

```text
START
  ↓
control ── cancel ───────────────→ END
  │
  ├── interrupt → 持久化暂停 → resume → control
  ↓
plan ── call_tool → execute_tool ─→ control
  │
  ├── finish ───→ verify ──失败──→ control
  │                    └─通过────→ END
  └── escalate/error ────────────→ END
```

条件边只决定节点跳转；节点负责读取状态并返回状态增量。框架负责把节点输出合并进状态、保存checkpoint和按照同一thread恢复。

## 4. Graph State为什么只保存JSON安全值

checkpoint是长期数据，不应该直接反序列化任意项目Python对象。

当前实现：

- checkpoint只保存基础类型；
- 不使用pickle fallback；
- PostgreSQL saver显式使用受限`JsonPlusSerializer`；
- 恢复时根据工具名和精确版本，从Registry找到允许的输出Schema；
- 找不到原工具版本时抛出`AgentCheckpointCompatibilityError`，不把旧数据强行解释为新契约。

这同时解决安全和演进问题：checkpoint不会悄悄导入任意类，工具升级也不会让旧Observation被新Schema无声误读。

## 5. checkpoint、业务幂等和Exactly-once的关系

checkpoint只能证明“这个节点的成功结果已经保存”。它不能单独保证外部副作用Exactly-once。

存在一个崩溃窗口：

```text
写工具已经成功
→ 进程在checkpoint提交前崩溃
→ 恢复后框架只能看到上一个checkpoint
→ 工具节点可能再次执行
```

所以：

- 对当前只读查询，重复调用主要影响成本和延迟；
- 对退款等写操作，必须使用业务幂等键、唯一约束、审批绑定和执行后查询；
- checkpoint负责恢复控制流，幂等负责消除重复副作用，两者不能互相替代。

## 6. 两套数据库迁移责任

ResolveFlow业务表由Alembic管理。LangGraph的`checkpoint_migrations`、`checkpoints`、`checkpoint_blobs`和`checkpoint_writes`由官方checkpointer的`setup()`管理。

部署时分别执行：

```powershell
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe -m app.agent.setup_checkpoints
```

不要把框架内部表复制成项目ORM，也不要让业务Alembic脚本猜测LangGraph内部版本。

## 7. 自研循环与LangGraph的职责对比

Day8自研循环仍然有价值，因为它明确了Agent的业务语义：

- 目标、Decision、Observation、Verifier；
- 错误分类和恢复策略；
- 预算、循环检测和终止条件；
- LLM和确定性后端的边界。

LangGraph替项目解决的是通用运行时问题：

- 节点和条件边编排；
- checkpoint版本历史；
- thread定位；
- interrupt和resume协议；
- 从最后成功节点继续；
- 状态历史查询和故障恢复基础设施。

框架没有替项目决定业务证据是否充分、退款能否执行、权限是否允许或哪个工具错误应该重试。

## 8. 官方依据

- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangGraph Checkpointer Integrations](https://docs.langchain.com/oss/python/integrations/checkpointers/index)
- [PostgreSQL Checkpointer](https://pypi.org/project/langgraph-checkpoint-postgres/)
