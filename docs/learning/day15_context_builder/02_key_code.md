# Day 15 关键代码

## 1. 文件与架构职责

| 文件 | 职责 |
|---|---|
| `app/domain/context.py` | 上下文来源、可信度、候选片段、预算、选择结果和 Trace 契约 |
| `app/context_engineering/builder.py` | 去旧、去重、相关性过滤、预算装配、渲染和 fail-closed 记录 |
| `app/context_engineering/tokens.py` | 目标模型 Token 计算与未知模型编码回退 |
| `app/context_engineering/agent_context.py` | 把 Agent State/Observation/工具描述翻译成候选片段，并筛选当前步骤工具 |
| `app/agent/planner.py` | 在 Model Gateway 前调用 Context Builder，并关联 `context_build_id` |
| `app/agent/runtime.py` | 用同一 PostgreSQL 为 Context Trace 和 Model Call 组装生产适配器 |
| `app/repositories/sqlalchemy_context_trace_repository.py` | Context Run 与全部片段决策的事务写入 |
| `migrations/versions/20260804_0010_add_context_engineering_traces.py` | 新增两张 Trace 表和模型调用外键 |
| `evaluation/run_day15_context_builder.py` | 同数据、同预算、同 tokenizer 的基线对照评估 |

## 2. ContextFragmentCandidate：先把信息变成有语义的候选

```python
class ContextFragmentCandidate(BaseModel):
    fragment_id: str
    semantic_key: str
    source: ContextSource
    trust_level: ContextTrustLevel
    content: JsonValue
    priority: int
    relevance: int
    ordinal: int = 0
    required: bool = False
    replaceable: bool = False
    eligible: bool = True
    exclusion_reason: ContextDecisionReason | None = None
```

输入是单个信息片段及其元数据，输出仍是不可变候选对象。Builder 不靠正文关键词猜它是系统规则还是历史记录，而是由上游适配器显式声明。系统规则被模型校验器强制为 `SYSTEM + required`；已经失效或越权的候选必须同时给出 exclusion reason，不能悄悄消失。

它位于 Agent State/RAG/Memory 与最终 Model Request 之间，不写 Agent State。校验失败时在构建前直接拒绝，避免无来源片段进入选择算法。

## 3. ContextWindowBudget：输出和推理空间先预留

```python
@computed_field
def input_budget_tokens(self) -> int:
    available = self.context_window_tokens - (
        self.reserved_output_tokens
        + self.reserved_reasoning_tokens
        + self.safety_margin_tokens
    )
    return min(self.max_input_tokens, available)
```

输入是模型窗口及四项配置，输出是本轮输入硬上限。这样 `max_input_tokens` 不会把模型的输出和推理空间占满。若预留后没有至少一个输入 Token，配置启动时或 Pydantic 构造时失败，而不是等 Provider 返回超窗口错误。

## 4. 选择算法：required 先行，optional 逐个试装

```python
required_tokens = self._measure(request, required)
if required_tokens > request.budget.input_budget_tokens:
    self._record(failed_run, traces)
    raise RequiredContextOverflowError()

for candidate in optional:
    trial = [*selected, candidate]
    if self._measure(request, trial) <= request.budget.input_budget_tokens:
        selected.append(candidate)
        decisions[candidate.fragment_id] = ContextDecisionReason.SELECTED
    else:
        decisions[candidate.fragment_id] = (
            ContextDecisionReason.TOKEN_BUDGET_EXCEEDED
        )
```

执行顺序是预排除 → superseded → duplicate → low relevance → required → optional。每加入一个 optional 都重新渲染并计算完整输入，避免“各片段 Token 相加”漏掉 JSON 字段名、分隔符、系统指令和输出 Schema。

required 超预算先记录失败，再抛异常；可选片段超预算只丢该片段。Builder 不修改历史或状态，只生成本次 `ContextBuildResult`。存储失败会转换为 `ContextTraceRecordingError` 并阻止模型调用。

## 5. 最新修改与旧 Observation 失效

```python
invalidated = _references_different_order(
    observation,
    state.order_id,
)
```

工具成功不等于永远有效。输入是一个 Tool Observation 和当前权威订单号；若输出里的 `order_id` 不同，候选被设置为 `eligible=False / INVALIDATED`。History 仍保留修改过程，但本轮模型不会同时看到 10087 目标和 10086 物流事实。

