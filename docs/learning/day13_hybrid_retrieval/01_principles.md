# Day 13 原理：安全、可观测的混合知识检索

## 1. 今天解决的真实问题

Day12 已经把政策变成可追溯的 Document 和 Chunk，但“数据库里有知识”不等于“Agent 能找到正确知识”。企业售后查询同时存在两种特点：

- `RF-K003`、产品型号、错误码等精确词必须按字面命中；
- “要主管点头”与“需要人工审核”字面不同，但语义相同。

纯关键词检索会漏掉第二类表达，纯向量检索又可能漏掉精确编号。更严重的是，如果先取全库 Top K 再检查租户、角色和有效期，受限知识可能已经参与排序，甚至泄露给 Agent。

Day13 建立的是检索层，不是回答层：它把查询改写、关键词召回、语义召回、安全过滤、融合排序和轨迹记录组成一个确定的后端用例，输出带来源的候选证据，供 Day14 重排和引用。

## 2. 完整操作流程

```text
Day12 Document/Chunk
  → 独立索引任务读取不可变 Chunk
  → OpenAI Embedding 适配器批量生成固定 256 维向量
  → content_hash 再校验
  → 一个事务写入 embedding/model/embedded_at

用户查询 + 可信 Actor + as_of
  → 后端检查 TOOL_POLICY_READ 权限
  → 确定性 Query Rewrite
  → 语义通道：query embedding → pgvector cosine Top N
  → 关键词通道：FTS + 精确包含 + pg_trgm Top N
  → 两个通道都在候选 SQL 内先过滤 tenant/ACL/有效期
  → RRF 按名次融合
  → 截取 Top K
  → 先持久化 Retrieval Run 和 Hit Trace
  → 返回可追溯候选证据
```

入库、索引和检索被拆成三个用例。政策成功发布不依赖外部 Embedding 服务；索引失败可以单独重跑，也不会把一半 chunk 标成已索引。

## 3. Embedding 和向量检索到底做什么

Embedding 把一段文本转换成固定长度的数字向量。表达相近的文本通常方向更接近，所以“主管点头”和“人工审核”即使没有共同词，也可能被语义通道找回。

本项目固定使用 256 维存储契约，生产默认模型是 `text-embedding-3-small`。维度同时出现在模型请求、领域校验、pgvector 列和查询代码中，任何一处不一致都直接失败，避免把不同维度或不同模型的向量混在一个索引里。

pgvector 使用 cosine distance：距离越小越相似。检索只接受距离不超过 `0.45` 的候选，再按距离升序取有限数量。阈值和 Top K 是不同控制：

- 阈值决定候选是否足够相似；
- Top K 决定最多返回多少个候选；
- 只设置 Top K 而没有阈值，会在“完全没有相关文档”时仍强行返回最不差的错误文档。

HNSW 索引用更多存储和建索引成本换取更快的近似最近邻查询。它提升的是检索效率，不自动保证业务相关性；相关性仍需评估数据验证。

## 4. 为什么还需要关键词通道

关键词通道组合三种 PostgreSQL 能力：

1. `to_tsvector('simple', content)` 与 `websearch_to_tsquery` 负责全文匹配；
2. `ILIKE` 提升完整术语、政策编号等精确包含的权重；
3. `pg_trgm` 的 trigram similarity 为中文片段和轻微拼写差异提供补充。

英文全文检索常依赖词干和语言词典；中文没有天然空格，单用 PostgreSQL 内置分词不能覆盖所有场景。因此项目没有假装 FTS 能完美处理中文，而是用精确包含与 trigram 补足，并通过评估决定以后是否接入专门中文搜索引擎。

## 5. Query Rewrite 的职责和安全边界

当前改写器是确定性规则：删除订单号/工单号等一次性资源标识，统一“没收到包裹”“退钱”等领域别名，再提取有限关键词。

它只改善检索表达，不能生成 `tenant_id`、角色或有效期。安全过滤必须来自后端可信 Actor 和服务器时间参数。让模型或用户文本生成安全过滤条件，相当于允许不可信输入决定自己能看到什么。

改写后的正文和原始查询也不直接写日志；轨迹只保存规范化哈希、改写策略和候选数量，降低政策查询中客户信息泄露的风险。

## 6. 安全过滤为什么必须在 Top K 之前

候选 SQL 同时包含：

```text
chunk.tenant_id == actor.tenant_id
allowed_roles 与 actor.roles 至少有一个交集
effective_from <= as_of
effective_to 为空，或 as_of < effective_to
embedding_model == 当前查询模型（语义通道）
```

然后才排序和 `LIMIT`。如果先对全库取 Top K 再过滤：

