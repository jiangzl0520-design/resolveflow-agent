# Day 12 关键代码与架构位置

## 1. 入库命令：先固定可信业务身份

关键位置：`app/domain/knowledge.py`

```python
class IngestPolicyDocumentCommand(BaseModel):
    tenant_id: UUID
    source_key: str
    document_version: str
    source_text: str
    effective_from: datetime
    effective_to: datetime | None = None
    idempotency_key: str
    request_id: str
    trace_id: str
```

输入是外部文件及其业务元数据，输出是经过 Pydantic 校验的内部 Command。它拒绝无时区日期、倒置有效期、非法 source/version 和额外字段。这样 Service 接到的不是任意字典，而是满足基本不变量的业务请求。

Command 位于协议适配层与应用服务之间。它只保证“格式和基础关系合法”，不判断某版本是否已存在；后者需要查询 Repository，因此属于 Service。

## 2. 内容哈希与请求哈希

关键位置：`app/services/knowledge_ingestion_service.py`

```python
content_hash = _content_hash(command.source_text)
request_hash = _request_hash(command, content_hash)
run = self._start_or_resume(
    command,
    request_hash=request_hash,
    content_hash=content_hash,
    now=now,
)
```

输入是校验后的 Command，输出是本次业务内容身份和一个 processing/恢复后的 run。内容哈希先规范化 BOM、换行和行尾空格，避免操作系统格式差异制造假版本；请求哈希再绑定 source、version、URI 和有效期，防止同一幂等键被复用给不同意图。

状态变化是“没有 run → processing”或“failed → processing，attempts + 1”。同一键不同请求会直接抛 `KnowledgeIdempotencyConflictError`，不会覆盖原任务。

## 3. 去重与版本保护

关键位置：`KnowledgeIngestionService.ingest()`

```python
existing = repository.find_document_by_source_version(
    tenant_id,
    source_key,
    document_version,
)
if existing is not None:
    if existing.content_hash != content_hash:
        raise KnowledgeVersionConflictError()
    return skipped_result_for(existing)
```

输入是租户、逻辑 source、版本和内容哈希。输出有两种：完全相同的内容指向已有文档并将 run 标成 skipped；同版本不同内容则失败。

这段代码保护“版本号一旦发布便不可变”。如果允许原地覆盖，同一个历史引用会在不同日期指向不同内容，Agent 轨迹、人工审批和事故复盘都无法重放。

## 4. 标题语义解析

关键位置：`app/knowledge/markdown.py`

```python
ParsedMarkdownDocument(
    sections=(
        MarkdownSection(
            heading_path=("配送政策", "签收争议"),
            blocks=(...),
        ),
    )
)
```

解析器输入 Markdown 字符串，输出带标题路径、块类型和原始行范围的结构化文档。它先恢复文档结构，不在这一层做数据库写入，也不调用模型。

fenced code block 是重要边界：围栏里的 `#` 是代码数据，不是章节标题。解析错误会变成安全的 `knowledge_parse_error` 并令 run 失败。

## 5. 标题优先的确定性切块

关键位置：`app/knowledge/chunking.py`

```python
HeadingAwareChunker(
    target_chars=800,
    max_chars=1200,
)
```

输入是 `ParsedMarkdownDocument`，输出多个 `ChunkDraft`。切块顺序是：先按标题章节隔离，再在同章节内合并相邻块；过长时按中英文句子边界切，最后才硬切。

每个 Draft 保留 `section_path` 与 `source_line_start/end`。它还没有 document_id，因为父文档尚未生成；Service 创建 Document 后再补齐不可变的 `KnowledgeChunk`。

这段逻辑使用规则而不是 LLM，原因是文档标题和字符边界属于可确定的问题。规则切块可重复、零模型费用、容易回归，后续是否需要语义模型必须由检索评估证明。

## 6. Service 构造完整血缘

关键位置：`app/services/knowledge_ingestion_service.py`

```python
KnowledgeChunk(
    tenant_id=command.tenant_id,
    document_id=document.id,
    section_path=draft.section_path,
    source_uri=command.source_uri,
    document_version=command.document_version,
    source_line_start=draft.source_line_start,
    source_line_end=draft.source_line_end,
    effective_from=command.effective_from,
    effective_to=command.effective_to,
)
```

输入是 Command、Document 和 ChunkDraft，输出是可持久化、不可变的 KnowledgeChunk。Service 在这里把“外部文档结构”和“内部业务身份”合并，因为 Parser 不应该知道 tenant/version，Repository 也不应该推导业务字段。

任意 chunk 都能通过 document_id 回到完整原文，并直接携带检索过滤最常用的 tenant、版本和有效期。这种适度反规范化减少 Day13 查询时遗漏关键过滤条件的风险。

## 7. 单事务发布与显式 flush

关键位置：`app/repositories/sqlalchemy_knowledge_repository.py`

```python
session.add(_document_to_record(document))
session.flush()
session.add_all(_chunk_to_record(chunk) for chunk in chunks)
_apply_run(run_record, run)
session.commit()
```

输入是 completed run、父 Document 和全部 Chunks，输出是数据库中的一个原子快照。`flush()` 先执行父记录 INSERT，使 PostgreSQL 在校验 chunk 外键时已经能在当前事务内看到父记录；它不对外提交数据。

如果 chunk 插入或 run 更新失败，异常处理会 rollback，父文档也一起消失。只有最后 commit 后，文档、全部 chunks、current 指针和 completed run 才同时可见。

真实 PostgreSQL 测试曾在这里发现外键顺序问题；SQLite 专项测试没有暴露它。这说明“使用 ORM”不等于“可以只在 SQLite 上证明 PostgreSQL 行为”。

## 8. 数据库约束

关键位置：

- `app/db/models.py`
- `migrations/versions/20260804_0006_add_knowledge_ingestion.py`

核心表：

| 表 | 作用 | 关键约束 |
|---|---|---|
| `knowledge_documents` | 不可变原文版本 | tenant/source/version 唯一 |
| `knowledge_chunks` | 检索与引用单元 | document 外键、document/chunk_index 唯一 |
| `knowledge_ingestion_runs` | 入库过程与恢复状态 | tenant/idempotency_key 唯一 |

Service 提供可读的业务错误和正常控制流；数据库约束是并发竞争下的最后防线。两者不能互相替代。

## 9. CLI 只做适配

关键位置：`app/knowledge/ingest_policy.py`

CLI 读取 UTF-8 文件、解析日期和 UUID、建立 Command，然后调用 Service。它输出 run/document/hash/chunk count/request/trace，不输出原始政策正文，降低日志泄露风险。

以后换成 HTTP、定时扫描或消息队列时，只需新增 Adapter：

```text
HTTP Router / Scanner / Worker / CLI
        → IngestPolicyDocumentCommand
        → KnowledgeIngestionService
        → KnowledgeRepository
```

如果把去重和切块写进 CLI，新入口就会复制一套规则并产生行为漂移。

## 10. 关键异常与恢复动作

| 异常 | 是否重试 | 正确动作 |
|---|---:|---|
| 文档为空或解析失败 | 否 | 修复源文档 |
| 同版本不同内容 | 否 | 升版本后重新投递 |
| 幂等键绑定了不同请求 | 否 | 修复调用方键管理 |
| run 仍 processing | 否（立即） | 等待当前执行者或超时接管机制 |
| 存储临时不可用 | 是 | 同一幂等键、有界重跑 |
| 重试预算耗尽 | 否 | 告警并人工调查 |

