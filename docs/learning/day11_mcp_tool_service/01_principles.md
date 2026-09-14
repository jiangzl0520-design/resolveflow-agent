# Day 11 原理：MCP 工具服务化与 Agent 适配

## 1. 今天解决的真实问题

Day7 的 `order_lookup` 已经有清晰的内部 Tool Contract，但它仍与 Agent 运行在同一个 Python 进程中。只要另一个 Agent、IDE 或独立服务想复用订单工具，就必须了解 ResolveFlow 的 Python 类、导入路径和调用方式，接入成本高，也无法独立部署。

Day11把`order_lookup@1.0.0`暴露为独立MCP Server，并在Agent侧增加MCP适配器。结果是：

1. MCP客户端通过标准`tools/list`发现工具和JSON Schema；
2. 通过标准`tools/call`调用工具；
3. Agent内部仍只认识原来的`ToolDefinition`、`ToolExecutor`和`ToolObservation`；
4. 工具从进程内调用变成远程调用，不需要重写Agent Loop；
5. 认证、租户隔离、超时、熔断、输出校验和审计仍由后端显式实现。

核心结论：MCP解决“工具怎样被不同客户端发现和调用”的互操作问题，不自动解决“谁能调用、能看哪个租户、失败是否重试、是否满足业务规则”等工程问题。

## 2. 完整操作流程

### 2.1 Agent启动时发现工具

Agent侧先用一个可信`ToolExecutionContext`签发短期MCP服务令牌，然后调用`tools/list`。适配器检查：

- 只能出现一个名为`order_lookup`的目标工具；
- 版本必须是`1.0.0`；
- 风险等级必须是`low`；
- 副作用必须是`read_only`；
- 所需权限必须是`tool:order:read`；
- 输入字段只能是`order_id`，且长度和正则约束一致；
- 输出Schema哈希必须与本地`OrderObservation`一致；
- 只读、非破坏、幂等和闭世界注解必须一致。

任何契约漂移都会在工具进入Agent Registry之前被拒绝。这样模型不会看到一个“名字相同、含义已经改变”的工具。

### 2.2 适配为内部 Tool Contract

发现成功后，MCP适配器创建一个普通`ToolDefinition`：

- 输入仍是`OrderLookupArguments`；
- 输出仍是`OrderObservation`；
- 权限仍是`Permission.TOOL_ORDER_READ`；
- 风险和副作用仍是`LOW + READ_ONLY`；
- handler内部才执行MCP远程调用。

所以Agent主链路没有改变：

```text
Planner
  → ToolRegistry
  → ToolExecutor
  → ToolDefinition.handler
  → MCP Adapter
  → MCP Server
  → Order Data Source
```

这叫反腐层或适配层：外部协议变化被限制在边界内，Agent的内部语言保持稳定。

### 2.3 本地权限先拒绝

模型只产生工具名和`order_id`。`ToolExecutor`先用可信Actor检查本地权限，再校验输入Schema。没有`tool:order:read`权限时，请求在发出网络调用前就被拒绝。

这一步能减少不必要的远程请求，但不能代替服务端认证。客户端代码可能被绕过，所以MCP Server必须再次验证身份。

### 2.4 签发独立MCP服务令牌

Agent不会把用户登录使用的主应用JWT直接转发给MCP服务。它根据经过认证的`AuthenticatedActor`签发一个新的短期令牌，绑定：

- 固定issuer；
- 精确MCP Server URL作为audience；
- Agent Runtime client ID；
- actor与tenant；
- 最小scope：`tool:order:read`；
- request、trace、run和step；
- `iat`、`nbf`、`exp`和唯一`jti`。

令牌默认60秒过期。主应用JWT与MCP JWT使用不同密钥、audience和用途，所以主JWT不能冒充MCP服务身份。这符合“不要把上游Token透传给下游服务”的安全边界。

### 2.5 MCP Server验证服务身份

HTTP请求到达后，MCP SDK的Bearer认证中间件先验证令牌。自定义Verifier检查：

- HS256固定算法和签名；
- issuer；
- 精确audience；
- 必需claims；
- 有效时间；
- client ID；
- tenant不是隔离保留值；
- scope格式。

只有通过后，工具函数才能从认证上下文读取tenant。`tenant_id`不在工具参数中，模型或客户端无法通过参数切换租户。

### 2.6 MCP Server查询并返回结构化结果

Server使用令牌中的tenant和请求中的`order_id`查询订单数据源，然后返回`OrderObservation`。MCP SDK把Pydantic模型编码成`structuredContent`，并发布输出Schema。

服务端区分：

- 资源不存在：`tool_resource_not_found`，不可重试；
- 依赖不可用：`tool_dependency_unavailable`，可重试；
- 未知内部错误：不泄露堆栈和内部数据。

每次真正执行的工具调用都保存tenant、actor、工具版本、request/trace/run/step、结果、错误码和耗时，但不记录Bearer Token。

### 2.7 Agent侧重新校验远程结果

远程返回成功不等于结果可信。适配器必须：

1. 检查MCP的`isError`；
2. 只在非错误结果中读取`structuredContent`；
3. 用本地`OrderObservation`再次校验；
4. 把远程协议错误映射为内部工具错误；
5. 交给`ToolExecutor`生成统一`ToolObservation`。

