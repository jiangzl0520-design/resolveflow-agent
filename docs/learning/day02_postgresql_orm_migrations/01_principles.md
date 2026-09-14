# Day 2 原理：PostgreSQL、SQLAlchemy 与 Alembic

## 1. 今天解决的真实问题

Day 1 的 `InMemoryTicketRepository` 只能在一个 Python 进程中保存工单：

- 服务重启后数据全部丢失；
- 多个 API 或 Worker 进程看不到彼此的数据；
- 无法使用数据库事务保护写操作；
- 不能为工单、Agent Run 和审计事件建立长期关系；
- 无法通过迁移记录结构变更历史。

Day 2 把生产默认 Repository 替换为 PostgreSQL 适配器，并建立三张基础表：

- `tickets`：长期保存工单业务实体；
- `ticket_events`：为后续状态变化和审计事件预留；
- `agent_runs`：为后续 AgentState、当前节点和运行版本持久化预留。

今天没有实现 Agent Runtime，所以 `agent_runs` 只建立结构，不伪造 Agent 已经能够 checkpoint 或恢复。

## 2. 创建工单的完整数据库流程

```text
客户端 POST /api/v1/tickets
→ Middleware 生成 request_id
→ Router + 输入 Schema
→ CreateTicketCommand
→ TicketService 生成领域 Ticket
→ SqlAlchemyTicketRepository.add(ticket)
→ SessionFactory 创建短生命周期 Session
→ sessionmaker.begin() 开启事务
→ Ticket 映射成 TicketRecord
→ Engine 从连接池借出 PostgreSQL 连接
→ 执行 INSERT
→ 成功：COMMIT
→ Session 关闭，连接归还连接池
→ Repository 返回领域 Ticket
→ 输出 Schema 序列化
→ 客户端收到 201
```

失败路径：

```text
INSERT 或 COMMIT 失败
→ Session 自动 ROLLBACK
→ Repository 捕获 SQLAlchemyError
→ 抛出 StorageUnavailableError
→ Service 不返回成功结果
→ Router 正常流程中断
→ 全局 Exception Handler 生成安全的 503
→ request_id 关联内部日志
```

只有事务提交成功后，工单才算持久化创建成功。在内存中构造出 `Ticket` 不等于数据库已经保存。

## 3. Engine、连接池、Connection、Session、Transaction 的关系

### Engine

Engine 是应用访问数据库的总入口，持有数据库方言和连接池。它随一个应用进程长期存在，不应该为每个请求重新创建。

`create_engine()` 默认不会立刻创建所有连接；连接池按实际使用按需增长。当前 PostgreSQL 配置为：

- `pool_size=5`：正常保留最多 5 个池内连接；
- `max_overflow=10`：短时高峰允许额外创建 10 个连接；
- `pool_timeout=30`：拿不到连接时最多等待 30 秒；
- `pool_pre_ping=True`：借出连接前检查其是否仍可用。

这些参数不是性能数字承诺，后续必须根据真实并发、数据库连接上限和压测证据调整。

### Connection

Connection 是一次实际数据库连接的代理。Session 需要执行 SQL 时从 Engine 的池中借出 Connection，结束后归还，而不是每次真正销毁 TCP 连接。

### Session

Session 是一次数据库工作上下文，负责：

- 跟踪 ORM Record；
- 执行查询和写入；
- 管理 identity map；
- 绑定当前事务；
- flush、commit、rollback 和 close。

Session 不是数据库本身，也不是跨请求共享的全局缓存。一个可变 Session 不能被多个并发请求共同使用。

### Transaction

Transaction 规定一组数据库修改要么全部提交，要么全部回滚。当前代码使用：

```python
with session_factory.begin() as session:
    session.add(record)
```

上下文正常退出时自动 commit；出现异常时自动 rollback；随后关闭 Session。

当前一个 Service 用例只执行一个 Repository 操作，因此事务暂时位于 Repository 方法中。以后一个用例需要同时写 `tickets`、`ticket_events` 和其他表时，事务边界必须提升到 Unit of Work，确保多个 Repository 操作属于同一事务。

## 4. 为什么 Engine 长期存在，Session 短期存在

Engine 和连接池的创建有成本，而且需要集中控制最大连接数，所以它们属于应用生命周期。

Session 会保存当前事务、查询对象和未提交修改。如果跨请求共享：

- 请求 A 的未提交数据可能影响请求 B；
- 一个请求 rollback 可能回滚另一个请求；
- 多线程并发会破坏 Session 的状态；
- 长期 identity map 会持有过多对象。

因此生命周期关系是：

```text
应用进程
└── Engine + Pool
    ├── Session/Transaction 1
    ├── Session/Transaction 2
    └── Session/Transaction 3
```

应用关闭时通过 lifespan 调用 `engine.dispose()`，释放该进程持有的连接池资源。

## 5. Domain 与 ORM Model 为什么仍然分开

### Domain `Ticket`

表达工单的业务含义，使用 `TicketCategory`、`TicketStatus` 等业务类型。它不知道 SQLAlchemy、表名和数据库字段。

### ORM `TicketRecord`

表达 `tickets` 表如何存储数据，关心：

- `String(64)`、`Text`、`DateTime(timezone=True)`；
- 主键、外键和索引；
- PostgreSQL UUID 和 JSONB 方言；
- ORM 与数据库之间的读写。

