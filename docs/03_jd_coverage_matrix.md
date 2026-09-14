# JD 能力覆盖矩阵

这张表用于防止“学过很多名词，却没有工程证据”。最终每一行都必须链接到代码、测试、Trace 或评估报告。

| JD 能力 | 本项目对应模块 | 面试时展示的证据 |
|---|---|---|
| 真实业务建模 | 售后调查、退款审批、执行后验证 | 用例、状态机、业务指标 |
| Agent 规划与工具调用 | Planner/Executor/Verifier、Tool Registry | 动态轨迹、工具准确率 |
| 上下文工程 | Context Builder、预算、压缩、来源与优先级 | 上下文清单、长对话回归测试 |
| RAG | 入库、混合召回、ACL、rerank、引用 | Recall@k/MRR、引用评估报告 |
| Memory | 状态/摘要/长期事实分层，过期与冲突 | 跨会话测试、删除与冲突案例 |
| MCP/协议 | 独立 MCP 工具服务与客户端适配 | 契约测试、故障降级演示 |
| 后端工程 | FastAPI、Postgres、Redis、Worker、事务 | OpenAPI、迁移、并发/幂等测试 |
| 高可靠 | checkpoint、重试、取消、断路、降级 | 故障注入与恢复报告 |
| 安全可控 | RBAC、租户隔离、风险策略、Prompt injection 防护 | 越权/注入测试、审计日志 |
| 人机协同 | 审批、拒绝、修改、接管、恢复 | 暂停恢复演示、审批指标 |
| 可观测性 | OTel Trace、Metrics、Logs、版本记录 | Trace 回放、Grafana 面板 |
| 自动化评估 | Golden Dataset、规则评分、LLM Judge | 基线对比、发布门禁 |
| 数据飞轮 | 失败归因、人工标注、回归集更新 | 一次完整失败→改进记录 |
| 成本与性能 | Token 预算、缓存、背压、压测 | 成本/延迟分解和压测报告 |
| Prompt/模型迭代 | Prompt registry、Provider gateway、版本实验 | 可复现实验记录 |
| 多 Agent | 不默认使用；评估证明需要时才拆子图 | 选型 ADR 和单/多 Agent 对照 |
| 后训练/RL | 学原理和数据接口，不在 30 天伪造训练成果 | trajectory/reward schema 与适用边界说明 |

## 面试岗位适配

### AI 应用/Agent 工程师

这是项目的主要目标岗位。重点讲业务闭环、Agent loop、上下文、工具、安全、评估和效果迭代。

### AI 后端/平台工程师

重点讲 API/Worker 解耦、事务、幂等、checkpoint、可观测性、并发、降级和部署。

### RAG 工程师

重点讲 ingestion、混合召回、重排、ACL、时效性、引用、检索评估和错误归因。

### 偏算法/后训练岗位

本项目只能覆盖数据构建、trajectory、reward/eval 接口和 LLM 基础。若 JD 强制要求 PyTorch、SFT、DPO、RLHF、vLLM 或分布式训练，需要在本项目完成后单独补强，不能声称一个应用项目已经完全覆盖。

