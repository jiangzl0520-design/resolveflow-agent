# Day18 测试与量化证据

## 1. Day18 专项测试

文件：

- `tests/test_observability_tracing.py`：9个测试；
- `tests/test_day18_evaluation.py`：1个测试。

覆盖正常、失败和边界路径：

- HTTP 提取上游 W3C Context，Server Span 与上游保持同一 Trace；
- Dispatcher 注入 `traceparent`，Consumer 成为 Producer 子 Span；
- 模型版本、Token、重试被记录，Prompt/邮箱/密码不进入属性；
- Tool selection failure 被准确归因，参数正文不泄露；
- AgentRunner 记录状态转换，Tool Span 是 Agent 子 Span；
- LangGraph plan/execute 节点位于 Durable Run Span 下；
- SQLAlchemy 成功与失败 Span 只记录 SQL 动词；
- Retrieval outage 与 Policy deny 保留不同 domain；
- RAG Prompt Injection 被标记为 security，原问题不进入 Span；
- 评估脚本重复执行结果完全一致。

专项命令：

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/test_day18_evaluation.py `
  tests/test_observability_tracing.py `
  -q --basetemp=.pytest_tmp_day18_eval2
```

真实结果：10/10通过。

## 2. 受影响模块回归

受影响范围包含 HTTP、Celery、ModelGateway、ToolExecutor、AgentRunner、LangGraph、Context、Retrieval、Grounded Answer、Refund Policy 和配置，共收集101个测试。

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/test_day18_evaluation.py `
  tests/test_observability_tracing.py `
  tests/test_health.py `
  tests/test_celery_worker.py `
  tests/test_model_gateway.py `
  tests/test_tool_registry.py `
  tests/test_agent_loop.py `
  tests/test_agent_workflow.py `
  tests/test_context_builder.py `
  tests/test_context_security.py `
  tests/test_knowledge_retrieval.py `
  tests/test_knowledge_answering.py `
  tests/test_refund_policy.py `
  tests/test_refund_approval_workflow.py `
  tests/test_llm_config.py `
  -q --basetemp=.pytest_tmp_day18_targeted2
```

真实结果：99个通过，2个需要 PostgreSQL 的测试在这条定向命令中跳过；没有失败。

## 3. 固定量化评估

数据集：`evaluation/datasets/day18_trace_diagnosis_v1.json`

脚本：`evaluation/run_day18_trace_diagnosis.py`

原始报告：`evaluation/reports/day18_trace_diagnosis_v1_report.json`

数据集版本 `day18-trace-diagnosis-v1`，明确标记 `synthetic: true`，共70例：

- model、retrieval、tool、database、policy、security 各10例失败；
- 10例正常成功；
- 60例失败载荷包含固定邮箱和密码，用于检查属性过滤；
- 不调用付费模型，不依赖外部 Trace Backend。

对照定义：

- Baseline：只有业务关联 ID 和根节点通用错误，失败 domain 为 unknown；
- Final：OpenTelemetry component child spans + 稳定 failure domain/error code + 最深失败 Span 归因。

运行命令：

```powershell
.\.venv\Scripts\python.exe `
  evaluation/run_day18_trace_diagnosis.py `
  --output evaluation/reports/day18_trace_diagnosis_v1_report.json
```

真实结果：

| 指标 | Baseline | Day18 Final |
|---|---:|---:|
| 根因定位正确率 | 14.29% | 100.00% |
| 正确案例 | 10/70 | 70/70 |
| 未分类失败 | 60 | 0 |
| 敏感载荷暴露 | 60 | 0 |
| 正常案例误报失败 | 0 | 0 |

实测差值：根因定位提高85.71个百分点，新增定位60个失败，阻止60个合成敏感载荷进入 Span。

这只是固定合成数据，不是生产收益，也不代表所有未知异常都能100%归因。Baseline 是本项目 Day18 之前的“关联 ID + 通用终止错误”能力，不代表商业 APM 产品。

## 4. 全量回归与真实基础设施

启动项目 PostgreSQL/Redis 后：

```powershell
$env:TEST_DATABASE_URL = `
  "postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test"
.\.venv\Scripts\python.exe -m pytest `
  -q --basetemp=.pytest_tmp_day18_full
```

真实结果：241/241通过。全量包含 PostgreSQL Repository、迁移、pgvector Retrieval、Grounded Answer、Memory、Context Trace、Celery、MCP、AgentRunner 和 LangGraph checkpoint。

环境验证：

- PostgreSQL：healthy；
- Redis：healthy；
- `python -m compileall -q app evaluation tests`：通过；
- `python -m pip check`：`No broken requirements found.`；
- Alembic heads/current：均为 `20260805_0011 (head)`。

Day18 没有增加业务持久化字段，因此没有新增 Alembic migration。Trace 由 OpenTelemetry Exporter 输出；原有 ModelCall、RetrievalRun、ContextRun、Agent State 和业务审计继续负责持久化证据。

## 5. 新增运行依赖

- `opentelemetry-api==1.44.0`
- `opentelemetry-sdk==1.44.0`
- `opentelemetry-exporter-otlp-proto-http==1.44.0`

没有引入框架自动埋点包；Day18 手工控制业务 Span 和敏感字段，SQLAlchemy 使用项目内事件钩子。未来若改用自动 instrumentation，仍必须验证是否记录 HTTP header、SQL statement 或 GenAI messages。

## 6. 尚未覆盖

- 外部 Collector/Tempo/Jaeger 的进程级演示；
- Dashboard、Logs、Metrics、SLO 和告警；
- 采样与 OTLP 网络开销；
- 跨数小时 checkpoint 恢复时继续原始完整 Trace Context。

这些边界分别进入 Day19 和后续性能阶段，不在 Day18 报告中伪装完成。
