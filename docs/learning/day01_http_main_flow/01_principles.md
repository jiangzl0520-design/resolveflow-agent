# Day 1 原理：需求建模与 HTTP 主链路

## 1. 当天业务目标

ResolveFlow 首先需要一个稳定入口接收售后问题。Day 1 只完成最小闭环：

1. 客户端提交一张售后工单。
2. 系统校验工单格式。
3. 业务层创建一张状态为 `open` 的工单。
4. 系统暂时把工单保存在内存中。
5. 客户端可以根据工单 ID 查询它。
6. 输入错误和工单不存在时返回统一错误结构。

Day 1 不接模型、不接 Agent Loop，也不接 PostgreSQL。原因是 Agent 后续的每次运行都要依附于可靠的业务对象和 HTTP 入口，必须先把这条基础数据流看清。

## 2. 核心业务对象

### Ticket

`Ticket` 表示系统正在处理的一件售后问题。当前字段包括：

- `id`：系统生成的唯一工单 ID；
- `customer_id`：问题属于哪个客户；
- `subject`：简短标题；
- `description`：问题描述；
- `category`：未收到货、破损、错发、退款失败或其他；
- `status`：Day 1 创建时固定为 `open`；
- `created_at / updated_at`：创建和更新时间。

工单不是一段聊天文本。它是一个结构化业务对象，后续的 Agent Run、工具调用、审批和审计都要引用它。

## 3. HTTP 请求的完整操作流程

```text
客户端
  ↓ POST /api/v1/tickets + JSON
Uvicorn / ASGI Server
  ↓ 把 HTTP 转为 ASGI 事件
FastAPI/Starlette Middleware
  ↓ 生成 request_id
Router
  ↓ 匹配 HTTP 方法和路径
Pydantic TicketCreate
  ↓ 解析、转换、校验
FastAPI Dependency Injection
  ↓ 注入 TicketService
TicketService
  ↓ 创建领域 Ticket
TicketRepository
  ↓ 保存并返回
TicketRead / TicketResponse
  ↓ 序列化为 JSON
客户端收到 201 Created
```

这条链路中每层只负责一种主要问题，避免把 HTTP、业务规则和数据库代码塞进同一个函数。

## 4. FastAPI、Starlette、Pydantic、Uvicorn 的关系

### Uvicorn

Uvicorn 是 ASGI Server。它监听端口、接收网络中的 HTTP 请求，并把请求转换成 Python ASGI 应用可以处理的事件。它不负责“如何创建工单”。

### Starlette

Starlette 提供底层 Web 能力，例如 Request/Response、路由、中间件和测试客户端。FastAPI 构建在 Starlette 之上。

### Pydantic

Pydantic 根据类型和约束把外部 JSON 转成可信的 Python 对象。例如空标题、未知分类、超过 2000 字的描述会在进入 Service 前被拒绝。

### FastAPI

FastAPI 把路由、Pydantic 校验、依赖注入、异常处理和 OpenAPI 文档连接起来。它负责 HTTP 应用编排，但不应该承载全部业务规则。

四者的关系可以概括为：

```text
Uvicorn 接网络请求
    ↓ ASGI
Starlette 提供 Web 底座
    ↓
FastAPI 编排路由、依赖和 OpenAPI
    ↓
Pydantic 校验输入输出数据
```

## 5. ASGI 解决什么问题

ASGI 是 Python Web Server 与应用之间的调用协议。Server 不需要了解 ResolveFlow 的业务，只需要按照 ASGI 规范把请求事件交给应用，再把应用产生的响应事件发回客户端。

这层协议让 Uvicorn 可以替换为其他 ASGI Server，也让 FastAPI 应用不需要自己管理底层 socket。

本项目 Day 1 的测试不会真的绑定网络端口；`TestClient` 直接调用 ASGI 应用。因此它测试的是完整应用链路，但不测试真实网络和 Uvicorn 进程。

## 6. HTTP 方法与状态码

### POST 创建工单

- 路径：`POST /api/v1/tickets`
- 成功状态：`201 Created`
- 请求体：`TicketCreate`
- 响应体：`TicketResponse`

POST 表示向工单集合提交一个创建动作。Day 1 还没有实现幂等键，因此同一个请求发送两次会创建两个工单；幂等会在后续后端工程阶段加入。

