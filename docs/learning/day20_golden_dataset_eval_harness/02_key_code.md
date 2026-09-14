# Day20 关键代码

## 1. Golden Dataset 和 Run Config 契约

位置：`evaluation/harness/models.py:14`、`evaluation/harness/models.py:25`、`evaluation/harness/models.py:49`

**输入输出**：JSON 文档输入，Pydantic 产生不可变 `GoldenDataset/GoldenCase/RunConfig`；格式错误时不产生可执行对象。

**执行流程**：`load_dataset()` 解析 JSON，先扫描常见敏感信息，再校验 schema、case ID 唯一性和四类案例完整性；`load_config()` 独立校验运行配置。

**为什么这样写**：Dataset 变更和运行阈值变更是两类不同审计事件，不应写在脚本常量里。`extra="forbid"` 防止拼错字段被静默忽略。

**架构位置**：这是 Eval 的输入协议层，不依赖具体 Agent Runtime。

**状态变化**：原 JSON 不被修改；成功后得到验证对象和后续用于指纹的规范数据。

**异常处理**：文件/JSON/schema/隐私任一门禁失败都转为稳定 `DatasetValidationError` 或 `RunConfigurationError`，Subject 不会被调用。

## 2. EvalHarness 主编排

位置：`evaluation/harness/core.py:37`、`evaluation/harness/core.py:65`

**输入**：已校验 Dataset、RunConfig、Baseline Subject 和 Candidate Subject。

**输出**：`EvalReport`，包含 Dataset/Config 指纹、两个 Subject 汇总、分类分数、improvement/regression、质量门禁和逐案例原始结果。

**执行流程**：

1. 对 Dataset 和 Config 作规范 JSON SHA-256；
2. 按案例顺序各执行一次 baseline/candidate；
3. 用 expected-field-match 分别判分；
4. 汇总四类和总体准确率；
5. 计算 improvement/regression 和准确率差；
6. 同时检查最低准确率和最大回退数；
7. 用两个指纹派生稳定 run ID。

**为什么这样写**：Harness 只编排和汇总，Subject 用 Protocol 注入，所以 Day21 可以把真实 Agent 适配进来，无需重写 Dataset、Report 和 CLI。

**架构位置**：离线评估应用层；它不在生产请求链路内，不修改 AgentState 或业务数据。

**状态变化**：每个 case 从未执行变为两份 `SubjectCaseResult`，再聚合为 Category/Subject Summary 和 Report；只有 `write_report()` 会写文件。

**异常处理**：Subject 异常在 case 内被隔离，不保存异常原文；非 dict/JSON 输出变为稳定 error code；Dataset/Config/报告写入属于整次运行级失败。

## 3. 单案例隔离与脱敏

位置：`evaluation/harness/core.py:167`

`_execute_subject()` 的顺序很重要：

1. 执行 Subject；
2. 检查输出是 dict；
3. 使用原输出和 expected 判分；
4. 对将写入报告的 actual 递归脱敏；
5. 验证脱敏后对象可 JSON 序列化；
6. 只保存安全 actual、passed 和稳定 error code。

先判分再脱敏，是为了避免 expected 本来需要检查“模型是否泄露敏感值”时，脱敏器提前修改输出导致误判。但 Day20 的基础 Evaluator 还不会独立给泄露行为打安全分；这属于 Day21 的多维度轨迹/安全评估。

## 4. 汇总与质量门禁

位置：`evaluation/harness/core.py:210`

`_summarize()` 按固定 `CaseCategory` 顺序产生每类 passed/total/accuracy，再给出总体数据。不使用模型自己的置信度代替判分。

`exit_code_for()` 只有在质量门禁通过时返回 0，所以 CI 不需要解析中文报告文本。总分达标但存在超限 regression 仍然失败。

## 5. 版本化运行配置与 CLI

位置：`evaluation/configs/day20_offline_v1.json`、`evaluation/run_day20_harness.py:22`、`evaluation/run_day20_harness.py:52`

完整命令：

```powershell
.\.venv\Scripts\python.exe -m evaluation.run_day20_harness `
  --dataset evaluation\datasets\day20_golden_v1.json `
  --config evaluation\configs\day20_offline_v1.json `
  --output evaluation\reports\day20_golden_v1_report.json
```

CLI 只负责适配命令行参数、调用 Harness、写报告和返回稳定退出码。评分规则不写在 CLI 里，所以自动化测试和其他调用方可直接复用 Harness。

## 6. Day20 合成 Subjects

位置：`evaluation/day20_subjects.py:6`、`evaluation/day20_subjects.py:21`

`KeywordBaseline` 故意只根据用户/外部文本的退款关键词和少量物流状态判断；`TrustedFactCandidate` 只根据结构化支付、订单、物流、proof 和 dependency status 判断，忽略 untrusted instruction。

它们是验证 Harness 的确定性合成被测对象，不是完整 Agent，也不应该用它们的分数宣称 ResolveFlow Agent 已达到 100% 准确率。它们的价值是给 Harness 一个结果已知、差异明确的校准对象。