同一工具的多次 Observation 使用相同 semantic key 和递增 ordinal，Builder 只保留最新可替换片段。失败 Observation 没有订单字段时仍保留，因为它说明最近的工具执行为什么失败。

## 6. 动态工具集合

```python
missing: set[str] = set()
if _latest_valid_success(state, "logistics_lookup") is None:
    missing.add("logistics_lookup")
if _latest_valid_success(state, "policy_lookup") is None:
    missing.add("policy_lookup")
return frozenset(missing)
```

输入是当前 Agent State，输出是仍能填补证据缺口的工具名集合。订单事实是下游调查的前提，所以初始只提供 `order_lookup`；订单确认后，物流和政策如果都缺失就同时提供，让 Planner 决定先查哪个。某个工具已经产生有效 Observation 后，它的完整 Schema 不再重复进入下一轮。

该函数只决定可见能力，不决定退款资格和最终动作。权限过滤仍先由 Tool Registry 完成；实际调用仍由 Tool Executor 重新做名称、版本、参数、权限、超时和输出 Schema 校验。

## 7. Planner 接入位置

```python
context = self.context_builder.build(ContextBuildRequest(...))
response = self.gateway.generate(
    StructuredModelRequest(
        instructions=context.instructions,
        input_text=context.input_text,
        response_model=AgentDecision,
        context_build_id=context.run.id,
        ...,
    )
)
```

输入来自 `AgentRunState + registry.descriptors_for(actor)`，Builder 输出真正发给 Provider 的 instructions 和 input text。它位于 Planner 内、Model Gateway 前，因此所有 Agent 决策调用都经过同一预算和审计门禁。

Context 错误被翻译成 `AgentPlannerError`，继续走现有 Agent 终止/恢复规则；模型网关错误仍按原有重试和记录处理。`context_build_id` 同时写进成功和失败的 Model Call，便于从模型故障回查本轮输入组成。

## 8. 为什么 instructions 和 runtime data 分开

```text
instructions = 稳定系统规则
input_text = <agent_runtime_data>{带来源标签的动态 JSON}</agent_runtime_data>
```

系统规则不会与用户修改、外部工具内容混在同一数组中。动态数据内部还保留 source 和 trust，模型看到的不是无边界文本拼接。稳定前缀也符合 Prompt Caching 的基本布局要求，但缓存是否命中必须以后从真实 Provider usage 中测量。

## 9. PostgreSQL Trace 与事务边界

`SqlAlchemyContextTraceRepository.record()` 在一个事务中插入一条 `context_build_runs` 和全部 `context_fragment_traces`。任意一条失败就回滚，避免 Run 存在但片段决策不完整。

Model Call 是下一次独立事务，因为外部模型调用位于两者之间：先持久化 Context，才允许调用 Provider，再记录调用结果。这样 Provider 超时也能找到它原本收到的 Context；代价是可能出现“Context 已构建但模型尚未调用”的记录，这是真实故障状态，不应回滚伪装成从未发生。

## 10. 配置与运行组合

`Settings` 新增：

- `LLM_CONTEXT_WINDOW_TOKENS`
- `LLM_MAX_INPUT_TOKENS`
- `LLM_RESERVED_REASONING_TOKENS`
- `LLM_CONTEXT_SAFETY_MARGIN_TOKENS`

`create_model_gateway_agent_planner()` 把目标模型 tokenizer、SQL Context Repository 和这些预算统一注入 Planner。测试可以注入内存 Repository 和 Fake Provider；生产组合不能让 SQL Model Call Repository 搭配内存 Context Repository，否则外键链路不完整。

## 11. Tokenizer 不可用时的保守降级

`TiktokenTokenCounter` 的输入仍是模型名和文本，正常输出仍是目标编码的精确 Token 数。模型名未知时先尝试 `o200k_base`；如果编码资源因网络或本地缓存故障无法加载，则改由 `Utf8ByteTokenCounter` 返回 UTF-8 字节数。

BPE 的每个 Token 至少承载一个字节，因此字节数可能高估、但不会低估 Token 数。它位于 Context Builder 之前，不改变候选片段、优先级或审计结构；状态变化仅是预算更保守，代价是故障期间可能少装入一些可选上下文。编码加载失败不再阻止 Planner 初始化，而 required 片段放不下时仍沿用原有拒绝调用模型的安全门禁。
