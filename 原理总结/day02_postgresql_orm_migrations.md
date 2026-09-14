# Day 02：PostgreSQL、ORM 与数据库迁移

## 完成内容

把Day01内存存储替换为PostgreSQL持久化，引入SQLAlchemy Repository、Session事务边界和Alembic迁移，使工单在进程重启后仍然存在。

## 完整流程

```text
HTTP Router
→ TicketService
→ SQLAlchemy Repository
→ Session取得连接池连接
→ Domain Ticket映射为TicketRecord
→ INSERT/SELECT
→ commit或rollback
→ ORM Record映射回Domain Ticket
→ Service与HTTP响应
```

Engine长期存在，管理数据库方言和连接池；Connection代表一次实际连接；Session是短生命周期工作单元，跟踪ORM对象并组织SQL；Transaction决定一组修改是否一起提交。每个请求使用独立Session，用完归还连接，不能把全局Session跨请求共享。

Domain Ticket表达业务含义，ORM TicketRecord表达表结构，Repository负责二者转换。分开后，业务层不会依赖SQLAlchemy列、Session和数据库异常，未来替换存储或测试时不必重写Service。

## 迁移与失败

Alembic负责数据库Schema从旧版本升级到新版本。应用启动不能用`create_all()`冒充迁移，因为它无法可靠表达已有表的演进、回滚和发布顺序。写入成功后commit；发生约束或连接错误时rollback，不能把失败Session继续复用。

PostgreSQL是持久化事实源，Docker容器running不等于数据库ready，必须通过healthcheck确认。真实测试只连接名称以`_test`结尾的专用数据库，避免破坏开发数据。

## 测试证据

15项测试通过，并用真实PostgreSQL完成迁移、写入和重读；Redis健康检查与真实PING/PONG同时通过。

## 与后续Day的关系

Day02提供持久化和事务基础；Day03在其上增加幂等、乐观锁和审计，Day05保存异步任务状态，Day09保存checkpoint，Day12保存知识版本。

