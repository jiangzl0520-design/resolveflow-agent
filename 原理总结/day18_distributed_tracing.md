# Day18：OpenTelemetry 全链路 Trace

## 业务问题

Day17 前虽然有 request ID、业务 trace ID、模型记录、检索记录、Tool Observation 和 Agent Step，但它们只是分散的关联字段。失败后仍要人工猜测问题在模型、检索、工具、数据库还是 Policy。

## 完整流程

HTTP Middleware 提取上游 W3C `traceparent` 并创建 Server Span；Router/Service/DB 在当前 Context 中执行。API 投递 Celery 时创建 Producer Span并把 Context 注入消息 header，Worker 提取后创建 Consumer Span。Agent Run、LangGraph 节点、Context Builder、LLM、RAG、Retrieval、Tool、Policy 和 DB 再创建各自的子 Span，最终由 Exporter 输出。

`request_id` 标识一次HTTP尝试；业务 `trace_id` 用于持久化关联；OpenTelemetry Context 才真正保存 Trace ID、Parent Span ID 和采样信息。只有同一个字符串不等于父子 Trace。

## 各层职责

- API：入口 method/route/status 和响应 Trace ID；
- Worker：消息 send/process、重试和 outcome；
- Agent：步骤、状态转换、Token、终止原因；
- LLM：provider/model、Prompt版本、Schema、重试、Token和耗时；
- RAG/Retrieval：query hash、模式、候选数、冲突、引用和降级；
- Tool：名称/版本、参数hash、Schema hash、风险、状态和错误类型；
- Policy：允许/拒绝、稳定规则码和证据缺口；
- DB：数据库类型和SQL操作类型，不保存SQL正文及参数。

## 状态、失败和安全

Trace只观察业务状态，不替代 AgentState、checkpoint 或审计表。最深失败 Span 优先：Agent只知道 `agent_failed`，子 Tool明确是 `tool_timeout` 时，根因应归到 Tool。

统一属性出口拒绝 authorization、cookie、password、secret、Prompt和消息正文，对邮箱/JWT/手机号等脱敏，对工单/订单只保留hash，并限制字符串长度。异常message也不直接记录，避免SQL、HTTP响应或用户数据泄露。

Tool handler在线程池运行，必须手动传递 Context；Celery发送端 inject、消费端 extract。否则它们会形成互不相干的根Span。

Policy拒绝属于正常业务门禁，不等于系统崩溃；它保留 policy domain/code，但不必伪装成服务器异常。取消、暂停和人工审批同理。

## 配置与恢复边界

默认 exporter 为 `none`；可配置 `console` 或 OTLP HTTP。没有 Collector 时使用官方内存 Exporter测试真实Span。checkpoint在很久后由新请求恢复会产生新入口Trace，当前通过业务trace ID和Agent Run ID关联，不伪造旧Parent Span。

## 测试和量化证据

Day18专项10/10通过，受影响模块101个测试无失败；真实PostgreSQL全量241/241通过，PostgreSQL/Redis healthy，编译、依赖和迁移检查正常。

70例固定合成数据中，关联ID+通用根错误Baseline根因定位正确率14.29%，组件级Trace为100%，未分类失败从60降到0，60个固定敏感载荷均未进入最终Span。该结果只代表合成评估集，不是生产收益。

## 前后关系

Day5提供API/Worker异步边界，Day6–17提供模型、Agent、RAG、Context、Memory和安全事实；Day18把这些事实串成可下钻的因果树。Day19将在稳定的component、status、failure domain和error code上建立Metrics、Logs、Dashboard和告警。
