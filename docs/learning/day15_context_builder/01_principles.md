# Day 15 原理：Context Builder 与 Token 预算

## 1. 今天解决的真实问题

Agent 每一步都要把“当前真正需要的信息”交给模型，但 History、Agent State、Memory、RAG Evidence、Tool Observation 和工具描述会持续增长。直接把全部内容拼进 Prompt 会产生四类问题：旧订单证据和最新修改同时出现、无关历史挤掉关键目标、无关工具干扰选择、超过模型上下文窗口或产生不必要成本。

Day15 建立独立 `ContextBuilder`。它不修改业务事实，也不替模型作最终业务判断；它负责在每次模型调用前，从已有候选信息中构造一个有来源、有预算、有丢弃理由且可以审计的本轮 Context。

## 2. 五个容易混淆的概念

| 概念 | 保存什么 | 生命周期 | 是否直接等于本轮 Context |
|---|---|---|---|
| History | 原始交互过程与修改事实 | 一次会话或更久 | 否，只是候选来源 |
| Agent State | 当前目标、主实体、步骤、Observation、预算、终止状态 | 一次 Agent Run | 否，只是当前事实来源 |
| Memory | 跨会话可复用的稳定偏好或事实 | 多次会话 | 否，要经过可信度、时效和权限筛选 |
| RAG Evidence | 本轮从知识库检索并验证过的证据 | 一次查询或一次 Run | 否，要按相关性和预算选择 |
| Context | 这一次模型调用实际看到的指令与数据 | 一次 Model Call | 是最终装配结果 |

关系是：History、State、Memory、RAG 和工具结果提供候选片段，Context Builder 决定本轮哪些候选进入模型。Day15 已支持这些来源标签，但不实现 Memory 的写入、冲突、过期和删除生命周期，那是 Day16 的范围。

## 3. 完整操作流程

```text
Agent Planner 收到 AgentRunState + 当前 Actor 有权使用的工具
  → 把系统规则、当前目标、完成条件、状态、Observation、验证失败和工具描述
    转成 ContextFragmentCandidate
  → 给每个候选标记 source、trust、priority、relevance、ordinal、required
  → 先剔除越权、失效和与当前步骤无关的候选
  → 同一 semantic_key 的可替换片段只保留最新 ordinal
  → 去掉重复内容和低相关可选片段
  → 先装入 required 片段
  → 使用目标模型 tokenizer 计算“指令 + 运行数据 + 输出 Schema”真实 Token
  → required 超预算：记录 failed trace，并拒绝调用模型
  → 按优先级、可信度、相关性和新鲜度逐个尝试加入可选片段
  → 每次重新计算完整输入，超预算的片段记录 token_budget_exceeded
  → system policy 放入 instructions；其余内容放入结构化 runtime data
  → 先持久化 Context Build Run 和所有片段决策
  → 调用 Model Gateway，并用 context_build_id 关联 Model Call
  → 模型返回结构化候选动作，后续仍由 Tool Executor、Verifier 和 Policy Gate 校验
```

## 4. 分层选择的核心维度

### 4.1 Source 说明信息是什么

系统规则、当前目标、用户修改、Agent State、工具 Observation、RAG Evidence、History、Memory 和工具描述不能混成一段无类型字符串。`source` 让系统知道片段的业务角色，也让评估可以统计某类信息为什么丢失。

### 4.2 Trust 说明信息从哪里来

当前四级可信度是：`system`、`verified_internal`、`user_provided`、`external_data`。可信度不是“内容一定正确”：工具 Observation 属于外部数据，仍可能超时、陈旧或指向错误订单；用户修改可以改变目标，但不能覆盖系统安全规则。

### 4.3 Priority 与 relevance 不是一回事

`priority` 表示丢失后的业务损失，系统规则、当前目标和完成条件优先级最高；`relevance` 表示它对当前步骤是否有帮助。一个通用政策可能很重要，但在当前只需确认订单是否存在时并不相关。

### 4.4 Freshness 通过 semantic key 和 ordinal 表达

同一个 `semantic_key` 代表同一可更新事实，例如“当前订单号”。`ordinal` 更大的片段是更新版本。旧订单号仍留在 History 中用于审计，但不能和最新订单号一起进入当前推理数据。

## 5. Token 预算为什么要预留

模型上下文窗口不只包含用户输入，还包含系统指令、工具定义、结构化输出 Schema、模型输出和部分模型的 reasoning tokens。Day15 使用：

```text
可用输入上限 = min(
    配置的最大输入 Token,
    模型上下文窗口
    - 预留输出 Token
    - 预留推理 Token
    - 安全余量
)
```

