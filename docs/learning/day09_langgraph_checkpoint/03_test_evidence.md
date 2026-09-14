# Day 9 测试与评估证据

## 1. 环境

- 日期：2026-07-27
- Python：3.12.13
- LangGraph：1.2.9
- langgraph-checkpoint-postgres：3.1.0
- PostgreSQL测试库：`resolveflow_test`
- 真实付费模型调用：0

## 2. 依赖完整性

```powershell
.\.venv\Scripts\python.exe -m pip check
```

结果：

```text
No broken requirements found.
```

## 3. Day9专项测试

文件：

- `tests/test_agent_workflow.py`
- `tests/test_day09_evaluation.py`

覆盖：

1. 状态图正常完成；
2. checkpoint历史存在；
3. 第一步后暂停和继续；
4. 已checkpoint工具不重复；
5. 暂停状态取消；
6. 非法恢复动作失败关闭；
7. thread ID防覆盖；
8. 工具版本兼容性检查；
9. 时间预算跨Runtime；
10. 人工暂停时长不计入执行预算；
11. 恢复后的活跃执行时间继续受原预算限制；
12. PostgreSQL关闭连接、重建Runtime、继续执行；
13. 30例合成恢复评估回归。

阶段结果：

```text
9 non-PostgreSQL Day9 tests passed
1 PostgreSQL runtime recreation test passed
```

## 4. 全量回归

命令：

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test"
.\.venv\Scripts\python.exe -m pytest -W error
```

最终结果：

```text
117 passed in 15.04s
```

同时通过：

```text
python -m compileall -q app tests evaluation
pip check
alembic check
python -m app.agent.setup_checkpoints
```

## 5. PostgreSQL进程重建证据

测试：

```text
tests/test_agent_workflow.py::
test_postgres_checkpoint_survives_runtime_recreation
```

执行过程：

1. Runtime A调用`order_lookup`；
2. checkpoint后进入interrupt；
3. 关闭ToolExecutor和PostgresSaver连接；
4. 新建Runtime B、Registry、Executor和PostgresSaver；
5. 使用相同run_id读取checkpoint；
6. Runtime B只调用`logistics_lookup`和`policy_lookup`；
7. 最终completed。

这证明的是数据库级checkpoint恢复，不是同一Python对象继续运行。

## 6. 合成评估

数据集：

`evaluation/datasets/day09_checkpoint_recovery_v1.json`

运行器：

`evaluation/run_day09_checkpoint_recovery.py`

报告：

`evaluation/reports/day09_checkpoint_recovery_v1.json`

命令：

```powershell
.\.venv\Scripts\python.exe evaluation\run_day09_checkpoint_recovery.py `
  --output evaluation\reports\day09_checkpoint_recovery_v1.json
```

30例合成场景：

- 已取消订单15；
- 已签收且有证明15；
- 中断点均为第一次`order_lookup`成功之后。

| 指标 | 重启后从头执行 | LangGraph checkpoint |
|---|---:|---:|
| 正确Outcome | 30/30 | 30/30 |
| 从准确位置恢复 | 0/30 | 30/30 |
| 总工具调用 | 90 | 60 |
| 重复`order_lookup` | 30 | 0 |
| 避免的工具调用 | 0 | 30 |
| 工具调用降低 | 基线 | 33.33% |

## 7. 数据解释边界

这些数据证明：

- 保存checkpoint后，新Runtime能跳过已成功节点；
- 30个固定中断场景中避免30次重复查询；
- Outcome没有因为恢复而变化；
- 恢复过程可以由自动化测试复现。

这些数据不证明：

- 真实业务的故障率或收入提升；
- 生产PostgreSQL的P50/P95恢复延迟；
- LLM语义判断质量；
- 写操作Exactly-once；
- checkpoint提交前已经发生的外部副作用不会重复。

离线报告使用`InMemorySaver`保证确定性；真正的存储耐久性由单独PostgreSQL集成测试证明。所有业务事实、Planner Decision和中断均为合成数据。
