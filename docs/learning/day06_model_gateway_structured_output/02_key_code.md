# Day 6 关键代码讲解

## 1. 阅读顺序

建议按真实执行链路阅读：

```text
InvestigationTriageService
→ PromptTemplate / PromptRegistry
→ StructuredModelRequest
→ ModelGateway
→ OpenAIResponsesProvider 或 FakeLLMProvider
→ InvestigationTriage
→ ModelCallRecord Repository
```

## 2. 业务输出契约

位置：`app/domain/investigation_triage.py`

### 输入和输出

它没有接收 HTTP 请求，而是定义模型输出进入业务时必须成为的内部对象：

- `normalized_goal`
- `required_evidence`
- `risk_flags`
- `needs_human_attention`
- `decision_summary`

### 执行流程

Provider 返回的数据交给 Pydantic：

```text
原始 dict / JSON / BaseModel
→ 字段是否齐全
→ 类型和枚举是否合法
→ 是否存在额外字段
→ 列表是否重复
→ 字符串长度是否合法
→ 生成 InvestigationTriage
```

### 为什么这样写

`extra="forbid"` 的重点不是代码整洁，而是能力白名单。模型只能表达 Schema 允许的内容，不能临时增加 `execute_refund` 这样的动作。

使用有限枚举让后续 AgentState、评估和工具映射拥有稳定语言，避免模型一会儿写 `delivery_proof`，一会儿写 `signed_receipt_photo`。

### 架构位置

这是领域层的模型输出契约。它不依赖 OpenAI SDK，也不是对外 HTTP Schema。

### 状态变化

校验本身不修改工单或 AgentState，只把不可信外部输出转换为可信的类型化候选结果。

### 异常处理

任何缺失、额外、类型、枚举、长度或唯一性错误都会产生 Pydantic `ValidationError`，随后由网关映射为 `model_output_invalid`。

## 3. Prompt 模板与注册中心

位置：`app/llm/prompts.py`

### 输入和输出

输入：

- Prompt `name/version`；
- 固定 instructions；
- input template；
- 精确的变量集合；
- 本次变量值。

输出 `RenderedPrompt`：

- `instructions`
- `input_text`
- `prompt_hash`
- `name/version`

### 执行流程

```text
检查 missing variables
→ 检查 unexpected variables
→ 用稳定 JSON 格式序列化每个变量
→ 渲染 input_text
→ 对 name/version/instructions/input_text 计算 SHA-256
```

### 为什么这样写

- 精确变量集合能发现字段漏传或上下文被无意扩张；
- `sort_keys=True` 和稳定分隔符让同一输入产生同一 hash；
- 按明确版本读取，避免“latest”造成评估漂移；
- 工单数据经过 JSON 序列化后作为一个 data payload，不用字符串拼接制造额外模板结构。

### 架构位置

Prompt Registry 属于模型应用基础层，业务服务负责选择使用哪个 Prompt，Provider 不负责决定业务 Prompt。

### 状态变化

模板是不可变对象；渲染只产生新值，不修改 Registry。

### 异常处理

- 版本不存在：`PromptNotFoundError`
- 变量集合不匹配：`PromptRenderError`

这两类是配置/程序错误，不应伪装成模型随机失败。

## 4. 统一请求与 Provider 协议

位置：`app/llm/contracts.py`

### 输入和输出

`StructuredModelRequest` 是业务提交给网关的请求，包含：

- 任务与租户；
- Prompt 身份和实际 hash；
- 关联资源；
- instructions/input；
- 期望 Pydantic 类型；
- request/trace。

`ProviderModelRequest` 是网关交给 Provider 的供应商无关请求，增加模型、reasoning 和输出 token 配置。

`RawProviderResponse` 是 Provider 返回的中立结果。

`StructuredModelResponse` 是网关校验、记录成功后返回给业务的结果。

### 执行流程

```text
业务请求
→ Gateway 补充统一运行配置
→ Provider 协议
→ 外部 SDK
→ 中立 RawProviderResponse
→ Gateway 本地校验
→ 类型化 StructuredModelResponse
```

### 为什么这样写

