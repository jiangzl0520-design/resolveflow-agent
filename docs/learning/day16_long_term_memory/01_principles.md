# Day 16 原理：短期状态与策略控制的长期记忆

## 1. 今天解决的真实问题

客服 Agent 会在不同工单、不同会话中重复服务同一个客户。系统确实需要记住“用户偏好中文、回答尽量简洁”这类跨会话稳定信息，但不能把“订单 10086 已签收”“模型猜测用户喜欢短信”“当前退款符合条件”等临时内容都长期保存。

如果把整段聊天直接写进向量库，会产生四个问题：

- 临时业务事实脱离原订单和时间后仍被召回，导致错误决策；
- 模型推测、恶意文本和用户明确陈述混在一起，来源不可判断；
- 用户改口、信息过期或要求删除后，旧内容仍可能被语义检索命中；
- 上下文越来越大，却无法解释某条信息为什么被写入和召回。

Day16 因此实现的不是“聊天归档”，而是一套受业务策略控制的长期语义记忆：只允许稳定、明确、可验证的信息进入；每条记忆都有主体、租户、语义键、来源、置信度、版本、有效期和状态；冲突、过期和删除内容不会进入 Agent Context。

## 2. 五类信息必须分开

| 类型 | 保存内容 | 生命周期 | 权威用途 | 本项目中的位置 |
|---|---|---|---|---|
| Raw History | 用户与系统的原始交互、改口过程 | 会话或审计保留期 | 回放“发生过什么”，不能自动当当前事实 | 会话历史/审计记录 |
| Agent State | 当前目标、订单号、步骤、Observation、预算、终止状态 | 单次 Agent Run，可 checkpoint 恢复 | 驱动当前工作流 | LangGraph State + PostgresSaver |
| Summary | 对较长历史的有损压缩 | 当前线程内按需更新 | 节省 Context，不是事实源 | Context 候选 |
| Business Fact | 订单、物流、审批、退款状态 | 由业务系统决定 | 高风险决策的权威依据 | 每次通过工具/业务服务查询 |
| Long-term Memory | 跨会话稳定偏好和少量客户资料 | 跨线程，带过期和删除 | 个性化 Context，不参与硬性资格判定 | `long_term_memories` |

它们的关系是：History 用于追溯，State 用于推进当前 Run，Summary 用于压缩，业务事实必须从权威系统重新取得，长期 Memory 只提供跨会话个性化信息。Context Builder 每次从这些来源中选择本轮真正需要的片段，但 Context 本身不是新的永久存储。

## 3. LangGraph 框架边界与本项目选择

