# Day21：Agent 任务、轨迹与工具评估

## 业务问题

最终答案正确不等于 Agent 正确。错误订单、未审批退款、跳过证据或无效工具即使被后端拦截，仍说明 Agent 决策有风险。

## 完整主流程

Golden Case 与配置校验 → Baseline/Candidate 使用同一输入执行 → 业务 Case 进入真实 AgentRunner → Planner 决策 → Security Policy 校验 → ToolExecutor 调用 → Observation 更新 State → Completion Verifier 判断能否结束 → 提取稳定 Step 轨迹 → 六维确定性评分 → 汇总分类准确率、失败归因和回归 → 质量门禁决定候选版本是否合格。

## 六维规则

1. 最终结果：状态、终止原因、Outcome、错误码正确。
2. 工具选择：必需工具成功，未尝试集合外工具。
3. 工具参数：订单号、类别、地区严格绑定当前权威资源。
4. 步骤效率：证据完整前提下，总步骤和单工具重试不超预算。
5. Policy 合规：无未审批退款、串单、无关工具或提前结束尝试。
6. 失败归因：正确区分 planner、retriever、tool、context、policy、model。

单 Case 必须六维全部通过。硬安全失败不能被其他高分平均掉。

## 状态与数据变化

Runner 每一步把 Decision 写入 StepTrace；工具真正执行后生成 Observation 并回写 AgentState；安全门禁拦截的 Decision 没有 Observation，但仍保留在轨迹中参与扣分。报告只保存稳定字段，不使用随机 UUID、时间戳和耗时做离线比较。

## 异常与恢复

临时 Tool 依赖错误允许在预算内重试；错误工具、错误参数和 Policy 拒绝不能当作“没发生”。六类错误域帮助定位应该优化 Planner、检索、工具、上下文、Policy 还是模型网关。

## 核心结论

固定 18 个合成案例中，Baseline 最终结果 18/18 正确，但整体轨迹只有 2/18；Candidate 六维与整体均为 18/18。16 条“碰巧答对但过程危险”的案例被成功拒绝，证明轨迹评估比只看最终答案更适合 Agent 工程。

## 与前后 Day 的关系

Day18/19 提供 Trace、Metrics 和 Logs；Day20 提供统一 Harness；Day21 把 Agent 过程变成硬门禁；Day22 再用 LLM Judge 与人工校准评价解释质量等软语义。确定性事实优先用代码规则，不能交给 Judge 猜。

## 证据边界

12 条业务案例运行真实 AgentRunner，6 条失败归因是显式 typed fixture。数据是合成回归集，100% 仅代表通过当前固定门禁，不代表生产准确率。
