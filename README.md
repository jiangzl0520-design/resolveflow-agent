# ResolveFlow：企业售后工单调查与处置 Agent

它解决的不是“陪用户聊天”，而是企业售后团队需要在订单、物流、退款规则和知识库之间反复查询、判断、审批、执行和复核的问题。

## 一句话业务目标

输入一张售后工单，系统自动收集证据、补全信息、制定处置计划，并调用只读工具完成调查；涉及退款、补偿等写操作时必须暂停并请求人工审批，获批后执行并验证结果，最终留下可审计的完整轨迹。

## 为什么它是真 Agent

普通 RAG 的流程通常是“检索一次，然后回答”。ResolveFlow 必须在不确定环境中循环：

1. 理解目标与约束。
2. 判断当前缺少什么证据。
3. 选择工具并生成结构化参数。
4. 观察工具结果，更新任务状态。
5. 验证结果是否足够、是否矛盾、是否满足完成条件。
6. 信息不足时改写计划，存在风险时转人工，满足条件时执行或结束。

模型负责不确定性的理解和决策；权限、金额上限、幂等、事务、审计等确定性约束由后端代码负责。

## 最终可演示的完整流程

“用户称包裹未收到并要求退款” → Agent 查询订单 → 查询物流轨迹 → 检索退款政策 → 发现物流信息矛盾 → 再查询签收证明 → 给出带证据的判断 → 生成退款提案 → 人工批准或修改 → 幂等执行退款 → 查询退款状态验证 → 关闭或升级工单。

## 技术主线

- Python 3.12、FastAPI、Pydantic v2
- PostgreSQL、SQLAlchemy 2、Alembic、pgvector
- Redis、后台任务 Worker
- Provider-neutral 模型网关、OpenAI Responses 适配器、Fake LLM
- LangGraph 状态图；关键运行循环会先自己实现再接框架
- 标准化 Tool Registry、Function Calling、一个独立 MCP 工具服务
- 混合检索、重排、引用、短期状态与长期记忆
- OpenTelemetry、Prometheus、Grafana；可选接入 Langfuse
- pytest、Golden Dataset、确定性评估、LLM-as-a-Judge
- Docker Compose、GitHub Actions、压测与故障注入
- 一个轻量人工审批和 Trace 查看页面

## 当前最小 API

- `GET /health`：服务健康检查。
- `POST /api/v1/auth/development-token`：仅开发环境签发本地测试 JWT。
- `POST /api/v1/tickets`：创建售后工单。
- `GET /api/v1/tickets/{ticket_id}`：查询工单。
- `PATCH /api/v1/tickets/{ticket_id}/status`：使用 expected_version 更新工单状态。
- `GET /api/v1/tickets/{ticket_id}/events`：查询结构化审计事件。
- `POST /api/v1/tickets/{ticket_id}/investigation-jobs`：持久化受理调查任务，返回 202。
- `GET /api/v1/investigation-jobs/{job_id}`：查询异步任务状态。
- `POST /api/v1/investigation-jobs/{job_id}/cancel`：请求取消任务。
- `GET /api/v1/investigation-jobs/{job_id}/events`：查询任务状态转换轨迹。
- `GET /api/v1/authorization/audit-events`：主管或租户管理员读取本租户授权审计。
- `/docs`：FastAPI 自动生成的 OpenAPI 交互文档。

除健康检查和开发态签发接口外，业务接口必须携带
`Authorization: Bearer <JWT>`。服务端验证固定算法、签名、签发方、受众、有效期和必需声明，然后把 `sub`、`tenant_id` 和 `roles` 转为内部身份。JWT 经过签名但没有加密，不能放密码、客户隐私或其他敏感数据。

创建工单还必须携带 `Idempotency-Key` 请求头。相同租户内，相同 key 和相同业务输入会返回第一次创建的工单；相同 key 配合不同输入会返回 409。幂等键按租户隔离，不同租户可以使用相同 key。

