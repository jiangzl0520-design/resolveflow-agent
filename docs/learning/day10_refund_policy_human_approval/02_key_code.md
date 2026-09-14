# Day 10 关键代码与架构位置

## 1. 模型只能输出候选方案

```python
class RefundProposalCandidate(BaseModel):
    amount_minor: int = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    reason: str = Field(min_length=5, max_length=300)
```

这段代码只校验模型输出的基本结构，不能证明订单可退款，也不能授权资金写入。它位于Planner输出边界，作用是把自由文本变成可校验的候选数据。

## 2. Policy Engine重查业务规则

```python
if candidate.amount_minor > order.amount_minor:
    return RefundPolicyEvaluation(
        allowed=False,
        code="refund_amount_exceeds_order_total",
    )
if candidate.currency != order.currency:
    return RefundPolicyEvaluation(
        allowed=False,
        code="refund_currency_mismatch",
    )
```

这里使用工具Observation中的订单金额和币种，而不是相信模型。它位于模型与审批之间，任何拒绝都会让状态图停止在写工具之前。

## 3. 普通resume不能通过退款门

```python
if (
    _refund_interrupt_payload(snapshot) is not None
    and action != "cancel"
):
    raise RefundApprovalRequiredError(
        "Use review_refund with a trusted reviewer identity."
    )
```

通用暂停允许`continue`，退款审批不允许。高风险恢复必须走`review_refund()`，因为只有该入口接收后端认证后的审核人身份、版本和哈希。

## 4. 审批绑定版本与哈希

```python
if (
    command.expected_proposal_version != proposal.version
    or command.expected_proposal_hash != proposal.proposal_hash
):
    raise RefundReviewDeniedError(
        "refund_review_binding_mismatch"
    )
```

这段代码防止“审核100元，执行1000元”和“审核旧订单，执行新订单”。参数一旦修改就会产生新版本和新哈希，旧审批无法继续。

## 5. 四眼原则

```python
if command.actor.actor_id == proposal.requester_actor_id:
    raise RefundReviewDeniedError(
        "refund_review_four_eyes_required"
    )
```

发起退款提案的人不能审核自己的提案。修改者也不能批准自己创建的新版本，必须由另一名有权限的审核人确认。

## 6. 后端签发精确执行凭证

```python
arguments["execution_grant"] = (
    self._refund_execution_guard.issue(
        arguments,
        tenant_id=proposal.tenant_id,
        actor_id=reviewer.actor_id,
    )
)
```

执行凭证由后端HMAC密钥生成，绑定完整退款参数、租户和审核人。它位于审批节点之后、写工具之前；模型、用户和普通客户端无法自行计算有效签名。

## 7. 工具自己验签

```python
if not execution_guard.verify(
    parsed,
    tenant_id=context.tenant_id,
    actor_id=context.actor.actor_id,
):
    raise ToolExecutionGrantError()
```

这是最内层安全边界。即使内部调用者绕开Agent工作流，只要没有精确执行凭证，退款工具仍会拒绝。权限检查和执行凭证检查缺一不可。

## 8. 幂等冲突

```python
existing = self._refunds.get(key)
if existing is not None:
    if existing.fingerprint != fingerprint:
        raise ToolIdempotencyConflictError()
    return existing.observation
```

相同幂等键和相同参数返回第一次结果；相同键但参数变化直接冲突。它解决写成功后checkpoint未提交所造成的重复执行窗口。

## 9. 执行后核验

```python
verified = (
    observation.output.order_id == proposal.order_id
    and observation.output.amount_minor == proposal.amount_minor
    and observation.output.proposal_hash == proposal.proposal_hash
    and observation.output.approval_id == approval.approval_id
)
```

写工具返回成功只是一个Observation。核验节点重新查询支付状态，并把外部事实与当前提案、审批逐项比较；只有全部一致才产生`refund_completed`。

## 10. 关键文件

- `app/agent/models.py`：模型Decision与退款候选结构；
- `app/policy/refund.py`：确定性Policy与版本化提案；
- `app/agent/approval.py`：人工审批命令与审批记录；
- `app/agent/workflow.py`：审批中断、状态变化、执行与核验；
- `app/tools/refund.py`：写/读工具、HMAC执行凭证和幂等数据源；
- `app/domain/auth.py`：审批、退款执行和状态读取权限；
- `tests/test_refund_approval_workflow.py`：端到端安全门测试；
- `evaluation/run_day10_refund_safety.py`：可复现价值评估。
