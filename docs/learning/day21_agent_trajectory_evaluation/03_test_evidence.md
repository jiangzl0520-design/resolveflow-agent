# Day21：测试与评估证据

## 1. 专项与相关回归

执行命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_day21_evaluation.py tests\test_eval_harness.py tests\test_agent_loop.py -q --basetemp=.pytest_day21
```

结果：`35 passed`，无失败。

覆盖内容：

- 数据集同时包含 normal、boundary、failure、adversarial；
- failure 覆盖 planner、retriever、tool、context、policy、model；
- 六个评分维度分别可以独立触发失败；
- 最终答案正确但危险轨迹仍失败；
- 真实 AgentRunner 轨迹与 typed failure fixture 有显式区分；
- 临时 Tool 依赖故障只重试一次后成功；
- 已退款订单只查订单，不再查物流或 Policy；
- 相同数据和配置重复运行得到相同报告；
- CLI 写入报告并使用稳定退出码；
- Day20 默认字段评分与现有 AgentLoop 测试无回归。

## 2. 固定评估结果

数据集：`evaluation/datasets/day21_agent_trajectory_v1.json`

配置：`evaluation/configs/day21_trajectory_v1.json`

报告：`evaluation/reports/day21_agent_trajectory_v1_report.json`

结果：

| 指标 | Baseline | Candidate |
|---|---:|---:|
| 最终结果维度 | 18/18 | 18/18 |
| 整体轨迹通过 | 2/18 | 18/18 |
| 整体准确率 | 11.11% | 100% |
| 相对提升 | - | 88.89 个百分点 |
| 回归案例 | - | 0 |
| 质量门禁 | - | 通过 |

Baseline 中有 16 个 `lucky_final_but_unsafe_cases`。这 16 个 Case 的最终字段完全正确，但至少一个过程维度失败。

## 3. 正常、异常与边界证据

- 正常：在途、已签收、无签收证明、物流异常均按真实 AgentLoop 收集证据并结束。
- 边界：未付款和已取消在一次订单查询后停止；已退款不会进入第二次退款路径；依赖故障允许有限重试。
- 对抗：直接退款、旧订单号、跳过证据和虚构工具指令都不能改变 Candidate 的安全路径。
- 异常：六类稳定错误码分别映射到六个 failure domain；错误归因 Baseline 即使最终错误码正确也不能通过。

## 4. 全量回归

执行命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -W error --basetemp=.pytest_day21_full
```

结果：`256 passed, 15 skipped in 32.57s`，无失败、无警告。15 项跳过是需要显式 PostgreSQL 集成环境的既有测试，不是 Day21 新增跳过。

若后续 Day 修改代码，应以新的全量结果为准，不能把本次结果当作永久证明。

## 5. 证据的适用边界

- 18 条是合成回归集，不代表生产准确率或真实用户收益。
- 12 条业务轨迹使用真实 AgentRunner；6 条 failure domain 使用 typed fixture 验证分类接口。
- Planner 是确定性的，目的是隔离 Evaluator；真实模型泛化和解释质量需要更大的生产脱敏集、Day22 Judge 与人工标注验证。
- 本次 100% 只能表述为“候选实现通过当前固定合成门禁集”，不能表述为“线上 Agent 100% 正确”。
