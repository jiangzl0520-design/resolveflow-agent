# Day20 原理：Golden Dataset 与统一 Eval Harness

## 1. 今天解决的问题

前 19 天的每个能力都有自己的评估脚本，但它们的数据结构、版本字段、运行参数、结果格式和失败行为不完全一致。这会造成三个真实工程问题：

1. Prompt、Model 或 Policy 更改前后可能不是在同一批案例、同一标准下比较；
2. 评估中某个案例抛异常时，整批运行中断，看不到其他失败分布；
3. 报告没有数据集、被测系统、评估器和运行配置指纹，数字无法重现。

Day20 建立固定主流程：

```text
版本化 Golden Dataset
        ↓ 读取+完整校验+去隐私检查
版本化 Run Config
        ↓
同一 Case 分别执行 Baseline 和 Candidate
        ↓ 单案例异常隔离
同一 Evaluator 判定两份输出
        ↓
逐案例结果 → 分类汇总 → 差值/回退
        ↓
持久化 Report → CI 质量门禁退出码
```

## 2. Golden Dataset 不是普通测试数据

普通 fixture 常常只是为某段代码构造输入；Golden Dataset 是被人工审核、长期保留、用于比较多个系统版本的判定基准。它至少需要：

- 稳定 `case_id`：用于判断哪些案例改善或回退；
- `category`：区分 normal、boundary、failure、adversarial；
- 输入和经审核的 expected；
- 数据集 ID、语义版本、schema 版本、task type 和合成/真实标记；
- 内容 SHA-256：即使有人不改版本号却修改案例，报告也能发现数据已变。

Day20 在执行被测系统前完成整体校验：额外字段、版本格式错误、重复 ID、空输入/期望、缺任一类案例都会使整次运行失败。这不是“一个 case 没通过”，而是“评估本身不可信”，所以不允许继续出分。

## 3. 四类案例之间的关系

| 类别 | 检查的问题 | Day20 例子 |
|---|---|---|
| normal | 常见业务是否完成 | 运输中继续跟踪，已签收无争议结单 |
| boundary | 临界状态是否被错当成正常或失败 | 未付款、已取消、已退款、proof `null` 与 `false` |
| failure | 依赖失败时是否正确重试、转人工或失效关闭 | timeout、unavailable、malformed Observation |
| adversarial | 敌对内容是否能绕过状态、证据和策略 | 文档要求跳过 proof 直接退款 |

只有 normal 会产生“幸福路径偏差”；只有 adversarial 会看不到普通故障恢复；四类必须同时存在。但“每类 5 条”只是 Day20 Harness 的合成验收规模，不表示已覆盖真实业务长尾。

## 4. 数据集、运行配置、被测系统和评估器为什么要分开

- **Dataset** 回答“考什么”；
- **Subject/Executor** 回答“被测的哪个系统版本怎样执行”；
- **Evaluator** 回答“怎样判定对错”；
- **Run Config** 锁定 Harness/Evaluator/Subject 版本、seed 和质量门禁；
- **Report** 保存“这些确定版本实际得到什么”。

如果更换 Evaluator 后分数提升，不一定是 Agent 变好，可能只是判卷规则变宽。因此报告同时保存 `dataset_sha256`、`config_sha256`、baseline/candidate subject 版本和 evaluator 版本。

## 5. 公平的 Baseline 对比

基线和候选必须使用：

1. 同一个 Dataset 内容指纹；
2. 同一案例顺序；
3. 同一 seed 和运行参数；
4. 同一 Evaluator 及版本；
5. 同一汇总方法。

Harness 保存每个 `case_id` 的 baseline/candidate actual、passed 和 error code，所以除了总分差，还能分出：

- improvement：基线错、候选对；
- regression：基线对、候选错；
- 两者都错：需要继续定位，不能被总分提升掩盖。

当前门禁是 candidate accuracy 至少 95% 且 regression 为 0。两个条件必须同时满足：否则一个大数据集的总分提升可能掩盖少量但高风险的退款回退。

## 6. 可重复执行不只是“脚本能再跑一次”

Day20 为了使相同 Dataset+Config 产生完全一致的报告，做了以下限制：

- 输入使用规范 JSON 排序后计算 SHA-256；
- `run_id` 由 Dataset 和 Config 指纹确定性派生；
- 报告不写随机 UUID 和当前时间；
- 案例按 Dataset 顺序执行；
- 当前合成 Subject 是确定性代码，seed 仍作为契约保存。

当后续接入真实 LLM 时，即使 temperature/seed 固定也可能有非确定性。那时“可重复”应理解为配置和输入可追溯、统计在容差内稳定，不能保证逐字节一致。

## 7. 案例异常与评估基础设施失败必须分开

- **Dataset/Config 无效**：评估本身无效，执行前终止，CLI 返回 2；
- **单个 Subject 异常**：只将该案例记为 `subject_unclassified_error`，继续其他案例；
- **Subject 输出非 JSON**：记为 `subject_output_not_json`，不让报告持久化在最后才崩溃；
- **报告写入失败**：这是基础设施失败，CLI 返回 3；
- **质量门禁未通过**：评估正常完成但候选版本不可发布，CLI 返回 1；
- **全部通过**：CLI 返回 0。

单案例异常不保存原始 exception message，防止凭证、PII 或 Prompt 泄露。Subject actual 在判分后、写报告前再次递归脱敏。Dataset 若检测到常见密钥、JWT、邮箱或手机号，在运行前整体拒绝，必须先去标识化。

## 8. Day20 实测结果和不能误读的部分

Day20 使用 20 个明确标记的合成案例，每类 5 个：

| Subject | 通过 | 准确率 |
|---|---:|---:|
| 仅根据用户文本关键词判断的合成 Baseline | 2/20 | 10% |
| 只使用可信结构化事实的合成 Candidate | 20/20 | 100% |

差值为 90 个百分点，18 个 improvement，0 regression。这个巨大差值是为了验证 Harness 能否正确比较两个故意设计成不同的确定性 Subject，**不是真实 Agent 提升，也不是生产业务收益**。Day21 才会把真实 Agent 最终状态、Tool 选择/参数、轨迹和 Policy 合规接入这个 Harness。
