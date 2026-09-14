# Day 15：Context Builder 与 Token 预算

一次 Agent 决策开始时，Planner 收到当前 Agent State 和 Actor 有权使用的工具。系统规则、当前目标、完成条件、状态、用户最新修改、Tool Observation、RAG、History、Memory 和工具描述先被翻译成独立候选片段，每个片段都标明来源、可信度、优先级、相关性、新鲜度、是否必需以及预先排除原因。

Context Builder 先排除越权、失效和当前步骤无关的片段；同一语义事实只保留最新版本，再去重并过滤低相关内容。系统规则、当前目标、完成条件和最小 State 属于 required，必须先进入 Context。订单号从 10086 改成 10087 时，History 保留修改过程，但当前 State 以 10087 为准，依赖 10086 的订单和物流 Observation 标记失效，下一步重新查询 10087，旧方案和审批不能复用。

Token 预算不是只限制输入文本。系统要从模型上下文窗口中预留输出 Token、reasoning Token 和安全余量，再与配置的最大输入取较小值。项目优先使用目标模型 tokenizer 计算“系统指令 + 运行数据 + 输出 Schema”的完整 Token；编码资源不可用时用 UTF-8 字节数作为不会低估 Token 的本地保守上界，代价是可能少装入可选片段，但网络或缓存故障不会阻断 Agent 初始化。required 放不下时先记录失败再停止模型调用，不能为了继续运行而静默丢掉安全规则或当前目标；可选片段放不下时记录明确的 `token_budget_exceeded`。

工具描述也属于 Context。没有订单事实时只暴露订单查询；订单确认后暴露仍缺失的物流和政策工具，两个都缺失时由模型选择先查哪一个；证据齐全或订单已经没有可退款付款时不再发送无意义的读取工具。确定性代码负责限定合法、有用的能力集合，LLM 仍根据目标、Observation 和失败反馈选择下一步，Tool Executor、Verifier、Policy Engine 和人工审批继续负责硬性门禁。

选择完成后，稳定系统规则进入 `instructions`，其余带来源标签的数据进入 `<agent_runtime_data>`。系统先把 Context Build Run 和每个片段“进入/丢弃的原因”写入 PostgreSQL，再调用 Model Gateway；Model Call 通过 `context_build_id` 与本轮 Context 相连。Trace 只保存内容哈希和选择元数据，不复制客户原文。这样既能回答模型为什么看到或没看到某条信息，也降低审计表泄露原文的风险。

60 个版本化合成案例的可复现实验中，关键片段完整保留率从 66.67% 提升到 100%，陈旧片段误选择从 40 次降到 0，动态工具描述精确率从 33.33% 提升到 100%，平均输入 Token 从 463.67 降到 370.35（-20.13%）。这些数字只证明当前数据和预算下的上下文选择机制，不代表生产模型质量或线上业务收益。真实 PostgreSQL 外键链路已通过，Day1–Day15 全量自动化测试为 202 passed。
