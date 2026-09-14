# Day 04：认证、RBAC 与多租户边界

## 完成内容

加入JWT认证、角色权限映射、资源级授权、多租户Repository边界和授权审计，使调用者身份不再来自客户端任意字段。

## 完整流程

```text
客户端携带Bearer JWT
→ FastAPI认证依赖验证签名、issuer、audience和有效期
→ 生成AuthenticatedActor
→ Router把Actor交给Service
→ AuthorizationService检查角色与权限
→ Unit of Work/Repository自动绑定tenant_id
→ 查询或修改本租户资源
→ 记录授权结果
→ 返回结果或401/403/404
```

认证回答“你是谁”，授权回答“你能做什么”，多租户隔离回答“你能访问哪一个租户的数据”。三者必须分开，拥有某个角色不代表能访问其他租户的同类资源。

`tenant_id`来自已验证JWT和内部Actor，不接受模型或请求正文自由传入。Repository查询同时包含资源id和tenant id，跨租户资源表现为不存在并返回404，避免泄露目标资源是否真实存在。

## 错误语义

- 401：没有合法身份或Token无效。
- 403：身份合法，但缺少执行某类操作的权限。
- 404：在当前租户边界内找不到资源，也用于隐藏跨租户资源。

授权拒绝也需要审计。审计写入与业务事务适当隔离，避免因为业务回滚把“曾发生过越权尝试”的证据一起删除。

开发态Token接口只用于本地测试，生产环境拒绝开发密钥并应接入正式身份提供方。

## 测试证据

全量37项测试通过，真实PostgreSQL授权与租户专项5项通过，覆盖无Token、错误角色、跨租户不可见和迁移一致性。

## 与Agent的关系

模型只能提出工具和业务参数，可信actor、tenant和permission由运行时注入。后续Tool Executor、MCP Server、退款审批和知识检索都继承这一边界。

