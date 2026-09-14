# Day21：关键代码与架构位置

## 1. Harness 的评分器扩展点

核心接口位于 `evaluation/harness/core.py`：

```python
class CaseEvaluator(Protocol):
    def evaluate(self, case: GoldenCase, actual: dict[str, Any]) -> bool: ...
```

解释：Day20 的 Harness 原来只会比较 Expected 字段。现在 Harness 仍负责数据、隔离执行、汇总和质量门禁，但“什么叫通过”由 Evaluator 注入。旧的 `ExpectedFieldEvaluator` 保持默认行为，所以 Day20 与更早评估不用重写；Day21 注入轨迹评分器。这是开闭原则在评估架构中的具体应用。

## 2. 六维确定性评分

核心代码位于 `evaluation/day21_trajectory.py` 的 `TrajectoryRuleEvaluator.assess()`：

```python
failures = {
    "task_outcome": outcome_failures,
    "tool_selection": selection_failures,
    "tool_arguments": argument_failures,
    "step_efficiency": step_failures,
    "policy_compliance": policy_violations,
    "failure_attribution": attribution_failures,
}
return {
    "overall_passed": all(
        item["passed"] for item in dimensions.values()
    ),
    "dimensions": dimensions,
}
```

解释：每个维度不仅返回布尔值，还返回具体 violation，报告可以回答“为什么失败”。`overall_passed` 使用 AND，确保硬安全失败不能被其他分数抵消。Case 内声明 required/allowed tools、精确参数和预算，评分器本身不写死某个订单。

## 3. 真实 AgentRunner 适配器

`ResolveFlowAgentSubject` 对 12 个业务案例调用 `_run_agent_case()`，其中装配了真实的 Tool Registry、ToolExecutor、AgentRunner、Security Policy 和 Completion Verifier：

```python
runner = AgentRunner(
    planner,
    registry,
    executor,
    budget=AgentBudget(max_steps=8, max_same_decision_attempts=2),
    wall_clock=lambda: OBSERVED_AT,
)
state = runner.run(AgentRunCommand(...))
```

解释：Evaluator 看到的不是手工拼出来的理想步骤，而是生产 Agent 循环实际留下的 `AgentRunState`。固定时钟只用于消除报告的非确定性；随机运行 ID、真实耗时等不参与评分。ToolExecutor 在 `finally` 中关闭线程池，避免测试资源泄漏。

## 4. 从 Agent State 提取稳定轨迹

`_state_result()` 遍历 `state.steps`，再用 `tool_call_id` 关联 `state.observations`：

```python
attempted_tools.append({
    "name": decision.tool_name,
    "arguments": decision.arguments.as_tool_arguments(),
    "executed": observation is not None,
    "status": observation.status.value if observation else "blocked",
    "error_kind": (
        observation.error.kind.value
        if observation and observation.error else None
    ),
})
```

解释：`executed=false` 代表决策已产生但被 Runner 的安全门禁拦截；它仍必须进入评估，否则后端越安全，越容易把模型的危险选择隐藏掉。Observation 的错误分类则区分选择错误、参数错误、依赖错误等真实执行结果。

## 5. “最终正确但轨迹危险”的 Baseline

`PreludePlanner` 只在第一步插入一次错误动作，然后委托给安全 Planner。错误动作包括提前结束、未知工具、错误订单、无审批退款和无关物流查询。

解释：这种设计控制了实验变量。Baseline 与 Candidate 后续使用相同 AgentRunner 和证据驱动流程，二者最终结果一致；评分差异来自轨迹规则，而不是来自两套完全不同的业务实现。

## 6. 已退款订单的完成校验修复

`app/agent/verifier.py` 把 `OrderStatus.REFUNDED` 加入不可再次退款的终态集合：

```python
if order.status in {
    OrderStatus.PENDING_PAYMENT,
    OrderStatus.CANCELLED,
    OrderStatus.REFUNDED,
}:
```

解释：若不加入，Verifier 会继续要求物流与 Policy，并可能再次进入退款相关调查。这个修复让 Agent 状态机、上下文工具裁剪逻辑和完成校验对“已退款是终态”保持一致，防止重复退款路径。

## 7. 报告增强

`evaluation/run_day21_trajectory.py` 在统一 Harness 报告上增加：

- 六个维度各自的通过数、总数和准确率；
- 每个 Case 的 violation；
- 失败域分布；
- `lucky_final_but_unsafe_cases` 清单。

解释：核心 Harness 的稳定报告模型不被 Day21 专属字段污染；Day21 Runner 负责生成领域增强报告。运行配置和数据集仍由 Harness 计算 SHA-256 指纹，保证报告能定位到精确输入版本。

## 8. 六类失败 fixture 的代码边界

归因 Case 使用 `execution_mode="typed_failure_fixture"`，正常业务 Case 使用 `execution_mode="agent_runner"`。

解释：显式标注防止把接口级归因测试包装成生产端到端故障注入。后续若把每个组件的真实故障注入接进 Candidate，只需替换 Subject 执行方式，Evaluator 与 Golden 标注无需改变。
