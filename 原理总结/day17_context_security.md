# Day 17：上下文安全与 Prompt Injection 分层防御

用户 Goal、History、工具 Observation、Memory 和 RAG 文档都是模型需要读取的数据，但只有系统 Policy 可以成为指令。Prompt Injection 可以直接出现在用户输入，也可以间接埋在知识文档、网页或工具返回值中。只在 Prompt 里写“忽略恶意指令”不构成安全边界，因为检测可能漏掉社会工程式攻击；系统还必须限制被操纵模型能够触达的工具、数据和高风险动作。

完整输入流程是：上游先把信息转换成带 source/trust 的 Context 候选 → SYSTEM 内容保持为指令 → 其他内容递归脱敏 API Key、JWT、密码式 token、邮箱和手机号 → user/external 内容命中已知注入模式时被隔离 → required Goal 被攻击则整个 Context Build 失败并记录 Trace，可选恶意证据只丢该片段 → Day15 再执行新鲜度、相关性、去重和 Token 预算 → 模型只看到系统 instructions 与结构化 runtime data。

知识问答还有独立门禁：直接恶意问题在检索前变为 `security_blocked`；检索仍先按 tenant、角色 ACL 和有效期过滤；恶意或含秘密知识在 rerank 前移除；模型生成的 claim 再检查秘密、evidence_id、exact_quote 和引用。ACL 解决“有没有权看”，Injection 防护解决“有权看的内容能不能安全影响模型”，两者不能互相替代。

模型输出合法 JSON 仍只是候选。`AgentDecisionSecurityPolicy` 会检查工具是否属于当前步骤、版本是否可用、order_id 是否绑定当前 State、policy category 是否匹配，以及终端文本是否含凭证或受保护系统指令。跨订单或隐藏工具决策不会到达 Tool Executor，而是写 Step Trace 和 verification failure 后重新规划；未知工具仍由 Executor 返回 selection error，缺参数仍返回 argument error，保证错误类型准确且 handler 不会误执行。

AgentRunner 和 LangGraph 工作流复用同一安全策略。LangGraph 安全拒绝后清空 pending decision 并 checkpoint，再从 control 回到 plan，因此崩溃恢复也不会重新执行被拒绝动作。Tool Executor 仍检查注册、RBAC、Schema、超时和输出契约；退款仍必须经过 Policy Engine、proposal 版本/哈希、四眼人工审批、执行授权和事后验证。检测器负责减少恶意输入，后端门禁负责保证漏检后仍不能越权或直接退款。

100 个固定合成案例包含 80 个攻击和 20 个正常对照。相对“只写安全 Prompt”基线，正确处理率从 20% 提升到 100%，阻止 40 个恶意片段进入模型、20 次敏感信息暴露和 20 个不安全动作，20 个正常样本误拦截为 0。数据没有调用付费模型或 embedding，只证明当前确定性样本和门禁，不代表能防住所有未知攻击或真实生产安全率。

Day1–Day17 全量 231 个测试通过，61 个 Day17 专项测试包含真实 PostgreSQL、知识 ACL、Context Trace、Grounded Answer、AgentRunner、LangGraph 和迁移回归。迁移头保持 `20260805_0011`，PostgreSQL/Redis、依赖和编译检查均通过。

一句话记忆：外部自然语言永远是 data；先尽量检测和脱敏，再用最小权限、资源绑定、结构化输出、Policy Engine 和人工审批限制它即使骗过模型也无法到达危险 sink。
