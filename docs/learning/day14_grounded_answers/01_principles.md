# Day 14 原理：重排、冲突检测与可验证引用

## 1. 今天解决的真实问题

Day13 返回的是“可能相关的候选片段”，不是最终事实。Top K 中仍可能出现重复内容、主题相关但不能支持结论的内容、同时有效却互相矛盾的政策版本。即使模型能稳定输出 JSON，也仍可能写出不存在的引用 ID、改写原文后声称是直接引语，或者在没有证据时补全一个听起来合理的答案。

Day14 的目标不是让模型“更自信”，而是建立一条可验证证据链：LLM 负责语义重排和候选表述，确定性后端负责权限、有效期、集合完整性、冲突门禁、引用存在性、原文包含关系、状态迁移和审计。无法验证时不输出确定结论。

## 2. 完整操作流程

```text
问题 + 可信 Actor + as_of
  → 创建 processing Answer Run，只保存问题哈希
  → 调用 Day13 混合检索并保存 Retrieval Run/Hit
  → 后端再次过滤有效期
  → 按正文哈希去重，分配本轮临时证据 ID：K1、K2……
  → LLM 结构化重排：逐条给 relevance/relation，并报告冲突对
  → 后端检查“输入证据 ID 集合 == 输出 assessment ID 集合”
  → 后端选择达到阈值的相关证据
  → 在相关证据全集上检查模型冲突 + 同 source_key 版本冲突
  → 有冲突：固定安全答复，跳过答案模型
  → 无相关证据：固定证据不足答复，跳过答案模型
  → LLM 只生成结构化 claims + evidence_id + exact_quote
  → 后端逐条验 ID、原文精确子串、重复 claim 和伪造引用标记
  → 后端自己拼接最终答案和 [Kx]
  → 一个事务完成 Answer Run，并保存 Citation Trace
  → 返回答案、来源、版本、行号和精确引文
```

每次运行先写 `processing`，成功、证据不足、冲突或失败时再进入终态。进程如果在中间崩溃，数据库会留下可扫描的 `processing` 证据，而不是整次执行凭空消失。

## 3. 结构化输出不等于事实正确

OpenAI Structured Outputs 可以让模型响应遵循 JSON Schema，Pydantic 模型也可以直接作为解析契约。这解决的是“字段是否存在、类型是否正确、枚举是否合法”，不解决以下问题：

- `evidence_id=K9` 是否真的出现在本轮上下文；
- `exact_quote` 是否真的存在于 K9 原文；
- 两份当前有效政策是否互相冲突；
- 政策是否已经过期；
- claim 是否被证据支持，而不只是语义上看起来合理。

因此项目把校验拆成两层：Model Gateway 用 Pydantic 验证结构；Grounded Answer Service 用数据库事实和确定性规则验证语义约束。前者失败叫输出契约错误，后者失败叫 grounding verification failure，两者不能混为一类。

官方参考：[OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)。

## 4. 为什么重排由 LLM 做，门禁由后端做

“这段政策与问题是直接相关、间接相关还是无关”需要理解自然语言，适合交给 LLM。重排输出包含：

- 每个输入 ID 的 `relevance: 0..100`；
- `direct / related / irrelevant`；
- 语义上互不兼容的证据 ID 对。

但模型不得决定自己少评估哪条证据。后端要求 assessment 的 ID 集合与输入证据 ID 集合完全相等，重复、缺失或额外 ID 都失败。这样模型只能在后端给定的证据集合内评分，不能通过遗漏不利证据改变结论。

`min_relevance=60` 和 `max_evidence` 由后端控制。阈值决定是否足够相关，数量上限控制进入答案 Prompt 的证据量；它们都不是权限规则，也不能替代冲突检查。

## 5. 去重与冲突不是一回事

完全相同的正文按规范化内容哈希去重，减少重复 Prompt Token 和重复引用。去重不表示“同一 source_key 只留最新一条”：两个正文不同、时间区间同时覆盖 `as_of` 的同源版本可能构成真正冲突，必须保留并检查。

冲突检测包含两条通道：

1. LLM 报告跨来源、语义层面的互不兼容规则；
2. 后端确定性检查同一 `source_key` 下，不同 document/version、不同正文且同时有效的证据。

