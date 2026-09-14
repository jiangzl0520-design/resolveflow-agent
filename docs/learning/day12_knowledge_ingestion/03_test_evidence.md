# Day 12 测试、评估与运行证据

## 1. 验收目标

Day12 的硬性验收是：任何知识 chunk 都能追溯到租户、逻辑来源、原始 URI、明确版本、有效区间、父文档和原始行范围；重复投递、同版本内容漂移、增量升级和失败重跑都有自动化证据。

## 2. 专项自动化测试

测试文件：

- `tests/test_knowledge_markdown_chunking.py`
- `tests/test_knowledge_ingestion_service.py`
- `tests/test_sqlalchemy_knowledge_repository.py`
- `tests/test_day12_evaluation.py`
- `tests/integration/test_postgres.py::test_real_postgres_persists_traceable_knowledge_chunks`

覆盖范围：

1. Markdown 标题层级、段落、列表和原始行号；
2. fenced code 中的标题符号不污染文档结构；
3. 超长中文段落按句子边界拆分且不超过 max chars；
4. 空文档安全失败；
5. 每个 Document/Chunk 的 tenant、source、version、有效期和行范围完整；
6. 相同幂等键重放不重复创建；
7. 不同幂等键投递相同 source/version/content 时跳过；
8. CRLF/LF 规范化后能够去重；
9. 同 source/version 不同内容被拒绝；
10. 新版本成为 current，旧版本与旧 chunks 保留；
11. 发布故障后无半成品，同一 run 第二次恢复且 attempts=2；
12. 幂等键冲突与跨租户读取被拒绝；
13. Alembic 迁移后的 SQLite Repository round trip；
14. 真实 PostgreSQL 的 JSONB heading path、外键、迁移、持久化重读和租户隔离。

专项单元与 SQLite 结果：

```text
17 passed in 0.41s
```

真实 PostgreSQL 全套集成结果：

```text
8 passed
```

连接真实 `resolveflow_test` PostgreSQL 后的全量回归：

```text
170/170 passed
```

其他工程检查：

```text
compileall: passed
pip check: No broken requirements found.
alembic heads: 20260804_0007 (head)
alembic check: No new upgrade operations detected.
```

## 3. 真实 PostgreSQL 发现并修复的问题

第一次运行真实 PostgreSQL 时，7 个旧集成案例通过，新知识入库案例失败：

```text
ForeignKeyViolation:
knowledge_chunks.document_id 不存在于 knowledge_documents
```

原因不是缺少事务，而是 DocumentRecord 与 ChunkRecord 没有 ORM relationship，SQLAlchemy flush 时没有可靠地先插父文档再插子分块。修复是在 `session.add(document)` 后显式 `session.flush()`，再加入 chunks；最后仍只执行一次 commit。

修复后 PostgreSQL 8/8 全部通过。这个案例证明真实数据库测试能发现 SQLite 或纯内存测试无法证明的外键执行顺序。

## 4. 真实中文政策入库演示

原文：`data/knowledge_sources/cn_delivery_dispute_v1.md`

首次执行：

```text
status: completed
document_id: e3e2a383-0199-41be-97b3-45a4d7a3fa5b
content_hash: b1be299353ec6ca5f118059f28555eec9687680463a1dd70d24e153a69c52a32
chunk_count: 5
deduplicated: false
request_id: day12-demo-request
trace_id: day12-demo-trace
```

使用不同 idempotency key 重投同一 source/version/content：

```text
status: skipped
document_id: e3e2a383-0199-41be-97b3-45a4d7a3fa5b
chunk_count: 5
deduplicated: true
```

两次返回同一个 document_id，证明第二次投递没有生成重复文档和重复分块。

## 5. 60 例确定性评估

数据集：`evaluation/datasets/day12_knowledge_ingestion_v1.json`

运行器：`evaluation/run_day12_knowledge_ingestion.py`

原始报告：`evaluation/reports/day12_knowledge_ingestion_v1.json`

| 场景 | 数量 | 实测结果 |
|---|---:|---:|
| 可追溯入库 | 20 文档 / 40 chunks | 40/40 血缘完整 |
| 重复投递 | 15 | 15/15 避免重复文档 |
| 同版本内容漂移 | 10 | 10/10 阻止静默覆盖 |
| 增量版本更新 | 10 | 10/10 current 更新且旧版本保留 |
| 发布失败后重跑 | 5 | 5/5 无半成品并在同一 run 恢复 |

量化结果：

```text
traceable_chunk_percent: 100.0
duplicate_documents_avoided: 15
silent_same_version_overwrites_prevented: 10
failed_runs_recovered_without_partial_documents: 5
paid_api_calls: 0
```

这些数字证明的是确定性入库完整性，不证明检索相关性、最终回答正确率或生产业务收益。Recall@k、MRR 和混合检索对比属于 Day13。

## 6. 复现命令

启动依赖：

```powershell
docker compose up -d postgres redis
```

Day12 专项：

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  tests\test_knowledge_markdown_chunking.py `
  tests\test_knowledge_ingestion_service.py `
  tests\test_sqlalchemy_knowledge_repository.py `
  tests\test_day12_evaluation.py
```

真实 PostgreSQL：

```powershell
$env:TEST_DATABASE_URL = `
  'postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test'
.\.venv\Scripts\python.exe -m pytest -q -W error `
  -m postgres tests\integration\test_postgres.py
```

重新生成评估报告：

```powershell
.\.venv\Scripts\python.exe `
  -m evaluation.run_day12_knowledge_ingestion `
  --output evaluation\reports\day12_knowledge_ingestion_v1.json
```

## 7. 证据边界

- 全部政策案例和注入故障均为合成、确定性数据；
- 中文政策确实写入本机 PostgreSQL，但不代表生产数据已接入；
- 没有调用付费模型，也没有宣称模型质量提升；
- 真实 PostgreSQL 套件证明迁移、外键、JSONB、持久化和租户查询，不证明生产吞吐与 P95/P99；
- 当前没有 embedding 与检索，所以不能用这份报告宣称 RAG 召回质量。