### GET 查询工单

- 路径：`GET /api/v1/tickets/{ticket_id}`
- 成功状态：`200 OK`
- 不存在：`404 Not Found`
- ID 格式错误：`422 Unprocessable Content`

GET 是读取操作，不应改变工单状态。相同 ID 多次查询应该得到相同业务结果，除非工单被其他合法流程更新。

### 422 输入错误

请求 JSON 能被 HTTP 层接收，但字段不符合 API 契约时返回 422。例如标题只有空格、分类不存在或描述超过上限。

## 7. Schema 与 Domain 为什么分开

### Schema

`TicketCreate` 和 `TicketRead` 是 HTTP 边界的数据契约。它们关心 JSON 字段、字符串长度、序列化和 OpenAPI。

### Domain

`Ticket` 是业务对象。它不导入 FastAPI，不知道 HTTP 状态码，也不知道数据最终保存到内存还是 PostgreSQL。

如果直接把 Pydantic 请求模型当成整个系统的业务对象，未来 API 字段变化会直接污染业务层。分开以后，Router 负责把外部 Schema 转成内部 Command，再交给 Service。

## 8. Router、Service、Repository 的关系

### Router：协议适配

Router 读取路径、请求体和依赖，调用业务用例，再组装 HTTP 响应。它不决定数据库怎样保存。

### Service：用例编排

`TicketService.create_ticket()` 表达“创建工单”用例：生成 ID 和时间、设置初始状态、构造领域对象并要求 Repository 保存。

### Repository：持久化端口

`TicketRepository` 只定义业务层需要的 `add()` 和 `get()` 能力。`InMemoryTicketRepository` 是 Day 1 的实现。

```text
Router → Service → TicketRepository 协议 ← InMemory 实现
                                      ← Day 2 PostgreSQL 实现
```

依赖方向由外向内：HTTP 层依赖业务层，业务层依赖抽象端口，具体存储实现端口。Day 2 替换存储适配器时不应该重写业务用例。

## 9. 依赖注入解决什么问题

Router 声明自己需要 `TicketService`，FastAPI 调用 `get_ticket_service()` 把应用中的 Service 注入进来。

它解决三个直接问题：

1. Router 不需要自己创建 Service 和 Repository。
2. 多个路由可以复用同一个依赖获取逻辑。
3. 测试或后续环境可以替换依赖，而不用修改路由函数。

依赖注入不是“自动让架构变好”。如果把所有业务逻辑仍然写在依赖函数里，分层一样会混乱。

## 10. request_id 与错误映射

中间件为每个请求生成 `request_id`，把它同时放入响应 JSON 和 `X-Request-ID` 响应头。后续日志、Trace、数据库和工具调用都可以沿用这个标识定位一次请求。

错误分为两类：

- 请求契约错误：Pydantic/FastAPI 产生 `RequestValidationError`，映射成统一 422。
- 业务查询错误：Service 抛出 `TicketNotFoundError`，HTTP 异常处理器映射成统一 404。

Service 不直接抛 `HTTPException`，因为“工单不存在”是业务事实，“返回 404”是 HTTP 表达。这样 Service 以后也能被 Worker 或命令行调用。

## 11. 当前限制

- 内存数据在进程退出后会丢失。
- 多个 API 进程之间不共享工单。
- 还没有数据库事务和迁移。
- 还没有认证、租户、幂等和审计表。
- 还没有 Agent Run、工具调用和人工审批。

这些不是被忽略的生产问题，而是后续 Day 按依赖顺序逐步替换和扩展的内容。

## 12. 面试问答后的统一架构主线

今天问答中最容易混淆的不是某个类名，而是“外部输入怎样一步步变成可信业务结果”。统一流程如下：

```text
外部输入
→ 具体入口的 Schema / Parser 校验格式
→ 具体入口的 Adapter 翻译外部字段
→ 内部 Command 表达要执行的业务用例
→ Service 编排用例
→ Domain 表达业务实体和合法状态
→ Repository 持久化完整领域对象
→ 输出 Schema 限制公开字段并序列化
→ 外部调用方收到结果
```

HTTP 只是其中一种入口：

