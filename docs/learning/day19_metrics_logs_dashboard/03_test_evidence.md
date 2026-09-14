# Day19 测试与运行证据

## 1. 专项自动化测试

新增：

- `tests/test_observability_metrics.py`：5 个；
- `tests/test_day19_evaluation.py`：1 个。

覆盖：

- 正常：`/metrics` 输出 HTTP Metric，评估定位 Tool 根因；
- 失败：Tool 失败 label 类型化，JSON Log 保留错误码但脱敏邮箱/Prompt；
- 边界：50 个动态 Tool/version/error 全部折叠为 `other`，空时间窗不除零；
- 分层：Policy denied 进业务 Metric，不产生 Agent 技术失败 sample；
- 可重复：两次 Day19 评估结果完全相同。

定向回归命令：

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/test_observability_metrics.py `
  tests/test_day19_evaluation.py `
  tests/test_observability_tracing.py `
  tests/test_knowledge_retrieval.py `
  tests/test_knowledge_answering.py `
  tests/test_celery_worker.py `
  tests/test_refund_policy.py `
  --basetemp=.pytest_day19_features3 -q
```

真实结果：46 个通过，0 失败。

## 2. 全量回归

```powershell
.\.venv\Scripts\python.exe -m pytest `
  --basetemp=.pytest_day19_full2 -q
```

真实结果：退出码 0；收集 247 个测试，232 个通过，15 个需要额外 PostgreSQL 环境的测试跳过，0 失败。首次 180 秒命令在测试进度 100% 后、清理阶段超时，不作为通过证据；上述结果来自第二次 360 秒限制的正式运行。

## 3. 监控配置与真实容器验证

Compose 配置：

```powershell
docker compose config --quiet
```

结果：退出码 0。

Prometheus 和告警规则：

```powershell
docker compose run --rm --entrypoint promtool prometheus `
  check config /etc/prometheus/prometheus.yml
```

结果：Prometheus 配置 SUCCESS，1 个 rule file，5 条 rule 全部 SUCCESS。

实际启动后的验证：

- `http://127.0.0.1:8000/health`：`status=ok`；
- `http://127.0.0.1:8000/metrics`：HTTP 200；
- Prometheus instant query `up{job="resolveflow-api"}`：值为 1；
- Prometheus 已拉取 `resolveflow_http_requests_total{route="/health"}` 和 `/metrics` 序列；
- Prometheus `/api/v1/rules`：5 条规则均 `health=ok`；
- Grafana `/api/health`：`database=ok`，版本 13.1.0；
- Grafana Search API：已加载 UID `resolveflow-agent-ops`、标题 `ResolveFlow Agent Operations`。

运行时版本：Prometheus 3.13.0、Grafana 13.1.0、prometheus-client 0.26.0。

## 4. 固定量化评估

- 数据集：`evaluation/datasets/day19_incident_diagnosis_v1.json`；
- 脚本：`evaluation/run_day19_observability.py`；
- 原始报告：`evaluation/reports/day19_incident_diagnosis_v1_report.json`；
- 数据类型：`synthetic_aggregate_windows`，不是生产数据。

```powershell
.\.venv\Scripts\python.exe evaluation\run_day19_observability.py
```

| 指标 | 只有总成功率的基线 | Day19 failure-domain Metrics |
|---|---:|---:|
| 样本量 | 200 | 200 |
| 基准窗口成功率 | 95% | 95% |
| 故障窗口成功率 | 70% | 70% |
| 成功率变化 | -25 个百分点 | -25 个百分点 |
| 首要根因 | unknown | tool |
| 根因正确 | 否 | 是 |
| Tool 失败归因 | 无法统计 | 20/30（66.67%） |

结论：基线能报警但不能解释原因；Day19 的低基数失败分类能在同一固定数据上定位 Tool 是主要贡献者。该结果只证明指标契约的归因能力，不代表生产故障都能自动正确定位。

## 5. 当前运行状态

Day19 验收时：

- ResolveFlow API 运行在 `127.0.0.1:8000`；
- Prometheus 运行在 `127.0.0.1:9090`；
- Grafana 运行在 `127.0.0.1:3000`；
- Dashboard URL：`http://127.0.0.1:3000/d/resolveflow-agent-ops/resolveflow-agent-operations`。

Grafana 开发环境默认账号为 `admin`，密码来自 `GRAFANA_ADMIN_PASSWORD`；Compose 中的默认密码只用于本地开发，生产必须使用 Secret Manager 覆盖。
