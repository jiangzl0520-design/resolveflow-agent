# Day 17 关键代码

## 1. 文件与职责

| 文件 | 职责 |
|---|---|
| `app/security/content.py` | 递归检查外部内容，标记注入并脱敏常见秘密/PII |
| `app/context_engineering/security.py` | 根据 Context trust 决定隔离或保留候选 |
| `app/context_engineering/builder.py` | 在选择和 Token 预算前执行安全预处理，记录失败 Trace |
| `app/agent/security.py` | 检查模型决策的当前步骤能力、资源绑定和终端输出 |
| `app/agent/runner.py` | 最小 Agent Loop 在工具执行前调用安全策略 |
| `app/agent/workflow.py` | LangGraph plan 节点执行相同策略并 checkpoint 安全拒绝 |
| `app/services/grounded_answer_service.py` | 在检索/重排/回答前过滤直接注入、恶意证据和秘密 |
| `evaluation/run_day17_context_security.py` | 100 例固定数据的 Prompt-only 与分层安全对照 |

## 2. Content Guard：递归保护结构化数据

```python
class UntrustedContentGuard:
    def protect(self, value: Any) -> ProtectedContent:
        protected, flags = self._protect(value)
        return ProtectedContent(protected, frozenset(flags))
```

输入可以是字符串、字典、列表或嵌套 JSON，输出包含安全版本 `value` 和 flags。递归处理很重要：Tool Observation 和 Context 不是单一字符串，秘密可能出现在 `output.summary`、列表元素或嵌套字段中。只扫描最外层序列化字符串虽然也能匹配，却很难保证替换后 JSON 仍合法。

它在整体架构中位于外部数据与 Context/Knowledge Model Call 之间，不负责权限和业务判断。异常不依赖外部服务，因此没有网络重试；未知攻击可能漏检，后续工具门禁必须继续工作。

## 3. 敏感信息脱敏

```python
for pattern in _SECRET_PATTERNS:
    replaced, count = pattern.subn("[REDACTED_SECRET]", protected)
protected, email_count = _EMAIL.subn("[REDACTED_EMAIL]", protected)
protected, phone_count = _CN_PHONE.subn(
    "[REDACTED_PHONE]", protected
)
```

输入是单个字符串，输出是保持业务结构的脱敏文本。当前覆盖 private key、OpenAI 风格 key、JWT、Bearer Token、常见 password/api_key/token 赋值、邮箱和中国大陆手机号。

采用替换而不是整段删除，是为了让模型仍知道“这里存在联系方式/凭证，但值不可见”，并保持其他业务内容。Context Trace 对脱敏后的实际内容做哈希，不保存原秘密。规则覆盖不是完整 DLP，后续应由真实数据补充而不是盲目增加正则。

## 4. ContextSecurityPolicy：信任标签决定是否隔离

```python
if candidate.trust_level is ContextTrustLevel.SYSTEM:
    return candidate
protected = self._guard.protect(candidate.content)
if (
    candidate.eligible
    and candidate.trust_level
    in {USER_PROVIDED, EXTERNAL_DATA}
    and protected.prompt_injection_detected
):
    return candidate.model_copy(
        update={
            "content": protected.value,
            "eligible": False,
            "exclusion_reason": SECURITY_POLICY_BLOCKED,
        }
    )
```

输入是带 source/trust 的 `ContextFragmentCandidate`，输出是同一 fragment_id 的安全版本。SYSTEM 不被外部检测器改写；所有非系统内容会脱敏；只有用户或外部来源的注入命中会被隔离。verified_internal 不因包含安全术语被误删，但仍会脱敏秘密。

该策略不直接写数据库，由 Context Builder 统一记录最终安全候选的内容哈希和决策。

## 5. Required 攻击为什么终止整个 Build

```python
if any(
    candidate.required
    and decisions.get(candidate.fragment_id)
    is ContextDecisionReason.SECURITY_POLICY_BLOCKED
    for candidate in candidates
):
    # 未选择片段标记 build_aborted，持久化 failed run
    raise RequiredContextSecurityError()
```

输入是预处理后的全部候选，输出要么继续选择，要么抛 `required_context_security_blocked`。当前 Goal 属于 required；把恶意 Goal 删除后继续等于让 Agent 在无真实目标下自主运行，因此必须 fail-closed。

可选恶意 RAG/History 只进入普通预排除流程，其他片段仍可完成 Build。失败前先写 Trace，保证模型没有调用也有可观测证据；Trace 写入失败仍沿用 Day15 的 `context_trace_recording_failed`。

## 6. Grounded Answer 的直接注入门禁

```python
protected_question = self._security_guard.inspect_text(query.question)
if protected_question.prompt_injection_detected:
    return self._safe_result(
        run,
        status=KnowledgeAnswerStatus.SECURITY_BLOCKED,
        answer_text=SECURITY_BLOCKED_ANSWER,
        started_tick=started_tick,
    )
safe_question = protected_question.value
```

