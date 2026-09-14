# ResolveFlow 原理总结目录

本目录用于在每个 Day 完成后保存一份“简明扼要但流程完整”的快速复习稿。详细原理、关键代码和测试原始证据仍位于 `docs/learning/`。

## 已完成内容

- [Day 00：Agent 项目基础认知](day00_agent_foundation.md)
- [Day 01：需求建模与 HTTP 主链路](day01_http_main_flow.md)
- [Day 02：PostgreSQL、ORM 与迁移](day02_postgresql_orm_migrations.md)
- [Day 03：状态机、幂等、乐观锁与审计](day03_consistency_events.md)
- [Day 04：认证、RBAC 与多租户](day04_auth_rbac_multitenancy.md)
- [Day 05：异步任务、重试、取消与恢复](day05_async_jobs_recovery.md)
- [Day 06：模型网关与结构化输出](day06_model_gateway_structured_output.md)
- [Day 07：工具契约、注册中心与 Observation](day07_tool_contracts_registry.md)
- [Day 08：最小 Agent Loop](day08_minimal_agent_loop.md)
- [Day 09：LangGraph 与 checkpoint](day09_langgraph_checkpoint.md)
- [Day 10：退款 Policy、人工审批与安全写操作](day10_refund_policy_human_approval.md)
- [Day 11：MCP 工具服务化](day11_mcp_tool_service.md)
- [Day 12：可追溯知识入库](day12_knowledge_ingestion.md)
- [Day 13：安全、可观测的混合检索](day13_hybrid_retrieval.md)
- [Day 14：重排、冲突检测与可验证引用](day14_grounded_answers.md)
- [Day 15：Context Builder 与 Token 预算](day15_context_builder.md)
- [Day 16：短期状态与策略控制的长期记忆](day16_long_term_memory.md)
- [Day 17：上下文安全与 Prompt Injection 分层防御](day17_context_security.md)
- [Day 18：OpenTelemetry 全链路 Trace](day18_distributed_tracing.md)
- [Day 19：Metrics、Logs、Dashboard 与故障归因](day19_metrics_logs_dashboard.md)
- [Day 20：Golden Dataset 与统一 Eval Harness](day20_golden_dataset_eval_harness.md)
- [Day 21：Agent 任务、轨迹与工具评估](day21_agent_trajectory_evaluation.md)
- [Day 22：LLM Judge 与人工校准](day22_llm_judge_human_calibration.md)

## 固定结构

每份总结按以下顺序编写：业务问题、完整主流程、各层职责、状态与数据变化、失败恢复、设计原则、测试证据、与前后 Day 的关系。
