# Day22：关键代码与架构位置

## 1. Judge 结构化输出契约

`app/evaluation/judge.py` 定义四维分数：

```python
class SemanticScores(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    clarity: int = Field(ge=1, le=4)
    evidence_grounding: int = Field(ge=1, le=4)
    reasoning_completeness: int = Field(ge=1, le=4)
    actionability: int = Field(ge=1, le=4)
```

解释：固定字段让不同运行可以逐维比较，1–4 边界阻止异常分数，`extra="forbid"` 防止模型偷偷加入未定义结论。冻结对象避免评分后被静默修改。

## 2. 偏好与分数必须内部一致

```python
difference = self.response_a.mean - self.response_b.mean
if self.preference is JudgePreference.RESPONSE_A and difference <= 0:
    raise ValueError("response_a preference conflicts with scores")
```

解释：结构化 Schema 只保证字段存在，`model_validator` 再保证字段之间的业务关系。若 B 分高却偏好 A，ModelGateway 把输出视为无效，而不是勉强使用一个自相矛盾的结果。

## 3. 盲化 Pairwise Prompt

`ExplanationJudge.evaluate()` 只装配：

```python
{
    "case_ref": case_ref,
    "task": task,
    "response_a": response_a,
    "response_b": response_b,
}
```

解释：Payload 没有 Candidate/Baseline、Provider 或模型版本。待评内容被 JSON 编码，并由 System instructions 明确声明为不可信数据。Prompt 还写明 Judge 不负责 Tool、授权、资源绑定和 Policy，避免职责漂移。

## 4. 统一 ModelGateway

Judge 不直接调用 Provider SDK，而是构造 `StructuredModelRequest[JudgeResult]` 交给现有 ModelGateway。

解释：因此 Judge 自动继承模型版本、Prompt 名称/版本/哈希、Schema、Token、错误码、request_id、trace_id 和调用记录。生产模型、Fake Provider 和未来其他 Provider 可以替换，Judge 业务接口不变。

## 5. 顺序交换与身份归一化

`evaluation/day22_calibration.py` 每个 Case 调用两次：

```python
original = judge.evaluate(response_a=case.response_a,
                          response_b=case.response_b, ...)
swapped = judge.evaluate(response_a=case.response_b,
                         response_b=case.response_a, ...)
```

解释：原顺序 A 代表 Baseline、B 代表 Candidate；交换后相反。`_normalize_preference()` 在 Judge 之外还原版本身份。如果两次归一化偏好不同，该 Case 标记为 `unstable`，不会用随意多数投票隐藏问题。

## 6. Judge 与人工标签校准

每个 Case 保存两名盲化人工标注者的原始偏好和一份裁决后的四维分数。运行时计算：

- 人工标注者偏好一致率；
- Judge 与人工裁决偏好一致率；
- 顺序一致率和顺序敏感率；
- 维度分平均绝对误差；
- Judge 分数落在人工分数 ±1 内的比例。

解释：一个指标无法区分“整体方向正确但分数偏高”“分数接近但顺序敏感”等不同问题，所以报告保留多个校准维度和逐 Case 结果。

## 7. 硬门禁与语义门禁组合

```python
semantic_passed = (
    candidate_semantic_score >= config.minimum_semantic_score
)
release_decision = (
    case.candidate_hard_gate_passed and semantic_passed
)
```

解释：`and` 的左侧来自 Day21 确定性门禁，右侧来自已校准 Judge。语义高分无法覆盖安全失败。校准指标本身不达标时，Judge 也不能作为自动发布依据。

## 8. 版本化资产

- Rubric：`evaluation/rubrics/day22_explanation_quality_v1.json`
- 双人标注数据：`evaluation/datasets/day22_judge_calibration_v1.json`
- 阈值配置：`evaluation/configs/day22_judge_calibration_v1.json`
- 运行器：`evaluation/run_day22_judge.py`
- 报告：`evaluation/reports/day22_judge_calibration_v1_report.json`

解释：Rubric、数据、阈值和模型必须分别版本化。只改阈值却沿用旧报告，或改 Rubric 后直接比较旧分数，都会产生不可审计的结论。
