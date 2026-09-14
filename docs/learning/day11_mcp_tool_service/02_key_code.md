# Day 11 关键代码与架构位置

## 1. 独立MCP Server发布工具

```python
@server.tool(
    name="order_lookup",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    meta=order_tool_metadata(),
    structured_output=True,
)
async def order_lookup(order_id: ...) -> OrderObservation:
    ...
```

这段代码把订单查询公开成标准MCP工具。客户端可以通过`tools/list`看到名称、输入/输出Schema、注解和版本元数据，再通过`tools/call`执行。注解只用于互操作提示，权限仍由服务端认证与数据访问边界决定。

架构位置：`app/mcp/order_server.py`，位于MCP协议边界和订单数据源之间。

## 2. tenant来自认证上下文

```python
token = _require_identity(get_access_token())
claims = token.claims or {}
tenant_id = UUID(str(claims["tenant_id"]))
order = source.get_order(tenant_id, order_id)
```

请求参数只有`order_id`，没有`tenant_id`。tenant来自签名令牌的可信claims，因此模型不能通过参数读取其他租户。即使两个租户都有订单`10086`，查询仍按令牌tenant隔离。

## 3. 不透传主应用JWT

```python
token = self._token_issuer.issue(
    context,
    permissions=frozenset({Permission.TOOL_ORDER_READ}),
)
```

Agent Runtime根据已经认证的内部Context重新签发短期、最小scope、精确audience的MCP Token。主应用JWT与MCP Token使用不同用途和边界，避免下游服务冒充上游用户Token去访问其他资源。

架构位置：`app/mcp/security.py`。

## 4. 发现时拒绝契约漂移

```python
if metadata.get(META_TOOL_VERSION) != ORDER_TOOL_VERSION:
    raise MCPToolContractError(...)
if document_hash(tool.output_schema) != expected_output_hash:
    raise MCPToolContractError(...)
```

适配器不是看到同名工具就直接注册。它检查版本、风险、副作用、权限、输入约束、输出Schema和注解。漂移在Agent看到工具前失败，防止远程服务升级后悄悄改变Agent行为。

架构位置：`app/mcp/client.py`中的`_validate_discovered_tool()`。

## 5. 保留内部 Tool Contract

```python
return ToolDefinition(
    name=ORDER_TOOL_NAME,
    version=ORDER_TOOL_VERSION,
    input_model=OrderLookupArguments,
    output_model=OrderObservation,
    required_permission=Permission.TOOL_ORDER_READ,
    handler=self._call,
)
```

MCP只替换handler内部的执行方式。Agent Planner、Registry、Executor、Observation、错误分类和后续状态图不需要理解MCP SDK。这使同一个Agent可以同时使用进程内工具、HTTP工具和MCP工具。

## 6. 远程结构化输出再次校验

```python
if result.is_error:
    raise ToolRemoteContractError()
output = OrderObservation.model_validate(
    result.structured_content
)
```

客户端先检查`isError`，再读取`structuredContent`，最后用本地Pydantic模型校验。远程返回成功、MCP传输成功和业务输出满足契约是三个不同判断，不能合并。

## 7. 错误翻译

```python
if code == "tool_resource_not_found":
    return ToolResourceNotFoundError()
if code == "tool_dependency_unavailable":
    return ToolDependencyUnavailableError()
```

MCP协议异常不会直接泄漏进Agent State，而是转换为已有的内部错误语言。`ToolExecutor`继续产生统一的`ToolFailureKind`，Planner可以沿用原来的恢复策略。

## 8. 熔断状态变化

```python
if failures >= threshold:
    state = "open"
...
if now - opened_at >= recovery_seconds:
    state = "half_open"
```

连续依赖失败达到阈值后，熔断器快速失败。恢复窗口后只放一次探测，成功才关闭。它减少故障扩散，不保证调用成功，也不能用于权限或资源不存在错误。

## 9. 服务端审计

```python
MCPCallAuditRecord(
    tenant_id=tenant_id,
    actor_id=actor_id,
    tool_name="order_lookup",
    tool_version="1.0.0",
    request_id=request_id,
    trace_id=trace_id,
    outcome=outcome,
    error_code=error_code,
    duration_ms=duration_ms,
)
```

记录可以把Agent本地Tool Observation和远程服务调用按trace关联起来。Token本身不进入日志，防止凭证泄漏。

## 10. 独立进程入口

```powershell
.\.venv\Scripts\python.exe -m uvicorn `
  app.mcp.runtime:app `
  --host 127.0.0.1 `
  --port 8011
```

`app/mcp/runtime.py`创建Streamable HTTP ASGI应用。测试会真实启动这个进程，而不是把MCP Server当普通Python函数调用。

## 11. 关键文件

- `app/mcp/contracts.py`：版本元数据、Schema哈希、审计结构；
- `app/mcp/security.py`：短期MCP Token签发与验证；
- `app/mcp/order_server.py`：独立`order_lookup` MCP Server；
- `app/mcp/client.py`：发现、调用、契约验证、错误翻译与熔断；
- `app/mcp/runtime.py`：Uvicorn可加载的独立进程入口；
- `app/tools/executor.py`：新增远程认证、超时和契约错误映射；
- `tests/test_mcp_process_integration.py`：真实进程和Streamable HTTP验证；
- `evaluation/run_day11_mcp_interoperability.py`：60例可复现评估。

