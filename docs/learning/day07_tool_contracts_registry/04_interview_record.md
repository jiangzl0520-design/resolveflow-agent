# Day 7 Agent 面试记录

## 状态

Day 7功能、测试、合成评估、原理文档和Agent面试已经全部通过。

本Day只考察Agent相关内容：

- Tool Contract在Agent动作空间中的作用；
- 工具选择错误、参数错误和工具执行失败的区别；
- Tool Observation与Agent计划修订；
- 工具Schema、权限和可信执行上下文；
- 工具轨迹评估。

不提问普通RBAC代码、线程池API或Pydantic语法。

## 预计范围

预计5道核心题；一次只问一道。用户原始回答、提示、错误点、完整讲解和换场景复问都按真实过程记录。

## 问答记录

### 第1题：模型选错工具与工具执行失败

Agent当前目标是“查询签收证明是否存在”，却选择了`order_lookup`，参数`order_id=10086`合法。工具成功返回“订单已发货”。

这次能否算作Agent已经成功完成当前步骤？它属于工具执行失败，还是工具选择错误？Agent下一步应该怎样处理？

用户第1次原始回答：

> 工具选择错误 agent下一步应该重新选择工具去查询订单是否存在

判断：部分正确，尚未通过。

- 正确点：这是工具选择错误，不是工具执行失败；
- 错误点：下一步仍偏离当前目标。当前目标是查询签收证明，不是查询订单是否存在；
- 当前处理：按规则先给提示，等待重新回答。

用户第2次原始回答：

> 应该选择logistics_lookup 不知道

判断：工具选择已修正，但仍未掌握工具结果在Agent架构中的位置，按规则完整讲解。

完整讲解：

1. `order_lookup`成功返回“订单已发货”，说明这个工具调用在技术上成功，但它没有回答“签收证明是否存在”，所以当前Agent步骤尚未完成。
2. 这不是工具执行失败。工具执行失败是timeout、dependency error、resource error等；当前是模型/Agent选择了与目标不匹配的工具。
3. Agent应重新选择`logistics_lookup(order_id=10086)`，因为它的输出契约包含`proof_available`和`signed_at`。
4. `proof_available=true`是Tool Observation：带工具来源、版本和观察时间的外部事实。Agent应把它写入当前AgentState，再根据任务目标判断证据是否充分。
5. Observation不是最终退款决定。即使存在签收证明，仍要结合用户异议、政策和人工审批。

关系：

```text
目标
→ 选择能回答目标的工具
→ 工具执行
→ Observation
→ 更新AgentState
→ 验证当前步骤是否完成
→ 决定下一步
```

换场景复问：

Agent当前目标是“确认订单是否已经付款”，却先调用`policy_lookup`并成功得到退款政策。这个调用属于工具执行失败还是工具选择错误？Agent应该改用哪个工具？该工具返回`paid=true`后，这个结果在Agent中叫什么，能否单独证明用户符合退款资格？

用户换场景回答：

> 1工具选择错误 2order_lookup3Tool Observation4不能

最终判断：通过。

- 能区分工具执行成功与Agent工具选择错误；
- 能根据目标改选`order_lookup`；
- 能把`paid=true`识别为Tool Observation；
- 能说明单一付款Observation不能直接证明退款资格。

### 第2题：错误分类与恢复动作

Agent查询物流时遇到三种情况：

1. 选择了不存在的`logistic_lookup`；
2. 选择了正确的`logistics_lookup`，但没有传`order_id`；
3. 工具名和参数都正确，但物流系统返回临时503。

这三种情况分别属于哪类错误？Agent下一步应该分别怎样处理？

用户原始回答：等待回答。

用户原始回答（连续两条消息）：

> 1不可以2至少应该检查实际调用的工具 参数 tool

> observation prompt名称 结构化schema agent的运行轨迹等

最终判断：通过。

- 能说明最终答案相同不能把两次Run评为同样成功；
- 能检查实际工具选择、参数和Tool Observation；
- 能检查Prompt、结构化Schema和Agent运行轨迹；
- 能识别Run B存在工具幻觉、非法参数和碰巧成功的问题。

