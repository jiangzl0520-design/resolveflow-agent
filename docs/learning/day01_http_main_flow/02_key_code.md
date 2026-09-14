# Day 1 关键代码解释

## 1. 应用工厂和依赖装配

关键位置：`app/main.py`

```python
def create_app() -> FastAPI:
    application = FastAPI(...)
    repository = InMemoryTicketRepository()
    application.state.ticket_service = TicketService(repository)
    ...
    return application
```

输入是当前应用需要的配置和代码依赖，输出是一套彼此隔离的 FastAPI 应用实例。执行时先创建具体 Repository，再把它交给 Service，最后把 Service 放进应用状态。这样写是为了集中完成“依赖装配”，而不是让每个 Router 自己创建对象。它位于程序最外层的启动位置。状态变化是应用从未初始化变成持有可用的 `ticket_service`。当前没有外部初始化异常；Day 2 接数据库后，连接和启动失败会在这里的 lifespan 流程处理。

为什么测试调用 `create_app()` 而不是一直复用全局 `app`：每个测试会得到全新的内存 Repository，不会被上一个测试创建的数据污染。

## 2. HTTP 请求模型

关键位置：`app/schemas/ticket.py`

```python
class TicketCreate(BaseModel):
    customer_id: CustomerId
    subject: Subject
    description: Description
    category: TicketCategory
```

输入是客户端 JSON，输出是校验后的 `TicketCreate` 对象。FastAPI 在 Router 执行前调用 Pydantic 完成解析和校验。这样写把外部输入约束放在系统边界，避免空标题或非法分类进入业务层。它位于 HTTP Schema 层，不是持久化模型。校验成功不会修改业务状态；校验失败直接产生 422，Service 不会被调用。

`StringConstraints(strip_whitespace=True, min_length=1, ...)` 会先移除首尾空白，再检查长度，因此只有空格的标题会被拒绝。

## 3. Router 到 Service 的转换

关键位置：`app/api/routes/tickets.py`

```python
ticket = service.create_ticket(
    CreateTicketCommand(
        customer_id=payload.customer_id,
        subject=payload.subject,
        description=payload.description,
        category=payload.category,
    )
)
```

输入是已经通过 HTTP 校验的 `TicketCreate`，输出是 Service 返回的领域 `Ticket`。执行流程是 Router 把外部 Schema 显式转换成内部 Command，再调用业务用例。这样写避免 Service 依赖 Pydantic 请求模型。它位于 HTTP 层和应用层的边界。调用前系统还没有新工单；调用成功后 Repository 已保存工单。校验异常在这段代码之前处理，业务异常则由注册的异常处理器转换成 HTTP 响应。

## 4. 创建工单业务用例

关键位置：`app/services/ticket_service.py`

```python
def create_ticket(self, command: CreateTicketCommand) -> Ticket:
    now = datetime.now(UTC)
    ticket = Ticket(
        id=uuid4(),
        ...,
        status=TicketStatus.OPEN,
        created_at=now,
        updated_at=now,
    )
    return self._repository.add(ticket)
```

输入是与 HTTP 无关的 `CreateTicketCommand`，输出是已经保存的领域 `Ticket`。执行时生成唯一 ID 和 UTC 时间，把初始状态设为 `OPEN`，然后调用 Repository。这样写把“新工单初始状态是什么”放到业务用例中，而不是交给客户端决定。它位于应用服务层。状态从不存在变为一个可查询的 `OPEN` 工单。Day 1 的内存保存不会抛数据库异常；Day 2 加入数据库后，事务失败和冲突会在 Repository/应用边界明确处理。

## 5. Repository 端口

关键位置：`app/repositories/ticket_repository.py`

```python
class TicketRepository(Protocol):
    def add(self, ticket: Ticket) -> Ticket: ...
    def get(self, ticket_id: UUID) -> Ticket | None: ...
```

输入输出是领域 `Ticket` 和 `UUID`，没有 HTTP Request、JSON 或状态码。Service 只依赖这份能力契约。这样写使存储实现可以从内存替换为 PostgreSQL。它位于应用核心与基础设施之间的端口。协议本身不改变状态，具体 `add()` 实现才保存数据。失败语义将在具体适配器中实现，Service 再把业务上的“不存在”转换成 `TicketNotFoundError`。

## 6. 业务异常到 HTTP 错误

关键位置：`app/services/ticket_service.py` 和 `app/api/exception_handlers.py`

```python
ticket = self._repository.get(ticket_id)
if ticket is None:
    raise TicketNotFoundError(ticket_id)
```

输入是工单 ID，输出要么是 Ticket，要么抛出业务异常。Service 只表达“工单不存在”，不知道 HTTP 404。HTTP 异常处理器随后把这个异常映射成统一错误响应。这样写让同一个 Service 将来可以被 API、Worker 或脚本复用。查询不会改变状态；异常路径也不会创建数据。

## 7. request_id 中间件

关键位置：`app/main.py`

```python
@application.middleware("http")
async def attach_request_id(request: Request, call_next):
    request_id = str(uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response
```

输入是一个 HTTP Request 和后续处理链，输出是带 `X-Request-ID` 的 Response。中间件先生成 ID，再继续执行路由，最后补充响应头。这样写让正常响应和异常响应共享同一个关联标识。它位于整个 HTTP 链路外层。状态变化只发生在本次请求的 `request.state`，不会成为跨请求业务状态。如果后续处理抛出未捕获异常，Day 1 尚未提供统一 500 处理；生产阶段会结合结构化日志和 Trace 处理。

