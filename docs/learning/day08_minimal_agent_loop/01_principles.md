# Day 8 原理：最小Agent Loop

## 1. 工程目标

Day 8把Day6模型决策边界和Day7工具执行边界串成一个受预算约束、可验证、可终止的最小Agent Runtime。

核心循环：

```text
observe
→ decide
→ act
→ verify
→ 更新AgentState
→ 根据新状态继续或终止
```

它不是固定顺序调用三个工具。订单Observation不同，下一步会不同：

```text
订单已取消
→ 直接结束为no_refundable_payment

订单已发货
→ 查询物流
→ 查询政策
→ 根据物流Observation选择delivery_in_progress或human_review_required
```

## 2. 完整运行流程

```text
AgentRunCommand
  actor、ticket、order、category、goal、request/trace
→ 创建AgentRunState(running)
→ 检查step/time/token预算
→ Registry只提供Actor有权看到的工具目录
→ Planner读取最小Runtime Context
  goal
  当前Observation
  最近Verifier失败
  剩余step/token
  allowed tool contracts
→ Planner返回结构化AgentDecision
  call_tool / finish / escalate
→ 记录Planner token和model_call_id
→ 检测重复Decision
→ call_tool:
     ToolExecutor执行
     生成成功或失败Observation
     Observation进入State
     权限/工具契约硬错误直接安全终止
     其他错误交给下一轮Planner修正或重试
→ finish:
     CompletionVerifier检查证据和结论是否一致
     通过才completed
     未通过写入verification_failures并继续规划
→ escalate:
     状态转为escalated
→ 任一预算耗尽或循环命中:
     状态转为failed并记录termination_reason
```

## 3. AgentState

当前State保存：

- run、tenant、actor、ticket、order；
- 当前目标和工单分类；
- request_id、trace_id；
- 最大step、时间、token和重复Decision预算；
- 当前状态和终止原因；
- terminal error code；
- 已用step和model token；
- 全部Tool Observation；
- 每一步Decision和执行/验证轨迹；
- Verifier拒绝完成的原因；
- 最终Outcome和Summary；
- 开始、更新和完成时间。

State是循环每轮决策的事实输入。Planner不能依赖隐藏的进程局部变量决定下一步。

Day8 State仍在当前进程中。Day9会把它映射到LangGraph checkpoint并持久化，实现进程重启后的暂停恢复。

## 4. Planner

`AgentPlanner`是Provider-neutral协议。当前有：

- `ModelGatewayAgentPlanner`：使用Day6 Gateway和结构化`AgentDecision`；
- `ScriptedAgentPlanner`：自动化测试使用的确定性Planner；
- 评估脚本中的Observation-driven Planner：对固定数据集产生可复现分支。

模型Planner的Context只包含当前任务所需数据，不拼接完整History。工具输出和错误被标记为不可信runtime data，不能覆盖高优先级规则。

模型只能建议：

- 调用一个只读工具；
- 提交完成候选；
- 转人工。

模型不能调用写工具、批准退款或绕过Verifier。

## 5. Decision

`AgentDecision`使用Pydantic约束三种互斥动作：

### call_tool

必须包含：

- tool name/version；
-结构化arguments；
- reason。

不能同时包含final outcome或escalation字段。

### finish

必须包含：

- outcome；
- final summary；
- reason。

不能包含工具调用字段。

### escalate

必须包含：

- escalation reason；
- reason。

这种互斥约束阻止模型返回“既调用工具又宣布完成”的模糊动作。

## 6. Observation驱动分支

Planner每轮读取最新State，而不是按照固定下标选择工具。

示例：

```text
初始State没有OrderObservation
→ order_lookup

OrderObservation.status=cancelled
→ 不再查询物流和政策
→ finish(no_refundable_payment)
```

另一条路径：

```text
OrderObservation.status=shipped
→ logistics_lookup

LogisticsObservation.status=delivered
→ policy_lookup
→ finish(human_review_required)
```

同一Planner面对不同Observation产生不同动作，才是最小Agent能力。

## 7. Completion Verifier

Planner提出`finish`只是完成候选，不能直接终止。

Verifier执行确定性检查：

- 当前order_id是否有可信OrderObservation；
- 未付款/取消订单是否使用`no_refundable_payment`；
- 已付款/发货订单是否有同一order_id的LogisticsObservation；
- 是否有当前TicketCategory对应的PolicyObservation；
- 物流进行中是否使用`delivery_in_progress`；
- 签收/异常场景是否转`human_review_required`。

Verifier明确绑定主事实：

```text
State.order_id=10086
```

即使State中存在订单10087的成功Observation，也不能满足10086的完成条件。

这阻止“工具调用成功但参数指向错误资源”碰巧通过最终验证。

## 8. 错误恢复

Tool Observation中的错误进入State后，由下一轮Planner决定恢复：

- selection error：换工具；
- argument error：改参数；
- resource error：核对主事实或转人工；
- timeout/dependency：在预算内有限重试；
- authorization：Runner直接转人工；
- output contract/internal：Runner安全失败。

Planner错误统一终止为`planner_error`，同时保存具体`terminal_error_code`，例如`model_output_invalid`。

## 9. 预算与终止

硬预算：

- `max_steps`；
- `max_duration_seconds`；
- `max_total_tokens`；
- `max_same_decision_attempts`。

终止原因：

- completed；
- human escalation；
- max steps；
- time budget；
- token budget；
- loop detected；
- planner error；
- tool runtime/permission/contract error。

`retryable=true`不覆盖这些预算。工具临时失败可能允许重试，但Runner仍保证有界终止。

## 10. 循环检测

每个Decision被规范化为稳定JSON并计算SHA-256。

当完全相同的Decision超过允许次数：

```text
第三次相同工具+相同参数
→ 不再执行工具
→ loop_detected
```

默认允许相同Decision执行两次，使一次临时工具失败有重试机会。第三次阻止无穷循环。

改变参数反复循环由`max_steps`兜底；更复杂的语义循环检测留在后续评估优化阶段。

## 11. 与固定工作流的区别

固定流程：

```text
order → logistics → policy
```

无论订单是否取消都会继续。

Agent Loop：

```text
order
→ 根据OrderObservation决定
   cancelled：finish
   shipped：logistics
```

Day8评估使用同一合成数据集证明分支和恢复差异，但不把确定性Planner结果写成真实LLM能力。

## 12. 当前限制

- AgentState尚未持久化；
- 没有checkpoint和进程重启恢复；
- 没有写工具；
- 没有人工审批暂停/恢复；
- 没有真实LLM语义质量评估；
- 没有OpenTelemetry span；
- 当前Verifier只覆盖Day8三类售后Outcome。

这些限制分别由Day9、Day10和后续可观测性/评估阶段处理。
