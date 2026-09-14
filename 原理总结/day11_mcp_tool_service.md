# Day 11：MCP 工具服务化与 Agent 适配

## 完成内容

把`order_lookup@1.0.0`作为独立MCP Server运行，并在Agent侧适配回原有Tool Contract，实现跨进程标准发现与调用，同时保留权限、租户、超时、输出校验和审计。

## 完整流程

```text
Agent启动并携带可信ToolExecutionContext
→ MCP Adapter签发短期最小权限服务Token
→ tools/list发现远程工具
→ 校验名称、版本、风险、权限、输入输出Schema和注解
→ 适配为内部ToolDefinition并注册
→ Planner选择order_lookup
→ 本地ToolExecutor先检查RBAC和参数
→ Adapter调用远程tools/call
→ MCP Server验证issuer、audience、scope、tenant和有效期
→ 从Token注入tenant并查询订单数据源
→ 返回structuredContent
→ Agent侧用本地Pydantic模型再次校验
→ 统一生成Tool Observation并记录审计
```

MCP解决不同进程、语言和客户端之间的工具发现与调用，不自动解决业务权限、多租户、幂等、重试和安全。Agent内部仍只认识ToolDefinition、ToolExecutor和Observation，因此远程化没有重写Planner与状态图。

主应用JWT不会直接透传给MCP服务；Runtime签发精确audience、60秒有效期和最小scope的独立Token。tenant来自签名claims而不是模型参数。工具注解只是互操作提示，真正权限必须由服务端执行。

## 故障处理

远程资源不存在、依赖不可用、超时、权限、契约漂移和非法输出被翻译成内部标准错误。连续依赖故障达到阈值后熔断器open，后续请求快速失败；恢复窗口后只允许一次half-open探测。参数和权限错误不会计入依赖熔断。

## 测试证据

专项14项、全量152项和真实PostgreSQL专项9项通过。30/30本地与MCP输出一致；10个越权或跨租户调用成功数为0；10/10契约漂移在Agent看到工具前被阻止；故障期10次请求中熔断避免7次无效下游调用。

## 与上下文工程的关系

远程工具只接收完成调用所需的最小可信Context，不传完整History、Memory、主JWT和其他Observation，减少上下文污染和敏感数据扩散。