输入是用户政策问题，输出是安全问题或终止结果。Run 在检查前已经创建，所以安全拒绝仍有 query hash、actor、request/trace 和耗时；但不会调用 Searcher、Embedding 或 LLM。

问题中的普通邮箱/电话会先脱敏再检索，避免进入 embedding 与 Prompt。当前政策领域不需要用联系方式检索；如果未来业务真的依赖某类 PII，必须设计受控的字段化查询，不能关闭整个脱敏层。

## 7. 恶意知识在 rerank 前过滤

```python
eligible_hits = _security_eligible_hits(
    _eligible_hits(retrieval.hits, query.as_of),
    self._security_guard,
)
```

输入先经过业务有效期过滤的 `KnowledgeSearchHit`，输出只保留没有已知注入和秘密的证据。它发生在 `_deduplicate_evidence`、rerank 和 answer generation 之前，因此被隔离文档不会作为模型输入，也不能被模型引用。

`eligible_candidate_count` 反映时间和安全过滤后的数量。若全部被过滤，服务返回 `insufficient_evidence` 且不调用模型；ACL 仍由 Day13 Repository 在数据库排序前执行。

## 8. 生成输出仍需检查

```python
protected_claim = security_guard.inspect_text(draft_claim.text)
if (
    protected_claim.prompt_injection_detected
    or protected_claim.sensitive_data_detected
):
    raise GroundingVerificationError()
```

输入是模型生成的结构化 claim，输出是继续进行引用验证或失败。即使安全证据中没有秘密，模型也可能生成不该返回的 token；因此输出必须独立检查。失败 Answer Run 记录 `grounding_verification_failed`，不会返回半安全答案。

随后 Day14 仍检查 evidence_id、exact_quote 和 claim citation，安全检查不替代 grounding。

## 9. AgentDecisionSecurityPolicy：Schema 合法不等于动作获准

```python
if (
    (decision.tool_name, decision.tool_version) in available
    and decision.tool_name not in relevant_tool_names(state)
):
    return blocked("agent_tool_not_allowed_for_step")

if decision.tool_name in {"order_lookup", "logistics_lookup"}:
    if (
        arguments.order_id is not None
        and arguments.order_id != state.order_id
    ):
        return blocked("agent_tool_resource_binding_mismatch")
```

输入是当前 `AgentRunState`、结构化 `AgentDecision` 和 Actor 可用工具描述；输出是 allowed/code。动态工具检查防止模型调用 Context 中没有开放的能力，资源绑定防止模型把当前订单 10086 偷换成 10087。

参数缺失不会在这里冒充攻击，而由工具 Schema 返回 `argument_error`；未知/无权限工具由 Tool Executor 返回 selection/authorization error。这种分层保留了准确错误类型，又保证 handler 只在所有确定性门禁通过后执行。

## 10. 终端输出和系统指令保护

```python
inspected = self._guard.inspect_text(output_text)
if inspected.sensitive_data_detected or _contains_protected_fragment(
    output_text,
    self._protected_fragments,
):
    return blocked("agent_output_security_blocked")
```

输入包括 reason、final_summary、escalation_reason 和 refund reason，输出决定该 Decision 是否可进入状态/返回。运行时把当前 `AGENT_PROMPT.instructions` 作为 protected value，拆成足够长的句子进行原文复现检查。

它只能拦常见秘密和大段原文，不保证识别所有改写，所以系统 Prompt 本身不得含秘密，真正权限必须在模型外部。

## 11. AgentRunner 的状态变化

```python
security = self._decision_security_policy.verify(
    state, decision, allowed_tools
)
if not security.allowed:
    state = self._append_step(... verification_code=security.code)
    state = replace(
        state,
        verification_failures=(
            *state.verification_failures,
            security.code,
        ),
    )
    continue
```

输入是 Planner 本轮候选，输出不是异常，而是一次可修正的安全反馈。被拒绝决策会增加 step_count 和 token 使用量，保存 Step Trace，但不会产生 Tool Observation，因为工具根本没有执行。下一轮 Context 会看到 verification failure；重复违规仍受 loop、step、time 和 token 预算限制。

## 12. LangGraph checkpoint 路径

```text
plan 产生 Decision
  → security denied
  → 保存 Step + verification_failure
  → pending_planner = None
  → route_after_plan 返回 control
  → checkpoint 持久化
  → control 再进入 plan
```

LangGraph 输入输出仍是 `AgentGraphState` patch。清除 pending 很关键：进程在拒绝后崩溃并恢复时，不能把旧恶意 Decision 路由到 `execute_tool`。规则与 AgentRunner 复用同一策略类，避免教学 Loop 和生产工作流产生不同安全语义。

## 13. 后端最终门禁仍保持不变

Decision Security 只保护 Agent 输出到 Tool Executor 的边界。Tool Executor 仍重新 resolve name/version、检查 Actor 权限、Pydantic 参数、超时和输出 Schema；退款写操作仍经过 Policy Engine、proposal hash/version、四眼审批、execution grant、幂等和事后验证。

这就是“模型负责不确定性推理，后端负责确定性安全”的具体代码位置。