因此Agent后续节点只看到经过本地契约验证的Observation，不直接依赖MCP SDK对象。

### 2.8 超时与熔断

远程调用比进程内调用多出网络和独立进程故障。Day11有两层时间边界：

- MCP/HTTP客户端有连接和读取超时；
- `ToolExecutor`仍保留工具总超时。

连续依赖失败达到3次后，熔断器进入`open`，后续请求快速返回`tool_circuit_open`，不再持续冲击故障服务。恢复窗口到达后进入`half_open`，只允许一次探测：

- 探测成功：回到`closed`并清零失败数；
- 探测失败：重新`open`。

资源不存在、参数错误、权限拒绝和契约错误不应累计为依赖故障，因为增加重试或熔断不能解决它们。

## 3. 架构位置与边界

```text
Agent State / Planner
        ↓ 选择 order_lookup
ToolRegistry + ToolExecutor
        ↓ 本地RBAC、输入校验、统一记录
MCPOrderToolAdapter
        ↓ 发现校验、专用Token、超时、熔断、错误翻译
MCP Streamable HTTP
        ↓ 标准 tools/list 与 tools/call
MCP Bearer Auth
        ↓ issuer/audience/scope/tenant
order_lookup MCP Tool
        ↓ 服务端租户注入、结构化输出、审计
Order Data Source
```

MCP层负责协议互操作；适配层负责内外契约隔离；ToolExecutor负责Agent内部的权限和Observation统一；MCP Server负责服务端身份、租户和数据访问。任何一层都不能假设其他层永远不会被绕过。

## 4. MCP能解决与不能解决的事情

| MCP能解决 | MCP不能自动解决 |
|---|---|
| 标准化工具发现 | 业务是否允许退款 |
| 标准化工具调用 | RBAC和多租户隔离 |
| 公开输入/输出Schema | Token签发、audience和scope设计 |
| 在不同语言/进程间互操作 | 超时、重试、熔断和幂等 |
| 客户端与工具服务解耦 | Prompt Injection |
| 传递结构化结果和错误 | 审计、SLO和告警 |

工具注解中的`readOnlyHint`、`destructiveHint`等是客户端提示，不是安全控制。真正的只读/写约束必须由服务端实现。

## 5. Context Engineering关系

Day11的上下文工程不是“把更多聊天记录发给远程工具”，而是只传完成一次调用所需的最小可信上下文：

- 业务参数：`order_id`；
- 服务身份：短期JWT中的actor、tenant和scope；
- 追踪上下文：request、trace、run和step；
- 本地固定契约：版本、权限、输入/输出模型；
- 不传：聊天History、用户自由文本、主应用JWT、其他工具Observation和长期Memory。

这样可以减少上下文污染、身份混淆和敏感数据扩散。MCP工具只需要回答订单事实，不需要知道整个Agent对话。

## 6. 异常处理关系

| 故障 | 内部分类 | Agent应对 |
|---|---|---|
| 工具名/版本不存在 | selection error | 修正选择或停止 |
| 本地参数不合法 | argument error | 修正参数，不调用远程 |
| 服务身份被拒绝 | authorization error | 停止并修复身份配置 |
| HTTP/MCP读取超时 | timeout error | 有界重试或转人工 |
| 远程依赖不可用 | dependency error | 有界重试，累计熔断 |
| 订单不存在 | resource error | 更新事实或重新确认订单号 |
| 输出不符合Schema | output contract error | 拒绝Observation并告警 |
| 连续依赖故障 | `tool_circuit_open` | 快速失败，等待恢复探测 |

错误分类决定恢复动作。把所有错误都写成“工具失败”会让Agent盲目重试，既浪费资源，也可能把权限或契约问题放大。

## 7. 官方资料与版本选择

Day11固定使用`mcp==2.0.0`，这是本项目实现时选择的稳定主版本。实现参考：

- [官方Python SDK仓库](https://github.com/modelcontextprotocol/python-sdk)
- [官方Python客户端文档](https://py.sdk.modelcontextprotocol.io/client/)
- [官方低层Server文档](https://py.sdk.modelcontextprotocol.io/v2/advanced/low-level-server/)
- [MCP授权文档](https://modelcontextprotocol.io/docs/tutorials/security/authorization)
- [MCP安全最佳实践](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices)
- [官方Python SDK测试文档](https://py.sdk.modelcontextprotocol.io/testing/)

生产环境必须使用HTTPS、真实授权服务/密钥管理和真实订单Provider。本地HTTP、共享HMAC密钥和JSON订单夹具只用于开发与测试。

## 8. 当前边界

- 当前独立服务只暴露`order_lookup`，物流和政策仍在进程内；
- MCP Runtime的JSON订单夹具只允许开发/测试，生产启动会要求注入真实数据源；
- 当前服务令牌使用独立HS256密钥，生产应接企业身份基础设施并支持密钥轮换；
- 熔断状态保存在单进程内，多实例需要共享或按实例观测；
- 尚未测量生产网络P95/P99、吞吐、可用性和真实订单系统故障；
- MCP增加了网络与运维成本，只有工具需要跨进程、跨语言或被多个客户端复用时才值得服务化。

