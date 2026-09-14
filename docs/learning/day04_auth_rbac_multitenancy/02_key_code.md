# Day 4 关键代码

## 1. JWT 验证器

位置：`app/core/security.py`

### 输入和输出

- 输入：Bearer token 字符串。
- 输出：只包含可信 `actor_id`、`tenant_id` 和 `roles` 的 `AuthenticatedActor`。

### 执行流程

`jwt.decode()` 使用服务端固定的 `HS256`，同时验证 signature、issuer、audience、时间声明和必需字段。随后代码继续校验 subject 长度、roles 类型和枚举、tenant UUID，并拒绝 legacy 零租户。

### 为什么这样写

Pydantic 只能验证 HTTP JSON 形状，不能证明 token 是谁签发的。必须先做密码学签名和标准声明验证，再把外部 claims 缩减成内部 Actor，避免后续业务层继续依赖任意字典。

### 架构位置

它位于 Core/Security，是 HTTP Bearer 与内部身份模型之间的认证适配器。它不决定工单权限。

### 状态变化

验证本身不写数据库，只把不可信字符串转成可信请求上下文。

### 异常处理

PyJWT、类型和枚举错误统一转成 `AuthenticationError`，HTTP 返回安全的 401，不暴露“到底是签名、受众还是哪个 claim 错了”。

## 2. FastAPI 当前身份依赖

位置：`app/api/dependencies.py`

### 输入和输出

- 输入：HTTP `Authorization` 请求头。
- 输出：注入 Router 的 `AuthenticatedActor`。

### 执行流程

`HTTPBearer(auto_error=False)` 提取凭证；缺失或 scheme 错误时主动抛统一认证异常；存在 token 时调用应用级 `JwtTokenService`。

### 为什么这样写

使用 FastAPI 标准 security dependency 可以生成正确 OpenAPI 安全声明，同时关闭框架默认错误，保留项目统一错误结构。

### 架构位置

它只适配 HTTP。Service 仍显式接收 Actor，所以未来 Worker、Slack 或 Agent Tool 不需要伪造 HTTP 请求。

### 状态变化

Actor 只在当前请求传播，不进入聊天 History，也不允许由请求体覆盖。

### 异常处理

缺失、错误或无效 token 都进入全局 401 handler，并带 `WWW-Authenticate: Bearer`。

## 3. RBAC 与资源级 Policy

位置：`app/services/authorization_service.py`

### 输入和输出

- 输入：Actor、Permission、具体 Ticket 或创建目标 customer、request_id。
- 输出：允许时正常返回；拒绝时抛 403 或隐藏成 404，同时写授权审计。

### 执行流程

先检查 `ROLE_PERMISSIONS`，再根据 `actor.is_staff` 和 `ticket.customer_id` 检查资源所有权。Ticket 未被 tenant Repository 返回时，统一记录 `not_found_or_out_of_tenant_scope`。

### 为什么这样写

角色权限和资源所有权是确定性规则，不需要 LLM 推理。集中 Policy 避免 Router、Worker 和将来的 Tool Adapter 各写一套。

### 架构位置

它位于应用安全策略层，由 TicketService 调用；不执行 HTTP 解析，也不执行 Ticket 状态机。

### 状态变化

每次决定追加一条 authorization audit。allow 不改变 Ticket；只有后续业务事务才改变 Ticket。

### 异常处理

角色缺权限抛 `AuthorizationDeniedError` → 403；跨租户、资源不存在或 customer 非 owner 抛 `TicketNotFoundError` → 404。

## 4. Tenant-scoped Unit of Work

位置：

- `app/services/unit_of_work.py`
- `app/db/sqlalchemy_unit_of_work.py`
- `app/repositories/sqlalchemy_ticket_repository.py`

### 输入和输出

- 输入：已验证 Actor 的 `tenant_id`。
- 输出：只能操作该 tenant 的 Ticket/Event/Idempotency Repository 集合。

### 执行流程

`unit_of_work_factory(tenant_id)` 把 tenant 固定到所有 Repository。Ticket 查询和乐观锁更新都将它加入 WHERE；add/update 还检查 Domain 对象 tenant 是否等于作用域 tenant。

