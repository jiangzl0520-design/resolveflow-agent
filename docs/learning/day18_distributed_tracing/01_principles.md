# Day18 原理：OpenTelemetry 全链路 Trace

## 1. 今天解决的真实问题

Day17 之前已经有 `request_id`、业务 `trace_id`、模型调用记录、检索记录、工具 Observation 和 Agent Step，但它们仍然分散在不同对象和表中。失败后只能先猜“可能是模型、检索、工具或数据库”，再逐处查询。

Day18 解决的是：一次请求经过 API、消息队列、Worker、Agent、模型、RAG、Tool 和数据库后，如何保留因果关系，使工程人员从失败 run 直接下钻到真正失败的组件。

`request_id` 只能说明“这是哪个 HTTP 请求”；业务 `trace_id` 只能作为检索条件；真正的分布式父子关系由 OpenTelemetry Context 和 W3C `traceparent` 建立。

## 2. Trace、Span、Context 的关系

- Trace：一次完整操作的因果树，共享同一个 128 位 Trace ID。
- Span：因果树中的一次具体操作，例如 HTTP 请求、模型调用或工具执行。
- Parent Span ID：说明当前 Span 是由哪一步触发的。
- Context：当前执行位置携带的 Trace ID、Span ID、采样信息等运行上下文。
- `traceparent`：Context 跨 HTTP、消息队列或进程边界时使用的 W3C 文本载体。

只给每个组件传同一个字符串，不能自动形成父子树。必须在发送端 `inject` 当前 Context，在接收端 `extract` 后把它作为新 Span 的 parent。

OpenTelemetry 官方同样将传播定义为跨服务移动上下文、建立分布式因果关系的机制，并推荐使用 W3C Trace Context：<https://opentelemetry.io/docs/languages/python/propagation/>。

## 3. ResolveFlow 的完整执行流程

### 3.1 HTTP 入口

1. FastAPI Middleware 从请求头提取上游 `traceparent`。
2. 创建 `SERVER` Span；没有上游 Context 时创建新的根 Span。
3. 生成独立 `request_id`，把 OpenTelemetry Trace ID 写入 `request.state.trace_id`。
4. Router、Service 和数据库操作都在当前 Span Context 中执行。
5. 响应返回 `X-Request-ID` 和 `X-Trace-ID`。

`request_id` 用于定位一次 HTTP 尝试；同一个业务任务重试三次会有三个 request ID。Trace ID 用于串联同一条因果链。

### 3.2 API 投递 Worker

1. Dispatcher 创建 `PRODUCER` Span。
2. 消息只携带 job ID、request ID、业务 Trace ID 和标准 `traceparent`/`tracestate`。
3. 不把租户对象、Actor、工单正文或其他业务对象塞进消息。
4. Worker 从消息 header 提取 Context，并创建 `CONSUMER` Span。
5. Worker 中的数据库查询成为 Consumer Span 的子 Span。

如果 Broker 失败，Producer Span 记录 `failure.domain=worker`、`broker_unavailable` 和 `retryable=true`。Worker 的有界重试记录 retry count、delay 和最终 outcome。

OpenTelemetry Messaging 语义约定区分 `send`、`receive` 和 `process`，并定义消息系统、目标和 message ID 等属性：<https://opentelemetry.io/docs/specs/semconv/messaging/messaging-spans/>。

### 3.3 Agent 与 LangGraph

`AgentRunner` 创建 Agent Run Span，并记录：

- workflow 名称和 Agent Run ID；
- 最大步数、实际步数和终止原因；
- 模型输入/输出 Token；
- 每一步 action、tool、model call、tool call、verification code；
- completed、failed、escalated 等最终状态。

Agent Step 不单独存正文，而是写成 `agent.state_transition` 事件。这样既能看到状态变化，又不会把用户内容重复复制到可观测平台。

LangGraph 的 `plan`、`execute_tool`、`verify`、`evaluate_refund`、`execute_refund`、`verify_refund` 节点各自生成子 Span。`interrupt()` 是暂停控制流，不是异常，因此容易触发中断的 `control` 和 `refund_approval` 节点不使用会自动标错的通用异常包装。

暂停后从另一次 HTTP 请求恢复时，会产生新的入口 Trace，但仍可通过持久化的业务 `trace_id` 和 Agent Run ID 关联。要在数小时后的恢复中强行继续原 Trace，需要额外持久化完整 Trace Context；当前没有用伪造 Span ID 冒充这种能力。

### 3.4 Context Builder

Context Span 记录候选数量、Token 预算、实际输入 Token、纳入数量和丢弃数量。每个 Fragment 只记录：

- fragment ID；
- source/trust level；
- 是否纳入；
- decision reason；
- estimated tokens。

不记录 Fragment 原文。required context 溢出归因到 `context`；恶意 required context 归因到 `security`；Trace 自身写入失败归因到 `database`。

### 3.5 LLM

模型 Span 记录 provider、请求模型、响应模型、Prompt 名称/版本/hash、结构化 Schema 名称、attempts、延迟和 Token。

它明确不记录 instructions、input text、output messages 和模型原文。GenAI 语义约定警告输入输出消息很可能包含敏感或 PII 数据，因此内容采集不能默认开启：<https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/>。

瞬时错误重试后成功时，Span 记录总 attempts 和 retry event；最终失败时使用稳定 error code，例如 `provider_timeout` 或 `model_output_invalid`。

