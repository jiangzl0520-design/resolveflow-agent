# Day 1 面试记录

## 1. Day 1 开始前的 Day 0 复问

### 复问 1

问题：用户先说订单号是 10086，后来改成 10087。为什么 History 不能直接覆盖 10086？

用户原始回答：

> 因为history就是要记录全部的聊天过程

判断：通过。

补充：History 保存不可变的事实过程，便于审计、解释状态变化、排查 Agent 是否正确处理改口和重放任务。

### 复问 2

问题：主订单号改变后，为什么依赖旧订单号得到的物流、证明、方案和审批不能继续使用？

用户第一次回答：

> 因为如果不同编号使用同一个记录那么日志记录分不清楚 而且容易出错

判断：部分正确，缺少“派生数据依赖旧前提”的核心原因。

提示：这些信息都是以订单号 10086 为前提查询或推导出来的。

用户提示后回答：

> 因为物流状态、签收证明和退款方案，都是以“订单号是 10086”为前提查询或推导出来

复问结果：通过。

正确原理：主事实改变后，依赖旧主事实形成的派生数据必须失效或重新验证。旧数据可以留在 History 和审计记录中，但不能进入当前 Context 作为有效证据。

### 复问 3

问题：人工批准 150 元，但系统规则最多退款 100 元，为什么后端仍必须拒绝？

用户原始回答：

> 因为业务规则规定不能违反

判断：通过。

补充：业务资格与人工授权是独立门槛。人工可以授权具体合规操作，但不能修改系统硬规则。

## 2. Day 1 正式面试

状态：进行中。

面试必须在功能、自动化测试、原理文档和关键代码文档全部完成后，一次一道题进行。本文件将在每道题讲评后追加原始回答、错误点、正确解释和换场景复问结果。

### 第 1 题：创建工单的 HTTP 主链路

问题：客户端发送合法的创建工单请求后，请按照实际执行顺序说明请求怎样从 HTTP 入口一直执行到工单被保存并返回 201。

用户第一次回答：

> http router middleware schema service schema repository 存储 客户端

判断：未通过。主要问题是 Middleware 和 Router 顺序颠倒，Repository、存储和输出 Schema 的顺序不正确，同时客户端没有作为起点。

提示：客户端既是起点也是终点；请求先经过 Uvicorn/ASGI 和 Middleware，再进入 Router；区分输入 Schema 与输出 Schema；Service 调用 Repository。

用户提示后回答：

> 客户端 uvicorn middleware router schema校验 service repository存储 schema输出序列化 客户端

复问结果：通过。

补充后的完整链路：客户端 → Uvicorn → ASGI → Middleware → Router 匹配 → Pydantic 输入 Schema 校验 → 依赖注入 Service → Router 组装 Command → Service 创建领域 Ticket → Repository 保存 → 输出 Schema 序列化 → 响应再次经过 Middleware 添加响应头 → 客户端收到 201。

关键结论：请求链路和响应链路方向相反。Middleware 包围后续处理链，所以它既能在请求进入前写入 `request.state.request_id`，也能在响应返回时添加 `X-Request-ID`。

### 第 2 题：Router 与 Service 的职责边界

问题：既然 Router 已经取得通过 Pydantic 校验的数据，为什么不直接在 Router 中创建 Ticket 并写入 Repository，而要经过 TicketService？

用户第一次回答：

> 因为router只负责路由对接并不负责创建ticket的操作

判断：部分正确。知道 Router 不负责业务创建，但没有说明 Service 的职责以及分层带来的复用和一致性价值。

提示：创建入口以后还可能包括后台 Worker、批量导入或内部流程；思考把规则写在 Router 后会怎样。

用户提示后回答：

> 不知道

处理：切回老师完整讲解。Router 适配 HTTP 协议；Service 编排创建工单业务用例，包括生成 ID、设置初始状态、执行业务规则和调用 Repository。多个入口共享 Service，可以避免复制规则、状态不一致和业务测试依赖 HTTP。

换场景复问：新增“批量导入历史工单”的后台 Worker，它是否应该调用 HTTP Router？应该调用哪一层？为什么？

用户回答：

> 不应该调用http他应该调用ticketservice 因为router适配外部协议 service保存保存并编排可以复用的业务用例

复问结果：通过。

关键结论：Router 回答“外部客户端怎样通过 HTTP 使用系统”；Service 回答“系统怎样完成一个可复用的业务用例”。HTTP Router、后台 Worker 和批量脚本都可以作为入口调用同一个 Service。

### 第 3 题：HTTP Schema 与领域对象的边界

问题：`TicketCreate` 已通过 Pydantic 校验，为什么不直接把它当成内部工单对象，而要创建独立的领域 `Ticket`？

用户第一次回答：

> 方便管理

判断：未通过。没有说明外部输入契约与内部可信业务状态的区别。

提示：`TicketCreate` 只包含客户端允许提交的字段；领域 `Ticket` 还包含系统生成的 `id`、`status` 和时间。思考两者混用会造成什么问题。

用户提示后回答：

> 因为schema只包含提交的字段 领域对象还包括系统生成的一些对象 不知道分开后能避免什么问题

判断：部分正确。能够区分字段来源，但不了解安全边界、业务耦合和多入口复用问题。

完整讲解：Schema 是不可信的外部数据契约，负责校验和序列化；Domain 是经过业务规则构造的内部可信状态。分开可以阻止客户端控制系统字段，避免 HTTP 契约变化污染业务模型，并让 Worker、批量导入等入口转换到同一内部模型。

换场景复问 1：客户端额外提交 `status=resolved`、自定义 `id` 和 `created_at`，这些字段应由谁决定，为什么？

用户回答：

> 这些字段应该由系统决定 不知道为什么 应该由service创建这些字段

判断：部分正确。知道权限归属和负责层，但没有解释信任边界以及错误后果。

进一步讲解：如果客户端能直接写 `resolved`，就会绕过 Agent 调查、证据收集、方案生成、人工审批、动作执行和结果验证，造成数据库状态与真实业务状态矛盾。这属于越权字段写入或 Mass Assignment 风险。

换场景复问 2：数据库显示 `resolved`，但没有调查、审批和退款执行记录，这个状态是否可信？为什么？

用户回答：

