# Day 2 关键代码解释

## 1. Database：Engine 与连接池的所有者

位置：`app/db/database.py`

```python
self.engine = create_engine(
    database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
)
self.session_factory = sessionmaker(
    bind=self.engine,
    autoflush=False,
    expire_on_commit=False,
)
```

1. 输入和输出：输入数据库 URL；输出一个长期 Engine 和可重复创建短期 Session 的 factory。
2. 执行流程：Engine 保存方言与连接池配置；真正执行 SQL 时才按需建立连接；Repository 每次调用 factory 获得新 Session。
3. 写法原因：复用连接池，同时避免全局共享可变 Session。`pool_pre_ping` 在借出连接前检测失效连接。
4. 架构位置：Database 属于基础设施装配层，不进入 Domain。
5. 状态变化：初始化只建立 Engine/Pool 对象；连接按需进入池，Session 关闭后连接归还。
6. 异常处理：连接失败在实际 checkout/执行 SQL 时产生；应用关闭时 `dispose()` 释放池资源。

## 2. 应用工厂：生产默认使用 PostgreSQL，测试显式替换

位置：`app/main.py`

```python
if repository is None:
    owned_database = database or Database(
        (settings or Settings.from_env()).database_url
    )
    repository = SqlAlchemyTicketRepository(
        owned_database.session_factory
    )
```

1. 输入和输出：可选 Repository、Database 和 Settings；输出装配完成的 FastAPI 应用。
2. 执行流程：没有显式提供测试替身时，创建 Database、SQLAlchemy Repository 和 TicketService；测试可以注入 InMemory Repository。
3. 写法原因：生产默认不能悄悄退回内存存储；测试替换依赖也不需要修改 Router 或 Service。
4. 架构位置：应用工厂是 Composition Root，唯一集中选择具体基础设施实现的位置。
5. 状态变化：`application.state.ticket_service` 持有使用 SQL Repository 的 Service；lifespan 结束时释放 owned Database。
6. 异常处理：Engine 创建是惰性的，真实连接错误在仓储操作时映射为存储异常。

## 3. Repository 写事务

位置：`app/repositories/sqlalchemy_ticket_repository.py`

```python
def add(self, ticket: Ticket) -> Ticket:
    record = self._to_record(ticket)
    try:
        with self._session_factory.begin() as session:
            session.add(record)
    except SQLAlchemyError as exc:
        raise StorageUnavailableError(
            "Ticket storage write failed."
        ) from exc
    return ticket
```

1. 输入和输出：输入完整领域 Ticket；只有事务提交成功才输出该 Ticket。
2. 执行流程：领域对象转 ORM Record；`begin()` 创建 Session 和事务；add 后在上下文退出时 flush、commit、close。
3. 写法原因：上下文管理器保证成功提交、失败回滚和资源关闭属于同一个结构，避免遗漏 rollback/close。
4. 架构位置：Repository 是 Domain 与数据库之间的输出适配器。
5. 状态变化：成功时数据库从无记录变成有记录；失败时 rollback，数据库不应出现部分写入。
6. 异常处理：SQLAlchemyError 被保留为 cause，但对上层只暴露稳定的 `StorageUnavailableError`。

## 4. Repository 读取与对象映射

位置：`app/repositories/sqlalchemy_ticket_repository.py`

```python
with self._session_factory() as session:
    record = session.get(TicketRecord, ticket_id)

if record is None:
    return None
return self._to_domain(record)
```

1. 输入和输出：输入 UUID；输出领域 Ticket 或 `None`。
2. 执行流程：新建读取 Session，按主键查询 ORM Record，关闭 Session，再映射为 Domain。
3. 写法原因：Service 只理解 `Ticket | None`，不依赖 SQLAlchemy Record。
4. 架构位置：ORM/Domain 转换只存在于 Repository 适配器。
5. 状态变化：读取不修改工单；Session 的 identity map 随本次读取结束而释放。
6. 异常处理：确实查不到返回 `None`；数据库无法完成查询抛 StorageUnavailable，不能伪装成不存在。

## 5. ORM 表模型

位置：`app/db/models.py`

```python
class AgentRunRecord(Base):
    __tablename__ = "agent_runs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    ticket_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tickets.id", ondelete="CASCADE"),
    )
    state: Mapped[dict[str, Any]] = mapped_column(JsonDocument)
    version: Mapped[int] = mapped_column(Integer, default=1)
```

1. 输入和输出：类定义输入 SQLAlchemy Metadata；输出 `agent_runs` 表的应用映射。
2. 执行流程：Alembic 读取同一 Metadata 检查迁移同步；运行时 ORM 把 Python UUID、dict 和 datetime 转成数据库类型。
3. 写法原因：外键把 Agent Run 绑定到工单；PostgreSQL 使用 JSONB 保存后续结构化 AgentState；version 为并发演进预留。
4. 架构位置：它是基础设施记录，不是 AgentState 本身，也不是 Agent Runtime。
5. 状态变化：当前只建表，没有任何代码写 Agent Run。
6. 异常处理：无效外键和非空约束由数据库拒绝；业务层未来还要提供更明确错误。

## 6. Alembic 初始迁移

位置：`migrations/versions/20260724_0001_create_core_tables.py`

```python
def upgrade() -> None:
    op.create_table("tickets", ...)
    op.create_table("ticket_events", ...)
    op.create_table("agent_runs", ...)


def downgrade() -> None:
    op.drop_table("agent_runs")
    op.drop_table("ticket_events")
    op.drop_table("tickets")
```

1. 输入和输出：输入数据库当前 revision；输出升级后的三表结构或回退后的旧结构。
2. 执行流程：Alembic 按 revision 顺序执行 upgrade，并把当前版本写入 `alembic_version`；downgrade 按依赖反序删除。
3. 写法原因：结构变化必须显式、可审查、可重复，而不是应用启动时临时猜测。
4. 架构位置：迁移属于部署流程，独立于 HTTP 请求。
5. 状态变化：数据库 Schema 从 base 进入 `20260724_0001`，或从该版本退回 base。
6. 异常处理：PostgreSQL 支持事务 DDL；迁移失败应阻止应用发布，不能继续用未知结构接收请求。

## 7. 存储异常的安全 HTTP 映射

位置：`app/api/exception_handlers.py`

```python
@application.exception_handler(StorageUnavailableError)
async def handle_storage_unavailable(...):
    ...
    return JSONResponse(status_code=503, ...)
```

1. 输入和输出：输入统一存储异常；输出不包含底层细节的 503 JSON。
2. 执行流程：Repository 抛异常，Service/Router 正常流程中断，全局 handler 生成错误响应，Middleware 添加 request_id。
3. 写法原因：客户端需要稳定错误契约，不能看到 SQL、表名、密码或连接地址。
4. 架构位置：业务/基础设施错误到 HTTP 的适配只发生在 API 边界。
5. 状态变化：失败响应不表示工单创建成功；数据库事务已经回滚。
6. 异常处理：内部日志应保留原始 cause 和 request_id，外部只返回 `storage_unavailable`。