角色权限：

- `customer`：创建自己的工单、读取自己的工单和事件；
- `agent`：处理本租户工单；
- `supervisor`：增加本租户授权审计读取、退款审批、退款执行和退款状态核验权限；
- `tenant_admin`：拥有当前租户内全部现有权限。

角色通过后仍会检查具体资源和 `tenant_id`。跨租户访问返回 404，既不返回数据，也不泄露目标资源是否存在。

### 本地安装

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

第一条命令创建项目隔离环境，第二条命令按照 `pyproject.toml` 安装运行和测试依赖。

### 运行测试

```powershell
.\.venv\Scripts\python.exe -m pytest
```

这条命令运行全部自动化测试。`pyproject.toml` 会把警告当成错误，同时只精确忽略 FastAPI/Starlette 当前触发的一条 AnyIO 第三方兼容性弃用警告，避免掩盖项目自身的新警告。

### 启动 PostgreSQL 和 Redis

```powershell
Copy-Item .env.example .env
docker compose up -d
.\.venv\Scripts\python.exe -m alembic upgrade head
```

Compose 启动 PostgreSQL 和 Redis；Alembic 再把数据库升级到代码要求的版本。不能用 `create_all()` 代替迁移，也不能仅凭容器处于 running 状态就认为数据库已经可用，Compose 中的 healthcheck 才负责就绪检查。

