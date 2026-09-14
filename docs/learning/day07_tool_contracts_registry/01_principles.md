# Day 7 原理：工具契约、注册中心与 Observation

## 1. 今天解决的真实问题

模型可以提出“查询订单、物流、政策”，但不能获得任意调用后端函数的能力。真实 Agent 工程必须解决：

- 模型可能编造不存在的工具名；
- 模型可能选错工具；
- 参数可能缺失、类型错误或额外携带伪造的 `tenant_id`；
- 当前用户可能没有调用某个工具的权限；
- 外部订单或物流系统可能超时、临时不可用或找不到资源；
- 工具实现可能返回不符合契约的数据；
- Agent 必须知道失败属于“改工具、改参数、稍后重试还是结束”，不能只得到一个 `tool failed`；
- 每个 Observation 必须能追溯到工具版本、Schema、Actor、参数摘要和 Agent step。

Day 7 建立的是 Agent 的受控动作空间。今天还没有完整 Agent Loop；模型选择工具与 Agent 根据 Observation 修订计划将在 Day 8 串起来。

## 2. 完整操作流程

```text
Agent形成候选Tool Call
  tool_name + tool_version + arguments
→ Tool Registry按name + version精确解析
→ 检查当前Actor是否有required_permission
→ Pydantic按照输入Schema本地校验arguments
→ 从可信ToolExecutionContext注入
     tenant_id
     actor
     request_id / trace_id
     agent_run_id / agent_step_id
→ 在线程池中执行只读Handler，并等待到工具超时
→ Handler访问tenant-scoped模拟订单/物流/政策数据源
→ 本地校验工具输出Schema
→ 生成统一ToolObservation
→ 记录成功或标准错误
→ Agent读取Observation，决定下一步
```

## 3. Tool Contract到底是什么

每个 `ToolDefinition` 声明：

- `name`：模型选择时使用的稳定工具名；
- `version`：契约版本；
- `description`：工具做什么、什么时候用；
- `input_model`：允许的参数；
- `output_model`：允许返回的Observation结构；
- `risk_level`：工具风险等级；
- `side_effect`：只读还是写；
- `required_permission`：执行所需权限；
- `timeout_seconds`：最长等待时间；
- `handler`：真正访问业务系统的实现。

它不是普通函数说明，而是模型与确定性执行系统之间的安全契约。

```text
模型只能提出符合契约的动作
→ 后端再次验证
→ 后端决定是否真正执行
```

模型看到Schema不代表获得权限。Schema用于帮助模型正确生成参数，后端校验才决定参数能否进入Handler。

## 4. Registry的作用

Registry回答两个问题：

1. 当前Actor可以向模型暴露哪些工具？
2. 模型选定名称和版本后，对应的确定性契约是什么？

当前按 `name + version` 精确解析，不支持 `latest`：

```text
order_lookup@1.0.0
logistics_lookup@1.0.0
policy_lookup@1.0.0
```

这样评估时可以知道Agent当时使用的契约。若自动使用最新版本，同一个评估案例可能因为工具参数含义悄悄变化而不可复现。

Customer看不到三个内部调查工具，Agent、Supervisor和Tenant Admin只看到自己有权限的工具目录。即使未授权Actor伪造工具名直接请求Executor，执行前仍会被权限门禁拒绝。

所以“目录过滤”改善模型动作空间，“执行时鉴权”才是真正安全边界，两者缺一不可。

## 5. JSON Schema与Pydantic的关系

Pydantic输入模型可以生成JSON Schema。Registry把Schema暴露给模型，告诉模型：

- 有哪些字段；
- 哪些字段必填；
- 类型和枚举；
- 长度、正则等限制；
- 是否允许额外字段。

JSON Schema官方说明，`properties`只描述字段，默认仍允许额外字段；必须使用`additionalProperties: false`才能禁止未声明字段。因此当前参数模型使用`extra="forbid"`。

执行时仍要调用Pydantic `model_validate`，因为：

- 模型可能没有遵守Schema；
- 调用请求可能来自错误代码或恶意客户端；
- 外部模型生成的内容永远是不可信输入。

今天还发现一个重要边界：不能机械地给所有参数开启Pydantic全局`strict=True`。模型传递的是JSON，`"not_received"`需要合法解析成内部`TicketCategory.NOT_RECEIVED`枚举。正确目标是：

- 允许JSON到领域类型的明确解析；
- 拒绝非法枚举；
- 拒绝缺失字段；
- 拒绝额外字段；
- 拒绝不符合pattern的订单号。

## 6. 为什么tenant_id不能由模型传参

错误设计：

```json
{
  "order_id": "10086",
  "tenant_id": "模型自己填写"
}
```

模型不是身份系统，可能填错、被注入或越权。当前工具参数只有业务参数，例如`order_id`。

真实租户来自：

```text
已验证JWT
→ AuthenticatedActor
→ ToolExecutionContext.tenant_id
→ tenant-scoped数据源查询
```

如果模型额外传入`tenant_id`，`extra="forbid"`直接返回`argument_error`，Handler不会被调用。

这是上下文工程与权限边界的连接：可信安全上下文由系统注入，不与模型生成的业务参数混在一起。

## 7. Tool Output与Observation

Handler输出经过本地Pydantic校验后才成为Observation。

Observation包含：