> 不可信 因为必须要经过一个完整的处理过程否则缺少的步骤可能会出现问题因为可能是外来消息的污染导致的出错等

复问结果：通过。

关键结论：状态字段本身不是完成证据。可信状态必须由合法状态转移产生，并能由对应的调查、审批、执行和验证记录证明。外部客户端只能提交业务原材料，系统身份、生命周期状态和审计字段由 Service 和后端规则控制。

### 第 4 题：Repository 协议与数据库替换

问题：Day 2 会把内存存储替换为 PostgreSQL。为什么 TicketService 不应该直接编写 SQL 或依赖 SQLAlchemy？理论上应该主要替换哪一部分？

用户第一次回答：

> 不知道

提示：Service 只需要保存和查询工单，不应该决定这些能力由内存、PostgreSQL、SQLAlchemy 还是远程服务完成。思考 Repository 协议的作用。

用户提示后回答：

> servoce通过ticketrepository写入数据库 应该替换ticketrepository

判断：通过，但需修正表述。

正确表述：`TicketService` 调用 `TicketRepository` 协议请求保存或查询，不直接操作数据库。Day 2 保留协议和 Service，新增 PostgreSQL/SQLAlchemy Repository 实现，并在应用装配处用它替换 `InMemoryTicketRepository`。

关键结论：Repository 协议隔离“业务需要什么持久化能力”和“基础设施怎样实现这种能力”。数据库技术变化主要影响具体适配器，不应迫使业务用例重写。

### 第 5 题：输入校验失败时的短路

问题：客户端提交的 `subject` 只有空格。请求会在哪一层被拒绝？后面的 Service 和 Repository 会不会执行？为什么？

用户回答：

> schema拒绝 不一会执行 因为不符合输入格式要求

判断：通过。

补充：`StringConstraints` 先去除标题首尾空白，结果成为空字符串，再因 `min_length=1` 失败。FastAPI/Pydantic 在调用 Router 业务函数前产生 `RequestValidationError`，统一异常处理器返回 422。因此 Service 不会创建领域对象，Repository 也不会写入数据。

关键结论：边界校验会让请求提前失败，防止无效外部输入进入业务层并产生状态变化。

### 第 6 题：业务异常与 HTTP 异常映射

问题：查询格式正确但不存在的工单 ID 时，Repository、Service 和 HTTP 异常处理器分别做什么？为什么 Service 不直接抛 FastAPI 的 `HTTPException`？

用户第一次回答：

> service router 不知道

判断：部分正确。Service 会解释“不存在”，但 Repository 才先返回 `None`；404 由独立 HTTP 异常处理器生成，不是具体 Router 函数决定。

提示：Repository 返回 `Ticket | None`；Service 把 `None` 解释为业务异常；HTTP 异常处理器负责协议映射。思考后台 Worker 是否存在 HTTP 状态码。

用户追问：

> 专门的 HTTP 异常处理器把业务异常映射成 404，不是 Router 自己处理是什么意思 router和http之间的工作流程是router把业务结果转换成http响应再由http决定具体返回201还是404这些吗

完整讲解：HTTP 是协议，不是主动决策组件。成功时 Router 的装饰器声明 201，函数返回结果后由 FastAPI 序列化；失败时 Service 抛 `TicketNotFoundError`，Router 函数停止，FastAPI 分发到注册的异常处理器，由处理器构造 404 `JSONResponse`。把统一错误映射移出具体 Router 可以避免重复，并让 Service 被 Worker 等非 HTTP 入口复用。

澄清期间的问题：Router 具体负责什么？

用户回答：

> 根据请求方法和路径匹配接口 声明并接受经过校验的http输入 把外部schema转成内部command

判断：通过。补充职责是调用 Service，并把领域结果转换成 Response Schema；FastAPI 负责实际校验、依赖注入、异常分发和序列化执行。

换场景复问：后台 Worker 直接调用 `TicketService.get_ticket()`，工单不存在时会收到 404 还是 `TicketNotFoundError`？为什么？

用户回答：

> ticketnotfounderror 因为404是返回到客户端用的不是后台worker

复问结果：通过。

关键结论：`TicketNotFoundError` 表达可复用的业务失败；404 是 API 层针对 HTTP 客户端的协议表达。Worker 不经过 FastAPI HTTP 异常处理器，因此接收业务异常而不是 HTTP 状态码。

### 第 7 题：Middleware 与 request_id

问题：为什么 `request_id` 适合在 Middleware 中统一生成，而不应让每个 Router 分别生成？Middleware 在请求进入和响应返回时分别做什么？

用户回答：

> 在middleware中统一生成如果出现问题方便管理而且不同订单用的是同一套可以复用的方便修改 进入时加了request id 返回时加了x response id

判断：通过，需修正术语。

补充：Middleware 为所有 HTTP 接口提供一套横切逻辑，避免每个 Router 重复实现或遗漏。每个请求生成不同的 `request_id`；请求进入时写入 `request.state.request_id`，响应返回时把同一值加入响应 JSON 和 `X-Request-ID` 响应头。响应头不是 `X-Response-ID`。

关键结论：Middleware 包围后续 HTTP 处理链，适合处理所有接口都需要的横切能力。`request_id` 关联的是一次请求，不是永久工单。

### 第 8 题：ticket_id 与 request_id

问题：同一张工单创建一次、后来查询三次，应该有几个 `ticket_id` 和几个 `request_id`？两者分别追踪什么？

用户第一次回答：

> 都是一个

判断：未通过。混淆了长期业务标识和单次请求关联标识。

提示：`ticket_id` 跟随工单长期存在；Middleware 每收到一次 HTTP 请求都会生成新的 `request_id`；创建加三次查询共四次请求。

用户提示后回答：

> 一个ticketid 用来追踪工单本身 三个requestid 追踪查询过程

判断：部分正确。能够区分用途，但漏掉创建请求本身也有一个 `request_id`。

完整讲解：创建请求和三次查询请求都是需要独立追踪的 HTTP 请求，因此是一张工单、一个 `ticket_id`、四个 `request_id`。创建过程同样可能在校验、Service 或 Repository 中失败。

换场景复问：同一张工单经历一次创建、两次查询和一次更新，共有几个 `ticket_id` 和几个 `request_id`？

