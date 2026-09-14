# Day 6 测试、评估与运行证据

## 1. 环境

- 日期：2026-07-24
- Python：3.12.13
- OpenAI Python SDK：2.46.0
- SQLAlchemy：2.0.51
- Alembic：1.18.5
- PostgreSQL：18.3-alpine
- 操作系统：Windows 11
- Day 6 Alembic head：`20260724_0006`

当前没有配置或请求用户提供 OpenAI API Key，没有发起真实收费模型调用。真实模型的语义质量、延迟、Token 和成本尚未验证。

## 2. 依赖和官方资料核对

新增项目依赖：

```text
openai==2.46.0
```

实现依据：

- OpenAI 官方 Models 页面；
- OpenAI 官方 latest model guide；
- OpenAI 官方 Python SDK；
- Python SDK structured outputs helper 文档。

当前代码默认配置 `gpt-5.6-terra`，但业务服务不依赖模型名。默认值会随评估结果调整，不能把一次模型选择当作永久结论。

最终依赖检查：

```text
pip check
→ No broken requirements found.
```

## 3. 数据库迁移

迁移链：

```text
20260724_0004
→ 20260724_0005 Add provider-neutral model call records
→ 20260724_0006 Add model call schema hash and resource linkage
```

`model_call_records` 保存：

- provider/model；
- prompt name/version/hash；
- response schema name/hash；
- resource type/id；
- status、attempts、attempt error codes；
- latency、input/output tokens；
- provider request/response ID；
- error code；
- request_id/trace_id；
- tenant 和时间。

自动化迁移测试覆盖：

- upgrade 创建表和字段；
- tenant_id 非空；
- ORM metadata 与迁移 head 一致；
- downgrade；
- PostgreSQL JSONB 元数据往返；
- tenant + trace 查询隔离。

## 4. Day 6 单元测试范围

### 4.1 ModelGateway

`tests/test_model_gateway.py` 覆盖：

1. 正常输出被 Pydantic 校验并记录成功；
2. timeout → rate limit → success 的有限重试；
3. 退避时间为 1、2 秒的指数序列；
4. 额外危险字段被拒绝并记录 `model_output_invalid`；
5. 认证错误只调用一次，不盲目重试；
6. 调用轨迹写入失败时阻止模型输出离开网关；
7. schema hash、resource linkage、Token 和 Provider ID 被记录。

### 4.2 调查分诊

`tests/test_investigation_triage.py` 覆盖：

1. 业务服务只依赖 Gateway，不暴露具体 Provider；
2. Prompt 没有发送 customer 身份；
3. 非法模型输出进入确定性保守 fallback；
4. 连续 timeout 用尽预算后进入 fallback；
5. Prompt injection 文本保持为 data；
6. 相同 Prompt 内容产生稳定 hash；
7. Registry 拒绝不存在的版本；
8. 模板拒绝缺失或意外变量。

### 4.3 OpenAI adapter

`tests/test_openai_provider.py` 使用模拟 SDK 对象验证：

1. 调用 `responses.parse`；
2. Pydantic 模型通过 `text_format` 传入；
3. reasoning、max output tokens、metadata 和 `store=False` 正确传递；
4. 供应商模型名、request/response ID 和 token usage 正确映射；
5. timeout、rate limit、refusal 映射为中立错误。

这证明 adapter 行为，不证明真实网络调用成功。

### 4.4 配置

`tests/test_llm_config.py` 覆盖：

- API Key 不出现在 Settings `repr`；
- 非法 reasoning effort 启动失败；
- 真实 runtime 必须显式提供 API Key；
- 生产环境不会因缺 Key 悄悄使用 Fake。

## 5. 真实 PostgreSQL 专项

运行命令：

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test"
.\.venv\Scripts\python.exe -m pytest -W error -m postgres
```

最终验收结果：

```text
7 passed, 70 deselected
```

Day 6 新增场景证明：

- `attempt_error_codes` 在 PostgreSQL JSONB 中正确保存；
- schema hash 和 resource linkage 正确往返；
- 只能通过相同 tenant + trace 查询本租户记录。

## 6. 全量回归

最终运行命令：

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test"
.\.venv\Scripts\python.exe -m pytest -W error
```

最终结果：

```text
77 passed in 11.63s
```

此外已运行：

```text
python -m compileall app tests evaluation
→ passed

alembic check
→ No new upgrade operations detected.

alembic current
→ 20260724_0006 (head)

pip check
→ No broken requirements found.
```

