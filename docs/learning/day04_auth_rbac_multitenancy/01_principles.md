# Day 4 原理：认证、RBAC 与多租户边界

## 1. 今天解决的真实问题

ResolveFlow 将来会处理订单、物流、退款证据和人工审批。如果服务端不知道调用者是谁，或只靠前端隐藏按钮控制权限，就会出现三类真实事故：

- 匿名或伪造身份调用内部接口；
- 普通客户或客服执行只有主管才能执行的动作；
- A 公司读取或修改 B 公司的工单和审计记录。

Day 4 建立三道不能互相替代的门：

1. 认证：证明“你是谁”。
2. RBAC：判断“你的角色能做什么类型的操作”。
3. 租户和资源级鉴权：判断“你能否操作这一条具体数据”。

今天实现的是确定性后端安全边界，没有实现 Agent Runtime。以后即使模型选择了一个工具，工具后端仍必须经过这些检查，不能把模型的意图当成权限。

## 2. 一次受保护请求的完整流程

```text
客户端
  Authorization: Bearer <JWT>
  Idempotency-Key: ...
  JSON 业务输入
→ FastAPI/ASGI
→ Middleware 生成 request_id
→ HTTP Bearer 依赖提取 token
→ JwtTokenService 验证
     固定 HS256 算法
     签名
     iss 签发方
     aud 受众
     exp/nbf/iat 时间
     sub/tenant_id/roles/jti 必需声明
→ 生成内部 AuthenticatedActor
→ Schema 校验外部业务输入
→ Router 翻译为 Command
→ TicketService 调用 AuthorizationService
→ 写入 allow/deny 授权审计
→ 用 actor.tenant_id 创建 tenant-scoped Unit of Work
→ Repository 查询自动带 tenant_id
→ 检查具体 Ticket 的 owner
→ Domain 状态机和业务编排
→ PostgreSQL 事务提交 Ticket/Event/Idempotency
→ Schema 序列化
→ 客户端收到结果和 X-Request-ID
```

`tenant_id` 不从请求体读取，只能来自签名验证后的 JWT。否则攻击者只需把 JSON 中的 tenant 改成别人的值，就能主动切换数据边界。

## 3. JWT 到底解决什么

JWT 是一种带签名的声明载体。服务端验证签名后，可以确认载荷没有被调用者篡改，并确认它是可信签发方为本 API 签发、且仍在有效期内的令牌。

JWT 不是加密容器。拿到 token 的人通常可以解码载荷，所以里面只保存最小身份声明：

- `sub`：调用者唯一标识；
- `tenant_id`：所属租户；
- `roles`：角色；
- `iss`、`aud`：签发方和使用方；
- `iat`、`nbf`、`exp`：签发、生效和过期时间；
- `jti`：令牌唯一标识。

不能把密码、身份证号、订单隐私或其他敏感数据放进去。

验证算法必须由服务端配置固定为 `HS256`，不能读取 token 头后让调用者决定算法。当前项目使用对称密钥，是单体本地阶段的最小实现；接入企业身份提供方时，通常会改为非对称签名、OIDC 和 JWKS，不应把当前开发签发接口说成完整生产登录系统。

## 4. 认证与授权为什么必须分开

认证成功只说明身份可信，不代表拥有所有权限。

```text
正确签名的 customer JWT
→ 身份可信
→ 但 ticket:transition 不在 customer 权限中
→ 403
```

如果把“token 有效”直接当成“操作允许”，任何登录用户都会变成管理员。

当前角色：

- `customer`：创建自己的工单，读取自己的工单和事件；
- `agent`：创建、读取、流转本租户工单；
- `supervisor`：在 agent 基础上读取本租户授权审计；
- `tenant_admin`：当前租户内全部现有权限。

RBAC 是“角色 → 权限”的粗粒度判断。资源级规则继续判断客户是不是工单 owner；tenant 规则继续判断资源是否属于同一租户。因此：

```text
角色允许 ≠ 具体资源一定允许
```

## 5. 多租户隔离为什么放进 Unit of Work 和 Repository

如果只在 Router 写：

```text
先查 Ticket
→ 再判断 ticket.tenant_id
```

未来 Slack Adapter、Worker 或 Agent Tool 直接调用 Service 时，就可能绕过 Router。更危险的是某个新查询忘记补 tenant 条件。

当前设计让 `unit_of_work_factory(actor.tenant_id)` 创建租户作用域 Repository。读取和更新都带：

```sql
WHERE id = :ticket_id
  AND tenant_id = :authenticated_tenant_id
```

Repository 还拒绝写入 tenant 不匹配的 Domain 对象。这样租户边界成为数据访问默认条件，而不是每个 Router 的自觉。

