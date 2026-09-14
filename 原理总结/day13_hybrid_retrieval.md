# Day 13：安全、可观测的混合检索

## 业务问题

关键词能准确找政策码，却不懂“主管点头”和“人工审核”语义相同；向量能找语义，却可能漏精确编号。检索还必须保证其他租户、无权限角色和失效政策在 Top K 之前就被排除。

## 完整主流程

```text
已入库 Chunk
  → 独立索引任务生成固定 256 维 Embedding
  → content_hash 校验后原子写入 pgvector

查询 + 可信 Actor + as_of
  → RBAC
  → 确定性 Query Rewrite
  → pgvector 语义召回
  → PostgreSQL FTS + ILIKE + pg_trgm 关键词召回
  → 两个通道先做 tenant/ACL/有效期过滤
  → RRF 按名次融合
  → 保存 Run/Hit Trace
  → 返回带来源和行号的候选证据
```

## 核心原理

- Embedding 是语义表示，不是事实；固定维度、模型名和距离阈值必须由后端校验。
- Top K 只限制数量，距离阈值防止“没有相关文档时也硬返回一个”。
- Query Rewrite 只规范查询，不能生成 tenant、角色或有效期。
- 安全条件必须进入候选 SQL 并发生在排序与 LIMIT 前，不能召回后再过滤。
- cosine distance 与关键词 score 量纲不同，不能直接相加；RRF 使用每个通道的 rank，跨评分体系更稳定。
- hybrid 的语义通道故障可以降级为关键词，但必须记录原因；semantic-only 失败不能伪装成空结果。
- 检索结果是外部证据，不能覆盖 System Policy，也不能直接触发退款。

## 状态和数据变化

- Day12 的 Document/Chunk 内容和版本不被检索改写；
- 索引任务为 chunk 增加 embedding、embedding_model、embedded_at；
- 每次查询新增 Retrieval Run；
- 每个结果新增 Hit Trace，保存最终名次、双通道名次和分数；
- 原始查询不落表，只保存哈希、策略、数量、耗时和 request/trace。

## 失败恢复

- Embedding 临时错误有限重试，鉴权/参数/维度错误不盲目重试；
- 整批向量成功后才原子写入，content_hash 变化会阻止旧任务覆盖；
- hybrid 单通道失败可降级，双通道都失败则明确报错；
- 轨迹记录失败时不把不可审计结果交给 Agent。

## 量化证据

- 60 个合成相关性案例：keyword Recall@5 `66.67%`，semantic `66.67%`，hybrid `100%`；
- hybrid MRR `1.0`，比最佳单通道提升 `33.33` 个百分点；
- 90 次 tenant/ACL/有效期检查：受限知识命中 `0`；
- 真实 PostgreSQL：pgvector cosine、关键词 SQL、扩展检查和轨迹持久化 `2/2` 通过。

这些数据证明当前确定性夹具上的检索编排和安全边界，不代表生产模型质量。真实业务上线前必须使用人工标注的售后查询重新评估。

## 与前后 Day 的关系

```text
Day12 生产可信知识单元
  → Day13 安全召回候选
  → Day14 重排、冲突检查、引用和 grounded answer
  → Day15 按 Token 预算组装 Agent Context
```
