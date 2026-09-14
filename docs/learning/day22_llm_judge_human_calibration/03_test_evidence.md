# Day22：测试与评估证据

## 1. 专项与相关回归

执行命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_day22_evaluation.py tests\test_day21_evaluation.py tests\test_model_gateway.py -q --basetemp=.pytest_day22
```

结果：`27 passed`，无失败。

覆盖内容：

- 8 个 Case 覆盖 normal、boundary、failure、adversarial；
- 每个 Case 有两个不同的盲化人工标注 Token；
- Rubric 只包含四个软语义维度并明确排除硬安全事实；
- Judge Payload 不含 Baseline/Candidate 身份；
- Judge 通过真实 ModelGateway 和结构化 Schema 调用；
- 偏好与分数冲突时输出被拒绝；
- 人工一致性、Judge/人工一致性、顺序一致性和分数误差可计算；
- A/B 交换能暴露指定顺序敏感案例；
- Judge 满分不能覆盖失败的硬门禁；
- 报告可重复，16 次 Judge 调用全部记录；
- 敏感校准数据在加载阶段被拒绝；
- CLI 写入报告并使用稳定退出码；
- Day21 与 ModelGateway 原有行为没有回归。

## 2. 固定校准报告

报告：`evaluation/reports/day22_judge_calibration_v1_report.json`

| 指标 | 结果 | 门槛 | 状态 |
|---|---:|---:|---|
| 人工偏好一致率 | 87.5% | 仅观测并裁决 | 已保留分歧 |
| Judge/人工偏好一致率 | 87.5% | ≥80% | 通过 |
| A/B 顺序一致率 | 87.5% | ≥80% | 通过 |
| 顺序敏感率 | 12.5% | ≤20% | 通过但需关注 |
| 分数平均绝对误差 | 0.125 | 观测 | 已记录 |
| 人工分数 ±1 命中率 | 100% | ≥90% | 通过 |
| Candidate/Baseline 四维平均分 | 3.4375 / 2.25 | 合成对照 | +1.1875 |
| 发布决策正确率 | 100% | 100% | 通过 |
| Judge 覆盖硬门禁 | 0 次成功 | 必须为 0 | 通过 |

组合质量门禁通过。一个语义满分但硬门禁失败的控制 Case 被正确拒绝。

## 3. 全量回归

执行命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -W error --basetemp=.pytest_day22_full
```

结果：`266 passed, 15 skipped in 31.97s`，无失败、无警告。15 项跳过是需要显式 PostgreSQL 集成环境的既有测试。

后续 Day 修改代码后必须重新运行，不能把当前结果当作永久证明。

## 4. 证据边界

- Judge 输出来自确定性 Fake Provider，证明的是 ModelGateway、Schema、盲化、校准计算和门禁组合，不是某个线上模型的真实评价能力。
- 8 个双人标签是合成小样本，不能代表生产人工共识。
- 两次顺序交换只能检测顺序敏感，不能穷尽所有偏差。
- 100% 发布决策正确率只针对 8 个固定控制样本。
- 真实上线前必须使用脱敏生产样本、互不知情的真实标注者和目标 Judge 模型重新校准。
