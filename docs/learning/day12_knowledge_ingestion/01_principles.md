# Day 12 原理：可追溯的知识入库流水线

## 1. 今天解决的真实问题

ResolveFlow 后续要让 Agent 根据企业政策判断“签收争议是否允许退款”。如果只是把 Markdown 文档切成几段文本再做向量化，会出现四类真实风险：

1. 找到的片段不知道来自哪份原文、哪个版本和哪些原始行，无法引用和审计；
2. 新旧政策混在一起，Agent 可能使用已经失效的规则；
3. 同一文件被重复投递，产生重复片段并污染召回排序；
4. 入库中途失败后从头乱跑，可能留下只有文档没有分块、或只有分块没有成功记录的半成品。

Day12 建立的不是一个“上传文件 Demo”，而是一条确定性的知识数据生产线。它把外部 Markdown 转成带租户、来源、版本、有效期和原文行号的知识分块，并把去重、增量更新和失败恢复落到后端代码与数据库约束中。

## 2. 完整操作流程

```text
Markdown 文件 + 可信入库参数
  → Pydantic 命令校验
  → 规范化内容并计算 content_hash/request_hash
  → 创建或恢复 ingestion run
  → 按 tenant + source_key + version 检查重复和版本冲突
  → Markdown 标题层级解析
  → 标题语义切块，保留 heading path 和原文行号
  → 构造不可变 Document 与 Chunk
  → PostgreSQL 单事务发布
  → run=completed / skipped / failed
```

### 2.1 输入进入系统

当前入口是 `python -m app.knowledge.ingest_policy`。CLI 只负责把命令行和文件内容适配成 `IngestPolicyDocumentCommand`，真正的入库规则全部在 `KnowledgeIngestionService`，所以以后增加 HTTP 上传、对象存储扫描器或后台 Worker 时可以复用同一个用例。

命令必须提供：

- `tenant_id`：知识属于哪个租户；
- `source_key`：同一逻辑政策的稳定身份，例如 `policy/cn/delivery-dispute`；
- `document_version`：这次内容属于哪个明确版本；
- `source_uri`：原文在哪里；
- `effective_from/effective_to`：业务上从何时起有效、何时失效；
- `idempotency_key`：一次投递的稳定身份；
- `request_id/trace_id`：把本次入库与日志、任务和数据库记录关联起来。

文件正文和这些字段都属于外部输入，不能直接写数据库。Pydantic 先拒绝非法 source key、无时区日期、倒置的有效期、空文档、超大文档和隔离保留租户。

### 2.2 两种哈希解决两个不同问题

服务先把 BOM、Windows/Unix 换行和行尾空格规范化，再计算 `content_hash`。因此同一份文本只因 `CRLF` 变成 `LF` 不会被误判成新政策。

`request_hash` 则包含 source、版本、标题、URI、有效期和内容哈希，但不包含 request/trace。它回答的是：“这个幂等键所代表的业务请求是否仍是同一个请求？”

- `content_hash` 相同：正文语义输入相同；
- `request_hash` 相同：整个入库意图相同；
- 同一幂等键、不同 request hash：调用方错误复用了幂等键，必须拒绝，不能猜测覆盖。

### 2.3 Ingestion Run 是恢复和观测的状态载体

每次投递先建立 `KnowledgeIngestionRun`：

```text
processing → completed
           → skipped
           → failed → processing（同一幂等键重跑）
```

Run 保存 attempts、max_attempts、错误码、document_id、chunk_count、request_id、trace_id 和时间。它不是文档本身，而是“这次入库过程做到了哪一步”的状态。

状态规则如下：

- 已 `completed/skipped` 的同一请求直接返回原结果，不重复执行；
- `processing` 说明可能仍有执行者，拒绝并发接管；
- `failed` 且未耗尽预算时，用同一 run 增加 attempts 后恢复；
- 达到最大尝试次数后拒绝继续，避免无限重试。

### 2.4 去重、版本冲突和增量更新不是一回事

服务按 `tenant_id + source_key + document_version` 查询已有文档：

| 情况 | 后端行为 | 原因 |
|---|---|---|
| 同一幂等键、同一请求 | 返回原 run | 网络重发不能重复执行 |
| 不同幂等键、同 source/version/content | `skipped`，指向已有文档 | 不让重复投递制造重复知识 |
| 同 source/version、内容不同 | `knowledge_version_conflict` | 禁止在版本号不变时静默改历史 |
| 同 source、新 version | 新建文档和分块，旧版本保留 | 支持审计、回放和按时间检索 |

这里最重要的原则是：内容变了必须升版本。否则某个历史 Agent Run 声称使用 `1.0.0`，后来数据库中的 `1.0.0` 却被换成另一份内容，审计就失去意义。

### 2.5 Markdown 解析先恢复结构，再进行切块

解析器先识别标题层级、普通段落、列表和 fenced code block。标题不是普通文本，它建立语义路径：

```text
配送争议政策
  ├─ 签收证明
  └─ 退款限制
```

代码围栏内部出现的 `#` 不会被误认成标题。每个内容块同时保存开始和结束行号，使后续引用能回到原文。

切块器遵守两个原则：

