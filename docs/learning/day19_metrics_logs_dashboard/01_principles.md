# Day19 原理：Metrics、Logs、Dashboard、Alert 与 SLO

## 1. 今天解决的真实问题

Day18 已经能沿一次 Agent Run 定位最深失败点，但 Trace 不适合回答“今天总体成功率降了多少”。Day19 建立聚合视角：

1. 稳定的业务或 Agent 操作在代码内产生 Counter、Histogram 和 Gauge；
2. `GET /metrics` 把当前 API 进程的序列化样本暴露给 Prometheus；
3. Prometheus 每 5 秒拉取一次，存成带时间的序列；
4. Grafana 用 PromQL 查询成功率、P95、失败 domain、Tool 错误类型和业务结果；
5. 告警规则按时间窗口评估 SLI，且用 `for` 抑制短暂毛刺；
6. 看到下降后，先用 Metrics 定位类别，再用 Logs/Trace 查单次细节。

## 2. Trace、Metric 和 Log 怎样串起来

| 信号 | 最擅长回答 | 不应该承担 |
|---|---|---|
| Metric | 多少次、比率多高、P95 多少、哪类失败最多 | 单个订单的完整过程 |
| Log | 某个时刻发生了什么事件、稳定错误码是什么 | 依靠文本日志现场统计全局 P95 |
| Trace | 一次 API→Worker→Agent→LLM/RAG/Tool/DB 中谁是上下游、耗时和根因 | 未采样的全量统计 |

Metric 不携带 `request_id/trace_id`，因为这会为每次请求生成新时间序列，使基数失控。Log 和 Trace 可以携带这两个关联 ID，所以排障时的顺序是：“Metric 选定时间+失败类别→Log/Trace 进入一次具体运行”。

Metrics 必须独立于 Trace 采样产生。否则当 Trace 只采样 10% 时，用 Span 数量冒充总请求数会得到错误成功率。

## 3. Counter、Gauge 和 Histogram

- Counter 只累加，用于请求、Agent Run、Tool Call、Policy Decision 总数。PromQL 用 `rate()` 或 `increase()` 得到速率或窗口增量。
- Gauge 可增可减，用于当前正在处理的 HTTP 请求。不能用 Gauge 存“历史总数”。
- Histogram 在客户端按 bucket 累计延迟，Prometheus 端用 `histogram_quantile(0.95, ...)` 计算 P95。它能跨实例聚合，但精度取决于 bucket 边界。

当前 bucket 从 10ms 到 10s，适用 API、Model、Tool、Retrieval 的开发环境。生产上应根据实测分布和 SLO 边界调整，不应该凭感觉增加 bucket。

## 4. Label 基数为什么是核心安全边界

一条时间序列由“指标名+全部 label 组合”唯一决定。如果把 `tenant_id/order_id/request_id/raw_error` 放进 label，请求越多，时间序列就无上限增长，同时还可能泄露资源标识。

Day19 使用三道限制：

1. HTTP 记录 FastAPI 注册的 route template，不记录带 ID 的原始 URL；
2. Tool、Model、Operation、Error Code 经过允许集，未知值统一折叠为 `other`；
3. 资源 ID、Prompt、客户正文只能进受控 Log/Trace，不进 Metric label。

代价是 `other` 过多时信息不够细。正确处理是把经常出现且有运营价值的稳定枚举加入允许集，而不是放开任意字符串。

## 5. 技术指标与业务指标必须分开

技术成功不等于业务正确：Tool 成功返回“不符合退款”时，Tool 技术状态是 succeeded，Policy 业务决策是 denied，两者都是正确结果。因此：

- `resolveflow_http/agent/llm/tool/retrieval/database/worker_*` 表示技术健康；
- `resolveflow_business_policy_decisions_*`、`human_escalations_*`、`rag_answers_*` 表示业务结果；
- Grafana 用两个独立 Row 展示，不把 Policy 拒绝错标成系统故障。

## 6. “今天成功率为什么下降”的实际排查流程

1. 成功率面板确认下降时间和幅度；
2. `Failures by domain` 判断是 model、retrieval、tool、context 还是 agent 本身；
3. 若 Tool 最高，`Tool failures by kind` 继续区分 selection、argument、authorization、timeout、dependency 和 contract；
4. 在相同时间窗口查 JSON Log 的稳定 `error_code`，通过 `trace_id` 进入 Day18 Trace；
5. 在 Trace 中确认最深失败 Span、参数哈希、重试、上下游耗时；
6. 修复后看告警恢复，并用固定评估数据集做回归，不只凭 Dashboard 曲线判定 Agent 质量。

Day19 的 200 个合成 Run 中，基线只知道成功率从 95% 到 70%，原因为 unknown；当失败按 domain 分类后，系统定位 Tool 为首要原因：20/30，占 66.67%。

## 7. Alert 和 SLO 草案

| SLI/SLO | 当前草案 | 说明 |
|---|---|---|
| API 可采集性 | target 持续 2 分钟 down 告警 | 这是监控可见性，不等于业务成功 |
| Agent 自动完成率 | 10 分钟样本≥10 且 completed <90% 持续 5 分钟 | 人工升级可能是正确安全结果，因此它是自动化 SLO，不是正确性 SLO |
| Tool 失败率 | 10 分钟 >10% 持续 5 分钟 | 后续可按工具等级设不同阈值 |
| Model 失败率 | 10 分钟 >5% 持续 5 分钟 | 与输出质量分开，这里只表示调用/契约失败 |
| API P95 | 10 分钟 P95 >1s 持续 10 分钟 | 生产阈值需压测和用户目标校准 |
| 未授权退款 | 0 次 | 安全目标，最终要由审计事件+评估验证，不只靠时序指标 |

## 8. 失败、恢复与当前边界

- 业务执行不应被监控故障阻断；本地 Counter 记录不发网络，Prometheus 暂时不可用时 API 仍可运行。
- Prometheus 保存时间序列，Grafana 只查询和展示；重建 Grafana 不会丢失 Prometheus 数据，Dashboard 可从 JSON 重建。
- 当前 API 的 `/metrics` 是内部运维端点，演示环境直接暴露。生产必须由内网/服务发现/防火墙限制，不向公网客户开放。
- Celery Worker 会在自己的进程内记录 Worker/Agent/Tool 指标，但当前 Prometheus 只拉取 API 进程。生产需为 Worker 运行独立 exporter，或正确配置 Prometheus Python multiprocess 模式；不能宣称当前面板已聚合全部 Worker 进程。
- 结构化 Log 当前输出到 stdout，还未引入 Loki/ELK。因此 Grafana 已有 Metrics Dashboard，但还不是集中日志检索系统。

## 9. 主要官方依据

- [Prometheus instrumentation practices](https://prometheus.io/docs/practices/instrumentation/)
- [Prometheus metric and label naming](https://prometheus.io/docs/practices/naming/)
- [Prometheus Python ASGI export](https://prometheus.github.io/client_python/exporting/http/asgi/)
- [Prometheus alerting rules](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)
- [Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/)