用户回答：

> 一个 四个

复问结果：通过。

关键结论：`ticket_id` 标识长期业务实体；`request_id` 标识一次短暂的 HTTP 调用链。同一业务实体可以对应很多次请求。

### 第 9 题：依赖注入与对象生命周期

问题：为什么 Router 不应该自己创建 `InMemoryTicketRepository` 和 `TicketService`，而应该通过 FastAPI 依赖注入取得统一装配好的 Service？

用户第一次回答：

> 这个是可以复用的但是router不可以复用 而且如果出现问题不需要一个一个修改代码

判断：部分正确。认识到统一复用和集中修改，但没有说明多个独立 Repository 会导致状态割裂。

提示：如果创建接口和查询接口分别创建新的内存 Repository，思考创建的数据能否被查询接口找到。

用户提示后回答：

> 一统一注入可以复用二如果通过router创建那么在每一次查询接口又要创建一个新的repository 之前创建的那个工单就没办法被查询接口找到了

复问结果：通过。

补充：应用工厂统一装配一个 Service/Repository，应用内的多个接口共享同一依赖。Day 2 更换 PostgreSQL 时主要修改装配和适配器；测试也可以替换依赖，不需要修改 Router。

关键结论：依赖注入同时解决依赖创建位置、对象生命周期、跨接口共享、实现替换和测试隔离问题。它不只是少写几行代码。

### 第 10 题：应用内共享与测试间隔离

问题：`create_app()` 为什么能同时满足同一应用内的接口共享 Repository，以及不同自动化测试之间不共享旧数据？

用户第一次回答：

> 不知道

提示：每调用一次 `create_app()` 都会创建新的 Repository 和 Service，并保存到这一个应用的 `state`；测试 fixture 为每个测试重新调用 `create_app()`。

用户提示后回答：

> 因为同一次测试是从同一个state里查询数据 但是不同会调用不同的creat

复问结果：通过。

正确解释：同一次测试持有同一个应用实例，创建和查询接口从同一 `application.state` 取得 Service，因此共享 Repository。下一个测试重新调用 `create_app()`，获得新的应用、Service 和 Repository，不会看到前一个测试的数据。

关键结论：依赖生命周期不是简单的“全局或不全局”。依赖可以在单个应用实例内共享，同时通过应用工厂在测试实例之间隔离。

### 第 11 题：TestClient 的验证边界

问题：`TestClient` 没有启动 Uvicorn 或占用网络端口，它实际验证了什么，又没有验证什么？为什么不能说真实网络部署已经通过？

用户第一次回答：

> 不知道

提示：`TestClient` 直接调用 FastAPI ASGI 应用，仍经过应用内部链路，但不经过真实网络端口、Uvicorn 进程、反向代理、HTTPS 和容器网络。

用户提示后回答：

> 可以证明Middleware → Router → Schema 校验 → 依赖注入
> → Service → Repository → 异常处理 → 响应序列化正常 不能证明网络端口、Uvicorn 进程、反向代理、HTTPS、容器网络

复问结果：通过。

关键结论：TestClient 是应用级 ASGI 集成测试。它证明应用内部请求链路和断言覆盖的业务行为，不证明真实 Server、操作系统网络、代理、TLS 或容器部署配置。测试报告必须明确验证边界。

### 第 12 题：路由 404 与业务资源 404

问题：请求系统没有注册的 URL，以及请求正确 URL 但工单不存在，都可能返回 404。两者分别由哪里产生，表达什么问题？

用户第一次回答：

> 第一种404由schema产生 第二种由service产生

判断：未通过。Schema 只有在路由已经匹配后才执行，无法判断一个未注册 URL；Service 产生业务异常，但最终 HTTP 404 由异常处理器构造。

提示：未注册 URL 由 FastAPI/Starlette 路由系统发现；业务资源不存在时 Repository 返回 `None`、Service 抛异常、Exception Handler 映射 404。

用户提示后回答：

> 第一个404表示接口不存在第二个表示业务资源不存在 第一个经过了客户端 http然后由fastai发现不存在 第二个经过了客户端 http router service发现异常Exception Handler 转成 HTTP 404

复问结果：通过。

补充：第二条完整链路还包含输入解析和 Repository 查询。相同 HTTP 状态码不代表相同根因，应结合统一业务错误码、日志和 `request_id` 区分。

关键结论：路由 404 表示没有匹配的 HTTP 接口；业务 404 表示接口存在，但请求的领域资源不存在。

### 第 13 题：外部契约变化与内部业务隔离

问题：外部 API 把 `customer_id` 改成 `customerId`，内部业务概念不变。哪一层适配，哪些层不应被迫修改，为什么？

用户第一次回答：

> 主要由schema适配 不知道

判断：部分正确。识别了 HTTP Schema，但没有说明 Router 的转换职责以及业务层保持不变的架构原因。

提示：Schema 可以把外部字段解析出来，Router 转成内部 `CreateTicketCommand.customer_id`；这是 HTTP 契约变化，不是业务含义变化。

用户提示后回答：

> 因为schema已经把id解析成了内部字段不需要他们跟着修改不知道

判断：部分正确。知道后续层无需修改，但不了解协议细节泄漏与紧耦合问题。

完整讲解：外部字段名属于传输契约，内部字段名属于业务语言。Schema 和 Router 组成适配边界；如果 Service、Domain 和 Repository 因 HTTP 命名变化而修改，说明协议细节泄漏进业务层，会扩大改动范围并妨碍 Worker、CSV 等入口复用。

换场景复问：手机 API 使用 `customerId`，CSV 使用 `Customer Number`，最后都要形成内部 `customer_id`。哪些部分分别适配，哪些业务层共同复用？

用户第一次回答：

> 手机 API 提交字段 customerId需要分别适配，CSV 文件使用列名 Customer Number可以共同复用 因为外部字段是客户端和 HTTP API 之间约定的名字内部字段router会进行翻译转成内部command

判断：未通过。两个外部入口都需要适配，CSV 不能直接复用 HTTP Router。

进一步讲解：手机 API 使用 HTTP Schema/Router；CSV 使用 CSV Parser/Import Adapter；两者都转换成同一 `CreateTicketCommand`，再共同调用 Service、Domain 和 Repository。

