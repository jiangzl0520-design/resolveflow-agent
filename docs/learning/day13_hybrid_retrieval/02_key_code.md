# Day 13 关键代码

## 1. 文件与职责

| 文件 | 职责 |
|---|---|
| `app/domain/retrieval.py` | 查询、改写、候选、结果、Run 和 Hit 的领域契约 |
| `app/knowledge/embeddings.py` | Provider-neutral Embedding 协议与 OpenAI 适配器 |
| `app/knowledge/query_rewrite.py` | 确定性售后查询规范化，不产生安全条件 |
| `app/services/knowledge_indexing_service.py` | 批量生成并原子发布 chunk embeddings |
| `app/repositories/knowledge_search_repository.py` | 检索存储协议与内存测试适配器 |
| `app/repositories/sqlalchemy_knowledge_search_repository.py` | pgvector、FTS、trigram、ACL SQL 与轨迹持久化 |
| `app/services/knowledge_search_service.py` | 权限、双通道调用、降级、RRF、审计编排 |
| `app/knowledge/index_document.py` | 真实 Embedding 索引 CLI |
| `app/knowledge/search_policy.py` | 关键词/语义/混合搜索 CLI |
| `migrations/versions/20260804_0008_add_hybrid_retrieval.py` | 扩展、列、索引和轨迹表 |

## 2. 固定维度是一份跨层契约

```python
KNOWLEDGE_EMBEDDING_DIMENSIONS = 256

response = client.embeddings.create(
    model=self._model,
    input=list(texts),
    dimensions=self.dimensions,
    encoding_format="float",
)
```

解释：模型请求明确指定 256 维，返回后还检查数量、维度和所有数字是否有限。数据库列、索引服务和查询服务引用同一个常量，避免模型换了但存储契约没换。真实运行没有 `OPENAI_API_KEY` 时直接拒绝创建 Provider；Fixture 只出现在测试和离线评估。

## 3. 索引发布带内容哈希栅栏

```python
updates = tuple(
    ChunkEmbeddingUpdate(
        chunk_id=chunk.id,
        content_hash=chunk.content_hash,
        embedding=vector,
        embedding_model=self._embedding_provider.model,
        embedded_at=now,
    )
    for chunk, vector in zip(chunks, vectors, strict=True)
)
self._embedding_repository.store_embeddings(tenant_id, updates)
```

解释：先把整批向量生成和校验完，再交给 Repository 一个事务写入。Repository 的 `UPDATE` 同时匹配 tenant、chunk id 和 content hash；索引期间正文被替换时，旧任务无法把过期向量写到新内容上。

## 4. 安全条件进入候选 SQL

```python
return (
    KnowledgeChunkRecord.tenant_id == tenant_id,
    KnowledgeChunkRecord.allowed_roles.op("?")(role.value),
    KnowledgeChunkRecord.effective_from <= as_of,
    or_(
        KnowledgeChunkRecord.effective_to.is_(None),
        KnowledgeChunkRecord.effective_to > as_of,
    ),
)
```

解释：实际实现会把 Actor 的多个角色组成 OR，但 tenant 和有效期始终是 AND。所有条件在 `ORDER BY ... LIMIT` 前执行，禁止先召回全库再在 Python 中过滤。

## 5. 语义通道与关键词通道

```python
distance = cast(
    KnowledgeChunkRecord.embedding,
    VECTOR(KNOWLEDGE_EMBEDDING_DIMENSIONS),
).cosine_distance(list(embedding))
```

解释：语义通道按 cosine distance 排序，同时要求 embedding model 相同且距离不超过阈值。HNSW 使用 `vector_cosine_ops`，使查询距离和索引操作符一致。

```python
search_vector = func.to_tsvector("simple", KnowledgeChunkRecord.content)
search_query = func.websearch_to_tsquery("simple", query_text)
```

解释：关键词通道以 PostgreSQL FTS 为基础，并叠加精确 `ILIKE` 与 `pg_trgm similarity`。三种分数只在关键词通道内部排序，不与向量距离直接相加。

## 6. RRF 只融合名次

```python
scores[key] = scores.get(key, 0.0) + channel_weight / (
    rrf_k + rank
)
```

解释：每个通道按自己的方法排好名次后，RRF 给排名靠前的候选更多分。同一 chunk 被两个通道找到会累加两次。这样避免把 cosine distance 和 FTS score 这两种不同量纲硬加在一起。

## 7. 降级不是静默吞错

```python
if not semantic_succeeded and not keyword_succeeded:
    raise channel_errors[0]
if query.mode is RetrievalMode.SEMANTIC and not semantic_succeeded:
    raise channel_errors[0]
```

解释：只有 hybrid 仍有另一个成功通道时才允许返回降级结果，并把错误码写入 `degraded_reason`。semantic-only 失败必须暴露真实故障，不能返回空列表让调用方误以为“政策不存在”。

## 8. 检索轨迹先落库再返回

```python
self._repository.record_retrieval(run, traces)
return KnowledgeSearchResult(
    run=run,
    rewritten_query=rewritten,
    hits=tuple(hits),
)
```

解释：Run 记录输入与过程元数据，Hit Trace 记录每个候选在两个通道和融合后的名次。记录失败时服务返回明确的 recording error，而不是把无法审计的结果当正常结果交给 Agent。

## 9. PostgreSQL 专用索引的迁移边界

HNSW、`to_tsvector` 表达式 GIN 和 `gin_trgm_ops` 只由 Alembic revision 0008 在 PostgreSQL 创建。它们不放进跨数据库 ORM 元数据，因为 SQLite 无法反射这些表达式和操作符类。`migrations/env.py` 明确把这三个名字从 autogenerate 对比排除，真实索引仍留在 PostgreSQL 中。

这不是忽略 schema 漂移，而是明确所有权：普通表、列和可移植索引由 ORM 元数据对比；数据库专属表达式索引由手写迁移和 PostgreSQL 集成测试负责。
