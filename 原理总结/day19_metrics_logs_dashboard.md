# Day19：Metrics、Logs、Dashboard 与故障归因

## 业务问题

Day18 Trace 能说明一次 Agent Run 怎样失败，但不能低成本回答“今天成功率为什么下降”。Day19 用全量聚合 Metric 发现趋势和失败类别，再用带 request/trace ID 的 Log 和 Trace 下钻单次运行。

## 完整操作流程

1. HTTP、Worker、Agent、LLM、Retrieval、RAG、Tool、DB 和 Policy 在结果已定型的代码边界埋点。
2. Counter 累计次数，Gauge 表示当前请求，Histogram 累计延迟 bucket；不改变业务实体。
3. label 只保留 route template、status、failure domain、稳定 error code 等低基数维度；ID、Prompt、客户正文不进 Metric。
4. API `/metrics` 把进程内 sample 序列化，Prometheus 每 5 秒拉取并存成时间序列。
5. Grafana 自动加载 datasource 和 Dashboard，分别展示技术健康与业务结果。Policy denied 是业务决策，不是系统失败。
6. Prometheus 每 15 秒评估 target down、Agent 成功率、Tool/Model 失败率和 API P95 告警。
7. 成功率下降时，先看 failure domain，再看 Tool error kind/code，最后通过 Log 的 trace ID 查 Day18 完整 Trace。
8. 修复后同时验证告警恢复和固定数据集回归；Dashboard 变好不能单独证明 Agent 正确。

## 状态、失败与恢复

- Prometheus 故障不阻断 Agent 业务流程；它会形成 target-down 告警和数据空窗。
- Grafana 是查询/展示层，Dashboard 由 JSON 恢复；Prometheus 才保存序列。
- JSON Log 通过 ContextVar 自动关联 request/trace，离开请求时 reset，避免串号；敏感/未允许字段脱敏。
- 当前 Prometheus 只拉取 API 进程；Celery Worker 指标已埋点，但生产仍需独立 exporter 或 multiprocess 聚合。当前日志是 stdout JSON，尚未引入 Loki/ELK。

## 量化证据

200 个固定合成 Run 的基准/故障窗口从 95% 降到 70%。只有总成功率的基线能发现下降但根因为 unknown；Day19 保留低基数 failure domain 后定位 Tool 占 20/30 次失败（66.67%）。这是合成归因评估，不是生产收益。

真实容器验证：Prometheus target `up=1`，5 条 rule `health=ok`，Grafana 已加载 UID `resolveflow-agent-ops` Dashboard。全量收集 247 个测试，232 通过、15 跳过、0 失败。

## 与前后 Day 的关系

- Day18 提供单次 Trace 和根因层级；Day19 提供时间窗口、比率、分位数和告警。
- Day20 将把这些在线信号与版本化 Golden Dataset/Eval Harness 连接，用离线回归判断修复是否真正改善 Agent 质量。