再次复问：补全“手机 API → 适配器 → Command → 业务层”和“CSV → 适配器 → Command → 业务层”。

用户回答：

> HTTP Schema + Router 适配TicketService  CSV Parser + Import Adapter 适配TicketService

复问结果：通过。

关键结论：每一种外部协议或文件格式都有自己的输入适配器；适配后统一进入内部 Command 和 Service。外部格式变化应被限制在系统边界，不能扩散到核心业务层。

### 第 14 题：初始业务状态应该由哪一层决定

问题：所有新工单的默认状态从 `open` 改为 `pending_triage`。这个修改应该放在哪一层？为什么不能分别写在 HTTP Router 和 CSV Import Adapter 中？

用户第一次回答：

> 不知道

提示：Router 和 Import Adapter 都只是不同入口；“所有新工单采用什么初始状态”是统一业务规则，应由两个入口共同调用的内部业务层集中执行。

用户提示后回答：

> service里的state

判断：部分正确。识别到了 Service，但“Service 里的 state”不准确。Service 是业务用例编排者；`status` 是领域对象 `Ticket` 的业务状态，不是 Service 自己保存的状态，也不是 Agent State。

完整讲解：当前实现需要在 Domain 的 `TicketStatus` 中定义合法的 `PENDING_TRIAGE` 状态，再由 `TicketService.create_ticket()` 创建 `Ticket` 时设置该初始状态。HTTP Router 和 CSV Import Adapter 只把不同格式的外部输入转换为同一种内部 `CreateTicketCommand`，不能各自决定业务初始状态；Repository 只保存已经创建好的领域对象。

换场景复问：退款申请既可以通过 HTTP 创建，也可以通过批量文件导入。所有新退款申请必须从 `pending_review` 开始，并且客户端不能指定状态。哪一层设置状态，两个适配器分别做什么？

用户第一次回答：

> TicketService.create_ticket()设置pending_review 不知道

判断：部分正确。状态应由统一的 Service 创建用例设置，但没有说明两个外部适配器的职责。

提示：HTTP 接收 JSON，文件入口读取 CSV；两者都只应把自己的外部数据格式转换成同一种内部 Command，而且 Command 不接收客户端指定的 `status`。

用户提示后回答：

> 将外部的命令翻译成command

复问结果：通过。用词需要修正为“把外部输入数据翻译成内部 Command”，因为 JSON 或 CSV 行在进入系统时还不是内部命令。

关键结论：统一业务规则由 Service/Domain 执行；入口适配器只负责协议和格式转换。当前简单架构中，Domain 定义合法状态，Service 的创建用例选择初始状态，Repository 负责持久化。这样 HTTP、CSV、Worker 等入口都能得到一致结果。

### 第 15 题：输出 Schema 与响应安全边界

问题：`TicketService` 返回领域对象 `Ticket` 后，为什么 Router 还要将其转换成 `TicketRead` 和 `TicketResponse`，而不是把领域对象原样返回给客户端？输出 Schema 解决了什么问题？

用户第一次回答：

> 统一规定客户端返回格式 方便阅读

判断：部分正确。输出 Schema 可以统一响应格式，但核心价值不只是可读性。

提示：如果领域对象以后增加 `internal_risk_score`、`reviewer_notes` 和 `fraud_flag` 等内部字段，直接返回完整领域对象可能泄露内部信息。

用户提示后回答：

> 会发生数据泄露

判断：方向正确但不完整。

完整讲解：输出 Schema 是输出字段白名单和稳定的 API 契约。它隔离 Domain 与外部响应，防止内部字段泄露；校验 Service 返回的数据是否符合外部契约；把 UUID、时间和枚举等 Python 类型序列化成 JSON 类型；还通过统一响应外壳提供 `data` 和 `meta` 等固定结构。

换场景复问：退款领域对象包含完整银行卡号、内部风控分数和退款状态，但客户端只能看到脱敏卡号和退款状态。应该由什么组件规定允许返回的字段？为什么不能直接返回退款领域对象？

用户回答：

> schema 因为里面会包含不能返回的内部信息

复问结果：通过。准确组件是输出 Schema；它只声明允许公开的字段，避免领域对象中的内部信息被直接暴露。

关键结论：输入 Schema 保护系统不接收不可信字段，输出 Schema 保护系统不泄露内部字段。二者分别位于进入和离开系统的边界，中间的 Service/Domain 保持内部业务表达。

### 第 16 题：HTTP Schema、Command 与业务层解耦

问题：Router 已经取得经过 Pydantic 校验的 `TicketCreate`，为什么还要转换成 `CreateTicketCommand`？为什么不能让 `TicketService.create_ticket()` 直接接收 HTTP Schema？

用户第一次回答：

> 为了隔离外部语言和内部语言防止外部信息污染产生错误指令

判断：核心方向正确，但“产生错误指令”不准确。主要问题是业务层对 HTTP/Pydantic 产生技术耦合，而不一定是恶意数据污染。

提示：`TicketCreate` 属于 HTTP/Pydantic 边界模型，`CreateTicketCommand` 属于内部创建工单用例。CSV 和后台 Worker 没有 HTTP 请求；如果 Service 直接接收 HTTP Schema，它们也会被迫构造 HTTP 对象。

用户提示后回答：

> 不知道

完整讲解：HTTP Schema 描述外部字段名称、JSON 类型、必填规则和 OpenAPI 契约；Command 描述内部业务用例需要的参数。Router、CSV Adapter 和 Event Adapter 分别将自己的外部格式转换成同一种 Command。这样 Service 不依赖 FastAPI、Pydantic 或某一种入口，HTTP 契约变化也不会传播到业务层。

换场景复问：工单既能通过第三方 Webhook 创建，也能由内部定时任务创建。如果 Service 直接接收第三方 `WebhookPayload`，会造成什么问题？两个入口应该共同转换成什么？

用户第一次回答：

> 不知道

进一步讲解：如果 Service 接收第三方 Payload，内部定时任务会被迫伪造 Webhook 对象，第三方字段变化还会传播到 Service 和定时任务。正确结构是 `Webhook Adapter → CreateTicketCommand → TicketService` 和 `Scheduler Adapter → CreateTicketCommand → TicketService`。