### 为什么这样写

tenant 不能靠每个调用者临时传给每个查询，否则漏一次就可能泄露。把它绑定到 UoW，使“默认只能看到本租户”成为基础设施约束。

### 架构位置

Unit of Work 是 Service 到 SQLAlchemy 的事务适配器；Repository 是 Domain 与表模型之间的数据访问适配器。

### 状态变化

读操作只返回本租户数据；写操作只能把当前租户数据提交。跨租户 id 查询得到 `None`。

### 异常处理

数据库故障仍转成 `StorageUnavailableError`；代码试图把其他 tenant 的实体写入当前 UoW 时立即抛内部 `ValueError`，暴露编程错误而不是静默写错。

## 5. TicketService 中的安全编排

位置：`app/services/ticket_service.py`

### 输入和输出

- 输入：`AuthenticatedActor`、Command、`request_id`。
- 输出：授权范围内的 Ticket/Events 或统一业务异常。

### 执行流程

创建先授权，再用 actor tenant 构造 Ticket、Event 和 IdempotencyRecord。读取先用 scoped Repository 查资源，再做 owner/role 判断。状态更新授权后重新开启 UoW 读取并使用 expected_version 更新，保留 Day 3 并发保护。

### 为什么这样写

Service 是可被 HTTP、Worker 和未来 Agent Tool 复用的业务入口。若只在 Router 鉴权，其他入口会绕过。

### 架构位置

它连接认证后的请求上下文、授权 Policy、Domain 状态机和持久化事务。

### 状态变化

授权创建后新增带 tenant 的 Ticket/Event/Idempotency；状态更新事件记录真实 `actor_id` 和 customer/staff 类型。

### 异常处理

认证在进入 Service 前失败；授权在业务写入前失败；状态机、并发和存储异常继续沿用 Day 3 的 409/503 语义。

## 6. 授权审计 Repository

位置：`app/repositories/sqlalchemy_authorization_audit_repository.py`

### 输入和输出

- 输入：结构化 `AuthorizationAuditEvent`。
- 输出：持久化完成，或按 tenant 返回最近事件。

### 执行流程

每次 add 创建独立 Session 和短事务，提交 allow/deny。list 始终按 `tenant_id` 过滤并限制数量。

### 为什么这样写

deny 事件如果跟业务事务一起 rollback 就会消失。独立提交保证拒绝路径仍有证据；审计失败则 fail closed。

### 架构位置

它是 AuthorizationService 的基础设施适配器，与 `ticket_events` 的业务事实存储分开。

### 状态变化

只追加审计行，不修改历史决定。

### 异常处理

写入或查询失败 rollback，并转成不泄露 SQL 细节的 `StorageUnavailableError`。

## 7. 开发态 token 路由与环境配置

位置：

- `app/api/routes/auth.py`
- `app/core/config.py`
- `app/main.py`

### 输入和输出

- 输入：开发态 actor、tenant、roles。
- 输出：短期 Bearer JWT、token type 和有效秒数。

### 执行流程

`create_app()` 只在 `AppEnvironment.DEVELOPMENT` 注册路由。Settings 在 production 检查开发默认密钥并拒绝启动。

### 为什么这样写

本地开发需要可复现身份，但不能让测试后门进入生产路由表。环境判断发生在应用组装阶段，不是前端隐藏。

### 架构位置

开发路由模拟外部 IdP；真正业务路由只消费 JWT。

### 状态变化

签发不写用户数据库，只产生短期 token；响应禁止缓存。

### 异常处理

输入 schema 拒绝空角色、非法 UUID 和 legacy 零租户；非 development 环境直接没有该路由。

## 8. Alembic 0003

位置：`migrations/versions/20260724_0003_add_auth_and_tenant_boundaries.py`

迁移新增 tenant 字段、事件 actor、授权审计表和租户组合索引，并把幂等主键升级为 `(tenant_id, operation, key)`。旧数据先进入不可登录的 legacy quarantine tenant，再把字段改为 NOT NULL，避免捏造真实归属。
