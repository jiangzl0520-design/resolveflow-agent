# Day 13 测试、评估与运行证据

## 1. 验收目标

1. 固定维度 Embedding 契约能拒绝错误输出；
2. pgvector 语义检索和 PostgreSQL 关键词检索能够真实运行；
3. tenant、ACL 和有效期在 Top K 前过滤；
4. RRF 能融合两个评分体系；
5. Embedding 故障可受控降级但不伪装空结果；
6. 每次结果都能通过 Run/Hit Trace 重建排序过程；
7. Recall@5、MRR 和安全过滤有可复现数据。

## 2. 专项测试覆盖

测试文件：

- `tests/test_knowledge_embeddings.py`
- `tests/test_knowledge_retrieval.py`
- `tests/test_day13_evaluation.py`
- `tests/integration/test_knowledge_retrieval_postgres.py`

覆盖内容：

- OpenAI 请求明确携带 model、dimensions 和 float 编码；
- Embedding 数量或维度错误被契约拒绝；
- 查询改写删除一次性订单号并规范领域别名；
- 双通道命中的 chunk 通过 RRF 排在单通道候选前；
- 跨租户、角色不可见、已失效和未生效知识不会参与结果；
- customer 没有内部政策读取权限；
- hybrid 在 Embedding 故障时降级为 keyword 并记录原因；
- 轨迹存储失败时不返回不可审计结果；
- 索引服务把已入库 chunk 的 embedding 原子写入；
- 真实 PostgreSQL 安装 vector 扩展、执行 cosine 查询并持久化 Run/Hit；
- PostgreSQL 关键词 SQL 在数据库内执行 tenant/ACL/时间过滤。

## 3. 真实 PostgreSQL 证据

Day13 将 Compose 数据库镜像切换为：

```text
pgvector/pgvector:0.8.2-pg18-trixie
```

保留原数据卷后执行 revision：

```text
20260804_0007 → 20260804_0008
```

迁移成功创建 `vector` 与 `pg_trgm` 扩展、256 维 embedding 列、HNSW/FTS/trigram 索引和 retrieval trace 表。真实 PostgreSQL 专项结果：

```text
2 passed
```

其中测试直接查询 `pg_extension` 确认 vector 已安装，并重读 `knowledge_retrieval_runs` 与 `knowledge_retrieval_hits`，不是只断言 Service 的内存返回值。

## 4. 60 例相关性评估

数据集：`evaluation/datasets/day13_hybrid_retrieval_v1.json`

运行器：`evaluation/run_day13_hybrid_retrieval.py`

原始报告：`evaluation/reports/day13_hybrid_retrieval_v1.json`

数据把 60 个查询平均分为：

- 20 个只提供精确政策码，关键词通道应命中；
- 20 个只提供语义改写，语义通道应命中；
- 20 个同时提供两类证据，两个通道都应命中。

| 模式 | Recall@5 | MRR | 找到正确政策 |
|---|---:|---:|---:|
| Semantic | 66.67% | 0.6667 | 40/60 |
| Keyword | 66.67% | 0.6667 | 40/60 |
| Hybrid RRF | 100.00% | 1.0000 | 60/60 |

在这组受控数据中，混合检索相对最佳单通道提升 `33.33` 个百分点。这个设计故意隔离通道能力，证明融合逻辑能取两者互补结果，不证明生产 Embedding 模型有相同提升。

## 5. 90 次安全过滤检查

评估另外生成 30 个不应被当前 Agent 看见的 chunk：

- 10 个属于其他 tenant；
- 10 个只允许 supervisor；
- 5 个已经失效；
- 5 个尚未生效。

分别用 semantic、keyword 和 hybrid 三种模式查询，共 90 次：

```text
security_filter_checks: 90
unauthorized_retrieval_hits: 0
blocked_percent: 100.0
```

这是合成数据的回归证据，不等同于完整安全审计或渗透测试。

## 6. 全量工程验证

全量测试连接专用 `resolveflow_test`，同时执行内存、SQLite 和真实 PostgreSQL 案例：

```text
182 passed in 27.93s
```

其他工程检查：

```text
compileall: passed
pip check: No broken requirements found.
alembic heads: 20260804_0008 (head)
alembic check: No new upgrade operations detected.
```

复现命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -W error
$env:TEST_DATABASE_URL = 'postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test'
.\.venv\Scripts\python.exe -m pytest -W error
.\.venv\Scripts\python.exe -m alembic check
.\.venv\Scripts\python.exe -m compileall -q app evaluation tests
.\.venv\Scripts\python.exe -m pip check
```

第一次全量回归发现 SQLite 无法反射 PostgreSQL 专属的 FTS 表达式索引。修复方式是让 revision 0008 继续拥有 HNSW/FTS/trigram 索引，并从可移植 ORM autogenerate 对比中明确排除；之后 SQLite migration sync 和真实 PostgreSQL `alembic check` 都必须通过。

## 7. 复现评估

```powershell
.\.venv\Scripts\python.exe `
  -m evaluation.run_day13_hybrid_retrieval `
  --output evaluation\reports\day13_hybrid_retrieval_v1.json
```

## 8. 证据边界

- 评估语料、查询和向量都是确定性合成夹具，付费 API 调用为 0；
- Fixture Embedding 只证明检索编排，不代表 `text-embedding-3-small` 的生产质量；
- 真实 PostgreSQL 测试证明 pgvector SQL、扩展、过滤与持久化，不证明大规模数据下的 P95/P99；
- 当前结果是候选召回，不是最终答案正确率；
- 生产上线前必须从真实售后流量抽样标注，重新测 Recall、MRR、延迟、成本和越权率。