SQLAlchemy 提供 `with_loader_criteria()` 给 SELECT 自动附加全局条件，PostgreSQL 还提供 Row-Level Security。Day 4 没有宣称已启用 RLS：当前使用显式 tenant-scoped Repository，因为它同时覆盖 SELECT 和条件 UPDATE，并且能在 SQLite 与 PostgreSQL 测试中复现。RLS 是未来可增加的数据库级最后防线，不能在未实现时写成项目能力。

## 6. 为什么跨租户访问返回 404

假设 A 租户请求一个实际属于 B 租户的 ticket_id：

```text
A 的 tenant-scoped Repository
→ 查询结果 None
→ 审计 not_found_or_out_of_tenant_scope
→ 404 ticket_not_found
```

不返回 403，是为了不向 A 泄露“这个 ID 的资源确实存在，只是属于别人”。同租户客户读取其他客户的工单也按 404 隐藏。

角色本身不允许某操作时返回 403，例如 customer 流转工单。此时并不是隐藏资源，而是明确说明当前已认证身份没有这个操作权限。

## 7. 401、403 和 404 的区别

- 401：没有可信身份。缺少 token、签名被篡改、过期、签发方或受众错误。
- 403：身份可信，但角色不具备该操作权限，或创建资源时冒充其他 customer。
- 404：资源不存在，或者为防止泄露而隐藏跨租户/非 owner 资源。

401 响应携带 `WWW-Authenticate: Bearer`。所有错误继续使用统一 `error + meta.request_id` 结构，客户端能追踪，底层 token 验证细节不会泄露。

## 8. 授权审计为什么独立提交

授权审计记录：

- tenant、actor；
- permission；
- allow/deny；
- reason；
- request_id；
- resource type/id；
- 时间。

deny 决策不能放在随后必然回滚的业务事务中，否则抛出 403/404 后审计也会消失。当前审计 Repository 使用独立短事务先保存决定，再继续或拒绝请求。

审计写失败时系统 fail closed：返回存储不可用，不继续执行受保护业务动作。这比“审计坏了但退款继续”安全。

授权 `allow` 只表示调用者当时有资格尝试，不表示业务操作最后成功。业务结果由 `ticket_events`、状态和错误响应记录。授权审计与业务事件职责不同，不能混成一张无含义的日志。

## 9. 开发态身份边界

`POST /api/v1/auth/development-token` 允许本地开发者选择 actor、tenant 和 role，获得短期 JWT。它用于模拟企业身份提供方。

安全边界：

- 只有 `APP_ENV=development` 时路由才注册；
- `test` 和 `production` 返回路由 404；
- 生产环境拒绝项目内置开发密钥；
- 零 UUID 是旧数据隔离租户，不能获得 token；
- 响应使用 `Cache-Control: no-store`。

这不是生产登录接口，没有用户密码库、OIDC、JWKS、密钥轮换、注销和 token 撤销能力。

## 10. 数据迁移和旧数据

迁移 `20260724_0003` 给 `tickets`、`ticket_events`、`agent_runs` 和 `idempotency_keys` 增加非空 tenant，并新增授权审计表。

迁移前已有数据没有租户来源，不能伪造归属。它们被放入零 UUID 的 legacy quarantine tenant；JwtTokenService 明确拒绝该 tenant，旧数据不会自动暴露给任何正常租户。

幂等主键从：

```text
(operation, key)
```

变为：

```text
(tenant_id, operation, key)
```

同一租户内仍防止重复创建，不同租户使用相同 key 不会互相冲突。

## 11. 与后续 Agent 的关系

Day 4 以后，Agent 的工具调用将继承调用主体和 tenant：

```text
模型选择工具
→ 只代表“模型建议执行”
→ 后端仍验证 Actor、Permission、Tenant、Resource 和业务 Policy
→ 允许后才调用工具
```

Prompt 中写“不要越权”不是安全控制。模型可能误判、被 prompt injection 影响或生成错误参数；确定性后端才是最终门禁。

## 12. 官方资料

- [RFC 7519：JSON Web Token](https://www.rfc-editor.org/rfc/rfc7519)
- [FastAPI Security](https://fastapi.tiangolo.com/tutorial/security/)
- [FastAPI OAuth2/JWT 示例](https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/)
- [FastAPI OAuth2 Scopes](https://fastapi.tiangolo.com/advanced/security/oauth2-scopes/)
- [PyJWT decode API](https://pyjwt.readthedocs.io/en/stable/api.html#jwt.decode)
- [SQLAlchemy 全局查询条件](https://docs.sqlalchemy.org/en/20/orm/session_events.html#adding-global-where-on-criteria)
- [PostgreSQL Row Security Policies](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)