拆小复问：补全 `Webhook Adapter → ________ → TicketService` 和 `Scheduler Adapter → ________ → TicketService`。

用户回答：

> creatticket command

复问结果：在完整讲解和结构提示后通过。后续还需要换场景抽查能否独立解释。

关键结论：Schema/外部 Payload 归属于具体入口，Command 归属于内部业务用例。外部适配器依赖内部 Command 和 Service，核心业务层不能反向依赖某一种外部协议的数据模型。

### 第 17 题：适配层与业务层的依赖方向

问题：A. Router 导入并调用 `TicketService` 和 `CreateTicketCommand`；B. `TicketService` 导入并使用 Router 和 `TicketCreate`。哪种依赖方向正确，为什么？

用户回答：

> a 要先经过router翻译之后才能调用service 和CreateTicketCommand

判断：通过。Router 位于外部适配层，应将 HTTP 输入翻译为内部 Command，再调用 Service；Service 不能反向依赖 Router 或 HTTP Schema。

用词纠正：Router 是“构造 `CreateTicketCommand`，再调用 `TicketService`”。Command 是承载业务用例参数的数据对象，不是一个被调用的服务。

关键结论：依赖方向从外向内：HTTP Router/CSV Adapter/Worker Adapter 可以依赖 Command 和 Service，Service 不依赖具体入口。这样替换外部协议不会迫使业务层改变。

### 第 18 题：输入格式校验与业务规则校验

问题：退款金额必须是数字且大于 0，并且不能超过订单实际支付金额。这两类规则分别主要放在哪一层？为什么不能全部只写在 HTTP Schema 中？

用户回答：

> 1应该放在schema中 2应该放在后端的退款审核中 因为2金额多少不是schema审核的内容

判断：基本通过。第一类可以由输入 Schema 做类型解析和快速字段校验；第二类应明确放在 Service/Domain 的业务规则校验中，而不能笼统地称为“后端审核”。

补充纠正：

- “是否为数字”是外部数据的格式问题，由 Schema 解析和拒绝。
- “大于 0”可以在 Schema 提前拒绝；如果它是所有入口都必须满足的领域不变量，Service/Domain 也必须保证它，不能只依赖 HTTP。
- “不能超过实付金额”需要读取订单事实并比较，属于业务规则，由 Service 编排查询并由业务逻辑判断。Schema 不应访问 Repository，也不掌握数据库中的实付金额。
- CSV、Worker 或 Agent 工具调用可能不经过 HTTP Schema，所以核心业务规则必须在所有入口共同经过的业务层再次保证。

关键结论：Schema 负责边界处的格式和局部字段约束；Service/Domain 负责依赖业务事实的规则和不可绕过的不变量。边界校验提供快速失败，核心校验提供最终安全保证。

### 第 19 题：Schema 校验、业务事实与 Policy Engine

问题：Agent 生成 `order_id=10086`、`refund_amount=200`，工具 Schema 确认金额是大于 0 的数字，但订单实际只支付了 100 元。能否执行退款，由哪一层拒绝，为什么 Schema 通过不等于安全？

用户第一次回答：

> 不能执行退款 不知道 因为schema只审核输入是否符合规则等不审核不具体内容

判断：部分正确。知道不能执行，也知道 Schema 只检查输入形状，但没有识别业务执行层和策略层。

提示：实际支付金额需要由后端查询，并在调用支付系统之前由不可绕过的确定性规则检查。

用户提示后回答：

> 应该由policyengine拒绝 不知道为什么

判断：结论正确，但没有掌握原因。

完整讲解：Schema 只证明参数格式合法；RefundService 查询订单、退款状态和实付金额等真实业务事实；Policy Engine 在执行资金副作用前强制检查金额上限、审批要求和操作范围；退款执行器只接受 `ALLOW` 决策。模型可能幻觉、算错或遭受 Prompt Injection，因此安全规则不能由模型自行遵守。Service 可以提前拒绝，Policy Engine 是靠近危险操作的最终强制防线，并记录可审计的决策原因。

换场景复问：人工审批允许订单 `10086` 退款 80 元，但 Agent 最终提交订单 `10087`、退款 80 元。哪个组件必须拒绝，依据是什么？

用户第一次回答：

> RefundService拒绝 拒绝的依据是单号不匹配

判断：拒绝依据正确，但最终强制组件识别错误。RefundService 可以提前发现，审批范围匹配属于 Policy Engine 必须执行的授权策略。

进一步讲解：审批绑定的是具体操作快照，例如 action、order_id 和 amount；实际操作任何关键参数变化都会使原审批失效。Policy Engine 必须比较实际工具调用与审批范围，只有 `ALLOW` 才能进入退款执行器。

拆小复问：补全 `RefundService 提供订单事实 → ________ 比较实际操作与审批范围 → 只有 ALLOW 才能退款`。

用户回答：

> Policy Engine

复问结果：在完整讲解和结构提示后通过。后续需要无提示抽查 Service 业务校验与 Policy Engine 授权校验的区别。

关键结论：合法格式不等于合法业务，更不等于获得执行权限。高风险操作需要“Schema 格式校验 → Service 获取并验证业务事实 → Policy Engine 强制授权 → Executor 执行”的纵深防御。

### 第 20 题：业务资格与执行授权

问题：订单状态允许退款、实付 100 元、申请退款 80 元，但公司规定超过 50 元必须人工审批，当前没有审批记录。哪一层判断业务退款条件，哪一层因缺少审批阻止执行，为什么要分层？

用户第一次回答：

> RefundServicePolicy Engine 不知道

判断：组件选择正确，但没有说明两层分别回答的核心问题。

提示：RefundService 回答“这笔退款在业务上是否成立”；Policy Engine 回答“即使业务成立，现在是否被授权执行危险操作”。

用户提示后回答：

> 因为需要确定当前业务是否在人工审批范围内 以及是否有人工审批

复问结果：通过。

关键结论：业务资格和执行授权是两个不同维度。Service 根据订单事实判断是否可退、可退多少；Policy Engine 检查审批、操作范围和其他强制安全策略。只有业务条件成立并且授权决策为 `ALLOW`，执行器才能产生真实副作用。

### 第 21 题：应用状态、领域状态与 AgentState