只看字符串长度或估算“每个中文字符一个 Token”都不可靠，因此项目使用 `tiktoken` 按目标模型编码计算。调用 API 时仍可能存在少量供应商封装差异，所以还保留安全余量。

OpenAI 官方说明：上下文窗口包含输入、输出和 reasoning tokens；使用 `previous_response_id` 也不会让之前输入免费消失，链上的输入仍按输入 Token 计费。长任务还可以使用 compaction，但 compaction 解决“压缩旧上下文”，不能替代本项目的来源、权限、失效和保留理由判断。

官方参考：[Conversation state](https://developers.openai.com/api/docs/guides/conversation-state)、[Compaction](https://developers.openai.com/api/docs/guides/compaction)。

## 6. 为什么 required 放不下必须失败

系统安全规则、当前目标、完成条件和最小 Agent State 是本轮推理的前提。静默丢掉其中任意一项后继续调用模型，会得到“格式合法但前提不完整”的动作。因此 required 超预算不是普通截断，而是 `required_context_overflow`：先写失败记录，再让 Planner 失败并由 Agent 的既有终止/恢复机制处理。

同理，Context Trace 写库失败时返回 `context_trace_recording_failed`，不会继续产生一份无法解释输入来源的模型结果。这是可观测性门禁，不是普通日志的 best effort。

## 7. 主事实变化如何使派生证据失效

订单号从 10086 改为 10087 时：

- History 保留“原来是 10086，后来改成 10087”的事实；
- Agent State 的权威 `order_id` 更新为 10087；
- 10086 的订单和物流 Observation 标记 `invalidated`，不进入本轮 Context；
- 工具选择回到 `order_lookup`，重新调查 10087；
- 旧审批和退款方案不能沿用，后续必须基于新证据重新走确定性 Policy 和人工审批。

这体现了“主事实 → 派生事实/状态/计划”的依赖关系。只修改 Context 中的订单号而继续保留旧物流证据，会制造一个内部自相矛盾的快照。

## 8. 动态工具描述与 Agent 核心能力

Planner 不再每一步都把所有工具 Schema 发给模型：

- 没有可信订单事实时，只暴露 `order_lookup`；
- 已确认订单且物流、政策都缺失时，同时暴露两个仍能推进任务的只读工具，由模型选择下一步；
- 只缺一个证据时，只暴露对应工具；
- 最新工具错误可重试时，保留该工具供模型修正或重试；
- 订单未付款、已取消或已退款时，不暴露无意义的物流/政策调查工具；
- 证据齐全时不暴露读取工具，模型只能完成、提出候选方案或升级。

确定性代码负责缩小到“当前合法且有用的能力集合”，LLM 仍负责根据目标、Observation 和失败反馈选择动作。这既减少无关 Schema Token 和错误选择，也没有把退款资格、权限或人工审批交给模型。

OpenAI Prompt Caching 要求前缀精确匹配，建议把稳定内容放前面、动态内容放后面。Day15 将稳定系统指令与动态 runtime data 分开，为后续缓存观测留下了正确结构；是否命中及节省多少仍需真实调用测量。官方参考：[Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)。

## 9. 可观测性和隐私

`context_build_runs` 保存模型、预算、实际 Token、进入/丢弃数量、状态、错误码和 request/trace；`context_fragment_traces` 为每个候选保存来源、可信度、优先级、相关性、估算 Token、是否进入和决定原因。

Trace 不保存片段原文，只保存 SHA-256 内容哈希。`model_call_records.context_build_id` 把模型版本、Prompt/Schema 哈希、重试、耗时和真实 Token 连接到本轮 Context 选择。这样可以回答“模型为什么没看到某条证据”，同时避免在审计表再次复制客户文本和内部政策。

## 10. 评估结论与边界

版本化合成数据集包含 60 例：20 个长历史、20 个最新修改/旧证据失效、20 个动态工具描述。与“全部拼接后从前删除非系统片段”的确定性基线相比：

| 指标 | 基线 | Context Builder | 变化 |
|---|---:|---:|---:|
| 关键片段完整保留率 | 66.67% | 100.00% | +33.33 点 |
| 陈旧片段误选择次数 | 40 | 0 | -40 |
| 动态工具描述精确率 | 33.33% | 100.00% | +66.67 点 |
| 平均输入 Token | 463.67 | 370.35 | -20.13% |

这是同一合成数据、同一 tokenizer 和同一预算下的后端选择算法结果，付费模型调用为 0。它证明的是选择门禁和审计机制，不证明生产对话质量、真实成本或线上任务完成率。

