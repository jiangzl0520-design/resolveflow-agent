# Day 16 关键代码

## 1. 文件与架构职责

| 文件 | 职责 |
|---|---|
| `app/domain/memory.py` | Memory 命令、状态、版本、决策、审计事件和事务契约 |
| `app/memory/policy.py` | 精确 key 白名单、允许来源、最低置信度、最大 TTL、值标准化 |
| `app/services/memory_service.py` | 授权后编排写入、冲突、过期、删除和召回用例 |
| `app/repositories/memory_repository.py` | 与存储技术无关的 Repository 协议及内存实现 |
| `app/repositories/sqlalchemy_memory_repository.py` | PostgreSQL/SQLite 事务、行锁、幂等和查询实现 |
| `app/db/models.py` | `long_term_memories` 与 `memory_audit_events` ORM 模型 |
| `app/agent/planner.py` | 每次模型决策前读取当前 Ticket 所属客户的有效 Memory |
| `app/context_engineering/agent_context.py` | 把 Memory 转成带来源的 Context 候选，记录读取失败 |
| `app/api/routes/memories.py` | HTTP 输入适配，不能包含 Memory 业务决策 |
| `migrations/versions/20260805_0011_add_long_term_memory.py` | 可重复部署的数据库结构变更 |

## 2. Command：先把外部输入变成内部契约

```python
class MemoryWriteCommand(BaseModel):
    subject_id: str
    memory_key: str
    value: str
    source_type: MemorySourceType
    source_reference: str
    confidence: float = Field(ge=0, le=1)
    observed_at: datetime
    expires_at: datetime
    idempotency_key: str
    request_id: str
    trace_id: str
```

输入是 Router 已经适配后的内部命令，输出是不可变 Pydantic 对象。它只检查所有记忆都通用的结构约束，例如字段长度、时间顺序和置信度范围；“language 最多保存 365 天”属于业务策略，不能塞进 HTTP Schema，否则后台任务或其他 Adapter 调用 Service 时可以绕过。

`subject_id` 是被记忆的人，`actor_id` 是执行写入的人，两者不能混为一个字段。客服可以代表客户写入明确偏好，但审计必须知道真正的操作者。

## 3. Policy：精确白名单而不是让模型自由造 key

```python
MEMORY_KEY_POLICIES = {
    "preference.language": MemoryKeyPolicy(
        category=MemoryCategory.USER_PREFERENCE,
        allowed_sources=frozenset(
            {MemorySourceType.EXPLICIT_USER,
             MemorySourceType.HUMAN_REVIEWER}
        ),
        minimum_confidence=0.9,
        maximum_ttl=timedelta(days=365),
    ),
}
```

输入是 `memory_key` 对应的候选元数据，输出是允许写入的策略或不存在。未知 key 直接 `rejected`，不会因为模型给出很高 confidence 就放行。Policy 还规范 value，例如 language 统一成小写语言标签，避免 `ZH-CN`、`zh-cn` 被误当成两个偏好。

## 4. Service：模型推断只成为待确认候选

```python
if policy is None:
    return _no_write(command.memory_key, MemoryDecision.REJECTED)
if command.source_type is MemorySourceType.MODEL_INFERENCE:
    return _no_write(
        command.memory_key,
        MemoryDecision.CONFIRMATION_REQUIRED,
    )
```

输入是已授权 Actor 和 `MemoryWriteCommand`，输出是 `MemoryOperationResult`。LLM 可以推理出候选偏好，但 `MODEL_INFERENCE` 分支不会创建 Memory，只返回需要用户/人工确认的结构化状态。这里是人机协同点：人确认后以 `explicit_user` 或 `human_reviewer` 重新提交，而不是让模型自己提高置信度绕过门槛。

## 5. 新旧事实比较：用 observed_at，不用到库顺序

```python
if not live or command.observed_at > latest_observed:
    # 旧 live 版本变为 superseded，新版本 active
else:
    # 无法证明候选更新：全部变为 conflicted
```

输入是同一租户、主体和语义键的已有版本，输出是一次 `MemoryMutation`。严格更新的 observation 可以替换旧值；时间相同或更旧的不同值没有可靠胜者，因此停止召回并等待新证据。数据库 `created_at` 只表示请求抵达时间，不能替代事实发生时间。

同值候选返回 `unchanged`，不创建重复版本；到期记录先变为 `expired`；后续严格更新的可信输入可以把冲突版本 supersede 并恢复一个 active 版本。

## 6. 事务 Resolver：业务决策与持久化技术分离

```python
models = session.scalars(
    select(LongTermMemoryRecord)
    .where(
        LongTermMemoryRecord.tenant_id == transaction.tenant_id,
        LongTermMemoryRecord.subject_id == transaction.subject_id,
        LongTermMemoryRecord.memory_key == transaction.memory_key,
    )
    .order_by(LongTermMemoryRecord.version)
    .with_for_update()
).all()
mutation = resolver(tuple(_to_domain(item) for item in models))
```

