# Day 10 测试与评估证据

## 1. 环境

- 日期：2026-07-27
- Python：3.12.13
- LangGraph：1.2.9
- PostgreSQL测试库：`resolveflow_test`
- 真实付费模型调用：0

## 2. Day10专项自动化测试

文件：

- `tests/test_refund_policy.py`
- `tests/test_refund_tools.py`
- `tests/test_refund_approval_workflow.py`
- `tests/test_day10_evaluation.py`

覆盖：

1. Policy通过合法提案；
2. 超额退款、币种错误、缺少签收证明、订单不可退款和证据订单号不符；
3. 普通Agent不能执行退款；
4. 主管没有后端执行凭证也不能直接退款；
5. 精确幂等重放只产生一次副作用；
6. 相同幂等键配合不同参数返回冲突；
7. 所有合法候选在写操作前进入人工中断；
8. 普通resume不能跳过退款门；
9. 同租户、权限、四眼原则、版本和哈希绑定；
10. approve后执行一次并查询状态核验；
11. reject、takeover和过期都不执行；
12. modify生成新版本，旧版本失效，修改者不能自批；
13. Runtime重建后仍停在同一个审批门；
14. PostgreSQL关闭连接并重建Runtime后恢复审批；
15. 55例合成安全评估和20轮幂等重放回归。

## 3. 全量回归

命令：

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test"
.\.venv\Scripts\python.exe -m pytest -W error
```

结果：

```text
138 passed in 80.92s
```

其中真实PostgreSQL专项测试：

```text
9 passed, 129 deselected in 6.79s
```

Day10新增的PostgreSQL测试会先在Runtime A创建退款提案并停在人工审批中断，关闭Executor和PostgresSaver连接，再由Runtime B读取相同`run_id`、验证提案版本/哈希、批准、执行并核验。最终只产生一次退款副作用。

静态与迁移检查：

```text
compileall: passed
pip check: No broken requirements found.
checkpoint setup: LangGraph checkpoint tables are ready.
alembic check: No new upgrade operations detected.
```

LangGraph的四张`checkpoint_*`表由框架迁移管理。Alembic的`include_object`会在业务Schema diff中排除这些表，因此业务迁移不会建议删除框架状态，框架迁移也不会接管ResolveFlow业务表。

## 4. 合成评估

数据集：

`evaluation/datasets/day10_refund_safety_v1.json`

运行器：

`evaluation/run_day10_refund_safety.py`

报告：

`evaluation/reports/day10_refund_safety_v1.json`

场景共55例：

| 场景 | 数量 |
|---|---:|
| 合法独立审批 | 10 |
| 未提交审批 | 5 |
| 人工拒绝 | 5 |
| 人工接管 | 5 |
| 无权限审核人 | 5 |
| 跨租户审核人 | 5 |
| 发起人自批 | 5 |
| 旧版本/哈希审批 | 5 |
| 超订单金额候选 | 5 |
| 修改者自批新版本 | 5 |

基线定义是“模型产生退款候选后立即执行，不经过Policy和审批”。它是明确的安全反例，不是生产流量实测。

| 指标 | 无安全门基线 | ResolveFlow Day10 |
|---|---:|---:|
| 总写执行 | 55 | 10 |
| 未审批或非法写执行 | 45 | 0 |
| 合法批准并完成核验 | 不区分 | 10/10 |
| 避免非法写执行 | 0 | 45 |
| 非法写降低 | 基线 | 100% |

幂等重放：

| 指标 | 结果 |
|---|---:|
| 重放试验 | 20 |
| 重复请求 | 20 |
| 预期唯一副作用 | 20 |
| 实际唯一副作用 | 20 |
| 重复退款副作用 | 0 |

## 5. 数据解释边界

这些数据证明：

- 在固定合成场景中，写工具前人工门覆盖有效候选；
- 未审批、越权、跨租户、自批、旧审批、超额和修改者自批都没有产生退款；
- 合法独立审批可以执行并通过状态查询核验；
- 精确重复请求不会产生第二笔退款。

这些数据不证明：

- 生产订单分布、收入或客诉率提升；
- 真实LLM提案准确率；
- 真实支付Provider的可用性和延迟；
- HMAC密钥管理已经达到生产级；
- 仅靠LangGraph checkpoint可以提供Exactly-once。

所有订单、审核人与支付副作用均为合成数据，报告会显式保留这一限制。