业务服务不能依赖 `openai.types.responses.Response`，否则更换供应商会修改全部业务逻辑。Protocol 采用结构化接口，Fake 和真实 Provider 遵循相同契约。

### 架构位置

Contracts 位于业务层和外部 Provider 的六边形端口。OpenAI adapter 是端口的一个实现。

### 状态变化

这些 dataclass 是不可变的消息对象，不保存共享运行状态。

### 异常处理

协议本身不吞异常。Provider 把供应商错误翻译成统一错误，Gateway 再决定重试和业务可见错误。

## 5. ModelGateway

位置：`app/llm/gateway.py`

### 输入和输出

输入：`StructuredModelRequest[OutputT]`

成功输出：`StructuredModelResponse[OutputT]`

失败输出：统一的 `ModelGatewayError` 子类，并先尝试写入失败轨迹。

### 执行流程

```text
创建 call_id，记录开始时间
→ for attempt in 1..max_attempts
→ Provider.complete()
→ 若临时错误且仍有预算：指数退避并重试
→ 若永久错误或预算耗尽：记录失败并抛出
→ 对 raw output 做本地 Pydantic 校验
→ 计算 schema hash
→ 写入 succeeded ModelCallRecord
→ 返回类型化结果
```

### 为什么这样写

重试、校验和记录必须集中在同一个边界：

- 业务不会各写一套重试；
- 实际尝试次数可观测；
- 所有 Provider 共享相同安全门槛；
- 测试可注入 sleeper 和 clock，不需要真的等待；
- 只有记录成功的结果才能离开网关。

网关不会重试非法结构化输出。重复相同 Prompt 不保证模型会修好，还会增加成本；当前业务有更安全的确定性 fallback。

### 架构位置

它是 Agent Runtime 将来调用模型的统一基础服务。它不负责 Agent 下一步规划，也不负责执行工具。

### 状态变化

网关不修改 Ticket。它只新增一条 `ModelCallRecord`，状态为：

- `succeeded`
- `failed`

记录保存尝试次数、错误链和运行元数据。

### 异常处理

- retryable Provider error：有限重试；
- non-retryable Provider error：立即拒绝；
- output validation error：记录并拒绝；
- unclassified Provider exception：映射为拒绝，不无限重试；
- trace persistence failure：抛出 `ModelCallRecordingError`，不释放不可追踪输出。

## 6. OpenAI Provider adapter

位置：`app/llm/openai_provider.py`

### 输入和输出

输入统一 `ProviderModelRequest`；输出统一 `RawProviderResponse`。

### 执行流程

```text
ProviderModelRequest
→ client.responses.parse(
     model,
     instructions,
     input,
     text_format=Pydantic model,
     reasoning,
     max_output_tokens,
     metadata,
     store=False
   )
→ 提取 output_parsed
→ 提取实际模型、request/response ID、token usage
→ RawProviderResponse
```

### 为什么这样写

- 使用 Responses API 和 SDK 的 Pydantic parsing helper；
- `store=False` 避免在供应商侧主动保存本次响应；
- metadata 只放 trace 和 Prompt 身份，不放工单正文；
- SDK 异常在 adapter 内翻译，业务不认识供应商异常类。

### 架构位置

这是基础设施适配器。更换供应商时新增另一个 `StructuredLLMProvider` 实现。

### 状态变化

adapter 本身不写数据库。调用元数据由 Gateway 统一记录。

### 异常处理

- timeout、connection、rate limit、5xx → 可重试统一错误；
- authentication、permission、bad request → 永久统一错误；
- refusal、truncation、没有 parsed output → 不接受为正常结果。

## 7. Runtime 与配置

位置：

- `app/llm/runtime.py`
- `app/core/config.py`
- `.env.example`

### 输入和输出

输入环境变量；输出配置完成的 OpenAI client、Provider、SQL trace repository 和 ModelGateway。

### 执行流程

```text
Settings.from_env()
→ 校验 provider/model/reasoning/timeout/retry/output limit
→ 检查 OPENAI_API_KEY
→ OpenAI(max_retries=0, timeout=...)
→ OpenAIResponsesProvider
→ SqlAlchemyModelCallRepository
→ ModelGateway
```

