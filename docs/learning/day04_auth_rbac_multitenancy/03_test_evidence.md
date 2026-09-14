# Day 4 测试证据

## 1. 环境

- Python：项目 `.venv`
- FastAPI：0.139.0
- PyJWT：2.13.0
- SQLAlchemy：2.0.51
- Alembic：1.18.5
- PostgreSQL：18.3-alpine，Docker healthcheck healthy
- Redis：8.2-alpine，Docker healthcheck healthy
- Alembic head：`20260724_0003`

## 2. 迁移结果

`20260724_0003` 已应用到本地开发 PostgreSQL，并由测试自动应用到专用 `resolveflow_test`。

新增或修改：

- tickets/ticket_events/agent_runs/idempotency_keys 的非空 `tenant_id`；
- ticket_events.actor_id；
- authorization_audit_events；
- tenant 组合索引；
- 幂等复合主键 `(tenant_id, operation, key)`；
- 旧数据进入 legacy 零租户，正常 JWT 禁止使用该租户。

同步检查：

```text
alembic check
→ No new upgrade operations detected.

alembic current
→ 20260724_0003 (head)
```

## 3. 全量自动化测试

命令：

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test"
.\.venv\Scripts\python.exe -m pytest -W error
```

真实结果：

```text
37 passed in 3.92s
```

没有 skip，没有 warning。

## 4. PostgreSQL 专项

命令：

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test"
.\.venv\Scripts\python.exe -m pytest -W error -m postgres
```

结果：

```text
5 passed, 32 deselected in 0.50s
```

覆盖：

1. 真实 PostgreSQL 迁移和 Ticket 往返。
2. 并发相同幂等创建只有一个资源。
3. 并发相同 version 更新只有一个胜者。
4. B tenant 的 Repository 读不到 A tenant Ticket，并持久化 deny audit。
5. 两个 tenant 使用相同 Idempotency-Key 各自创建独立 Ticket。

## 5. 正常路径覆盖

- 签名有效、claims 完整的 agent JWT 能创建、读取和流转本租户工单。
- customer 能创建和读取自己的工单。
- supervisor 能读取本租户授权审计。
- development 环境能签发 5 分钟本地 JWT，且 token 可实际调用受保护 API。
- 工单事件记录真实 actor_id 和 customer/staff 类型。
- 同租户合法幂等 replay 仍保持 Day 3 行为。

## 6. 失败路径覆盖

- 缺少 Bearer token → 401。
- 修改 JWT payload、保留旧签名 → 401。
- 过期 JWT → 401。
- customer 尝试 ticket transition → 403。
- customer 创建其他 customer 的工单 → 403。
- agent 读取 authorization audit → 403。
- A tenant 查询、更新、读取 B tenant events → 404。
- 同 tenant 的 customer 读取其他 owner 工单 → 404。
- 授权拒绝被 supervisor 在本 tenant 审计中查到。
- 授权审计写入失败 → 503，且 Ticket 数量保持 0。
- 测试/生产态不存在 development-token 路由。
- production 使用项目默认开发密钥 → 启动配置拒绝。

## 7. 边界覆盖

- 客户端在 Ticket JSON 注入 tenant_id → 422。
- 角色集合不能为空。
- legacy 零 tenant 不能签发/验证 token。
- 审计列表按 tenant 过滤，A supervisor 看不到 B actor。
- 相同 Idempotency-Key 跨 tenant 不冲突。
- limit 输入由 Query schema 限制在 1 到 200。
- migration upgrade、downgrade 和 ORM metadata 同步检查通过。

## 8. 测试中发现并修复的问题

### 8.1 依赖安装超时

第一次 `pip install -e ".[dev]"` 停在构建隔离依赖并超时，PyJWT 实际未安装。随后先验证 import 失败，再只安装锁定版本 `PyJWT==2.13.0`，成功后才运行测试。没有把超时当成安装成功。

### 8.2 安全改造后的旧测试失败

第一次全量回归：

```text
17 failed, 3 passed, 3 skipped
```

迁移实际已成功到 0003。失败主要是旧测试仍匿名调用、构造无 tenant Ticket、调用无 tenant UoW。正确修复是给测试签发可信 token，并显式加入 tenant，而不是给业务 API 增加匿名后门。

### 8.3 篡改 token 的测试方法错误

最初只改变 JWT 最后一个 Base64URL 字符，某些情况下解码后的签名字节可能没有改变，测试意外走到 404。修复为直接改变 payload 段并保留原签名，确保签名校验必然失败并返回 401。

这个问题说明：安全测试本身也需要验证攻击输入真的改变了受签名内容，不能凭字符串看起来不同就断言 token 被有效篡改。

## 9. 代码和迁移静态验证

```text
python -m compileall -q app migrations tests
→ exit 0
```

```text
alembic check
→ No new upgrade operations detected.
```

## 10. 没有过度宣称的能力

当前没有：

- 企业 OIDC/JWKS；
- 非对称签名和密钥轮换；
- 用户密码库、MFA、SSO；
- refresh token、token 撤销和 denylist；
- PostgreSQL RLS；
- 安全网关、WAF、速率限制；
- Agent Runtime 或 Agent 工具权限传播。

因此 Day 4 证明的是后端 JWT 验证、RBAC、资源和应用层多租户隔离，不声称已经完成企业统一身份平台。
