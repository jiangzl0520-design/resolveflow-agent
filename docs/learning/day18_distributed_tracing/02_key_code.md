# Day18 关键代码

## 1. `app/observability/tracing.py`

### 输入与输出

输入是 operation name、Span kind、受控属性和可选 parent Context；输出是当前 OpenTelemetry Span。统一方法还负责属性脱敏、事件记录、错误分类、Context inject/extract 和 Trace ID 获取。

### 执行流程

`operation_span()` 获取全局 Tracer，创建当前 Span，写入安全属性；业务成功时正常结束，未分类异常时只记录异常类型，不记录异常 message。业务代码已经设置稳定错误时，通用异常处理不会覆盖它。

### 为什么这样写

如果每个模块直接调用 `span.set_attribute()`，敏感字段策略和错误分类会迅速失控。统一出口保证所有模块遵守同一条安全规则。

### 架构位置、状态与异常

它位于横切的 Observability 层，不依赖业务服务。它不修改业务 State，只生成可丢弃的遥测数据。Exporter 失败不能改变退款、工具或数据库业务结果。

## 2. `configure_global_tracing()`

输入是 Settings 和可选 Exporter，输出进程级 `TracerProvider`。生产支持 Console 或 OTLP HTTP，测试注入 `InMemorySpanExporter`。

同一进程只安装一个 Provider，但测试可以追加独立 Span Processor。默认 exporter 为 `none` 时不安装 SDK Provider，避免导入模块就联网。

## 3. `app/main.py` HTTP Middleware

输入是 HTTP Request 和可能存在的 `traceparent`；输出是 HTTP Response、`X-Request-ID`、`X-Trace-ID` 和 Server Span。

流程为：extract parent → start Server Span → 生成 request ID → 设置 request state → Router/Service 执行 → 记录 route/status → 返回关联 header。

HTTP Middleware 是最外层入口，因此下游 Service 和数据库才能自动成为子 Span。只在 Router 内埋点会漏掉路由匹配失败、中间件异常和异常处理阶段。

## 4. `app/worker/celery_dispatcher.py` 与 `app/worker/tasks.py`

Dispatcher 在 Producer Span 内调用 `inject_trace_context(headers)`，Worker 从 header 中 extract 后创建 Consumer Span。业务消息仍只包含 job identity 和安全关联字段。

成功时记录 worker outcome；重试时记录 retryable error、delay 和 retry count；终止失败记录稳定 reason；取消不是系统错误。

这段代码把“消息里有相同 trace_id”升级为真实 Parent/Child 传播。

## 5. `app/agent/runner.py`

公开 `run()` 是 Trace 包装器，原来的循环移动到 `_run()`，因此 Agent 核心算法没有为了可观测性被重写。

输入是 `AgentRunCommand`，输出仍是 `AgentRunState`。包装器读取最终 State，把每个 `AgentStepTrace` 转成 `agent.state_transition` 事件，并记录 Token、步数、termination reason 和 outcome。

订单号和工单号不写原值，只写稳定 hash。

## 6. `app/agent/workflow.py`

`start()`、`resume()` 和 `review_refund()` 是 Run 级 Span；`_traced_node()` 包装不会触发 interrupt 的关键 LangGraph 节点。

输入是 checkpoint state，输出仍是节点 patch。Span 只观察 node、step count、next status、next node 和 terminal error，不改变 checkpoint 数据。

人工审批记录 action、提案版本和 hash。审批拒绝归因到 policy；不记录审批理由正文。

## 7. `app/llm/gateway.py`

公开 `generate()` 包装原 `_generate()`：

1. 记录 provider/model/Prompt 版本和 Schema；
2. 调用原有有界重试与结构化校验；
3. 成功记录 attempts、latency 和 Token；
4. 模型失败归因到 model；
5. 模型调用记录写入失败归因到 database。

Prompt 内容和模型输入输出完全不进入 Span。持久化 `ModelCallRecord` 仍负责可审计复现，Span 不替代它。

## 8. `app/services/knowledge_search_service.py` 与 `grounded_answer_service.py`

Knowledge Search Span 输出 query hash、mode、候选数、结果数、延迟和 degraded reason；Grounded Answer Span 输出证据数量、冲突数、引用数和安全 abstention 状态。

权限、检索、模型、Grounding Policy 和记录失败分别保留不同 failure domain。这样上层 RAG 失败不会全部显示成同一个 `knowledge_answer_failed`。

## 9. `app/tools/executor.py`

Tool Span 从 `ToolCallRequest` 读取工具身份和参数 hash，执行 Registry 解析、权限、Schema、线程池 handler、输出校验和 Observation 记录，最后写入 Tool status/error kind/error code。

线程池提交前捕获当前 OTel Context，handler 线程 attach 后执行，再 detach；这解决了 ContextVar 不自动跨线程的问题。

## 10. `app/observability/sqlalchemy.py`

SQLAlchemy `before_cursor_execute` 创建 DB Client Span并 attach，`after_cursor_execute` detach/end，`handle_error` 先标记 database failure 再结束。

输入虽然包含 statement 和 parameters，但实现只解析第一个 SQL 动词，不把 statement/parameters 写入属性。这样既能判断数据库阶段，又控制数据泄露。

## 11. `app/observability/diagnostics.py`

输入是一条 Trace 的 finished spans，输出 `TraceFailureDiagnosis`：trace ID、domain、error code、span name 和深度。

Analyzer 根据 parent Span ID 计算深度，选择最深的显式失败分类。父 Agent Span 的 `agent_failed` 不会覆盖子 Tool Span 的 `tool_timeout`。

这是一种确定性归因规则，适合明确的组件错误；语义复杂的“答案是否有帮助”仍由 Day21/22 的轨迹评估和人工/LLM Judge 处理。