真实 PostgreSQL 集成测试必须使用专用测试库：

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test"
.\.venv\Scripts\python.exe -m pytest -m postgres
```

测试会拒绝数据库名不以 `_test` 结尾的连接地址，避免误连普通开发库或生产库。

### 启动开发服务

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

这条命令让 Uvicorn 加载 `app.main` 中的 FastAPI 应用；`--reload` 只用于本地开发。

### 启动异步 Worker 与投递补偿

Celery 官方不支持 Windows Worker。生产和可重复的完整进程验证应在 Linux、WSL 或 Linux 容器中运行：

```bash
celery -A app.worker.celery_app:celery_app worker --loglevel=INFO
celery -A app.worker.celery_app:celery_app beat --loglevel=INFO
```

Worker 消费调查任务；Beat 每 30 秒扫描 PostgreSQL 中尚未成功投递的任务并重新投递。Redis 只是消息传输层，任务状态、重试次数、租约、取消请求和最终结果都以 PostgreSQL 为准。

开发环境 Redis 开启 AOF，并使用 `noeviction`，降低重启和键淘汰导致的消息丢失风险。Celery 使用 late acknowledgement、worker-lost requeue 和单任务预取；这仍然是“至少一次投递”，所以业务副作用还必须由数据库租约、version fencing 和幂等状态转换保护。

### 模型网关

业务用例只依赖 `ModelGateway`，不直接导入具体厂商 SDK。当前 OpenAI 适配器使用 Responses API 和 Pydantic structured output；SDK 隐式重试被关闭，超时、限流、连接错误和服务端错误由网关统一做有界重试并记录每次错误。

默认配置使用 `gpt-5.6-terra` 作为质量与成本的平衡起点，可通过 `LLM_MODEL` 替换。真实调用必须显式设置 `OPENAI_API_KEY`；没有 Key 时不会偷偷使用 Fake LLM 伪装生产结果。自动化测试和离线评估使用脚本化 Fake LLM，因此不会产生真实 Token 或费用。

### OpenTelemetry Trace

API、Celery Worker、Agent、LangGraph节点、Context Builder、LLM、RAG、Retrieval、Tool、Policy和SQLAlchemy数据库调用已经接入OpenTelemetry。HTTP和消息队列使用W3C `traceparent`传播父子关系；Span只记录版本、hash、Token、耗时、状态和稳定错误码，不默认记录Prompt、用户问题、Tool参数、知识正文、SQL正文或参数。

默认不导出Trace。开发时可设置：

```powershell
$env:OTEL_TRACES_EXPORTER="console" # 或 otlp
$env:OTEL_SERVICE_NAME="resolveflow-api"
$env:OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="http://127.0.0.1:4318/v1/traces"
$env:OTEL_TRACES_SAMPLER_ARG="1.0"
```

`request_id`用于一次HTTP尝试，业务`trace_id`用于持久化关联，OpenTelemetry Context负责真正的分布式父子关系。Metrics、Logs、Dashboard和告警属于Day19。

每次调用记录 provider、model、Prompt 名称/版本/哈希、Pydantic Schema 哈希、资源、attempts、错误码、Token、耗时、request_id 和 trace_id，但不持久化原始 Prompt、工单正文或模型输出。

### 工具契约与注册中心

Agent只通过Provider-neutral `ToolRegistry`和`ToolExecutor`访问工具。当前有`order_lookup@1.0.0`、`logistics_lookup@1.0.0`和`policy_lookup@1.0.0`三个只读模拟工具。

每个契约声明输入/输出JSON Schema、风险等级、只读/写属性、所需权限和超时。模型只生成工具名和业务参数；tenant、actor、request/trace和Agent run/step从可信执行上下文注入。未知工具、非法参数、权限拒绝、超时、依赖故障、资源不存在和输出契约错误使用不同错误类型，供后续Agent Loop选择不同恢复动作。

当前线程timeout只限制等待，不能强制停止已经运行的线程，因此该实现只承载只读工具。退款等写工具会在后续增加幂等键、Policy、人工审批和执行后验证。

### 最小Agent Loop

`AgentRunner`把模型Planner、Tool Registry/Executor和确定性Completion Verifier串成`observe → decide → act → verify`循环。运行状态显式保存目标、预算、Decision、Tool Observation、Verifier失败、Token、终止原因和最终Outcome。

循环受到最大步骤、时间、Token和相同Decision次数限制。模型提出finish后必须由Verifier检查当前order/category对应的Observation；错误订单的成功工具结果不能完成当前任务。权限和工具输出契约错误安全终止，selection/argument/临时依赖错误则作为Observation交给下一轮修正。

### LangGraph 状态图与持久化恢复

`DurableAgentWorkflow`把Day8循环映射成`control → plan → execute_tool/verify`状态图。图状态只保存JSON安全数据；项目领域对象在节点执行时按已锁定的工具版本重新构造，缺失版本会拒绝恢复。

LangGraph在每个节点边界保存checkpoint。`control`节点通过`interrupt()`暂停，继续时使用同一个`thread_id`和`Command(resume=...)`；取消在控制节点安全终止。PostgreSQL checkpointer允许进程关闭后由新Runtime继续，checkpoint后的成功工具不会重复执行。

首次使用PostgreSQL checkpointer时执行：

```powershell
.\.venv\Scripts\python.exe -m app.agent.setup_checkpoints
```

这一步只创建和升级LangGraph自身管理的`checkpoint_*`表。Alembic仍管理ResolveFlow业务表，两套迁移责任不能混用。Day9只有只读工具；Day10加入退款写工具后，“工具副作用成功但checkpoint尚未提交”的崩溃窗口由业务幂等键处理，不能把checkpoint误当成Exactly-once保证。

### 退款Policy、人工审批与安全写工具

Agent收集订单、物流、签收证明和政策Observation后，可以输出`propose_refund`候选方案，但不能直接选择退款写工具。确定性`RefundPolicyEngine`重新检查订单状态、金额、币种、证据新鲜度和政策要求，通过后创建带版本、证据哈希、提案哈希、过期时间和后端幂等键的提案。

LangGraph在`refund_approval`节点强制`interrupt()`。普通resume不能通过退款门；审核人必须同租户、拥有审批权限、不是原始请求人，并提供与当前版本和哈希完全一致的审批。reject、过期和takeover不执行退款；modify会让旧版本失效，重新经过Policy并要求另一名审核人批准。

审批通过后，后端使用独立密钥为精确退款参数签发HMAC执行凭证。`refund_execute`工具同时检查RBAC和执行凭证，因此仅拥有主管角色也不能绕过工作流直接退款。写操作使用租户级幂等键；执行后还要调用`refund_status_lookup`核对订单、金额、币种、提案和审批，全部一致才完成任务。

本地开发可以先调用开发态接口获取短期 JWT：

```powershell
$body = @{
  actor_id = "local-agent"
  tenant_id = "11111111-1111-1111-1111-111111111111"
  roles = @("agent")
} | ConvertTo-Json

