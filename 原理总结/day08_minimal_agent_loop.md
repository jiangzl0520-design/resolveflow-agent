# Day 08：最小 Agent Loop

## 完成内容

实现最小但完整的`observe → decide → act → verify`循环，让模型能够依据工具Observation动态改变下一步，而不是用固定工作流冒充Agent。

## 完整流程

```text
创建AgentState并写入目标和预算
→ Planner读取当前State与Observation
→ 输出结构化Decision：call_tool / finish / escalate
→ call_tool进入ToolExecutor
→ Observation写回State
→ 回到Planner重新判断
→ finish进入Completion Verifier
→ 验证通过才生成最终Outcome
→ 失败则写入Verifier反馈并继续或安全终止
```

AgentState显式保存目标、当前订单、步骤、预算、Decision、Observation、Verifier失败、Token消耗、终止原因和最终结果。Planner只能提出候选动作；ToolExecutor负责真实工具调用；Completion Verifier是确定性完成门槛。

例如模型查询了错误订单，即使工具调用成功，也不能完成当前订单任务。模型说`finish`不等于任务真的完成，Verifier必须检查所需Observation是否存在、是否绑定正确资源、是否满足完成条件。

## 恢复与终止

selection和argument错误可以反馈给Planner修正；临时dependency错误可以在预算内换计划或重试；authorization和output contract错误安全终止。最大步骤、时间、Token和重复Decision次数共同防止无限循环。

`escalate`是合法结果：证据冲突、信息不足或预算耗尽时，转人工比编造完成更正确。

## 测试证据

专项13项、全量107项测试通过；30个确定性任务全部得到正确Outcome，5个临时故障全部恢复，并验证错误订单Observation不能完成任务。

## 与前后Day的关系

Day06提供结构化Decision，Day07提供安全工具和Observation，Day08负责循环与验证；Day09把该循环转换成可持久化状态图，Day10增加人工审批节点。

