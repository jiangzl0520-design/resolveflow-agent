# Day 14 测试、评估与运行证据

## 1. 验收目标

1. 候选证据能够去重和结构化重排；
2. 模型不能漏评估、增加或重复证据 ID；
3. 已过期政策不会进入答案 Prompt；
4. 冲突证据禁止生成确定结论，且不能被 `max_evidence` 截断隐藏；
5. 未知引用、虚构引文和 claim 内伪造标记全部失败关闭；
6. Answer Run、引用、检索和模型调用能够持久化串联；
7. 引用精确率和安全处理率可自动计算，并与明确基线比较。

## 2. 专项自动化测试

测试文件：

- `tests/test_knowledge_answering.py`
- `tests/test_day14_evaluation.py`
- `tests/integration/test_knowledge_answering_postgres.py`
- `tests/integration/test_migrations.py`

覆盖的正常、失败和边界路径：

- 相同正文去重，相关证据进入答案模型，无关证据被排除；
- 无候选和只有过期候选时不调用 LLM，返回证据不足；
- 同 source_key 的同时有效不同版本进入冲突终态；
- `max_evidence=1` 仍不能隐藏排在第二位的冲突版本；
- 重排漏评估一个 ID 时整个 run 失败；
- K9 未知 ID、不存在的 exact quote、claim 内 `[K9]` 被后端拒绝；
- Answer Run 记录失败时不返回不可审计结果；
- Alembic upgrade/downgrade 和 ORM schema sync；
- CLI 参数和生产组合根可加载。

Day14 核心与评估专项结果：

```text
11 passed
```

## 3. 真实 PostgreSQL 端到端证据

revision `20260804_0009` 已实际应用到本地 PostgreSQL，创建：

- `knowledge_answer_runs`；
- `knowledge_answer_citations`；
- Answer Run 的 tenant/trace 与 tenant/status 索引；
- 到 Retrieval Run、Model Call、Chunk 的外键。

真实集成测试执行以下链路：

```text
真实 PostgreSQL 入库政策
  → 真实 pgvector 256 维索引和语义查询
  → Fake Provider 经过真实 ModelGateway 产生重排/答案结构化输出
  → 后端精确验引
  → SQLAlchemy Answer Repository 完成状态和引用事务
  → 重新查询 5 类数据库记录验证关联
```

它确认 1 条 Answer Run、1 条 Citation、1 条 Retrieval Run 和 2 条 Model Call 全部落库，两个模型操作分别是 `knowledge_evidence_rerank` 与 `knowledge_grounded_answer`，并共同指向 Answer Run。专项结果：

```text
1 passed
```

模型响应是确定性夹具，因此该测试证明真实数据库、pgvector、外键、事务和可观测链路，不证明外部模型质量。

## 4. 60 例基线对比评估

数据集：`evaluation/datasets/day14_grounded_answer_v1.json`

运行器：`evaluation/run_day14_grounded_answer.py`

原始逐例报告：`evaluation/reports/day14_grounded_answer_v1.json`

案例构成：

| 场景 | 数量 | 正确行为 |
|---|---:|---|
| 可回答 | 20 | 输出带精确引用的结论 |
| 证据不足 | 10 | 拒绝确定结论 |
| 当前证据冲突 | 10 | 返回冲突并停止答案生成 |
| 只有过期政策 | 10 | 不引用并返回证据不足 |
| 模型伪造引用 | 10 | 后端阻断并记录失败 |

基线定义为“结构化输出格式正确就直接接受”，不检查有效期、冲突、ID 存在性和精确引文。它不是旧生产系统数据，而是同一合成数据上的确定性对照算法。

| 指标 | 仅结构化输出基线 | Day14 验证管线 | 变化 |
|---|---:|---:|---:|
| 正确处理率 | 33.33% | 100.00% | +66.67 点 |
| 答案引用精确率 | 33.33% | 100.00% | +66.67 点 |
| 过期政策引用数 | 10 | 0 | -10 |
| 伪造引用阻断数 | 0/10 | 10/10 | +10 |
| Answer Run 留痕率 | 未实现 | 100.00% | 新增 |

验证管线共记录 70 次模型调用：20 个可回答案例各 2 次、10 个冲突案例各 1 次、10 个伪造案例各 2 次；无证据和过期案例在 LLM 调用前停止。所有调用均为本地确定性 Fake Provider，付费 API 调用为 0。

“答案引用精确率”只统计 `answered` 结果中的支持引用；冲突响应附带的两个来源是冲突溯源，不是支持答案 claim 的引用，因此不进入答案引用精确率分母。

## 5. 全量工程验证

连接专用 `resolveflow_test` 后运行全部 Day1–Day14 测试：

```text
194 passed in 31.43s
```

其他检查：

```text
compileall: passed
pip check: No broken requirements found.
alembic heads: 20260804_0009 (head)
alembic current: 20260804_0009 (head)
alembic check: No new upgrade operations detected.
PostgreSQL: healthy
Redis: healthy
```

复现命令：

```powershell
$env:TEST_DATABASE_URL = 'postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test'
.\.venv\Scripts\python.exe -m pytest -W error
.\.venv\Scripts\python.exe -m alembic check
.\.venv\Scripts\python.exe -m compileall -q app evaluation tests
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m evaluation.run_day14_grounded_answer `
  --output evaluation\reports\day14_grounded_answer_v1.json
```

## 6. 本次测试真实发现并修复的问题

第一个问题是冲突检测最初发生在 `max_evidence` 截断之后。这样限制为 1 条证据时，第二个相关冲突版本可能被隐藏。修复后先在相关证据全集检查冲突，再截断答案上下文，并增加专门回归案例。

第二个问题来自全量回归而不是 Day14 主流程：MCP 2.0 的 HTTP 客户端继承 Windows 系统代理，本机服务请求被代理返回 502。原生 socket 请求证明 MCP Server 实际为 200。客户端改为 `trust_env=False` 后，Day11 MCP 独立进程和评估测试 `4/4` 通过，同时避免短期服务 Token 被隐式环境代理接收。

## 7. 证据边界

- 数据集、政策、预期输出和 Provider 都是确定性合成夹具；
- 100% 是当前 60 例后端门禁覆盖结果，不是线上答案正确率；
- 真实 PostgreSQL 测试没有调用付费 OpenAI API；
- 当前 exact quote 检查不能单独证明 claim 的完整语义蕴含；
- 生产价值仍需真实售后流量、人工标签、真实模型运行、P50/P95 延迟和 Token/成本数据。
