# Day19 关键代码

## 1. 指标契约

位置：`app/observability/metrics.py:19`

**输入输出**：各业务边界传入稳定的 status/domain/error code 和耗时；`ResolveFlowMetrics` 把它们转成 Prometheus sample，`render()` 返回 exposition bytes 和 content type。

**执行流程**：`AgentRunner`、`ModelGateway`、`ToolExecutor`、Retrieval、RAG、Policy、SQLAlchemy hook 和 Worker 只调用对应 `record_*`；指标类统一管理名称、label 和 bucket。

**为什么这样写**：如果每个业务模块自己声明 Counter，容易同名重复注册、label 不一致和敏感字段渗入。集中契约让 Dashboard/Alert 有稳定依赖。

**架构位置**：Observability 是横切层，它观察 Agent/业务运行，不决定退款、权限或 Agent 下一步。

**状态变化**：Counter 增加、Histogram bucket/sum/count 增加、in-progress Gauge 进入时 +1/结束时 -1；不修改业务实体。

**异常处理**：未知动态 label 被折叠为 `other`，防止时间序列无界增长；业务故障仍按原异常流程处理。

## 2. HTTP 入口与 `/metrics`

位置：`app/main.py:139`、`app/api/routes/metrics.py:10`

- Middleware 在请求进入时创建 request/trace 关联上下文，增加 in-progress；
- Router 完成后才能取到 route template，然后记录 method/route/status class/duration；
- 未处理异常记为 500，在重新抛出前保证 Gauge -1；
- `/metrics` 只序列化内存 CollectorRegistry，不查业务数据库。

`route` 不是 `request.url.path`：前者是 `/tickets/{ticket_id}` 稳定模板，后者会把每个 ticket ID 变成新 label。

## 3. Agent、LLM、Tool 和 Policy 埋点

位置：

- `app/agent/runner.py:91`：在终态已确定后记录 status、termination reason、failure domain、steps 和 tokens；
- `app/llm/gateway.py:107`：网关成功和类型化异常都记录，不保存 Prompt 正文；
- `app/tools/executor.py:100`：Tool Observation 已经把失败分成 selection/argument/authorization/timeout/dependency/contract，Metric 复用这个可信结果；
- `app/policy/refund.py:110`：`allowed/denied` 是业务决策 Metric，denied 不能算系统错误。

这些埋点放在“结果已定型”的包装层，而不是在 Planner 模型自由文本里猜测状态。

## 4. 结构化 JSON Log

位置：`app/observability/logging.py:26`、`app/observability/logging.py:36`

`log_context()` 用 `ContextVar` 让同一异步请求内的日志自动携带 request/trace ID，离开上下文时 reset，防止请求串号。`JsonLogFormatter` 统一输出 timestamp、level、logger、event 和允许字段。

客户正文、Prompt、Token、邮箱、手机号等字段被置为 `[REDACTED]`。未知字段默认也脱敏，这是失效安全，而不是黑名单放行。

## 5. Prometheus/Grafana 的配置即代码

位置：

- `compose.yaml:44`：锁定 Prometheus 3.13.0 和 Grafana 13.1.0；
- `monitoring/prometheus/prometheus.yml`：5 秒 scrape，15 秒 rule evaluation；
- `monitoring/prometheus/rules/resolveflow_alerts.yml:12`：5 条告警；
- `monitoring/grafana/provisioning/`：自动注入 Prometheus data source 和 Dashboard provider；
- `monitoring/grafana/dashboards/resolveflow-overview.json:6`：技术/业务两组面板。

配置文件使 Dashboard 不依赖某个人在网页里手工点出来的状态，重建容器也能复原。

## 6. 故障归因评估

位置：`app/observability/metrics.py:322`、`evaluation/run_day19_observability.py`

**输入**：各 100 次 Run 的基准窗口和故障窗口。

**输出**：前后成功率、差值、首要失败 domain、次数和占全部失败的比例。

**对照**：基线把全部失败放在 unknown，只能发现下降；Final 使用同一数据和判定标准，但保留稳定 failure domain，所以能定位 Tool。

这个 Analyzer 用两个独立 Registry 代表时间窗口；在线系统则由 PromQL `increase(...[window])` 对累计 Counter 求窗口增量。