## 7. 可复现量化评估

数据集：

`evaluation/datasets/day06_structured_output_v1.json`

运行器：

`evaluation/run_day06_structured_output.py`

原始报告：

`evaluation/reports/day06_structured_output_v1.json`

运行命令：

```powershell
.\.venv\Scripts\python.exe evaluation\run_day06_structured_output.py `
  --output evaluation\reports\day06_structured_output_v1.json
```

数据集明确标记为合成，共 30 例：

- 15 例立即返回合法输出；
- 10 例非法 Schema：
  - 缺少字段 3；
  - 增加动作字段 3；
  - 非法证据枚举 2；
  - 重复证据 2；
- 5 例第一次 timeout、第二次成功。

基线定义为“只要求 JSON、无统一网关、无本地强类型门禁、单次调用”。

| 指标 | JSON-only 基线 | ResolveFlow Gateway |
|---|---:|---:|
| 合成案例 | 30 | 30 |
| 安全类型化结果或保守降级 | 15 | 30 |
| 非法输出进入业务逻辑 | 10 | 0 |
| 临时失败成功恢复 | 0 | 5 |
| Provider 尝试次数 | 30 | 35 |
| 模型结果 | 不单列 | 20 |
| 确定性保守降级 | 无 | 10 |

这个结果具体证明：

- 在这 30 个合成结构/故障案例中，非法输出进入业务从 10 降到 0；
- 5 个一次性 timeout 全部由受控重试恢复；
- 代价是 Provider 尝试次数从 30 增加到 35；
- 不能安全使用的 10 个模型结果没有被伪装为成功，而是进入可识别的保守 fallback。

## 8. 评估边界

本次数据不能证明：

- 真实模型能正确理解售后语义；
- 真实客户任务完成率提高；
- 模型幻觉率降低；
- 生产 P50/P95 延迟；
- 真实 Token 或费用；
- 真实人工介入率；
- 完整 Agent 的工具选择和计划修订能力。

原因是本次使用 `FakeLLMProvider`，付费调用为 0。Day6 的指标只衡量结构门禁和故障恢复。后续必须在同一版本化评估体系中增加真实模型语义样本、Agent trajectory、政策合规和工具调用指标。

## 9. 实现过程中发现并解决的问题

### 9.1 官方 Docs MCP 当前任务未即时暴露

已通过 Codex CLI 注册 `openaiDeveloperDocs` MCP，但当前已打开的任务没有动态出现对应工具。没有伪造 MCP 查询结果，而是按技能规则使用 OpenAI 官方网页和官方 SDK 仓库作为 fallback。重新打开 Codex 后可核对 MCP 是否加载。

### 9.2 Latest model resolver 返回 403

官方技能附带的自动 resolver 两次返回 403。没有据此猜测模型，而是查阅官方 Models 和 latest model guide。默认模型仍保持配置化，未来由真实评估决定。

### 9.3 editable install 首次超时

首次安装依赖在 120 秒命令时限内未完成。随后完成 OpenAI SDK 安装、刷新 editable metadata，并用 `pip check` 验证，没有把超时当作成功。

### 9.4 已应用迁移不能改写

开发 PostgreSQL 已应用 0005 后，复核发现还需记录 schema hash 和关联资源。曾在工作区修改 0005，但在继续验收前立即发现并纠正：

- 恢复 0005；
- 新增 0006；
- 先 nullable；
- 回填旧记录；
- 再设 non-null。

这样新旧数据库都通过相同迁移链到达一致结构。

### 9.5 不让 SDK 和网关双重重试

OpenAI SDK 本身具有重试能力。如果网关再重试，会放大调用次数且难以观测。runtime 显式设置 `max_retries=0`，统一由 ModelGateway 管理最大尝试次数和错误链。

## 10. 当前没有过度宣称的能力

Day 6 尚未完成：

- 真实 OpenAI API 的端到端调用；
- 完整 Agent Loop；
- 订单、物流、政策工具；
- Observation 驱动的动态计划；
- 高风险退款 Policy 和人工审批；
- Prompt 语义质量的 Golden Dataset；
- OpenTelemetry 全链路 span；
- 生产成本和延迟优化；
- 多 Provider 自动切换。

这些分别属于 Day7–Day10、Day18–Day23 和后续可靠性阶段。