问题：`application.state.ticket_service`、`Ticket.status` 和 Agent 工作流中的 `AgentState` 是否是同一概念？分别保存什么，属于哪个范围，生命周期多长？

用户第一次回答：

> application.state.ticket_service负责保存当前tichet暂存了哪些状态TicketStatus 枚举负责定义系统允许出现哪些状态AgentState表示agent的工作状态

判断：第 1 项错误，第 2 项回答偏题，第 3 项方向正确但不具体。

提示：`application.state.ticket_service` 保存统一装配的 Service 实例；`Ticket.status` 是一张具体工单的当前业务状态；`TicketStatus` 枚举定义允许状态；`AgentState` 保存一次工作流的目标、事实、节点和等待事项。

用户提示后回答：

> 第一个属于应用 第二个属于业务实体 第三个属于agent工作流

判断：归属正确，但缺少生命周期。

完整讲解：

- `application.state.ticket_service` 随一个 FastAPI 应用实例存在，应用重启或测试重新 `create_app()` 时重新创建。
- `Ticket.status` 随工单业务实体长期存在，生产环境中应持久化到数据库并跨进程重启恢复。
- `AgentState` 随一次 Agent 工作流存在，跨多次 LLM 调用持续；需要暂停、恢复时应写入 checkpoint。它不同于跨任务保存用户偏好的长期记忆。

换场景复问：Agent 已完成订单查询并暂停等待人工审批，这时 FastAPI 重启。Service、工单状态和 AgentState 分别怎样恢复？

用户第一次回答：

> 不知道1

进一步讲解：Service 是可重建的业务执行对象，由应用工厂重新装配；工单是长期业务数据，从数据库查询；等待审批的工作流从 checkpoint 恢复。当前 Day 1 的内存 Repository 会在进程退出时丢失工单，这正是后续引入 PostgreSQL 的原因。

拆小复问：补全 `TicketService → 重启后____`、`Ticket.status → 从____恢复`、`AgentState → 从____恢复`。

用户回答：

> 重新创建 数据库 工作流的checkpoint 节点恢复

复问结果：在完整讲解和结构提示后通过。

关键结论：应用服务实例可以重建，业务实体状态必须持久化，长流程 AgentState 必须 checkpoint。三者的所有者、生命周期和恢复方式完全不同。

### 第 22 题：新增 Slack 输入适配器

问题：增加 Slack 机器人创建工单入口时，为了不修改现有 Service、Domain 和 Repository，应新增什么适配组件？Slack 消息最终转换成什么对象，再交给谁执行？

用户第一次回答：

> 不知道

提示：Slack 与 HTTP、CSV 一样是外部输入协议。参考 `HTTP JSON → HTTP Schema + Router → CreateTicketCommand → TicketService` 和 `CSV → Parser + Adapter → CreateTicketCommand → TicketService` 补全 Slack 链路。

用户提示后回答：

> router CreateTicketCommand

判断：`CreateTicketCommand` 正确，但只回答 Router 不完整。Router 只负责 HTTP 方法和路径匹配，不是所有外部协议的通用适配器。

完整讲解：如果 Slack 通过 HTTP Webhook 进入，链路是 `Slack 消息 → Webhook → SlackEventSchema → Router → Slack Input Adapter → CreateTicketCommand → TicketService`。如果通过 Slack SDK 消费事件，可能没有 HTTP Router，但仍必须有 Slack Input Adapter。只有该适配器理解 Slack 的 `event.text`、`user` 和 `channel` 等字段。

拆小复问：只有哪个组件理解 Slack 字段，它把字段转换成什么？

用户回答：

> Slack Input Adapter 业务层可以理解的描述

判断：第一项正确，第二项概念模糊。适配结果不是自由文本描述，而是结构化的内部业务用例输入。

再次追问具体类型名，用户回答：

> 不知道

进一步讲解：适配器应构造 `CreateTicketCommand(customer_id, subject, description, category)`；Service 接收该 Command 创建领域对象。Command 是外部适配器和内部业务用例之间的稳定边界。

最终复问：补全 `Slack 消息 → ________ → ________ → ________`。

用户回答：

> Slack Input Adapter → CreateTicketCommand → TicketService

复问结果：在完整讲解和结构提示后通过。后续需要无提示迁移复查。

关键结论：每种外部协议需要自己的输入适配器；Router 只属于 HTTP 入口。适配器把外部协议数据转换为明确的内部 Command，所有入口再共同调用 Service。

### 第 23 题：Command 与领域对象的创建权限

问题：为什么 Slack Input Adapter 只能创建 `CreateTicketCommand`，不能直接创建完整 `Ticket`？`id`、`status`、`created_at` 应由谁决定，为什么？

用户回答：

> 因为这些是外部消息 内部消息和外部消息是隔离的 这些字段应该由业务层service决定

判断：通过。识别了外部输入与内部业务对象之间的边界，也识别出系统字段应由 Service 决定。

用词纠正：`id`、`status` 和 `created_at` 不是“内部消息”，而是系统权威字段。外部适配器只提供创建用例需要的业务输入；Service 按统一规则生成完整领域对象，防止客户端或第三方入口伪造工单 ID、初始状态和系统时间。

关键结论：Adapter 创建 Command，Service 创建 Domain Entity，Repository 保存 Domain Entity。这个顺序同时实现协议隔离、业务规则集中和系统字段可信。

### 第 24 题：Repository 的持久化职责边界

问题：为什么 `TicketRepository.add()` 应接收完整 `Ticket`，而不是 `TicketCreate` 或 `CreateTicketCommand`？Repository 只负责什么，不应负责什么？

用户第一次回答：

> 不知道

提示：Service 已经根据 Command 生成 ID、设置初始状态和创建时间，形成完整 Ticket。如果 Repository 接收 Command，它将被迫自己补充这些字段。

用户提示后回答：

> 会把内部指令错误的放进repository

判断：不准确。核心问题不是 Command 出现在 Repository，而是 Repository 会被迫创建领域对象并决定业务规则。

完整讲解：`repository.add(ticket)` 的输入是完整领域对象，输出是成功保存后的领域对象。Service 先创建 Ticket，Repository 再持久化。Repository 负责保存、查询以及数据库记录与领域对象之间的转换；不负责 HTTP Schema、默认状态、审批规则、退款判断、HTTP 状态码或 Agent 决策。写入成功前对象只存在于当前执行内存，成功后才进入持久化存储；写入失败必须报告异常，不能返回假成功。