Repository 输入事务元数据和领域 Resolver，输出最终操作结果。它锁定同一语义键的版本，再把领域对象交给 Service 定义的 Resolver 计算；Repository 只负责把 `updated/created/event` 在同一数据库事务中提交。

这样内存测试和 SQL 实现复用完全相同的冲突规则，Service 不依赖 SQLAlchemy。已有记录并发更新会被行锁串行化；首次并发创建还由 `(tenant, subject, key, version)` 唯一约束保护，冲突调用会返回可重试的存储错误而不会制造两个 v1。

## 7. 幂等键：重放同一结果，拒绝偷换参数

```python
previous = self._event_for_key(
    session,
    transaction.tenant_id,
    transaction.idempotency_key,
)
if previous is not None:
    return _replay(previous, transaction.request_hash)
```

输入是租户内幂等键和稳定请求哈希。相同哈希返回第一次的 decision；不同哈希抛 `MemoryIdempotencyConflictError` 并由 HTTP 异常处理器映射为 409。幂等唯一范围包含 tenant，所以不同租户可以使用相同外部键而不会互相影响。

哈希排除 `request_id/trace_id`，因为它们是每次传输的观测字段；value 在标准化后进入哈希，保证大小写等规范化差异不会误报，但真正修改语义仍会冲突。

## 8. 删除：清除原文，不破坏审计链

```python
deleted = tuple(
    item.model_copy(
        update={
            "status": MemoryStatus.DELETED,
            "value": None,
            "updated_at": now,
        }
    )
    for item in visible
)
```

输入是同一 key 的所有未删除版本，输出是所有版本的删除变更。不能只删除 active 版本，否则 superseded/conflicted 版本仍含原文。`value_hash` 保留用于一致性审计，`value` 清空用于隐私删除；API 也只返回 active 版本，不暴露内部状态历史。

## 9. 召回：通过 Ticket 绑定主体并重复限制租户

```python
memories = self.memory_reader.list_active_for_ticket(
    state.tenant_id,
    state.ticket_id,
    at=self.clock(),
)
```

Planner 输入当前 `AgentRunState`，Repository 通过 Ticket 的 `customer_id` 找到 subject，并在 Ticket 与 Memory 两侧都限制 tenant。输出只包含 `status=active AND expires_at>now` 的记录，所以 conflicted、expired、deleted、superseded 都无法进入 Context。

Planner 不接受模型自己填写 subject_id，这可以阻止模型或 prompt injection 通过参数切换读取别人的偏好。

## 10. Memory 候选：仍要经过 Day15 Context Builder

```python
ContextFragmentCandidate(
    fragment_id=f"memory:{memory.id}",
    semantic_key=f"memory:{memory.memory_key}",
    source=ContextSource.MEMORY,
    trust_level=trust,
    content={
        "memory_key": memory.memory_key,
        "value": memory.value,
        "source_type": memory.source_type.value,
        "confidence": memory.confidence,
        "expires_at": memory.expires_at.isoformat(),
    },
    priority=74,
    relevance=80,
    ordinal=memory.version,
    replaceable=True,
)
```

输入是有效 `LongTermMemory`，输出是独立、可审计的 Context 候选。它保留来源、置信度、有效期和版本，而不是只拼接一段“用户喜欢中文”。同 key 的候选可替换，版本决定新鲜度；优先级低于系统规则和当前任务状态。

如果 Repository 抛 `MemoryReadError`，Planner 不把错误文本当 Memory，而是添加 `eligible=False / dependency_unavailable` 的 Trace 候选后继续安全任务。这个 fail-open 只适用于非安全关键的个性化依赖；退款资格、权限和人工审批仍然 fail-closed。

## 11. API 和异常边界

```text
POST   /api/v1/memories
GET    /api/v1/memory-subjects/{subject_id}/memories
DELETE /api/v1/memory-subjects/{subject_id}/memories/{memory_key}
```

Router 只处理 HTTP：读取 Header/Path/Body、构造 Command、调用 Service、序列化响应。业务错误由统一 Exception Handler 映射：幂等冲突为 409，Policy value 校验为 422，存储/读取不可用为 503，权限拒绝沿用统一 403。这样后台 Worker 或未来 Slack Adapter 可以直接复用 `MemoryService`，不会依赖 HTTP 状态码。

## 12. 数据库结构

`long_term_memories` 保存版本化内容和生命周期状态；唯一约束保证同一 `(tenant_id, subject_id, memory_key, version)` 只有一条。`memory_audit_events` 对 `(tenant_id, idempotency_key)` 唯一，并通过可空外键关联 Memory；删除原文后事件仍保留决策、哈希和调用链。

Alembic `20260805_0011` 同时创建两张表、查询索引、唯一约束和外键。ORM metadata 与迁移已通过 `alembic check`，SQLite 升级/降级和真实 PostgreSQL 都有自动化覆盖。