1. 标题边界优先于固定字符数，避免把“签收证明”和“退款限制”混成一个无明确主题的片段；
2. 单个语义块过长时先按中英文句子边界拆分，仍过长才硬切，保证任何 chunk 不超过上限。

每个 chunk 会重复标题路径。这样它被单独检索出来时仍然知道自己属于哪一章，而不是只剩一句脱离语境的“必须转人工”。

### 2.6 Document 与 Chunk 分别保存什么

`KnowledgeDocument` 是不可变版本快照，保存完整原文、source key、版本、内容哈希和有效期。`KnowledgeChunk` 是检索单元，保存：

- `document_id`：回到父文档；
- `tenant_id`：查询时必须隔离；
- `section_path`：原文标题语义；
- `source_uri + document_version`：来源与版本；
- `source_line_start/end`：原文位置；
- `effective_from/to`：业务有效区间；
- `content_hash`：检查片段身份和后续索引一致性。

因此“可追溯”不是只存一个 URL，而是能从 chunk 沿 `document_id` 找到完整的 source key、原文、版本和入库记录。

### 2.7 PostgreSQL 单事务发布

发布时必须一起完成四件事：

1. 把同一 tenant/source 的旧 `is_current` 改为 false；
2. 插入新文档；
3. 插入全部分块；
4. 把 run 更新为 completed 并记录 document/chunk_count。

只有最后 `commit` 后，其他事务才看到完整结果。任一步失败都会 rollback，所以不会把半份知识暴露给检索系统。

真实 PostgreSQL 测试发现：DocumentRecord 与 ChunkRecord 没声明 ORM relationship 时，SQLAlchemy 不保证两个 Python 对象的 flush 顺序。因此代码在加入父文档后显式 `flush()`，再加入分块。`flush` 只是把 SQL 发到当前事务，不等于提交；后续失败仍会整体回滚。

### 2.8 失败、重试与恢复

解析错误、版本冲突和存储错误使用不同安全错误码。存储发布失败后：

- 当前事务回滚，文档和分块都不可见；
- run 尽力写成 `failed` 并保存错误码；
- 调用方使用相同幂等键重跑；
- attempts 增加，成功后仍只产生一份文档。

如果数据库完全不可用，连“记录数据库故障”本身也可能失败。因此服务只返回安全错误，生产监控还必须从进程指标和日志检测这类盲区，不能假设所有故障都能写回同一个故障数据库。

## 3. 与上下文工程和 Agent 的关系

Day12 不直接让模型回答问题，但它决定未来 Context 的证据质量。链路是：

```text
Day12：建立可信、可追溯、带时效的知识单元
  → Day13：按 query + tenant + ACL + 有效期召回
  → Day14：重排、冲突检查、引用和 grounded answer
  → Day15：按优先级与 Token 预算进入本轮 Context
  → Agent：基于证据决定下一步，而不是把文档内容当系统指令
```

外部政策始终是“证据数据”，不能覆盖 System Policy。Agent 还必须区分：文档中的业务规则、工具返回的订单事实、当前 Agent State 和聊天 History，它们的来源与优先级不同。

## 4. `is_current` 与有效期必须分清

`is_current=true` 只表示“该 source 最新入库的版本”，不代表“在任意业务时间都有效”。真正检索某个 `as_of` 时间点时还必须满足：

```text
effective_from <= as_of
并且 effective_to 为空或 as_of < effective_to
```

例如下个月才生效的 v2 可以已经是最新入库版本，但处理今天的工单仍应使用当前日期有效的 v1。Day13 的 metadata filter 必须按 tenant 和有效区间查询，不能只看 `is_current`。

## 5. 架构边界

- CLI/未来 Router：外部协议适配，不实现去重和版本规则；
- Pydantic Command：校验输入结构与基础不变量；
- `KnowledgeIngestionService`：编排完整入库用例与状态变化；
- Parser/Chunker：把文档结构变成可检索的语义单元；
- Repository：隔离存储实现并保证租户查询边界；
- PostgreSQL/Alembic：约束、事务、持久化和可升级 schema；
- Day13 Retriever：使用这些数据做召回，不回头篡改入库历史。

## 6. 当前边界

- 当前只接受 UTF-8 Markdown；PDF、Word、OCR 和网页清洗尚未实现；
- 标题语义切块使用确定性规则，没有用 LLM 猜章节，成本为零且可回归；
- Day12 没有 embedding、pgvector、关键词索引或召回排序，它们属于 Day13；
- `is_current` 是最新入库指针，按业务时间有效的版本必须通过有效期过滤；
- 同一 source/version 的并发发布依靠数据库唯一约束阻止重复，但生产 Worker 还应配合任务级重试与告警；
- 原文可能含敏感信息，生产日志不能记录 raw content，权限与知识 ACL 会在后续继续加强。

## 7. 参考资料

- [SQLAlchemy Session flushing](https://docs.sqlalchemy.org/en/20/orm/session_basics.html#flushing)
- [PostgreSQL constraints](https://www.postgresql.org/docs/current/ddl-constraints.html)
- [Alembic operation reference](https://alembic.sqlalchemy.org/en/latest/ops.html)