换场景复问：内存 Repository 把新工单状态设为 `open`，PostgreSQL Repository 设为 `pending_triage`。错在哪里，初始状态由谁决定，两种 Repository 只做什么？

用户第一次回答：

> 说明业务层规则错误的泄露进了存储层 不知道 应该只存储完整的ticket

判断：已识别规则泄漏和 Repository 的保存职责，但没有识别初始状态的所有者。

进一步提示：Domain 的状态枚举定义合法状态，Service 创建用例选择新实体的初始状态，Repository 只保存。

拆小复问：补全 `Domain → 定义____`、`TicketService → 决定____`、`Repository → 负责____`。

用户回答：

> 允许出现哪些状态 新工单出现哪个初始状态 保存创建好的ticket

复问结果：通过。

关键结论：Domain 定义合法状态集合，Service/Domain 创建逻辑决定初始业务状态，Repository 只持久化完整实体。更换存储实现不能改变业务含义。

### 第 25 题：持久化失败与异常传播

问题：Service 已在内存构造完整 Ticket，但 PostgreSQL Repository 保存时连接失败。能否返回 201，是否创建成功，异常如何传播并转换为 HTTP 响应？

用户第一次回答：

> 不能 不算 service发现异常router schema http

判断：前两项正确，异常链路错误。数据库失败由 Repository 首先发现；Router 正常返回被中断，输出 Schema 不会执行。

提示：补全 `Repository 抛异常 → Service ____ → Router 中断 → Exception Handler ____ → Middleware 加 request_id → HTTP 5xx`。

用户提示后回答：

> 不知道

完整讲解：Repository 抛出存储异常；Service 不能吞掉异常或伪造成功，应继续传播，或者转换为不泄漏数据库细节的统一应用异常；Router 无法构造成功响应；全局 Exception Handler 映射为 503/500；Middleware 为错误响应保留 request_id。内存中构造过 Ticket 不等于持久化创建成功。

换场景复问：查询工单时数据库超时，能否当成工单不存在返回 404，应该返回哪类错误，为什么？

用户第一次回答：

> 不能 因为查询超时不等于找不到

判断：核心区别正确，但没有给出错误类别。

再次追问 404 与 503 的选择，用户回答：

> 503 因为是数据库连接不上 不是数据缺失

复问结果：通过。

关键结论：Repository 返回 `None` 才能表示已完成查询且资源不存在；数据库超时表示无法确认结果，应返回服务不可用类错误。异常不能被错误地转换为业务成功或业务不存在。

### 第 26 题：错误信息安全与 request_id 关联

问题：数据库异常包含 SQL、表名、数据库地址和完整堆栈。哪些信息可以返回客户端，哪些只能写入内部日志，request_id 有什么作用？

用户第一次回答：

> 表名 sql可以返回给客户端 其他的只能写入内部日志 区分每一个操作这样在当操作出现问题的时候便于快速找到问题

判断：前两项错误，request_id 方向正确。SQL 和表名同样属于内部实现信息，可能帮助攻击者推断数据库结构，不能返回客户端。

提示：客户端只获得稳定错误码、通用说明和 request_id；内部日志记录异常类型、调用栈及必要诊断信息。密码、令牌和个人敏感数据即使在日志中也要脱敏。

用户提示后回答：

> error_code
> 通用错误说明
> request_id 记录异常类型、调用栈和必要的数据库诊断信息同样的一次操作下出现的错误用同一个id

复问结果：通过。

补充纠正：准确范围是“同一次 HTTP 请求”的错误响应和相关日志共享一个 request_id；下一次请求生成新的 request_id。一个跨多次请求的长 Agent 工作流以后还需要更高层的 trace/workflow 标识。

关键结论：外部错误响应必须稳定且最小化，内部日志必须足够诊断但仍要脱敏；request_id 将一次请求的客户端报错与服务器日志关联起来。

## 规则调整后的 Agent 专项复核

后续面试规则已调整为只提问 Agent 相关内容。原第 27 题“邮件 Webhook 的后端适配链路”属于后端问题，已取消且不计入掌握验收；相关知识只保留讲解，不再复问。

### Agent 专项第 1 题：Observation、事实来源与下一步动作

问题：用户称订单 `10086` 未收到并要求退款；订单工具返回已发货，物流工具返回已签收；公司规则要求此类情况先查询签收证明。AgentState 应保存什么，下一步 Action 是什么，为什么不能直接退款？

用户回答：

> 应该保存订单工具：订单已发货
> 物流工具：包裹显示已签收但是客户说未收到货并要求退款 下一步应该选择先查询签收证明 因为observation信息和用户反馈信息有冲突

判断：通过。正确识别了工具 Observation、用户反馈冲突和下一步签收证明查询。

补充纠正：`订单已发货` 和 `物流已签收` 是带工具来源的已确认事实；`用户称未收到` 是带用户来源的 claim，不能被提升为同等可信的已确认外部事实。AgentState 还应保存当前目标、订单号、冲突标记、尚未取得签收证明和下一步待执行动作。缺少必要证据时不能进入退款判断，更不能直接产生资金副作用。

关键结论：AgentState 不是原样堆积消息，而是保存带来源和可信等级的事实、主张、冲突、缺失证据及当前计划。下一步动作由当前状态、业务规则和缺失信息共同决定。

### Agent 专项第 2 题：工具成功与证据有效不是一回事

问题：签收证明工具返回 HTTP 200，但图片模糊且没有订单号、收件人或运单号，无法确认属于订单 `10086`。能否标记证据查询成功，AgentState 如何更新，下一步怎样处理？

用户回答：

> 不能 更新为agent已经调用查询签收证明工具但是无法确认订单是否属于10086 下一步应该转入人工进一步审核

判断：通过。正确区分了工具调用完成与证据有效，并选择了安全的人机协同路径。