```text
HTTP JSON
→ Router + HTTP Schema
→ CreateTicketCommand
→ TicketService
```

CSV、Slack 和邮件不能直接复用 HTTP Router，因为它们有各自的外部字段和协议：

```text
CSV 行 → CSV Parser + Import Adapter ┐
Slack 事件 → Slack Input Adapter     ├→ CreateTicketCommand → TicketService
邮件事件 → Email Input Adapter       ┘
```

如果 Slack 通过 HTTP Webhook 进入，Router 仍然只负责匹配 Webhook 的方法和路径；Slack Input Adapter 才负责理解 `event.text`、`user` 和 `channel`。如果改用 Slack SDK 消费事件，可能完全没有 Router，但仍然需要 Slack Input Adapter。

关系可以压缩为一句话：

> Router 是 HTTP 适配的一部分，Adapter 理解具体外部协议，Command 统一内部业务语言，Service 执行业务用例。

## 13. Schema、Command、Domain、Repository 不能混用

四种对象可能暂时拥有相似字段，但所有者不同：

### 输入 Schema

归属于具体外部协议，负责字段名称、类型、必填规则和边界格式。例如 HTTP 的 `customerId` 和 CSV 的 `Customer Number` 可以表达同一个业务含义，但必须由各自适配器处理。

### Command

归属于内部业务用例。`CreateTicketCommand` 表达“创建工单需要哪些参数”，不包含 HTTP、CSV 或 Slack 技术信息。Service 接收 Command 后，就不需要知道请求来自哪个入口。

### Domain Entity

`Ticket` 是完整业务实体。Service 根据 Command 生成系统权威字段：

- `id`；
- 初始 `status`；
- `created_at / updated_at`。

外部输入不能指定这些字段，否则客户端可能伪造 ID、跳过合法状态流转或伪造系统时间。

### Repository

Repository 接收完整 `Ticket` 并保存。它不能接过 Command 后再自行决定 ID、初始状态和时间，否则业务规则会泄漏进存储层，内存实现和 PostgreSQL 实现可能产生不同业务结果。

完整职责链是：

```text
Adapter 创建 Command
→ Service 创建 Domain Entity
→ Repository 保存 Domain Entity
```

## 14. 输入 Schema 与输出 Schema 是两道相反的边界

输入 Schema 解决“外部能给系统什么”：

- 解析 JSON；
- 校验类型和局部字段约束；
- 拒绝多余字段；
- 阻止客户端提交 `id`、`status`、`created_at` 等系统字段。

输出 Schema 解决“系统允许对外公开什么”：

- 使用字段白名单避免泄漏内部风控分、审核备注等字段；
- 保持 API 契约稳定，不让 Domain 的内部修改自动传播给客户端；
- 校验 Service 返回的数据；
- 把 UUID、时间和枚举转换成 JSON 类型；
- 统一 `data`、`meta` 和 `request_id` 等响应结构。

二者关系是：

```text
不可信外部输入
→ 输入 Schema 限制进入系统的数据
→ Service / Domain 内部处理
→ 输出 Schema 限制离开系统的数据
→ 客户端
```

Schema 校验通过只代表数据形状合法，不代表业务成立，更不代表危险操作已经获得授权。

## 15. 格式校验、业务校验、执行授权必须分层

以退款金额为例：

1. Schema 判断金额是不是数字、字段是否齐全，并可提前拒绝小于等于 0 的值。
2. RefundService 查询订单真实支付金额、订单状态和已有退款记录，判断退款在业务上是否成立。
3. Policy Engine 检查金额阈值、人工审批、审批绑定的订单号和操作参数，决定当前是否被授权执行。
4. 退款执行器只接受 Policy Engine 的 `ALLOW`，然后调用真实支付系统。

```text
模型生成工具参数
→ Tool Schema：格式是否合法
→ Service：业务事实是否支持
→ Policy Engine：当前操作是否获授权
→ Executor：产生真实副作用
```

Service 和 Policy Engine 回答不同问题：

- Service：这件事在业务上是否成立？
- Policy Engine：即使业务成立，现在是否允许执行？

模型不能替代 Policy Engine。模型可能幻觉、算错或受到 Prompt Injection；公司硬规则必须由确定性、不可绕过、可审计的后端组件强制执行。