$token = (
  Invoke-RestMethod `
    -Method Post `
    -Uri "http://127.0.0.1:8000/api/v1/auth/development-token" `
    -ContentType "application/json" `
    -Body $body
).access_token
```

该接口只在 `APP_ENV=development` 时注册。`test` 和 `production` 环境中路由不存在；生产环境还会拒绝项目自带的开发密钥，必须通过密钥管理系统设置独立 `JWT_SECRET`。当前开发接口只是模拟外部身份提供方，不能作为生产登录系统。

### MCP工具服务

`order_lookup@1.0.0`现在也可以作为独立MCP Server运行。Agent侧先通过`tools/list`校验版本、风险、副作用、权限、输入约束和输出Schema，再把远程工具适配回原有`ToolDefinition`；Planner、ToolExecutor和Tool Observation不依赖MCP SDK。

Agent Runtime不会透传主应用JWT，而是签发短期、精确audience、最小`tool:order:read` scope的MCP服务Token。MCP Server再次验证身份，并从Token注入tenant。客户端还负责超时、错误翻译、输出重校验和连续依赖故障熔断。

启动独立服务：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.mcp.runtime:app --port 8011
```

开发/测试可通过`MCP_ORDER_FIXTURES_JSON`提供订单夹具；生产会拒绝夹具Runtime，必须注入真实订单Provider并使用HTTPS与正式身份基础设施。MCP解决标准发现和调用，不替代RBAC、多租户、业务Policy、幂等、超时、熔断和审计。

### 可追溯知识入库

政策知识通过独立入库用例从UTF-8 Markdown解析为标题语义分块。每个chunk都绑定tenant、父文档、source URI、版本、有效期、标题路径、原文行范围和内容哈希，可以回到不可变原文版本进行引用与审计。

入库run保存幂等键、请求哈希、attempts、request/trace和最终状态。相同内容的重复投递会被跳过；同一source/version出现不同内容会被拒绝，必须显式升版本；新版本发布时旧文档与旧chunks仍然保留。文档、分块、current指针和run状态在一个PostgreSQL事务内发布，故障重跑不会留下半成品。

`is_current`只表示最新入库版本，不等于任意业务时间都有效。后续检索必须同时过滤tenant和`effective_from/effective_to`。Day12只建立可信知识数据，不包含embedding、向量召回或答案生成。

### 安全、可观测的混合知识检索

Day13把已入库chunk独立索引为固定256维向量，并在PostgreSQL中组合pgvector cosine、全文检索、精确包含与pg_trgm。查询先用可信Actor执行RBAC，再做确定性改写；tenant、角色ACL和业务有效期全部在排序与LIMIT前过滤。

语义与关键词通道通过RRF按名次融合，不直接相加不同量纲的原始分数。hybrid在Embedding服务故障时可降级到关键词，并记录原因；每次检索都持久化Run和Hit Trace，但不保存原始客户查询。Day13只返回带来源的候选证据，重排、引用和grounded answer属于Day14。

### 重排、冲突检测与可验证引用

Day14把Day13候选证据先按正文哈希去重，再通过版本化结构化Prompt逐条重排。模型必须评估每个证据ID；后端检查输出集合完整性，并在全部相关证据上组合模型冲突与同source_key版本冲突，冲突和无证据时使用固定安全结果且跳过答案生成。