冲突在“达到相关阈值的证据全集”上检查，然后才使用 `max_evidence` 截断答案上下文。否则把 `max_evidence=1` 就可能把排在第二名的冲突版本藏起来。冲突出现后直接返回固定文本并保留冲突来源，不调用答案生成模型。

## 6. 为什么模型只生成候选 claim，不生成最终自由文本

答案模型返回：

```text
disposition
claims[]
  text
  supports[]
    evidence_id
    exact_quote
```

模型没有最终引用编号的控制权。后端验证通过后，才把 claim 与 `[K1]` 等标记拼接成最终答案。这样可以分别验证每个 claim 的证据，而不是拿一大段自由文本做模糊相似度判断。

证据在 Prompt 中明确标为 `UNTRUSTED_EVIDENCE_JSON`。政策正文只是数据，正文中的“忽略系统规则”等文本不能升级为指令。这是基础隔离；更完整的 Prompt Injection 防护仍属于后续安全 Day。

## 7. 确定性引用验证做什么

每个 claim 返回后依次检查：

1. 引用 ID 必须属于本轮重排后选中的证据；
2. `exact_quote` 必须是对应 chunk 正文的精确子串；
3. claim 文本不能自己夹带 `[K9]` 等引用标记；
4. 规范化后的 claim 不能重复；
5. 同一个证据 ID 在不同 claim 中不能对应两段互相不同的引文；
6. answered 至少有一个 claim，每个 claim 至少有一个 support。

验证不是判断自然语言 claim 的全部逻辑蕴含。当前自动指标证明的是“引用存在、有效、原文可定位，并且不在已知冲突状态下生成结论”。更强的 claim entailment 仍需要人工标注或校准后的 Judge，不能把精确子串验证夸大成完整事实正确率。

## 8. 证据不足、冲突和依赖失败必须分开

- 检索成功但没有有效候选：`insufficient_evidence`；
- 相关政策互相冲突：`evidence_conflict`；
- 模型给出伪造引用：运行记为 `failed`，错误码 `grounding_verification_failed`；
- 检索/模型/数据库超时：抛出真实依赖错误，不能伪装成“没有政策”；
- Answer Run 或 Citation Trace 无法落库：阻止结果返回，不能交给 Agent 一份不可审计的答案。

只有前两类可以返回固定安全业务文本。技术故障表示没有完成观察，不代表事实不存在。

## 9. 可观测性与隐私

`knowledge_answer_runs` 保存 tenant、actor/roles、问题哈希、Retrieval Run ID、两个 Model Call ID、各阶段候选数量、冲突数、引用数、状态、错误码、耗时和 request/trace。`knowledge_answer_citations` 保存 citation ID、chunk ID、source/version/行号、引文哈希和支持的 claim 序号。

原始客户问题、完整答案和原始引文不写入 Answer Run 表，只保存哈希；原文仍由受 ACL 保护的知识表管理。Model Call 表保存模型、Prompt、Schema、Token、重试和 trace 元数据，不保存完整 Prompt 正文。这既能串联运行轨迹，也降低日志复制客户信息和内部政策的风险。

## 10. 与 Agent 整体架构的关系

```text
Day12：把政策变成可追溯 Document/Chunk
  → Day13：安全召回候选 Evidence
  → Day14：把候选变成可验证 Grounded Answer
  → Day15：决定哪些规则、状态、工具 Observation 和证据进入本轮 Context
  → Agent Loop：根据事实动态选择下一步工具或转人工
```

Grounded Answer 是 Agent 的政策 Observation，不是退款授权。它能说明“政策要求先查签收证明”，但订单是否属于当前用户、实际物流状态、金额是否合规、是否有人工审批，仍必须由工具和确定性 Policy Engine 检查。RAG 不能替代业务资格判断。

## 11. 当前证据边界

60 例评估使用确定性合成政策和模型输出，证明后端编排与门禁，不证明生产 LLM 的语言质量。真实 PostgreSQL 集成测试已经覆盖 pgvector 检索、两次模型调用记录、Answer Run、Citation Trace 和外键，但模型 Provider 使用确定性 Fake，付费 API 调用为 0。生产上线前仍需真实售后问题、人工标注答案和 claim-level 支持关系重新评估。