- call_id；
- tenant_id、actor_id；
- tool name/version；
- 输入/输出Schema hash；
- risk level和side effect；
- 类型化output或标准error；
- arguments hash；
- duration；
- request/trace；
- agent run/step。

Observation是“工具在某时刻返回的可追踪结果”，不是永恒事实，也不是Agent下一步决策。

例如：

```text
OrderObservation:
  order_id = 10086
  status = shipped
  observed_at = 2026-07-25T09:00:00Z
```

Agent把它加入当前状态后，才能决定是否查询物流。如果用户把订单号改成10087，这条Observation必须标记为依赖旧主事实而失效。

## 8. 工具的职责边界

### 8.1 Order Lookup

只读取：

- 订单状态；
- 是否付款；
- 是否发货；
- 金额和币种；
- 观察时间。

它不决定退款资格。

### 8.2 Logistics Lookup

只读取：

- 最新物流状态；
- 是否有签收证明；
- 签收时间；
- 观察时间。

它不解释用户是否撒谎，也不执行退款。

### 8.3 Policy Lookup

只读取当前分类和区域对应的：

- 政策ID和版本；
- 必需证据；
- 是否需要人工审批；
- 简要规则；
- 生效时间。

它返回政策证据，不替代后端Policy Engine。读取到“需要人工审批”只是事实；真正退款时仍由确定性代码验证审批是否存在、参数是否一致。

## 9. 六类标准失败及Agent应对

### 9.1 selection_error

原因：工具名或版本不存在。

说明模型选错工具或引用了已下线版本。Agent应重新查看可用工具目录并重选，不应重试同一个不存在的名称。

### 9.2 argument_error

原因：字段缺失、枚举错误、订单号格式错误或额外字段。

说明工具可能选对，但参数生成错误。Agent应修正参数；如果缺少业务信息，应追问用户或调用别的工具补齐。

### 9.3 authorization_error

原因：Actor没有权限。

Agent不能通过重试绕过权限，应停止、转授权流程或人工处理。

### 9.4 resource_error

原因：参数格式正确，但当前tenant下资源不存在。

它与参数错误不同。Agent可以核对订单号、询问用户，不能把“找不到”写成“订单已取消”。

### 9.5 timeout_error / dependency_error

原因：工具超时或依赖临时不可用。

它们可被标记为retryable，但是否重试、最多几次由Agent Runtime的预算与策略决定。Day7 Executor只描述错误，不在内部无限重试。

### 9.6 output_contract_error

原因：Handler返回了不符合声明Schema的数据。

这是工具实现或上游契约漂移，不是模型参数问题。Agent不能使用该输出，应保守失败并告警。

这些分类把Day6的原则延伸到工具层：

```text
错误类型不同
→ 恢复动作不同
→ 不能全部写成“再试一次”
```

## 10. 超时的真实边界

当前只读模拟工具使用`ThreadPoolExecutor`，`Future.result(timeout)`超过时间会返回`timeout_error`。Python官方文档说明：

- `result(timeout)`只限制等待时间；
- `Future.cancel()`只能取消尚未开始的任务；
- 已经运行的线程不能被强制取消。

因此当前超时语义是：

```text
Agent停止等待并拒绝使用迟到结果
≠ 底层线程一定已经停止
```

今天三个工具全部只读，所以迟到线程不会产生写副作用。Day10写工具不能依赖线程timeout保证安全，必须同时使用：

- 下游客户端原生timeout；
- 幂等键；
- 审批；
- 执行记录；
- 执行后查询验证；
- 必要的补偿和恢复。

## 11. 风险等级与只读/写属性为什么分开

`risk_level=low`描述风险大小，`side_effect=read_only`描述是否修改外部状态。

它们不是同一个维度：

- 读取高度敏感数据可能是read-only但风险高；
- 某个内部非关键标记可能是write但风险中等；
- 退款是write且风险高。

今天三个工具是low + read-only。后续工具目录、Agent策略和人工审批会同时参考这两个维度。

## 12. 可观测性与Schema hash

只记录`order_lookup@1.0.0`还不够。有人可能修改参数Schema但忘记升级版本。因此Observation同时记录输入和输出Schema hash。

`arguments_hash`让系统可以判断两次调用参数是否一致，而不用默认把完整参数写入日志。当前内存Recorder保存完整类型化Observation供测试；生产持久化与敏感字段策略将在Agent状态和全链路Trace阶段完善。

如果Observation记录失败，Executor fail closed，不把无法追踪的结果交给Agent。

## 13. 与Day6、Day8的关系

```text
Day6模型结构化输出
  让模型产生可校验的候选判断
→ Day7工具契约
  把候选动作限制在受控工具和参数中
→ Day7 ToolObservation
  把外部结果转换成可追踪事实
→ Day8 Agent Loop
  observe → decide → act → verify
  根据不同Observation或错误修改下一步
```

工具本身不是Agent。三个工具顺序写死调用也不是Agent。只有Day8根据每次Observation动态选择下一步、维护状态和终止条件，才形成最小Agent Loop。

## 14. 官方资料

- [JSON Schema对象、required与additionalProperties](https://json-schema.org/understanding-json-schema/reference/object)
- [Pydantic JSON Schema](https://pydantic.dev/docs/validation/latest/concepts/json_schema/)
- [Pydantic Validation Errors](https://pydantic.dev/docs/validation/latest/errors/validation_errors/)
- [Python concurrent.futures](https://docs.python.org/3/library/concurrent.futures.html)