答案模型只返回claims、evidence_id和exact_quote。后端验证引用属于本轮选中证据、引文是对应chunk精确子串、claim没有夹带伪引用，再自行拼接最终`[Kx]`。Answer Run关联Retrieval Run和两个Model Call，Citation Trace保存source/version/行号与引文哈希。60个合成门禁案例中，正确处理率和答案引用精确率均从基线33.33%提升至100%，10个过期政策引用降为0，10个伪造引用全部阻断；这些结果不代表生产模型质量。

### 可审计 Context Builder 与 Token 预算

Day15把系统规则、当前目标、完成条件、Agent State、用户修改、Observation、RAG、History、Memory和工具描述转成带来源、可信度、优先级、相关性和新鲜度的候选片段。Builder先处理越权、失效、被新事实替代、重复和低相关内容，再使用目标模型tokenizer按“上下文窗口减输出、推理与安全预留”装配本轮输入；required片段放不下或Trace无法持久化时拒绝调用模型。若首次启动时 tokenizer 编码资源因网络或缓存故障不可用，系统改用 UTF-8 字节数作为不会低估 Token 的本地保守上界，优先保证上下文不溢出和服务可用性。

Planner每一步只发送仍能填补当前证据缺口的工具描述，主订单号变化会使依赖旧订单的Observation失效。Context Build Run记录预算和状态，每个Fragment Trace记录进入或丢弃原因且只保存内容哈希，Model Call通过`context_build_id`关联本轮输入组成。60个合成案例中，关键片段保留率从66.67%提升到100%，陈旧片段误选择从40降到0，工具描述精确率从33.33%提升到100%，平均输入Token降低20.13%；这些是合成基线对照，不代表生产收益。

### 策略控制的跨会话长期记忆

Day16明确分离Raw History、Agent State/checkpoint、Summary、业务事实和长期Memory。LangGraph PostgresSaver继续保存thread内工作流状态；跨thread的用户偏好进入独立PostgreSQL Memory领域表，并经过精确key白名单、来源、置信度、TTL、版本冲突、多租户、权限、幂等和删除门禁。模型推断只产生待确认候选，订单/物流/退款资格等业务事实禁止写入长期记忆。

Planner通过受信任的tenant和Ticket-customer绑定读取未过期active Memory，再交给Context Builder选择；冲突、过期、删除和被替代版本不会进入模型。删除会清空所有版本原文但保留哈希审计。80个合成案例中，相对“保存全部聊天”基线，正确处理率从31.25%提升到100%，阻止40次不安全写入和15次失效召回，持久化value载荷减少99.86%；这些结果只证明固定数据上的确定性Memory策略，不代表线上收益。

### 上下文安全与 Prompt Injection 分层防御

Day17把用户Goal、History、工具Observation、Memory和RAG文档统一视为带来源的数据，只有SYSTEM_POLICY可以成为指令。Context进入模型前递归脱敏常见密钥、JWT、邮箱和手机号；required直接注入终止本次Build，可选恶意证据只丢对应片段。知识问答在检索前拦直接注入，在rerank前移除恶意/含秘密证据，并继续执行tenant、角色ACL、有效期和精确引用门禁。

模型结构化Decision还要经过当前步骤工具集合、版本、订单号/类别资源绑定和输出泄露检查；AgentRunner与LangGraph复用相同规则，拒绝动作不会到达Tool Executor。未知工具和参数错误仍保留selection/argument分类，退款仍由Policy、四眼人工审批、执行授权和事后验证控制。100个合成对抗案例中，相对Prompt-only基线，正确处理率从20%提升到100%，阻止40个恶意片段进入模型、20次敏感信息暴露和20个不安全动作，20个正常对照误拦截为0；不代表未知攻击或线上模型安全率。

## 项目成功标准

最终结果不以“页面能聊天”为完成，而以可复现的证据为准：

