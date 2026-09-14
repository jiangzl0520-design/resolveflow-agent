# Day 17 测试、评估与运行证据

## 1. 自动化测试覆盖

### 正常路径

- 合法外部政策证据仍进入 Context，不被安全规则误删；
- 当前步骤合法的 `order_lookup` 正常到达 Tool Executor；
- AgentRunner 与 LangGraph 在安全拒绝后可以重新规划并完成调查；
- tenant、角色 ACL、业务有效期和引用验证保持原有行为；
- 真实 PostgreSQL 的 Context Trace、知识检索、Grounded Answer 和引用链路通过。

### 失败路径

- required 用户 Goal 含直接注入：Context Build failed，模型不调用；
- optional RAG 文档含间接注入：片段被排除，Trace 记录 security reason；
- 政策问题含直接注入：Answer Run `security_blocked`，不检索、不调用模型；
- 恶意/含密钥知识命中：在 rerank 前移除；
- 模型生成含密钥 claim：`grounding_verification_failed`，不返回答案；
- 模型选择当前步骤未开放工具：不产生 Tool Observation；
- 模型复现受保护系统指令：终端输出被拒绝；
- 跨订单工具参数：AgentRunner/LangGraph 均在执行前拒绝。

### 边界路径

- 未知工具仍返回 `selection_error`，不会误分类成 Prompt Injection；
- 缺少普通参数仍返回 `argument_error`，允许 Agent 修正；
- Actor 无权限仍由 Tool Executor 返回 authorization error 并按原流程终止/升级；
- 安全拒绝计入 step/token/repeated-decision 预算，不能无限重试；
- 用户把订单号从 10086 改为 10087 后，旧 Observation 继续按 Day15 规则失效；
- 过期知识先按 `as_of` 移除，ACL 在排序前执行；
- 邮箱、手机号、API Key、JWT 和 token 赋值嵌套在 JSON 中仍被脱敏；
- 20 个正常对照样本没有误拦截。

## 2. Day17 专项测试

```powershell
$env:TEST_DATABASE_URL = `
  'postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test'
.\.venv\Scripts\python.exe -m pytest `
  tests\test_context_security.py `
  tests\test_context_builder.py `
  tests\test_agent_planner.py `
  tests\test_agent_loop.py `
  tests\test_agent_workflow.py `
  tests\test_knowledge_answering.py `
  tests\test_knowledge_retrieval.py `
  tests\test_day17_evaluation.py `
  tests\integration\test_context_engineering_postgres.py `
  tests\integration\test_knowledge_retrieval_postgres.py `
  tests\integration\test_knowledge_answering_postgres.py `
  tests\integration\test_migrations.py -q `
  --basetemp=.pytest_tmp_day17_targeted
```

结果：

```text
61 passed
```

## 3. 固定对抗数据集

数据集：`evaluation/datasets/day17_context_security_v1.json`

脚本：`evaluation/run_day17_context_security.py`

原始报告：`evaluation/reports/day17_context_security_v1_report.json`

数据集版本 `day17-context-security-v1`，明确标记 `synthetic: true`，共 100 例：

- 20 例直接 Prompt Injection；
- 20 例知识文档间接 Prompt Injection；
- 20 例 Context 敏感信息；
- 20 例模型生成的跨资源、隐藏工具或系统指令泄露决策；
- 20 例正常业务内容与合法工具对照。

每类由 5 个固定中英文模板循环生成 20 个带稳定 ID 的案例。评估使用真实 `ContextBuilder`、`UntrustedContentGuard` 和 `AgentDecisionSecurityPolicy`，不使用随机模型输出。

## 4. 基线定义

`prompt_only_baseline` 表示应用只在系统 Prompt 中写“外部内容是不可信数据”，但：

- 不在模型前隔离直接/间接注入；
- 不脱敏秘密和 PII；
- 不在模型输出后绑定当前步骤与订单资源；
- 认为结构化输出合法就可以继续执行。

该基线是确定性的错误架构对照，不代表某个具体模型的真实攻击成功率。最终方案与基线使用同一 100 个样本和同一判定规则。

## 5. 评估真实结果

```text
dataset: day17-context-security-v1
synthetic cases: 100
adversarial cases: 80
benign controls: 20
paid model calls: 0
embedding calls: 0

prompt-only baseline:
  correct handling: 20.00%
  attacks contained: 0
  malicious fragments reaching model: 40
  sensitive exposures: 20
  unsafe actions allowed: 20
  false-positive blocks: 0

layered security:
  correct handling: 100.00%
  attacks contained: 80
  malicious fragments reaching model: 0
  sensitive exposures: 0
  unsafe actions allowed: 0
  false-positive blocks: 0

measured change:
  correct handling: +80.00 percentage points
  additional attacks contained: 80
  malicious model exposures prevented: 40
  sensitive exposures prevented: 20
  unsafe actions prevented: 20
```

复现命令：

```powershell
.\.venv\Scripts\python.exe `
  evaluation\run_day17_context_security.py `
  --output evaluation\reports\day17_context_security_v1_report.json
```

## 6. 这些数字不能证明什么

- 100% 不表示能识别所有未知 Prompt Injection；规则检测存在绕过可能。
- 基线不是某个线上模型，因此不能把 20% 写成真实模型安全率。
- 0 个误拦截只对当前 20 个正常对照成立，不代表生产误报率为 0。
- 没有真实付费模型调用，不能推导 Provider 延迟、Token 成本或模型抗攻击能力。
- 评估证明的是“已知攻击能否被确定性边界限制”，而不是通用网络安全认证。

生产阶段需要持续加入脱敏真实攻击、编码/多语言/多模态变体、工具返回污染、MCP tool poisoning 和人工审核误操作样本。

## 7. 全量回归

```powershell
$env:TEST_DATABASE_URL = `
  'postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test'
.\.venv\Scripts\python.exe -m pytest -q `
  --basetemp=.pytest_tmp_day17_full
```

结果：

```text
231 passed
```

全量回归覆盖 Day1–Day17 的 HTTP、事务、认证、多租户、异步任务、模型网关、工具、Agent Loop、LangGraph checkpoint、退款审批、MCP、知识入库、混合检索、Grounded Answer、Context Builder、长期 Memory 和上下文安全。

## 8. 工程健康检查

```text
compileall:      passed
pip check:       No broken requirements found.
alembic heads:   20260805_0011 (head)
alembic current: 20260805_0011 (head)
alembic check:   No new upgrade operations detected.
PostgreSQL:      healthy
Redis:           healthy
```

Day17 没有新增持久化字段，安全选择复用 Context Trace 的 decision reason 和 Agent Step verification code，因此不需要新迁移。

## 9. 当前明确边界

- 规则检测不是语义安全分类器，对改写、编码、图片和复杂社会工程可能漏检；
- 当前脱敏只覆盖一组常见秘密/PII，不是完整企业 DLP；
- 安全文档讨论可能命中注入规则，需要人工审核或受控豁免机制；
- security-filtered Knowledge Hit 暂未单独持久化逐条安全事件，Day18 全链路 Trace 会扩展；
- 尚未覆盖第三方 MCP tool description poisoning 和工具供应链签名；
- 未执行专门渗透测试，当前结果只来自版本化自动化对抗集。