- 其他租户或主管专属文档会占掉候选名额，导致当前用户明明有可见文档却召回为空；
- 排名、数量和调试信息可能形成侧信道；
- 应用层一处漏过滤就会直接泄露内容。

因此 tenant、ACL 和有效期不是“后处理”，而是检索查询本身的一部分。

## 7. 为什么用 RRF，而不是直接相加分数

向量通道输出 cosine distance，关键词通道输出 FTS/包含/trigram 的组合分数，两者量纲和取值范围完全不同。直接做 `0.5 * vector_score + 0.5 * keyword_score` 没有稳定含义，还会随模型或数据库评分算法变化。

Reciprocal Rank Fusion 只使用每个通道中的名次：

```text
score(document) = Σ channel_weight / (k + rank_in_channel)
```

项目使用 `k=60`。一个片段同时出现在两个通道时会得到两份贡献，通常排在只被单通道发现的候选之前。RRF 的优点是跨评分体系稳定、容易解释；缺点是它只看名次，不理解文档内容，Day14 仍需要重排与证据验证。

## 8. 失败处理与降级

索引阶段：

- Embedding 超时、限流、连接失败和服务端错误使用指数退避做有限重试；
- 鉴权失败、非法请求和输出维度错误不盲目重试；
- 所有 chunk 向量生成并校验成功后才原子写入；
- 写入时再次比较 chunk `content_hash`，内容已变化就拒绝旧向量覆盖新内容。

检索阶段：

- hybrid 的语义通道失败时可以降级为关键词，并记录 `degraded_reason`；
- semantic-only 的 Embedding 失败不能伪装成“没有结果”，而是明确失败；
- 两个通道都失败时返回真实依赖/存储错误；
- Retrieval Run 无法落库时不返回一份“无法审计的正常结果”。

“超时”和“查无结果”必须分开。超时表示没有观察到事实，空结果才表示一次成功查询没有命中。

## 9. 可观测性保存什么

每次搜索保存一条 `knowledge_retrieval_runs`：tenant、actor、roles、原查询哈希、改写查询哈希、改写策略、模式、Embedding 模型、Top K、两个通道候选数、最终结果数、降级原因、总耗时、request_id、trace_id 和创建时间。

每个返回候选保存一条 `knowledge_retrieval_hits`：最终 rank、chunk_id、两个通道各自 rank、融合分、向量距离和关键词分。这样既能回答“最后给了什么”，也能回答“为什么这个 chunk 排到这里”。原始客户查询不落表，必要时应由有权限且有脱敏策略的专用系统另行保存。

## 10. Recall@K 和 MRR

假设每个评估问题都标注了一个应召回政策：

- `Recall@5`：正确政策是否出现在前 5 个结果中，反映有没有找到；
- `MRR`：正确政策在第 `r` 名时计 `1/r`，再对所有问题平均，反映是否排得靠前。

Recall@5 为 100% 但 MRR 很低，说明正确文档虽然出现了，却经常被无关文档压在后面。最终回答质量还需要 groundedness、引用正确性和任务成功率，不能用 Recall 代替。

Day13 的 60 个确定性相关性案例测得：关键词 66.67%、语义 66.67%、混合 100% Recall@5；混合比最佳单通道高 33.33 个百分点，MRR 为 1.0。另有 90 次过滤检查未产生受限知识命中。

这些数值只证明当前合成数据上的编排性质：关键词能补语义盲区，语义能补字面盲区，RRF 能取并集。Fixture Embedding 不代表 OpenAI 模型在生产语料上的真实质量。

## 11. 与整体 Agent 架构的关系

```text
Day12：可信知识入库
  → Day13：安全召回候选
  → Day14：重排、冲突检查、引用与 grounded answer
  → Day15：上下文选择和 Token 预算
  → Agent Loop：把证据当 Observation 决定下一步
```

检索结果仍然是外部证据，不能覆盖系统规则，也不能直接触发退款。即使文档写着“可以退款”，Agent 还要查询真实订单事实，确定性 Policy Engine 还要重新检查金额、状态和审批。

## 12. 参考资料

- [OpenAI Embeddings 指南](https://platform.openai.com/docs/guides/embeddings)
- [OpenAI text-embedding-3-small 模型](https://platform.openai.com/docs/models/text-embedding-3-small)
- [pgvector 官方仓库](https://github.com/pgvector/pgvector)
- [pgvector-python SQLAlchemy 集成](https://github.com/pgvector/pgvector-python)
- [PostgreSQL 全文检索控制](https://www.postgresql.org/docs/18/textsearch-controls.html)
- [PostgreSQL pg_trgm](https://www.postgresql.org/docs/18/pgtrgm.html)
