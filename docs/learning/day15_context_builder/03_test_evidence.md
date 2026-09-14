# Day 15 测试与运行证据

## 1. 自动化测试覆盖

### 正常路径

- 长上下文在固定预算下保留系统规则、当前目标、最新用户修改和关键证据；
- Planner 首轮只看到订单工具，确认订单后看到仍缺失的物流/政策工具，证据齐全后不再看到读取工具；
- Context Run、Fragment Trace 与 Model Call 通过 `context_build_id` 连接；
- SQL Trace 只保存内容哈希，不保存片段原文；
- `tiktoken` 参与 60 例完整评估和真实 Planner 输入计算。

### 失败路径

- required 上下文超过预算时，先记录 failed run，再抛 `required_context_overflow`；
- Context Trace Repository 写入失败时抛 `context_trace_recording_failed`，模型不被调用；
- 数据库写入使用事务，SQLAlchemy 错误回滚并转换为存储不可用错误；
- 无效配置在输出、推理和安全预留占满上下文窗口时拒绝启动。

### 边界路径

- 同一 semantic key 的旧订单修改标记 `superseded`；
- 与当前订单不一致的成功 Tool Observation 标记 `invalidated`；
- 低相关片段标记 `low_relevance`；
- 未进入当前步骤的工具标记 `not_relevant_to_current_step`；
- 超预算可选片段标记 `token_budget_exceeded`；
- 订单从 10086 改为 10087 后，旧订单/物流证据失效，下一步重新暴露 `order_lookup`；
- SQLite 迁移升级、降级和 ORM metadata 同步检查通过；
- 真实 PostgreSQL 上 Context Build 与 Model Call 外键关联通过。

## 2. 评估设计

数据集：`evaluation/datasets/day15_context_builder_v1.json`

脚本：`evaluation/run_day15_context_builder.py`

原始报告：`evaluation/reports/day15_context_builder_v1_report.json`

数据集明确标记 `synthetic: true`，共 60 例：

- 20 例长 History 干扰；
- 20 例订单号最新修改、旧修改和旧 Observation；
- 20 例不同当前步骤的工具描述选择。

基线是确定性“全部候选拼接，超预算时从最早的非系统片段开始删除”，不做 supersede、主事实失效和动态工具过滤。基线与最终版本使用同一数据、同一 900 Token 输入预算、同一 `tiktoken` 编码和同一渲染元数据。

## 3. 评估真实结果

```text
dataset: day15-context-builder-v1
synthetic cases: 60
paid model calls: 0

baseline:
  critical fragment retention: 66.67%
  stale fragment selections: 40
  dynamic tool precision: 33.33%
  average input tokens: 463.67

context builder:
  critical fragment retention: 100.00%
  stale fragment selections: 0
  dynamic tool precision: 100.00%
  average input tokens: 370.35

measured change:
  critical retention: +33.33 percentage points
  stale selections: -40
  tool precision: +66.67 percentage points
  average input tokens: -20.13%
```

复现命令：

```powershell
.\.venv\Scripts\python.exe evaluation\run_day15_context_builder.py `
  --output evaluation\reports\day15_context_builder_v1_report.json
```

## 4. PostgreSQL 实测

Docker 状态：PostgreSQL 和 Redis 均为 healthy。测试只连接名称以 `_test` 结尾的专用数据库。

```powershell
$env:TEST_DATABASE_URL = `
  'postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test'
.\.venv\Scripts\python.exe -m pytest `
  tests\integration\test_context_engineering_postgres.py -q
```

结果：

```text
1 passed in 2.8s
```

该测试实际执行 Alembic 升级、Context Run/Fragment Trace 事务写入、Fake Provider 结构化模型调用、Model Call 写入和外键查询。Fake Provider 只用于避免付费和结果漂移，PostgreSQL 读写与外键是真实的。

## 5. 全量回归

```powershell
$env:TEST_DATABASE_URL = `
  'postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test'
.\.venv\Scripts\python.exe -m pytest -q `
  --basetemp=.pytest_tmp_day15_full
```

结果：

```text
202 passed in 108.3s
```

全量回归包含 Day1–Day15 的 API、事务、认证、多租户、异步任务、模型网关、工具、Agent Loop、LangGraph、退款审批、MCP、知识入库、混合检索、引用门禁和 Context Builder。

## 6. 评估边界

- 100% 是当前合成数据上的确定性 Context 选择门禁，不是生产任务成功率；
- 平均 Token 降低只对当前基线、数据分布和 900 Token 预算成立；
- 未调用真实 OpenAI 模型，因此没有真实缓存命中率、模型质量、延迟或费用结论；
- Day15 只消费 History/Memory/RAG 候选，不实现长期 Memory 生命周期；
- Prompt injection 深度测试、敏感信息脱敏和知识内容安全门禁仍属于 Day17；
- 生产环境还需要根据真实失败样本调优 priority、relevance threshold 和预算预留。

## 7. 2026-09 仓库加固回归

新增 `tests/test_token_counter.py`，覆盖目标编码可用、未知模型回退编码下载失败、已知模型编码加载失败和中文 UTF-8 字节上界。原本会因 `openaipublic.blob.core.windows.net` TLS/网络故障中断的跨 Agent Run Memory 场景也纳入回归。

```powershell
.\.venv\Scripts\python.exe -m pytest
```

结果：

```text
269 passed, 15 skipped in 25.94s
```

15 项跳过项需要显式配置 PostgreSQL 等集成测试环境；本轮没有把它们误报为通过。另修复 API Memory 测试使用固定到期日期导致随日历失效的问题，过期 Memory 的确定性边界测试仍保留在 `tests/test_long_term_memory.py`。

GitHub 首次在 Linux 新环境中运行时，Starlette 导入 AnyIO 旧别名触发第三方 `DeprecationWarning`，导致严格警告策略在测试收集前退出。`pyproject.toml` 现在统一将警告设为错误，并只按完整消息精确忽略这一条已知上游兼容性警告；CI 与本地都直接执行 `pytest`，防止命令行 `-W error` 覆盖精确豁免。
