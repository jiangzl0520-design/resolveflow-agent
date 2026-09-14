# Day 2 测试证据

## 1. 环境

- 日期：2026-07-24
- 操作系统：Windows 11
- Python：3.12.13
- FastAPI：0.139.0
- SQLAlchemy：2.0.51
- Alembic：1.18.5
- psycopg：3.3.4
- WSL：2.7.10.0，默认 WSL 2
- Docker Desktop：4.83.0
- Docker Engine：29.6.2，Linux/amd64
- Docker Compose：5.3.1
- PostgreSQL：18.3-alpine
- Redis：8.2-alpine

## 2. 容器与健康检查

启动命令：

```powershell
docker compose up -d postgres redis
docker compose ps
```

真实结果：

```text
resolveflow-postgres-1  Up (healthy)  0.0.0.0:5432->5432/tcp
resolveflow-redis-1     Up (healthy)  0.0.0.0:6379->6379/tcp
```

Redis 真实探测：

```text
redis-cli ping
PONG
```

PostgreSQL 18 首次启动暴露了一个真实兼容性问题：旧挂载目标 `/var/lib/postgresql/data` 会被 18.x 镜像拒绝。将命名卷挂载到 `/var/lib/postgresql` 后，镜像在 `/var/lib/postgresql/18/docker` 初始化版本化数据目录，healthcheck 转为 healthy。

## 3. Alembic 真实迁移

命令：

```powershell
$env:DATABASE_URL="postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow"
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current
.\.venv\Scripts\python.exe -m alembic check
```

真实结果：

```text
Running upgrade  -> 20260724_0001, Create tickets, ticket events, and agent runs.
20260724_0001 (head)
No new upgrade operations detected.
```

真实 PostgreSQL 表：

```text
agent_runs
alembic_version
ticket_events
tickets
```

真实列类型检查：

```text
agent_runs.state       jsonb
agent_runs.id          uuid
ticket_events.id       uuid
tickets.id             uuid
ticket_events.created_at  timestamp with time zone
tickets.created_at        timestamp with time zone
```

这证明迁移不是只在 SQLite 或离线 SQL 中成立，而是真正在 PostgreSQL 18 上创建了原生 UUID、JSONB 和带时区时间列。

## 4. PostgreSQL 专项测试

命令：

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test"
.\.venv\Scripts\python.exe -m pytest -W error -m postgres
```

真实结果：

```text
1 passed, 14 deselected in 0.28s
```

该测试只允许连接名称以 `_test` 结尾的数据库，并验证：

- 在真实 PostgreSQL 上执行 Alembic upgrade；
- 通过 psycopg 和 SQLAlchemy Repository 写入 Ticket；
- 再次查询后 Domain Ticket 与原对象相等；
- 实际 SQLAlchemy dialect 是 `postgresql`。

## 5. 全量自动化测试

命令：

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test"
.\.venv\Scripts\python.exe -m pytest -W error
```

真实结果：

```text
............... [100%]
15 passed in 0.74s
```

`-W error` 会把运行时 warning 当成失败。本次没有跳过测试，也没有 warning。

## 6. 已覆盖案例

### 正常路径

- Day 1 HTTP 创建和查询链路继续通过。
- Alembic upgrade 创建 `tickets`、`ticket_events`、`agent_runs`。
- 三张表主键、外键和索引可被检查。
- SQLAlchemy Repository 创建工单后，重建 Database、SessionFactory、Repository 和 FastAPI 应用仍能查询。
- ORM Metadata 与最新迁移一致，`alembic check` 无新增操作。
- 真实 PostgreSQL 完成 INSERT/SELECT 往返持久化。
- Redis healthcheck 和真实 `PING/PONG` 通过。

### 失败路径

- 重复主键写入触发 SQLAlchemyError。
- `sessionmaker.begin()` 自动 rollback。
- 原有行仍可读取，失败写入没有破坏已提交数据。
- `StorageUnavailableError` 被映射成安全 503。
- 客户端响应不包含底层 `"database write failed"` 文本。

### 边界路径

- Alembic downgrade 按外键依赖反序删除三张核心表。
- 数据库查询无记录和数据库无法查询使用不同错误语义。
- 测试临时数据库位于项目 `.pytest_tmp/`，避免 Windows 系统临时目录权限问题。
- PostgreSQL 专项测试拒绝连接不以 `_test` 结尾的数据库。

## 7. 静态检查

命令：

```powershell
.\.venv\Scripts\python.exe -m compileall -q app migrations tests
```

结果：成功，退出码 0。

## 8. 仍然不应夸大的能力

- Redis 目前只完成基础设施启动和 PING，没有接入缓存、锁或任务队列业务。
- `agent_runs` 目前只有表结构，没有 Agent checkpoint 写入与恢复实现。
- 已验证单个 Repository 方法的事务回滚；跨多个 Repository 的原子事务仍需要后续 Unit of Work。
- 当前证据来自本地 Docker 环境，不等于已经验证生产集群、备份恢复、高可用和压力容量。

Day 2 的本地真实 PostgreSQL 完成条件已经满足。
