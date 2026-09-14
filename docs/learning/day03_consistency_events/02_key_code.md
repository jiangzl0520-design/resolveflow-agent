# Day 3 关键代码

## 1. Domain 状态机

位置：`app/domain/ticket.py:15`

### 输入和输出

- 输入：当前 Ticket、目标 TicketStatus、变更时间。
- 输出：状态、时间和 version 已更新的新 Ticket。

### 执行流程

`transition_to()` 查询 `ALLOWED_TICKET_TRANSITIONS`。合法时用 `dataclasses.replace()` 生成新对象；非法时抛 `InvalidTicketTransitionError`。

### 为什么这样写

业务状态规则不能散落在 Router、Service 或 SQL 中。不可变对象让“旧状态”和“新状态”清楚分离，也避免半修改对象被其他代码继续使用。

### 架构位置和状态变化

它位于 Domain。调用成功只产生内存中的候选新状态；只有 Unit of Work commit 后，数据库状态才真正改变。

### 异常

非法状态边在写数据库之前终止，HTTP 统一映射为 409。

## 2. 幂等创建编排

位置：`app/services/ticket_service.py:55`

### 输入和输出

- 输入：CreateTicketCommand，包含业务字段和 idempotency_key。
- 输出：CreateTicketResult，包含 Ticket 和 replayed 标志。

### 执行流程

Service 生成请求指纹，先读取幂等记录；首次请求创建 Ticket、Event 和 IdempotencyRecord，同事务提交；合法重试读取旧 Ticket；并发唯一键竞争失败后重新读取胜者结果。

### 为什么这样写

Service 负责业务用例编排，但不自己执行 SQL。预查询优化普通重试，数据库复合主键处理真正并发。

### 架构位置和状态变化

它位于应用服务层。成功提交会同时新增一张 Ticket、一条 created Event 和一条幂等记录；replay 不产生新业务状态。

### 异常

相同 key 不同指纹抛 IdempotencyConflictError；唯一键竞争是内部控制流；数据库不可用统一转成 StorageUnavailableError。

## 3. 乐观锁条件更新

位置：`app/repositories/sqlalchemy_ticket_repository.py:38`

### 输入和输出

- 输入：候选新 Ticket 和 expected_version。
- 输出：成功保存的 Ticket。

### 执行流程

执行带 `id` 和 `version` 条件的 UPDATE，检查 `rowcount`。

### 为什么这样写

不长时间持有悲观锁，适合读取多、同一工单冲突相对少的场景。version 条件由数据库原子判断，关闭读取与更新之间的竞态窗口。

### 架构位置和状态变化

它位于基础设施 Repository。影响一行时，当前事务内状态和 version 更新；commit 后对外可见。

### 异常

影响零行抛 ConcurrentTicketUpdateError；SQL 执行失败抛 StorageUnavailableError。

## 4. SQLAlchemy Unit of Work

位置：`app/db/sqlalchemy_unit_of_work.py:19`

### 输入和输出

- 输入：session_factory。
- 输出：一次包含三个 Repository 的事务工作区。

### 执行流程

进入上下文时创建 Session 和 Repository；`flush()` 发送 SQL；`commit()` 原子提交；异常退出 rollback；最终 close Session 并归还连接。

### 为什么这样写

当一个用例写多张表时，事务边界不能继续留在单个 Repository 方法里。Unit of Work 让 Service 决定一次业务用例的提交边界。

### 架构位置和状态变化

它是应用服务与 SQLAlchemy 之间的基础设施适配器。它不决定业务状态边，只负责事务一致性。

### 异常

flush 或 commit 的 SQLAlchemyError 会 rollback 并转成安全的 StorageUnavailableError。

## 5. HTTP 契约

位置：`app/api/routes/tickets.py:24`

- POST 创建必须携带 `Idempotency-Key`。
- 响应头 `Idempotency-Replayed` 表示是否复用了第一次结果。
- PATCH 状态输入 `target_status` 和 `expected_version`。
- GET events 返回结构化审计轨迹。

Router 不计算请求指纹、不决定状态边、不管理事务；它只完成 HTTP 与内部 Command/Result 的适配。
