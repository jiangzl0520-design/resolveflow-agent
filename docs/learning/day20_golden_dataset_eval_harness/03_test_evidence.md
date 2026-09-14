# Day20 测试与量化证据

## 1. 专项自动化测试

文件：

- `tests/test_eval_harness.py`：9 个测试；
- `tests/test_day20_evaluation.py`：2 个测试。

测试覆盖：

- 正常：20 个案例被同时交给 baseline/candidate，生成分类汇总和逐案例结果；
- 失败：数据集结构错误在 Subject 运行前拒绝，Subject 抛异常只使对应 case 失败；
- 边界：四类案例必须齐全，case ID 必须唯一，最低准确率和 regression 门禁同时生效；
- 安全：Dataset 中的邮箱使整体拒绝，Subject 异常原文不持久化，actual 内的邮箱/密码在报告前脱敏；
- 可重复：相同 Dataset+Config 的两次 Report、run ID、Dataset/Config 指纹完全一致；
- CLI：成功运行返回 0 并写报告，无效阈值返回 2。

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/test_eval_harness.py `
  tests/test_day20_evaluation.py `
  --basetemp=.pytest_day20_harness4 -q
```

真实结果：11/11 通过，0 失败。

## 2. 全量回归

```powershell
.\.venv\Scripts\python.exe -m pytest `
  --basetemp=.pytest_day20_full -q
```

真实结果：退出码 0；收集 258 个测试，243 个通过，15 个需要额外 PostgreSQL 环境的测试跳过，0 失败。

## 3. 一条命令执行证据

```powershell
.\.venv\Scripts\python.exe -m evaluation.run_day20_harness `
  --output evaluation\reports\day20_golden_v1_report.json
```

真实结果：

- 进程退出码：0；
- 报告文件：`evaluation/reports/day20_golden_v1_report.json`；
- report schema：1.0；
- run ID：`be0b480e54763a3d43392844`；
- dataset：`resolveflow_after_sales_golden@1.0.0`；
- dataset SHA-256：`bad33be99d23f31c04d01bb8cd6717a205e74db684b25b044eb7081bece3d745`；
- config SHA-256：`0445bd052edeff8432a80af1451ce54b019a86dc8111fa21fb53b6e832f59e61`。

数据集、配置、脚本和报告的关系：

- Dataset：`evaluation/datasets/day20_golden_v1.json`；
- Run Config：`evaluation/configs/day20_offline_v1.json`；
- CLI：`evaluation/run_day20_harness.py`；
- Report：`evaluation/reports/day20_golden_v1_report.json`。

## 4. 固定合成对照结果

20 个案例：normal、boundary、failure、adversarial 各 5 个。

| 指标 | Keyword Baseline | Trusted-Fact Candidate |
|---|---:|---:|
| 总通过 | 2/20 | 20/20 |
| 总准确率 | 10% | 100% |
| normal | 2/5 | 5/5 |
| boundary | 0/5 | 5/5 |
| failure | 0/5 | 5/5 |
| adversarial | 0/5 | 5/5 |
| improvements | — | 18 |
| regressions | — | 0 |
| 质量门禁 | 不适用 | 通过 |

实测差值：90 个百分点。Dataset 和两个 Subject 都是为验证 Harness 而编写的确定性合成对象，所以该数据只证明：

1. Harness 能使用同一数据和评估标准比较基线/候选；
2. Harness 能保存原始结果、分类差异、improvement 和 regression；
3. Harness 能用门禁和退出码自动阻止不达标版本。

它不证明真实 ResolveFlow Agent 准确率为 100%，也不证明生产价值提升 90 个百分点。真实 Agent 轨迹评估属于 Day21。