- 高风险写操作未经审批的执行次数必须为 0。
- 所有写工具都具备幂等键、权限校验、审计日志和执行后验证。
- Golden Dataset 同时评估最终任务、工具选择、工具参数、轨迹、引用和安全性。
- 失败任务能通过 checkpoint 恢复，能取消，重试不会造成重复退款。
- 每次运行可查看模型版本、Prompt 版本、上下文组成、工具调用、耗时、Token、错误和最终状态。
- 有离线回归报告、线上指标面板、失败样本归因和数据回流流程。
- 有架构文档、API 文档、测试报告、压测报告、演示脚本和简历表述。

具体设计见：

- [Day 原理快速总结](原理总结/)
- [项目与架构蓝图](docs/01_project_and_architecture.md)
- [30 天路线与每日验收](docs/02_30_day_roadmap.md)
- [JD 能力覆盖矩阵](docs/03_jd_coverage_matrix.md)
- [每日学习与面试协议](docs/04_learning_protocol.md)
- [Day 0：Agent 项目基础认知总结](docs/day00_agent_foundation_review.md)
- [Day 1：HTTP 主链路学习目录](docs/learning/day01_http_main_flow/)
- [Day 2：PostgreSQL、ORM 与迁移学习目录](docs/learning/day02_postgresql_orm_migrations/)
- [Day 3：状态机、幂等、乐观锁与审计事件](docs/learning/day03_consistency_events/)
- [Day 4：认证、RBAC 与多租户边界](docs/learning/day04_auth_rbac_multitenancy/)
- [Day 5：异步任务、重试、取消与恢复](docs/learning/day05_async_jobs_recovery/)
- [Day 6：模型网关与结构化输出](docs/learning/day06_model_gateway_structured_output/)
- [Day 7：工具契约、注册中心与Observation](docs/learning/day07_tool_contracts_registry/)
- [Day 8：最小Agent Loop](docs/learning/day08_minimal_agent_loop/)
- [Day 9：LangGraph状态图与checkpoint](docs/learning/day09_langgraph_checkpoint/)
- [Day 10：退款Policy、人工审批与安全写操作](docs/learning/day10_refund_policy_human_approval/)
- [Day 11：MCP工具服务化与Agent适配](docs/learning/day11_mcp_tool_service/)
- [Day 12：可追溯知识入库流水线](docs/learning/day12_knowledge_ingestion/)
- [Day 13：安全、可观测的混合检索](docs/learning/day13_hybrid_retrieval/)
- [Day 14：重排、冲突检测与可验证引用](docs/learning/day14_grounded_answers/)
- [Day 15：Context Builder 与 Token 预算](docs/learning/day15_context_builder/)
- [Day 16：短期状态与策略控制的长期记忆](docs/learning/day16_long_term_memory/)
- [Day 17：上下文安全与 Prompt Injection 分层防御](docs/learning/day17_context_security/)
- [Day 18：OpenTelemetry 全链路 Trace 与失败归因](docs/learning/day18_distributed_tracing/)
- [Day 19：Prometheus Metrics、结构化 Logs 与 Grafana Dashboard](docs/learning/day19_metrics_logs_dashboard/)
- [Day 20：Golden Dataset 与统一 Eval Harness](docs/learning/day20_golden_dataset_eval_harness/)
- [Day 21：Agent 任务、轨迹与工具评估](docs/learning/day21_agent_trajectory_evaluation/)
- [Day 22：LLM Judge 与人工校准](docs/learning/day22_llm_judge_human_calibration/)

## 重要范围控制

30 天内不会为了“技术名词多”而盲目加入多 Agent、强化学习或模型微调。先用单 Agent 状态图解决业务闭环；只有离线评估证明某类任务需要独立角色时，才把检索审查或风险审查拆为子 Agent。RL、SFT、DPO 会掌握原理、数据接口和适用边界，但不伪造训练成果。
