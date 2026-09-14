# Day 10 原理：退款 Policy Engine、人机审批与安全写操作

## 1. 今天解决的真实问题

售后 Agent 可以理解用户、查询订单并提出退款方案，但退款会真实改变资金状态。模型存在事实理解错误、参数漂移、Prompt Injection、权限误判和重复执行风险，所以“模型认为应该退款”绝不能等于“系统执行退款”。

Day10建立以下硬边界：

1. 模型只能提出候选退款金额、币种和原因；
2. 确定性 Policy Engine 根据可信Observation重新检查候选方案；
3. 通过Policy的提案必须暂停，等待具有权限的独立审核人；
4. 审批必须绑定精确提案版本和哈希，参数变化后旧审批失效；
5. 后端为批准的精确操作签发执行凭证；
6. 写工具同时验证权限、执行凭证和幂等键；
7. 执行后必须调用只读工具核验真实退款状态；
8. 每个关键状态变化写入可追踪的审计事件。

## 2. 完整操作流程

### 2.1 Agent收集事实

Agent依次调用订单、物流和政策工具。工具结果是Observation，只代表外部系统在某个时间返回的事实，不是退款指令。

当前任务的证据必须与同一个`tenant_id`、`order_id`和工单类别匹配。10086的物流证据不能用于10087；用户后来修改订单号时，旧证据必须失效并重新收集。

### 2.2 模型生成候选提案

模型在证据齐全后输出`propose_refund`，其中只包含：

- `amount_minor`：最小货币单位金额；
- `currency`：三位大写币种；
- `reason`：候选退款原因。

这一步叫“候选判断”，因为它还没有经过确定性业务规则、人工审批和写工具验证。模型不能输出`approval_id`、幂等键、租户、审核人或执行凭证，这些可信字段全部由后端产生。

### 2.3 Policy Engine确定性校验

`RefundPolicyEngine`不调用LLM。它检查：

- 当前订单Observation是否存在并且订单号匹配；
- 订单是否已付款且没有取消、待付款或已退款；
- 候选金额是否大于0且不超过订单总额；
- 候选币种是否与订单一致；
- 物流和政策Observation是否存在且没有过期；
- 政策是否已经生效；
- 政策是否明确要求人工审批；
- 政策要求的订单、付款、物流、签收证明等证据是否满足。

任何一项不满足都返回稳定的错误码并停止退款链路。模型不能要求Policy忽略规则。

### 2.4 后端创建版本化提案

Policy通过后，后端创建`RefundProposal`，补充：

- proposal ID与版本；
- tenant、run、ticket、order和原始请求人；
-政策ID与版本；
- 证据哈希；
- 提案哈希；
- 后端幂等键；
- 创建时间和过期时间；
- `pending_approval`状态。

提案哈希覆盖不可变业务参数。审批记录同时保存提案ID、版本和哈希，所以审批的是某个精确快照，不是模糊的“同意退款”。

### 2.5 LangGraph人工中断

状态图进入`refund_approval`节点后调用`interrupt()`。此时：

- checkpoint已经保存提案；
- 退款写工具调用次数仍为0；
- 普通`resume("continue")`被拒绝；
- 只能通过`review_refund()`提交带可信审核人身份的审批命令；
- 取消只能安全终止，不能跳过审批。

`interrupt()`恢复时会从审批节点开头重新执行，因此写操作必须放在审批节点之后的独立`execute_refund`节点，不能放在`interrupt()`之前。

### 2.6 后端重新验证审核人

审批服务和审批节点都检查：

- 审核人与提案属于同一租户；
- 审核人具有`refund:approve`权限；
- 审核人不是原始请求人，满足四眼原则；
- 预期提案版本与当前版本相同；
- 预期提案哈希与当前哈希相同；
- 审批没有过期；
- action和参数组合符合契约。

两层校验是纵深防御：外层尽早拒绝非法请求，节点层防止内部调用或恢复载荷绕过。

### 2.7 approve、reject、modify、takeover

- `approve`：保存精确审批绑定，进入写操作；
- `reject`：提案变为`rejected`，流程结束，不能退款；
- `takeover`：Agent停止，转人工接管，不能自动退款；
- `modify`：旧提案变为`superseded`，新参数重新经过Policy，生成新版本并再次中断。

修改者不能批准自己修改的新版本。旧版本的审批、哈希和幂等键都不能授权新版本。

### 2.8 后端签发精确执行凭证

只有审批通过后，工作流才使用后端密钥生成HMAC执行凭证。签名绑定：

- tenant；
- 审核人actor；
- order；
- amount与currency；
- reason；
- proposal hash；
- approval ID；
- idempotency key。

`refund_execute`工具自己验签。即使某段内部代码绕过状态图直接调用工具，只有主管权限但没有有效执行凭证也会被工具拒绝。

执行凭证不会写入审计日志；工具调用只记录参数哈希。测试数据源的调用记录也会把凭证替换成`[REDACTED]`。

### 2.9 幂等执行

