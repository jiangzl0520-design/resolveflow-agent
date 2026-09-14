# Day 12：可追溯知识入库流水线

## 完成内容

建立从UTF-8 Markdown政策到不可变Document和可检索Chunks的确定性入库流水线，支持标题语义切块、完整血缘、版本保护、内容去重、增量更新和失败重跑。

## 完整流程

```text
CLI读取政策文件和tenant/source/version/effective metadata
→ Pydantic Command校验格式、时区和有效期
→ 规范化BOM、换行和行尾空格
→ 计算content_hash与request_hash
→ 按tenant + idempotency_key创建或恢复Ingestion Run
→ 按tenant + source_key + version检查已有文档
→ Markdown Parser恢复标题、段落、列表和原文行号
→ Chunker优先按标题语义切块，超长时按句子边界拆分
→ Service构造不可变Document与带血缘Chunks
→ PostgreSQL单事务更新current指针、插入Document/Chunks并完成Run
→ 返回completed、skipped或安全错误
```

`content_hash`判断正文是否相同；`request_hash`绑定source、版本、URI、有效期和正文身份，防止幂等键被复用给不同请求。Run表示一次入库过程，状态为`processing → completed/skipped/failed`；失败且未耗尽预算时使用同一键恢复并增加attempts。

## 去重与版本

- 同一幂等请求重放：返回原Run，不重新执行。
- 不同幂等键但source/version/content相同：新Run记为skipped，指向已有Document。
- 同source/version但内容不同：拒绝静默覆盖，调用方必须升版本。
- 新版本：旧Document与Chunks保留，新版本成为最新入库指针。

Chunk保存tenant、document id、标题路径、source URI、版本、原文行范围和有效区间，可回到父Document的完整原文。`is_current`只表示最新入库版本，不等于业务时间有效；Day13检索还必须按`effective_from/effective_to`过滤。

## 事务与失败

发布在一个事务中完成。真实PostgreSQL测试发现ORM未声明relationship时不能保证父子INSERT顺序，因此先加入Document并`flush`，再插Chunks，最后统一commit。flush不是commit；任一步失败时Document、Chunks、current指针和Run完成状态一起rollback。临时存储错误可重跑，版本冲突和非法文档不能盲目重试。

## 测试证据

专项17项、真实PostgreSQL 8项、全项目170项全部通过。60例评估中40/40 Chunks血缘完整、15/15重复文档被避免、10/10同版本漂移被阻止、10/10增量更新保留旧版本、5/5发布故障无半成品并成功恢复。

## 与Agent和后续Day的关系

Day12生产可信知识，不做embedding和答案生成；Day13负责租户与有效期过滤下的混合检索，Day14负责重排、冲突和引用，Day15再决定哪些证据进入本轮Agent Context。外部文档始终是证据数据，不能覆盖System Policy和后端硬规则。