审批还必须绑定具体操作快照。例如审批的是：

```text
action=refund
order_id=10086
amount=80
```

最终操作如果变成订单 `10087`，原审批立即失效，必须重新审批。不能只比较金额，也不能把“曾经批准过退款”理解成永久授权。

## 16. application.state、Ticket.status 与 AgentState

这三个名字相似，但所有者、内容和生命周期完全不同。

### `application.state.ticket_service`

- 属于一个 FastAPI 应用实例；
- 保存统一装配的 Service 对象；
- 让同一个应用内的多个请求共享同一套 Service/Repository；
- 应用重启或测试重新 `create_app()` 时重新创建；
- 它不保存某张工单当前的业务状态。

### `Ticket.status`

- 属于某一张具体工单；
- 表示该工单当前处于 `open`、`pending_triage` 等哪个业务阶段；
- `TicketStatus` 枚举定义允许出现哪些状态；
- 生产环境中必须写入数据库，进程重启后由 Repository 重新查询。

### `AgentState`

- 属于一次 Agent 工作流；
- 保存当前目标、订单号、已查询事实、工具结果、当前节点和待审批操作；
- 跨多次 LLM 调用持续；
- 需要暂停和恢复时写入 checkpoint；
- 不等于跨任务保存用户稳定偏好的长期记忆。

重启恢复规则：

```text
TicketService → 由 create_app() 重新创建
Ticket.status → 从业务数据库恢复
AgentState → 从工作流 checkpoint 恢复
```

当前 Day 1 的内存 Repository 会随进程退出而丢失工单，因此不能作为生产持久化方案。

## 17. 异常传播、错误语义与可观测性

不同失败必须表达不同事实：

### 422：外部输入不符合契约

Schema 校验失败后直接停止，Service 和 Repository 都不执行。

### 404：已经完成查询，确认业务资源不存在

Repository 返回 `None`，Service 抛 `TicketNotFoundError`，全局 HTTP Exception Handler 映射成 404。Service 不抛 HTTPException，因为 Service 还可能被 Worker 或脚本调用。

### 503：基础设施暂时不可用

数据库超时表示“现在无法得出查询结果”，不等于“工单不存在”。Repository 抛出存储异常，Service 不能伪造成功或转换成不存在；全局异常处理器应映射为服务不可用类响应。

数据库写入失败时，即使 Service 已在内存构造 Ticket，也不能返回 201。只有 Repository 成功持久化后，创建用例才算成功。

```text
Repository 抛出存储异常
→ Service 停止并继续传播
→ Router 正常返回流程中断
→ 输出 Schema 不执行
→ Exception Handler 生成 5xx
→ Middleware 保留 request_id
→ 客户端收到错误
```

客户端错误响应只能包含稳定错误码、通用说明和 `request_id`。SQL、表名、数据库地址和完整堆栈属于内部信息，不能对外暴露。内部日志可以记录异常类型、调用栈和必要诊断数据，但密码、令牌、银行卡号和个人信息仍必须脱敏。

同一次 HTTP 请求的响应和相关日志共享同一个 `request_id`，用于从客户端报错定位服务器日志。下一次请求必须使用新的 request_id；以后跨多个请求的 Agent 工作流还需要更高层的 trace_id 或 workflow_id。

## 18. 官方依据

- [FastAPI Request Body](https://fastapi.tiangolo.com/tutorial/body/)：Pydantic 请求体、校验、JSON Schema 和 OpenAPI。
- [FastAPI Dependencies](https://fastapi.tiangolo.com/tutorial/dependencies/)：依赖声明、调用和注入过程。
- [FastAPI Handling Errors](https://fastapi.tiangolo.com/tutorial/handling-errors/)：异常和 `RequestValidationError` 处理。
- [FastAPI Testing](https://fastapi.tiangolo.com/tutorial/testing/)：`TestClient` 与 pytest 的基本测试方式。
- [FastAPI on PyPI](https://pypi.org/project/fastapi/)：安装方式、Starlette/Pydantic 依赖关系和当前版本信息。
- [HTTPX2 on PyPI](https://pypi.org/project/httpx2/)：当前测试客户端使用的 HTTP client 包和安装方式。