LangGraph 官方把短期记忆定义为 thread-scoped state，由 checkpointer 在每一步保存；长期记忆通过 Store 在不同 thread 之间共享。参考：[Memory overview](https://docs.langchain.com/oss/python/concepts/memory)、[Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)、[Long-term memory](https://docs.langchain.com/oss/python/langchain/long-term-memory)。

ResolveFlow 保留 Day9 的 `PostgresSaver` 管理短期 Agent State，因为它解决的是节点恢复和同一 thread 的连续状态。Day16 没有把用户偏好塞进 checkpoint，因为换一个 thread 后 checkpoint namespace 不代表同一个客户，也不应把业务客户身份等同于运行线程 ID。

长期记忆采用独立 PostgreSQL 领域表和 Repository，而不是直接暴露通用 Store，原因不是框架不能存，而是本业务还要求：

- 固定白名单和每个 key 独立的来源、置信度、TTL 规则；
- 多租户和 subject 级访问控制；
- 新旧版本冲突处理、逻辑删除和原文脱敏；
- 幂等写入、事务和每次尝试的审计事件；
- 与 Ticket 的 customer_id 绑定后再进入 Agent Context。

因此框架负责工作流持久化，领域服务负责“哪些内容有资格成为长期记忆”。两者不是替代关系。

## 4. 完整写入流程

```text
客户端提交候选记忆 + Idempotency-Key
  → FastAPI Schema 校验字段格式
  → Router 翻译为 MemoryWriteCommand
  → AuthorizationService 校验角色、租户和 subject 身份
  → MemoryService 标准化 value
  → MemoryPolicy 按 memory_key 检查白名单、来源、置信度和最大 TTL
  → 生成不含 request_id/trace_id 的稳定 request_hash
  → Repository 在事务内检查幂等记录
  → 锁定同 tenant + subject + memory_key 的全部版本
  → 根据已有版本决定 unchanged / updated / conflicted / rejected
  → 原子更新版本状态、写入新版本和 MemoryAuditEvent
  → API 返回结构化 decision
```

`request_id` 和 `trace_id` 用于追踪一次 HTTP/调用链，每次重试可能不同；它们不属于业务请求语义，所以不进入 `request_hash`。否则同一个幂等键的合法重试会被误判为不同请求。Day16 的 API 测试曾准确暴露这一问题，修复后相同业务输入可以重放原结果，而改变 value 的复用会返回 409。

## 5. 写入白名单和来源门槛

当前只开放四个精确语义键：

| memory_key | 允许来源 | 最低置信度 | 最大 TTL |
|---|---|---:|---:|
| `preference.language` | explicit_user、human_reviewer | 0.90 | 365 天 |
| `preference.response_style` | explicit_user、human_reviewer | 0.90 | 180 天 |
| `preference.contact_channel` | explicit_user、human_reviewer | 0.90 | 90 天 |
| `profile.locale` | explicit_user、verified_system、human_reviewer | 0.95 | 365 天 |

`order.refund_eligible` 等业务状态不在白名单，即使置信度写成 1.0 也会拒绝，因为退款资格必须实时查询 Policy/业务系统。`model_inference` 不直接写入，服务返回 `confirmation_required`；模型可以提出“用户可能偏好简洁回答”这个候选，但只有用户确认或人工审核后才能转换成可信来源再次提交。

这体现了 Agent 与后端的职责边界：LLM 可以发现候选语义，后端决定候选是否有资格成为长期事实。

## 6. 生命周期和冲突状态机

同一个 `(tenant_id, subject_id, memory_key)` 可以保留多个版本，但召回只读取未过期的 `active` 版本。

```text
不存在记录
  → 合法候选：创建 v1 active

已有 active，候选值相同
  → unchanged，不制造重复版本

已有 active，候选值不同且 observed_at 严格更新
  → 旧版本 superseded，新版本 active

已有 active，候选值不同但时间不更新/无法证明谁新
  → 现有版本与候选版本都 conflicted，暂停召回

已有 conflicted，后来得到严格更新的可信候选
  → 冲突版本 superseded，新版本 active

expires_at <= 当前时间
  → 查询时直接排除；下次同 key 变更时持久化为 expired

用户请求删除
  → 所有未删除版本变为 deleted，value 置空，不再召回
```

冲突时不能“随便取最后一条”。写入时间 `created_at` 只说明数据库何时收到请求，`observed_at` 才说明候选事实何时被观察到。只有候选的 `observed_at` 严格更新，系统才有依据替换旧值；否则停止召回比选错偏好更安全。

## 7. 读取与 Context 注入流程

```text
Agent Planner 开始一步决策
  → 使用 tenant_id + ticket_id 查询 Ticket 对应 customer_id
  → Repository 只返回该客户未过期的 active Memory
  → 每条 Memory 转成独立 ContextFragmentCandidate
  → 标注 source=memory、来源可信级别、confidence、expires_at、version
  → Day15 Context Builder 再做相关性、替换和 Token 预算选择
  → 记录片段进入/丢弃原因
  → Model Gateway 只看到最终被选择的偏好片段
```

Ticket 查询同时约束 Ticket 和 Memory 的 `tenant_id`，避免跨租户 customer_id 相同造成串读。显式用户来源标为 `user_provided`；verified_system/human_reviewer 标为 `verified_internal`。Memory 只是 Context 的可选候选，优先级低于系统规则、当前目标、状态和关键 Observation，绝不能覆盖退款 Policy。

如果长期记忆数据库读取失败，当前调查任务采用安全降级：不把任何 Memory 发给模型，但创建一条 `dependency_unavailable` 的排除 Trace，然后继续执行订单查询等安全步骤。理由是“暂时不知道用户喜欢简洁回答”不应阻断客服调查；硬性业务规则从不依赖 Memory，所以不会因该降级被绕过。

## 8. 删除、隐私和审计

删除不是只把当前版本设为不可见。服务会把同一语义键的 active、superseded、conflicted、expired 版本全部置为 `deleted` 并将原始 `value` 设为 `NULL`，防止旧版本以后重新被召回或在普通数据库查询中泄露。

为了仍能证明删除前后的对象一致，记录保留 `value_hash`；来源引用只保存 `source_reference_hash`，审计表保存候选哈希、请求哈希、decision、actor、request_id 和 trace_id，而不复制用户原文。每一次 `created`、`updated`、`unchanged`、`conflicted`、`confirmation_required`、`rejected`、`deleted` 或 `not_found` 尝试都会有审计事件。

权限上，客户只能读写删除自己的 subject；Agent 可以读写但不能删除；Supervisor/Tenant Admin 可以在租户内执行删除。删除和写入都要求幂等键，数据库状态和审计事件在同一事务中提交。

## 9. 为什么 Day16 不需要 embedding

Embedding 适合“用户问法与大量非结构化内容语义相近”的检索问题。本阶段只有四个定义明确的键，Planner 需要的是 `preference.language` 这类精确事实，不需要在整段聊天中做近似搜索。

这里强行加入向量库会增加：误召回、跨主体 ACL 风险、删除残留、向量重建、成本和难以解释的相似度阈值，却不提升精确键读取。因此正确方案是先做结构化、可治理的 semantic memory。以后只有真实评估证明存在大量开放式长期知识，才考虑在严格 namespace、元数据过滤和删除传播之上增加语义检索。

要特别区分：semantic memory 是“记住事实/概念的一类长期记忆”，semantic search 是“一种按向量相似度检索的方法”；前者不等于必须使用后者。

## 10. 与前后 Day 的关系

- Day9：checkpoint 保存 thread 内 Agent State，负责中断恢复；Day16 负责跨 thread 的稳定用户信息。
- Day15：Context Builder 已能标记 Memory 来源并控制预算；Day16 为它提供真正经过生命周期治理的 Memory 候选。
- Day10：退款 Policy 和人工审批仍是硬门禁，不读取长期偏好决定退款资格。
- Day17：将在当前来源/权限边界上继续处理 prompt injection、敏感信息脱敏和外部内容安全。

最终边界可以概括为：Agent 负责发现“可能值得记住什么”，Memory Service 负责决定“能不能记、记多久、冲突怎么办”，Repository 负责原子持久化，Context Builder 负责决定“这一轮是否需要让模型看到”。