### 为什么这样写

SDK 的隐式重试关闭，是为了让网关成为唯一重试层。API Key 使用 `repr=False`，避免打印 Settings 时把密钥带入日志。生产 runtime 不允许悄悄换成 Fake；缺少 Key 会明确启动失败。

### 架构位置

Runtime 是 composition root：在系统边缘组装实现，不把具体实现硬编码进业务服务。

### 状态变化

创建客户端连接资源，关闭 runtime 时调用 `client.close()`。

### 异常处理

缺少 Key 抛出 `ModelConfigurationError("openai_api_key_missing")`。非法配置在 `Settings` 构造时立即失败，避免运行到第一次模型调用才暴露。

## 8. Fake Provider

位置：`app/llm/fake_provider.py`

### 输入和输出

构造时传入按顺序排列的结果或异常；每次 `complete()` 消费一个 outcome，并保存收到的请求。

### 执行流程

```text
第 1 次调用 → outcome[0]
第 2 次调用 → outcome[1]
...
```

### 为什么这样写

测试可以精确构造“timeout → rate limit → success”，验证尝试次数和退避，而不依赖真实网络或模型随机性。

### 架构位置

它是测试适配器，与 OpenAI Provider 实现同一个协议，不允许作为生产缺少 API Key 时的静默替代。

### 状态变化

内部 outcome 队列逐次消耗，请求列表逐次增加；`RLock` 保护并发访问。

### 异常处理

没有预设结果时抛出 AssertionError，表示测试本身缺少安排，而不是业务模型失败。

## 9. 调查分诊业务服务

位置：`app/services/investigation_triage_service.py`

### 输入和输出

输入 `TriageTicketCommand` 和 request/trace；输出：

- 类型化分诊结果；
- 来源是 `model` 或 `deterministic_fallback`；
- 降级原因；
- model call ID；
- Prompt name/version。

### 执行流程

```text
选择 ticket-investigation-triage@1.0.0
→ 只放 category/subject/description
→ 渲染为 untrusted ticket data
→ 调用 ModelGateway
→ 成功：返回模型的 InvestigationTriage
→ 失败：按 TicketCategory 构建保守证据清单并要求人工关注
```

### 为什么这样写

业务服务决定“模型失效时怎样继续服务”，因为降级是业务语义，不应由通用网关猜测。未收到货至少保留订单状态、物流、签收证明和政策；退款失败还需付款状态。

### 架构位置

这是应用服务层的用例。未来 Agent 节点可以调用它完成初始分诊，但不能把它当作完整 Agent Loop。

### 状态变化

当前只产生分诊结果和模型调用记录，不更新 Ticket 状态，不执行任何高风险动作。

### 异常处理

所有 `ModelGatewayError` 都进入明确 fallback，并保留 `degradation_reason`。不会返回空值，也不会假装模型结果成功。

## 10. 模型调用持久化

位置：

- `app/domain/model_call.py`
- `app/repositories/model_call_repository.py`
- `app/repositories/sqlalchemy_model_call_repository.py`
- `app/db/models.py`
- `migrations/versions/20260724_0005_add_model_call_records.py`
- `migrations/versions/20260724_0006_add_model_call_reproducibility_fields.py`

### 输入和输出

Repository 输入完整 `ModelCallRecord`，输出是持久化完成；查询按 `tenant_id + trace_id` 返回本租户轨迹。

### 执行流程

```text
Gateway 构建 record
→ Repository 开独立短事务
→ insert
→ commit
→ 查询时同时过滤 tenant 和 trace
```

### 为什么这样写

模型调用记录不应该等到上层长事务结束才保存，否则业务降级或回滚时会丢失失败证据。查询同时过滤 tenant，避免同一个 trace 字符串导致跨租户泄露。

### 架构位置

这是模型可观测性的持久化基础设施。Day18 会把它与 OpenTelemetry span 串联，但业务元数据仍需要结构化保存。

### 状态变化

每次模型调用新增一条不可变记录，不覆盖历史。

### 异常处理

写入失败向 Gateway 抛出，Gateway fail closed。迁移 0006 使用 nullable → backfill → non-null，保证已有记录可以升级。