### Repository

负责两者转换：

```text
Ticket → TicketRecord → PostgreSQL
PostgreSQL → TicketRecord → Ticket
```

如果 Service 直接操作 ORM Record，业务层会依赖 SQLAlchemy；以后修改表结构、测试替身或存储实现都会传播到核心业务。

## 6. 三张表之间的关系

```text
tickets
├── ticket_events.ticket_id → tickets.id
└── agent_runs.ticket_id → tickets.id
```

### `tickets`

保存当前工单快照。`customer_id` 和 `status` 建立索引，为常见筛选预留查询能力。

### `ticket_events`

保存“发生过什么”的事件。当前只建立表，后续 Day 3 才会让状态变化写入事件。事件不能取代当前快照；两者分别解决快速读取当前状态和保留审计历史的问题。

### `agent_runs`

一张工单可以对应多次 Agent Run。`state` 在 PostgreSQL 中使用 JSONB，为后续结构化 AgentState 预留；`current_node` 表示暂停或执行位置；`version` 为并发控制和 checkpoint 演进预留。

建表不等于 Agent 已经能够恢复。真正恢复还需要运行框架、checkpoint 写入时机、状态版本、幂等和恢复测试。

## 7. Alembic 迁移与 create_all 的区别

### `Base.metadata.create_all()`

- 根据当前 ORM Metadata 创建缺失的表；
- 不记录数据库处于哪个业务版本；
- 不会生成可审查的升级历史；
- 不适合表达复杂的数据迁移、重命名和回滚。

### Alembic

- 每个 revision 有唯一版本号和前后关系；
- `upgrade()` 明确怎样向前变更；
- `downgrade()` 明确怎样回退结构；
- `alembic_version` 表记录数据库当前版本；
- 迁移文件可以代码审查并随仓库发布；
- `alembic check` 可以检查 ORM Metadata 是否出现未迁移变化。

生产启动顺序应该是：

```text
数据库就绪
→ 发布流程执行 alembic upgrade head
→ 迁移成功
→ 应用开始接收请求
```

不能让每个 API 副本启动时并发执行 `create_all()`。

## 8. Docker Compose 的作用与边界

`compose.yaml` 定义本地开发所需的 PostgreSQL 和 Redis：

- 使用命名 volume 保存容器数据；
- 显式映射本地端口；
- PostgreSQL 使用 `pg_isready` healthcheck；
- Redis 使用 `redis-cli ping` healthcheck；
- 初始化时创建独立的 `resolveflow_test` 测试数据库。

PostgreSQL 18 改变了官方镜像的数据目录布局。命名卷必须挂载到 `/var/lib/postgresql`，镜像再把当前大版本的数据放到 `/var/lib/postgresql/18/docker`。如果继续沿用旧目标 `/var/lib/postgresql/data`，18.x 镜像会拒绝启动。这个约束不是业务代码问题，而是镜像版本、持久化目录和数据库升级策略之间的兼容性问题。

容器状态为 running 只表示进程启动，不表示数据库已经接受连接。healthcheck 才表达“依赖已就绪”。

Redis 今天只启动基础服务，没有接入业务代码。引入但不使用不算已经实现缓存、锁或任务队列。

## 9. 错误语义

- 查询完成且没有记录：Repository 返回 `None`，Service 转成 `TicketNotFoundError`，HTTP 返回 404。
- 连接超时、SQL 失败或 commit 失败：Repository 抛 `StorageUnavailableError`，HTTP 返回 503。
- 客户端不能看到 SQL、表名、连接串或底层异常文本。
- 内部日志应通过 request_id 记录根因，同时对密码和敏感字段脱敏。

“不存在”和“当前无法查询”必须分开，否则数据库故障会被误报成业务数据不存在。

## 10. 测试分层与验证边界

当前自动化验证分三层：

1. SQLite 隔离迁移测试：验证 upgrade、downgrade、主外键、Metadata 同步。
2. SQLite SQLAlchemy Repository 测试：验证跨应用重建持久化、事务回滚和 Domain/ORM 映射。
3. PostgreSQL 专用测试：验证真实 psycopg、PostgreSQL 方言、迁移和往返持久化。

SQLite 测试能快速证明大部分应用逻辑，但不能证明 PostgreSQL 的连接、JSONB、UUID、事务和容器网络真实可用。因此第 3 层不能省略。

当前本机已经通过 WSL 2 和 Docker Desktop 启动 PostgreSQL 18.3 与 Redis 8.2。真实 PostgreSQL 专项测试 `1 passed`，完整测试 `15 passed`，没有 skip；因此 Day 2 的本地数据库完成条件已经满足。

## 11. 官方资料

- [SQLAlchemy Session Basics](https://docs.sqlalchemy.org/en/20/orm/session_basics.html)
- [SQLAlchemy Connection Pooling](https://docs.sqlalchemy.org/en/20/core/pooling.html)
- [SQLAlchemy PostgreSQL Dialect](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html)
- [Alembic Tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html)
- [Alembic Autogenerate and Check](https://alembic.sqlalchemy.org/en/latest/autogenerate.html)
- [Docker Compose startup order and healthchecks](https://docs.docker.com/compose/how-tos/startup-order/)
- [PostgreSQL Official Image](https://hub.docker.com/_/postgres)