补充：HTTP 200 只证明工具在传输层返回成功，不证明 Observation 满足当前任务。AgentState 应记录原始 Observation 的来源、查询参数、语义验证结果 `inconclusive/invalid`、失败原因和尝试次数，不能设置 `proof_verified=true`。如果存在可靠的重试或替代查询策略，可以有限恢复；如果无法自动取得可信证据，则暂停并转人工，不能继续退款。

关键结论：Agent 必须验证工具结果，而不是只检查调用是否成功。Observation 要经过与目标和实体标识绑定的语义验证，验证失败也必须形成明确状态，供重试、改计划、暂停或人工接管使用。

### Agent 专项第 3 题：有限 Context 的信息选择

问题：聊天历史很长，本轮仍处理订单 `10086` 未收到货。公司规则、当前目标与订单号、工具结果及来源、签收证明无效原因、旧寒暄、另一个已结束订单的完整调查、等待人工状态中，哪些必须优先进入 Context，哪些可以压缩或丢弃？

用户回答：

> abcdg 因为另外两个对于这个任务来说不影响

判断：通过。

正确选择：

- A 系统和业务规则：约束 Agent 可以采取的动作；
- B 当前目标和主实体：决定本轮要解决什么；
- C 带来源的已确认事实：支持推理和下一步选择；
- D 证据无效原因：防止 Agent 把失败结果误当成功或无意义重试；
- G 当前等待人工的状态：决定工作流必须暂停，不能越过节点。

E 旧寒暄与任务无关，可以丢弃；F 其他已结束订单的完整轨迹不能直接塞入当前 Context。如果其中存在可复用经验，应先提炼成经过验证的规则或检索结果，而不是携带整段旧过程。

关键结论：Context 工程不是把 History 全塞给模型，而是根据当前目标选择规则、实体、可信事实、未解决冲突、失败原因和流程状态，同时丢弃无关内容，控制 Token 并减少旧信息干扰。

### Agent 专项第 4 题：主事实变化与派生状态失效

问题：Agent 正等待订单 `10086` 退款方案的人工审批，用户把订单号修改为 `10087`。History、AgentState、Context、旧证据、退款方案和审批应怎样处理，下一步是什么？

用户第一次回答：

> history应该完整记录之前的10086订单号写错了 现在转入查询10087 agentstate应该保存为现在要查询10087订单 context删除之前10086订单开始更新10087有关的信息 下一步是重新审批一遍

判断：部分正确。History 和当前订单号更新正确，但不能删除旧记录，也不能在缺少新订单调查和新方案时立即重新审批。

提示：保留修改事实和审计记录，将依赖 `10086` 的物流证据、方案和审批授权标记为 `stale/invalidated`，先重新调查 `10087`，重新形成方案后才能发起新审批。

用户提示后回答：

> 把之前的修改事实和审批记录保留 把旧的物流证据退款方案标记为失效 下一步应该先重新调查10087的订单物流和签收证明等

复问结果：通过。

补充：旧审批记录作为审计历史保留，但它授予的执行权限也必须失效，不能用于订单 `10087`。

关键结论：History 记录事实怎样变化；AgentState 保存当前有效主事实，并使依赖旧主事实的证据、计划、提案和审批授权失效；Context 只给当前推理提供有效信息，同时保留必要的修改说明防止模型误用旧事实。

### Agent 专项第 5 题：Observation 驱动动作选择与终止

问题：订单查询可能得到未付款且已取消、已发货仍在运输、已签收但用户否认收到三种 Observation。Agent 应怎样选择不同动作，为什么不能全部执行固定的查询和退款流程？

用户第一次回答：

> a应该选择记录下进程然后保存 b应该继续跟踪订单状态 c应该去查询签收证据 因为每个工单的状态可能不一样 如果都选选择同一套流程会浪费很多的内存算力等

判断：部分正确。C 正确，B 方向基本正确；A 缺少明确终止结果。固定流程的主要风险是动作不符合业务事实并可能造成错误副作用，资源浪费只是次要问题。

提示：A 没有支付资金可退，应向用户说明并结束退款任务；B 可以继续跟踪或说明运输状态，但不能进入退款或签收争议处理；Agent 必须根据 Observation 更新 State 后再选择 Action。

用户提示后回答：

> a应该明确说明订单未付款且已经取消然后结束此次任务 b继续跟踪这个订单状态但是不能进入退款或者签收争议处理 因为如果执行不符合当前事实的动作可能会造成错误退款

复问结果：通过。

补充：C 在“已签收但用户否认收到”时查询签收证明，验证后再决定继续调查、暂停或转人工，不能直接退款。

关键结论：真正的 Agent Loop 是 `State → Action → Observation → State 更新 → 验证/改计划/终止`。不同 Observation 必须产生不同分支；固定顺序不但低效，还会忽略事实、违反规则并触发危险动作。

### Agent 专项第 6 题：不能只评估最终回复

问题：轨迹 A 在查询订单、物流和签收证明后，因证据无效而转人工；轨迹 B 只查询订单便直接转人工。两者最终回复相同，评估系统还必须检查什么，为什么？

用户第一次回答：

> 还必须检查agent历史工作状态

判断：方向正确但过于笼统，无法形成可执行评估规则。

提示：需要检查必需工具、工具参数、Observation、State 更新、结果验证、规则合规和终止原因。

用户提示后回答：

> 因为b没有调用该调用的工具 没有验证工具结果 没有进行判断转人工的终止原因是否成立

复问结果：通过。

完整评估维度包括：

- 是否选择并实际调用了当前规则要求的工具；
- 工具参数是否绑定正确订单和当前任务；
- Observation 是否记录来源并正确进入 AgentState；
- Agent 是否验证工具结果的相关性和可信度；
- 状态转移和动作顺序是否遵守业务与安全规则；
- 暂停、转人工或终止是否具有可验证原因；
- 最终回复是否与真实执行轨迹和最终状态一致。

关键结论：Agent 评估必须同时检查任务结果和执行过程。只看最终文本会把“碰巧说对”误判为“正确完成”，无法发现漏工具、错参数、跳过验证、越权动作和虚假终止。

## Agent 专项复核结论

本轮共完成 6 道 Agent 专项题，覆盖 Observation、事实来源、工具结果验证、Context 选择、主事实变化后的状态失效、动态动作选择、终止条件以及轨迹评估。原第 27 题后端适配问题已按新规则取消，不再作为待答题。