支付系统以`(tenant_id, idempotency_key)`为幂等边界：

- 相同key和完全相同参数：返回第一次退款结果；
- 相同key但参数不同：返回冲突；
- 不同租户：相互隔离。

这是处理“外部退款成功、进程却在checkpoint提交前崩溃”的关键。恢复后写节点可能再次调用工具，但只能得到原退款结果，不能产生第二笔退款。

Checkpoint负责恢复控制流，幂等键负责消除重复副作用，两者共同工作但职责不同。

### 2.10 执行后核验

`refund_execute`成功不直接把任务标为完成。状态图继续调用`refund_status_lookup`，核对：

- 退款状态已经是`processed`；
- order、amount和currency与当前提案一致；
- proposal hash和approval ID一致；
- idempotency key一致。

全部一致后才把最终Outcome写为`refund_completed`。写工具超时也必须查询状态，因为超时只表示调用方没及时收到结果，不代表外部退款没有发生。

## 3. 状态图与状态变化

```text
control → plan → order/logistics/policy tools → control
             │
             └─ propose_refund
                    ↓
             evaluate_refund
             ├─ Policy拒绝 → END
             └─ 创建pending提案
                    ↓
             refund_approval → interrupt/checkpoint
             ├─ reject/expire/takeover/cancel → END
             ├─ modify → 新版本 → refund_approval
             └─ approve
                    ↓
             execute_refund
                    ↓
             verify_refund
             ├─ 不一致 → 人工升级
             └─ 一致 → refund_completed
```

核心状态包括当前提案、历史提案版本、审批记录、最后修改者和退款审计事件。History保留发生过的事实，当前State只指向有效版本，Context只给当前节点提供完成任务必需的数据。

## 4. LLM、Policy、审批人与工具的职责

| 组件 | 负责 | 不负责 |
|---|---|---|
| LLM Planner | 理解目标、选择只读工具、根据证据提出候选方案 | 最终业务授权、权限判断、直接退款 |
| Policy Engine | 金额、币种、状态、证据和政策硬规则 | 代替人承担高风险业务责任 |
| 人工审核人 | 结合业务语境批准、拒绝、修改或接管 | 绕过租户、权限、版本和哈希约束 |
| 写工具 | 验证权限与执行凭证，幂等调用支付系统 | 相信模型或客户端自报的审批字段 |
| Verification节点 | 查询外部真实状态并精确比对 | 仅凭“工具调用未报错”宣布成功 |

## 5. 异常处理策略

| 异常 | 处理 |
|---|---|
| 缺少或过期证据 | Policy拒绝，停止写链路 |
| 金额超订单总额/币种不符 | Policy拒绝 |
| 无审批、越权、跨租户、自批 | 审批门拒绝，保持暂停且写入为0 |
| 版本或哈希不匹配 | 拒绝旧审批 |
| 审批过期 | 标记`expired`并转人工 |
| 修改提案 | 旧版本失效，新版本重新Policy和审批 |
| 伪造执行凭证 | 工具返回authorization error |
| 相同幂等键不同参数 | 工具返回conflict error |
| 写调用超时/依赖错误 | 不盲目重写，先查询退款状态 |
| 执行后参数不一致/查不到 | `refund_verification_failed`并转人工 |
| 审计记录失败 | 失败关闭，不能把不可审计操作当成成功 |

## 6. 上下文工程

Day10不是把全部History原样塞给模型，而是分层保存：

- History：模型Decision、工具Observation、人工动作和所有提案版本；
- Agent State：当前order、预算、有效提案、审批状态和下一节点；
- Context：本轮Planner只看到目标、当前可信Observation、剩余预算和允许工具；
- Audit：Policy结果、提案哈希、审批绑定、写调用和执行核验；
- 长期Memory：用户语言偏好等跨窗口信息，本日退款授权不依赖长期Memory。

高风险业务事实只来自工具和后端状态，不能来自用户文本或模型自述。

## 7. 可观测性与价值指标

每次退款链路可以追踪：

- run/request/trace/step；
- Planner Decision与模型调用ID；
- Policy稳定错误码；
- proposal ID、版本和哈希；
- reviewer、action和approval ID；
- 工具名、版本、参数哈希、状态、错误和耗时；
- 幂等键；
- 执行后核验结果；
- 最终状态和终止原因。

Day10离线评估使用55个合成安全场景和20轮幂等重放。指标不宣称生产收入，只衡量可以由代码复现的安全价值：未审批/非法退款写入数、正确批准退款成功数、旧审批参数漂移执行数和重复退款副作用数。

## 8. 当前边界

- 当前支付系统和业务事实是内存模拟，真实接入需要支付Provider适配器；
- 执行凭证密钥由测试显式注入，生产必须来自密钥管理系统并支持轮换；
- 审计事件目前跟随LangGraph checkpoint持久化，后续会增加独立查询投影；
- 离线评估使用确定性Planner，不代表真实LLM语义准确率；
- HMAC、Policy和幂等保护的是后端执行边界，不能替代真实支付系统的事务与对账。
