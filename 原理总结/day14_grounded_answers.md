# Day 14：重排、冲突检测与可验证引用

## 业务问题

Day13 的 Top K 只是候选证据。模型即使输出合法 JSON，也可能遗漏不利证据、引用不存在的 K9、编造原文、使用过期政策，或在冲突政策中强行选择一条。Day14 要把“候选回答”变成后端可以逐条验证的 Grounded Answer。

## 完整主流程

```text
问题 + 可信 Actor/as_of
  → 先保存 processing Answer Run
  → Day13 安全混合检索并保存 Retrieval Trace
  → 后端再过滤有效期、按正文哈希去重、分配 K1/K2
  → LLM 逐证据结构化重排并报告冲突
  → 后端校验重排 ID 集合完整
  → 在全部相关证据上检查模型冲突和同源版本冲突
  → 无证据/冲突：固定安全答复，不生成答案
  → LLM 生成 claims + evidence_id + exact_quote
  → 后端验证 ID、原文精确子串、重复 claim、伪引用标记
  → 后端拼接最终 [Kx]
  → Answer Run 与 Citation Trace 同事务进入终态
```

## 核心原理

- Structured Outputs 只保证 JSON Schema，不保证内容有事实依据；Model Gateway 验结构，Grounded Service 验语义约束。
- LLM 适合做自然语言相关性和冲突候选判断；权限、有效期、集合完整性、引用存在性和状态转换必须由后端决定。
- 完全相同正文可以去重；同 source_key 的不同有效版本不能当重复删掉，它们可能是冲突。
- 冲突检查必须发生在 `max_evidence` 截断之前，数量预算不能改变安全结论。
- 模型只生成候选 claim 和支持信息，最终引用标记由后端拼接。
- 外部文档始终是 `UNTRUSTED_EVIDENCE`，不能覆盖系统规则。
- 超时/数据库失败不等于没有证据；不可审计的结果不能返回给 Agent。

## 状态与可观测性

Answer Run 从 `processing` 进入 `answered / insufficient_evidence / evidence_conflict / failed`。它关联 Retrieval Run 和两次 Model Call，保存候选数量、冲突数、引用数、错误、耗时和 trace；Citation Trace 保存 chunk、source/version/行号、引文哈希和 claim 序号。原始问题和答案只存哈希，减少日志泄露。

## 失败处理

- 无有效证据：固定证据不足结果，LLM 调用为 0；
- 证据冲突：固定冲突结果，只保留冲突来源，跳过答案模型；
- 重排漏 ID：契约失败；
- K9、虚构引文或 claim 夹带引用：grounding verification 失败；
- 运行或引用落库失败：阻断返回；
- 崩溃留下 processing，可由后续恢复任务识别。

## 量化证据

- 60 个合成案例：正确处理率从结构化输出基线 `33.33%` 提升到 `100%`；
- 答案引用精确率从 `33.33%` 提升到 `100%`；
- 10 个过期政策案例引用数从 `10` 降为 `0`；
- 10 个伪造引用全部被后端阻断；
- 真实 PostgreSQL/pgvector 链路验证 Retrieval、2 个 Model Call、Answer Run 和 Citation Trace 均能落库关联；
- 全量工程测试 `194 passed in 31.43s`。

这些数据证明当前合成夹具上的确定性门禁和工程链路，不代表生产 LLM 质量；真实业务仍需人工标注数据重新评估。

## 与前后 Day 的关系

```text
Day12 生产可追溯知识
  → Day13 召回安全候选
  → Day14 生成可验证政策 Observation
  → Day15 在 Token 预算内组装完整 Agent Context
```

Grounded Answer 仍不能直接退款。订单事实必须由工具查询，金额、审批和执行资格仍由确定性 Policy Engine 检查。
