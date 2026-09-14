# Day 7 关键代码讲解

## 1. 阅读顺序

```text
app/tools/contracts.py
→ app/tools/registry.py
→ app/tools/executor.py
→ app/tools/after_sales.py
→ app/domain/auth.py
```

## 2. ToolDefinition与ToolObservation

位置：`app/tools/contracts.py`

### 输入和输出

`ToolDefinition`输入一项完整工具契约：名称、版本、说明、输入/输出Pydantic类型、风险、只读/写属性、权限、超时和Handler。

`ToolObservation`输出一次调用的结果：成功时有类型化output，失败时有`ToolErrorDetail`，并携带trace和Schema身份。

### 执行流程

```text
定义工具
→ 校验name和semver
→ Pydantic生成input/output JSON Schema
→ Registry发布descriptor
→ Executor使用同一Pydantic类型做本地校验
→ 生成Observation
```

### 为什么这样写

同一个类型同时生成给模型看的Schema和后端实际校验规则，减少“文档说一种、代码收另一种”的漂移。输入和输出都要校验，因为Handler和上游系统也可能违反契约。

### 架构位置

这是Agent Runtime与业务工具适配器之间的端口，不依赖OpenAI SDK。

### 状态变化

契约对象不可变；每次调用产生新的Observation，不覆盖历史。

### 异常处理

工具身份格式非法在系统启动/注册时失败；运行期失败被Executor转换成标准错误Observation。

## 3. ToolRegistry

位置：`app/tools/registry.py`

### 输入和输出

输入：一组`ToolDefinition`和当前`AuthenticatedActor`。

输出：

- 精确解析出的ToolDefinition；
- 当前Actor有权看到的ToolDescriptor集合。

### 执行流程

```text
启动时按(name, version)建索引
→ 拒绝重复键
→ descriptors_for(actor)过滤required_permission
→ Agent只看到允许的工具目录
→ execute时再次resolve和鉴权
```

### 为什么这样写

工具目录过滤减少模型选到无权工具的概率，但不能替代执行时鉴权。按精确版本解析保证评估可复现。

### 架构位置

Registry是Agent动作空间目录，不访问订单或物流数据。

### 状态变化

当前Registry只读，不支持运行期热修改。

### 异常处理

- 重复name/version：配置错误；
- 未知name/version：`ToolNotFoundError`，Executor转为selection error。

## 4. ToolExecutor

位置：`app/tools/executor.py`

### 输入和输出

输入`ToolCallRequest`：

- tool name/version；
- 模型生成的arguments；
- 系统建立的ToolExecutionContext。

输出统一`ToolObservation`。

### 执行流程

```text
计算arguments hash
→ Registry解析工具
→ 权限检查
→ input_model.model_validate(arguments)
→ ThreadPoolExecutor执行Handler
→ Future.result(timeout)
→ output_model.model_validate(result)
→ 构建Observation
→ Recorder保存
→ 返回Agent
```

### 为什么采用这个顺序

1. 未知工具先报告selection error；
2. 未授权Actor在参数和Handler之前被拒绝；
3. 参数不合法时不访问外部系统；
4. 超时限制Agent等待；
5. Handler输出必须再次过契约；
6. 记录失败则fail closed，避免不可追踪Observation进入AgentState。

### 架构位置

Executor是Agent Runtime的确定性执行边界。它执行模型提出的动作，但不替Agent选择下一步。

### 状态变化

它不修改Ticket或AgentState，只产生并记录Observation。Day8由Agent Loop决定如何把Observation合并进State。

### 异常处理

| 发生位置 | 标准分类 |
|---|---|
| Registry找不到 | selection_error |
| Pydantic输入失败 | argument_error |
| 权限不足 | authorization_error |
| 等待超时 | timeout_error |
| 依赖临时失败 | dependency_error |
| 资源不存在 | resource_error |
| Handler输出非法 | output_contract_error |
| 未分类实现异常 | internal_error |

参数校验错误只返回安全的类型、字段位置和通用消息，不暴露调用栈。

## 5. ToolExecutionContext

位置：`app/tools/contracts.py`

### 输入和输出

输入可信`AuthenticatedActor`及request/trace/run/step标识；通过属性输出可信tenant_id。

### 执行流程

```text
JWT验证
→ AuthenticatedActor
→ Agent Runtime建立ToolExecutionContext
→ Handler读取context.tenant_id
```

### 为什么这样写

tenant、actor和权限不是模型应该决定的参数。把它们与业务arguments分离，模型无法通过参数伪造租户。

### 架构位置

它连接后端认证上下文与Agent工具执行上下文。

### 状态变化

不可变，只在一次调用中传播。

### 异常处理

模型额外传`tenant_id`会在输入Schema阶段被拒绝，Handler调用次数保持0。

## 6. 三个只读工具

位置：`app/tools/after_sales.py`

### 输入和输出

`order_lookup`：

```text
输入 order_id
输出状态、付款/发货、金额、币种、观察时间
```

`logistics_lookup`：

```text
输入 order_id
输出物流状态、签收证明可用性、签收/观察时间
```

`policy_lookup`：

```text
输入工单category和region
输出政策ID/版本、所需证据、人工审批要求、生效时间
```

### 执行流程

```text
已校验arguments
→ Handler从context取得tenant
→ tenant-scoped InMemoryAfterSalesDataSource
→ 领域记录
→ 类型化Observation
→ Executor再校验输出
```

### 为什么这样写

模拟数据源让Day7测试可重复，不需要真实企业订单系统。Handler负责协议适配，不负责Agent规划和退款资格。

### 架构位置

这些是工具适配器；将来可把数据源替换成HTTP/MCP客户端，而Tool Contract和Agent上层不变。

### 状态变化

全部`read_only`，只记录调用，不修改订单、物流或政策。

### 异常处理

- tenant范围内不存在：resource error；
- 注入临时失败：dependency error；
- 注入延迟超过超时：timeout error。

跨tenant订单同样表现为not found，不泄露另一个租户是否存在该订单。

## 7. 权限扩展

位置：`app/domain/auth.py`

新增：

- `tool:order:read`
- `tool:logistics:read`
- `tool:policy:read`

Agent、Supervisor、Tenant Admin拥有；Customer没有。

这是后端权限知识，面试不单独考RBAC实现，但必须理解Agent看到工具不等于有权执行，Executor仍会确定性鉴权。

## 8. 关键代码形成的整体关系

```text
LLM候选Tool Call
  不可信
→ Registry
  工具身份与可见动作空间
→ Input Schema
  参数是否合法
→ Trusted Context
  谁、哪个tenant、哪次run
→ Permission
  能否执行
→ Handler
  获取外部事实
→ Output Schema
  事实形状是否合法
→ Observation
  Agent可使用的可追踪输入
```
