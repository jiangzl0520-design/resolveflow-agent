# Day20：Golden Dataset 与统一 Eval Harness

## 业务问题

分散的单日评估难以保证基线和候选使用同一数据、同一判定标准和可追溯配置。Day20 建立统一 Harness，使后续 Agent/Prompt/Model/Tool/Context 优化都能用一条命令重现并比较。

## 完整操作流程

1. Harness 读取版本化 Golden Dataset，在执行前校验 schema、重复 ID、四类案例完整性和敏感数据。
2. 独立 Run Config 锁定 Harness/Evaluator/Baseline/Candidate 版本、seed、最低准确率和最大回退数。
3. Dataset 和 Config 规范序列化后计算 SHA-256，两者共同派生稳定 run ID。
4. 同一 case 分别交给 Baseline 和 Candidate，使用同一 expected-field Evaluator 判定。
5. 单个 Subject 异常只影响当前 case，异常原文不持久化；actual 判分后脱敏并检查 JSON 契约。
6. Harness 按 normal/boundary/failure/adversarial 和总体汇总，计算 improvement、regression 和准确率差。
7. Candidate 准确率≥95% 且 regression=0 才通过质量门禁。
8. CLI 保存逐案例 Report；成功/门禁失败/输入无效/持久化失败分别返回 0/1/2/3，CI 可直接判定。

## 核心原则与边界

- Golden Dataset 是经审核、长期保留的判定基准，不是临时 fixture；内容变更必须改变指纹并按语义版本管理。
- Dataset 决定考题，Subject 决定被测系统，Evaluator 决定判卷规则，Config 锁定本次实验，Report 保存事实。任一者版本变化都可能改变分数。
- 总分提升不能掩盖高风险 regression，所以门禁同时检查总准确率和回退数。
- Day20 Evaluator 只检查最终输出的 expected 字段；最终答案碰巧正确但轨迹危险的检查属于 Day21。

## 量化与测试证据

20 个明确标记的合成案例中，关键词 Baseline 通过 2/20（10%），可信结构化事实 Candidate 通过 20/20（100%），18 个 improvement、0 regression，差值 90 个百分点。两个 Subject 都是为校准 Harness 故意设计的确定性合成对象，这不是真实 Agent 质量或生产收益。

专项测试 11/11 通过。全量收集 258 个测试，243 通过、15 跳过、0 失败。CLI 真实返回 0 并生成含 20 条逐案例结果的报告。

## 与前后 Day 的关系

- Day18/19 提供单次 Trace 和线上聚合 Metric；Day20 提供版本更改前的离线回归基础设施。
- Day21 将把真实 Agent 的最终状态、Tool 选择/参数、步数效率、轨迹和 Policy 合规接入这个 Harness，避免“答案碰巧正确”被判为成功。