## Day 7最终面试结论

5道核心题全部完成。用户已能区分工具选择错误、参数错误和工具执行失败，理解可信执行上下文、Observation、受限重试和工具轨迹评估。

用户第1次原始回答：

> 不知道

判断：尚未掌握，按规则先给少量提示。

- `retryable=true`只表示错误可能是暂时的，不表示必须或可以无限重试；
- timeout只说明没有在期限内拿到结果，不能证明签收证明不存在；
- 当前处理：等待用户结合预算、状态和终止条件重新回答。

用户第2次原始回答：

> 1不是2状态记录一直记录每次重试过程在达到最大重试预算以后 转入人工提交处理或者终止处理3不一样 工具明确返回 proof_available=false才是没有签收证明 timeout是都没有查看到

最终判断：通过。

- 能说明retryable不等于无限重试；
- 能说明每次尝试和错误必须记录进State；
- 能在预算耗尽后选择转人工或终止；
- 能区分明确的`proof_available=false`与timeout导致的未知状态。

### 第5题：工具轨迹评估

两个Agent Run最终都正确回答“需要查询签收证明”：

- Run A第一次就选择`logistics_lookup(order_id=10086)`并获得Observation；
- Run B先编造不存在的工具，又传入伪造tenant_id，最后才碰巧改对。

能否因为最终答案相同就把两次Run评为同样成功？评估工具使用质量时，至少应该检查哪些轨迹维度？

用户原始回答：等待回答。

用户第1次原始回答：

> 不能 应该在authorization_error被禁止 真正可信的tenant_id应该来自于agent调用工具查询当前登录的用户信息得到的tool observestion

判断：部分正确，尚未通过。

- 正确点：不能按照模型提供的tenant_id查询租户B；
- 错误点：`tenant_id`不属于`order_lookup`输入契约，首先在参数Schema层成为argument error，而不是authorization error；
- 错误点：可信tenant_id不是Agent调用工具获得的Observation，而是认证系统验证身份后建立的ToolExecutionContext；
- 当前处理：按规则先给提示，等待重新回答。

用户第2次原始回答：

> 属于argument error 已验证的jwt 因为这是模型自己生成的来源不可靠

最终判断：通过。

- 能识别模型额外tenant_id属于argument error；
- 能说明可信tenant来自已验证JWT形成的安全上下文；
- 能说明模型生成内容来源不可靠，不能决定身份和租户边界。

### 第4题：可重试错误与终止条件

Agent必须获得签收证明才能继续判断，但`logistics_lookup`连续返回`timeout_error`。该错误被标记为`retryable=true`。

这是否意味着Agent可以无限重试？Agent应如何结合重试预算、状态记录和终止/转人工条件处理？如果最终仍未获得Observation，能否把它解释成“没有签收证明”？

用户原始回答：等待回答。

用户第1次原始回答：

> 1属于选择了错误的工具 2属于工具执行失败 3不知道

判断：部分正确，尚未通过。

- 第1项正确：不存在的工具名属于selection error；
- 第2项错误：缺少`order_id`会在参数Schema阶段被拦截，Handler尚未执行，不属于工具执行失败；
- 第3项缺失：需要识别外部物流依赖临时不可用；
- 当前处理：按规则先给提示，等待重新回答。

用户第2次原始回答：

> 1是selection_error应该重新选择工具2是argument_error应该修改参数3是dependency_error应该有限重试

最终判断：通过。

- selection error对应重新选工具；
- argument error对应修改或补充参数；
- dependency error对应在预算内有限重试或降级；
- 已能根据失败层次选择不同恢复策略。

### 第3题：可信执行上下文与权限

当前登录Actor属于租户A。模型调用`order_lookup`时生成：

```json
{
  "order_id": "10086",
  "tenant_id": "租户B"
}
```

系统能否按照模型提供的`tenant_id`查询租户B？这个调用应在哪一层被怎样处理？真正可信的tenant_id应该来自哪里？

用户原始回答：等待回答。
