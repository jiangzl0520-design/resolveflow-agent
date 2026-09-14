# Day 2 面试记录

## 是否进行面试

不进行后端面试。

Day 2 新增内容是 PostgreSQL、SQLAlchemy、Session、事务、Alembic、Docker Compose 和存储异常处理，属于后端基础能力。根据 `AGENTS.md` 的最新规则，后端知识由 Codex 直接讲解、实现并通过自动化测试验证，不作为面试问题。

## 与 Agent 的关系

`agent_runs` 表为未来 AgentState、当前节点、运行状态和版本持久化提供基础，但 Day 2 没有实现 Agent Loop、checkpoint 写入、暂停恢复或新的 Agent 决策逻辑，因此不能为了完成面试流程而提出后端替代题。

后续真正实现 Agent Runtime、Context、工具调用或 checkpoint 时，再针对新增的 Agent 原理进行面试验收。