### 3.6 RAG 与检索

Grounded Answer Span 是 RAG 总操作，Knowledge Search 是它的 Retrieval 子 Span。记录检索模式、top-k、query hash、embedding model、候选数量、结果数量、降级原因、证据数量、冲突数和引用数。

不记录原问题、知识正文、生成答案或 exact quote。检索失败归因到 `retrieval`，权限拒绝和 Prompt Injection 归因到 `security`，引用/grounding 校验失败归因到 `policy`。

### 3.7 Tool

Tool Span 记录工具名、语义版本、参数 hash、Schema hash、风险等级、副作用类型、耗时、status、error kind 和稳定 error code。

工具参数和 Observation 正文不进入 Span。Tool handler 在线程池运行，Python Context 不会天然跨新线程，因此提交线程任务前捕获当前 OpenTelemetry Context，在线程中 attach，结束后 detach。否则远程 MCP 或工具内部 Span 会变成孤立根 Span。

### 3.8 数据库

SQLAlchemy Engine 通过事件钩子产生 `CLIENT` Span，只记录数据库类型和 `SELECT/INSERT/UPDATE/DELETE` 等操作类型。

它不记录 SQL 正文和参数。SQL 可能包含邮箱、订单号、搜索内容甚至错误拼接的秘密；调试数据库性能不等于必须把完整 SQL 复制到 Trace。数据库语义约定要求 DB Client Span 表示调用方看到的逻辑数据库操作：<https://opentelemetry.io/docs/specs/semconv/db/database-spans/>。

## 4. 敏感字段过滤为什么必须在统一出口完成

只要求每个业务开发者“记得不要记录秘密”不可控。Day18 在 `set_safe_attributes()` 统一执行四层处理：

1. 拒绝 authorization、cookie、password、secret、API key、消息正文等敏感 key；
2. 对字符串 value 再执行秘密、JWT、邮箱、手机号和 Prompt Injection 检查；
3. 普通标识符只保留稳定 hash，避免记录订单号和工单号；
4. 字符串限制最大长度，防止高体积或恶意载荷污染 Trace。

异常也不直接 `record_exception(exc)`，因为异常 message 可能包含 SQL、HTTP 响应或用户数据；Span 只保存异常类型和稳定内部 error code。

## 5. 失败归因

父 Span 往往只能知道 `agent_failed` 或 HTTP 500，真正原因在更深的子 Span。例如：

```text
HTTP POST /investigation-jobs
└── run after-sales agent [agent_failed]
    └── execute logistics_lookup [tool_timeout]
```

`TraceFailureAnalyzer` 从失败 Span 中选择最深的显式分类 Span，因此结果是 `tool/tool_timeout`，而不是泛化成 `agent/agent_failed`。

当前分类包括 API、Worker、Agent、Model、Retrieval、Tool、Database、Policy、Context、Security 和 Business。稳定分类用于 Day19 的 Metrics 聚合和告警。

Policy deny 与系统异常要区分：退款不满足规则是预期业务决策，Span 不必标成系统 ERROR，但仍写入 policy domain 和 code，供失败 run 解释“为什么没有继续”。

## 6. SDK、Exporter 与部署

业务模块调用 OpenTelemetry API；SDK 负责采样、Span Processor 和导出；Exporter 再把 Span 发到 Console 或 OTLP Collector。

支持的环境变量：

```text
OTEL_TRACES_EXPORTER=none|console|otlp
OTEL_SERVICE_NAME=resolveflow-api
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://collector:4318/v1/traces
OTEL_TRACES_SAMPLER_ARG=1.0
```

默认 `none`，模块导入不会静默联网或打印 Trace。测试使用 OpenTelemetry 官方内存 Exporter；生产配置 `otlp` 后才连接 Collector。Day19 再增加 Prometheus、日志关联和 Grafana，不在 Day18 假装已经有 Dashboard。

OpenTelemetry 官方 Python 文档将 SDK 初始化、Tracer、嵌套 Span、属性、事件、状态和异常记录视为手工埋点的基本机制：<https://opentelemetry.io/docs/languages/python/instrumentation/>。

## 7. 与前后 Day 的关系

- Day5 的 API/Worker 异步边界提供消息传播场景；Day18 给它补上 W3C Context。
- Day6 的模型调用持久化记录负责审计和复现；Day18 Span 负责实时因果与耗时，两者不能互相替代。
- Day8/9 的 Agent State、Step 和 checkpoint 提供状态事实；Day18 把状态变化投影为 Trace 事件。
- Day13/14 的 Retrieval Run、Answer Run 和 Citation Trace 是业务证据；Day18 提供跨组件入口。
- Day15–17 的来源、Token、安全过滤决定哪些内容能进入模型；Day18 使用同样的“正文不默认进入可观测系统”原则。
- Day19 将按 Day18 的稳定 component、status、failure domain 和 error code 生成 Metrics、Logs、Dashboard 与告警。

## 8. 明确边界

- 固定评估是合成数据，不代表生产故障分布。
- 尚未运行外部 Collector 和 Trace UI；当前通过内存 Exporter 验证真实 Span。
- 调度补偿如果脱离原始活跃 Context，会创建新 Trace，并通过业务 trace ID 关联。
- 采样率、Exporter 网络开销和高并发成本属于后续性能阶段。
- Trace 不能代替持久化业务审计；采样或 Collector 故障不应改变退款、权限和事务结果。
