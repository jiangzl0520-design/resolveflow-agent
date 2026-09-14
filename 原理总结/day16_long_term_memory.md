# Day 16：短期状态与策略控制的长期记忆

ResolveFlow 不能把所有聊天都当长期记忆。原始 History 用来回放过程；Agent State 保存当前 Run 的目标、订单号、Observation 和步骤；Summary 是节省 Context 的有损压缩；订单、物流、审批等业务事实必须重新查询权威系统；只有明确、稳定、跨会话有价值的用户偏好和少量客户资料才进入长期 Memory。

LangGraph `PostgresSaver` 继续保存 thread 内短期 State，负责节点恢复。跨 thread 的长期 Memory 使用独立 PostgreSQL 领域表，因为业务需要精确白名单、来源、置信度、TTL、版本冲突、多租户、删除和审计，这些规则不能交给模型或通用存储层决定。

完整写入流程是：客户端提交候选和幂等键 → Schema 校验结构 → Router 转成 Command → Authorization 校验角色、租户和 subject → Policy 按 key 检查允许来源、最低置信度和最大 TTL → Repository 在事务内检查幂等并锁定同 key 版本 → Service 决定创建、保持、更新、冲突或拒绝 → Memory 状态和 Audit Event 原子提交。模型推断只返回 `confirmation_required`，必须由用户或人工确认后以可信来源重新提交；退款资格等业务状态永远不允许写入长期记忆。

同值重复提交返回 `unchanged`。不同值只有 `observed_at` 严格更新时才把旧版本设为 `superseded`、新版本设为 `active`；无法证明谁更新时，相关版本都变为 `conflicted` 并暂停召回。过期记录在查询时排除。删除会把同 key 所有历史版本设为 `deleted` 并把 value 清空，只保留哈希和审计链，避免旧版本重新出现。

每次 Agent 决策前，Planner 用受信任的 `tenant_id + ticket_id` 找到 Ticket 的 customer_id，只读取未过期的 active Memory。Memory 被转成带 source、confidence、expires_at 和 version 的 Context 候选，再经过 Day15 Context Builder 的相关性和 Token 预算选择。它的优先级低于系统规则、当前目标、State 和关键 Observation，绝不能覆盖退款 Policy。Memory 数据库暂时不可用时，系统记录 `dependency_unavailable` 并在没有个性化偏好的情况下继续安全调查；硬性 Policy 不依赖 Memory，所以仍然 fail-closed。

当前只有四个精确语义键，不需要 embedding。Embedding 是近似检索技术，不是 semantic memory 的必要条件；现在使用向量库只会增加误召回、ACL、删除传播、成本和解释难度。以后只有真实数据证明存在大量开放式长期知识时才考虑增加语义搜索。

80 个版本化合成案例中，策略控制方案相对“保存全部聊天”基线把正确处理率从 31.25% 提升到 100%，阻止 40 次不安全写入和 15 次失效召回，平均持久化 value 字符数从 2631.38 降至 3.56（减少 99.86%）。这些只证明当前固定数据上的确定性机制，不代表线上模型质量或业务收益。Day1–Day16 全量 219 个自动化测试通过，真实 PostgreSQL、Alembic `20260805_0011`、依赖与容器健康检查均通过。

一句话记忆：Agent 可以提出“值得记住的候选”，Memory Service 决定“能否记、记多久、冲突和删除怎么办”，Repository 保证原子持久化，Context Builder 决定“本轮模型是否需要看到”。
