# Day 8 测试与评估证据

## 1. 环境

- 日期：2026-07-25
- Python：3.12.13
- Day8没有新增依赖和数据库迁移
- Planner测试：Scripted/Fake/确定性Observation-driven
- 真实付费模型调用：0

## 2. 专项测试

文件：

- `tests/test_agent_loop.py`
- `tests/test_agent_planner.py`
- `tests/test_day08_evaluation.py`

覆盖：

1. 同一Planner面对已取消和已发货订单选择不同路径；
2. 提前finish被Verifier拒绝，补齐证据后完成；
3. 临时dependency error进入State并成功重试；
4. selection和argument error后修正；
5. 第三次相同Decision触发loop detection；
6. max step硬终止；
7. token超预算时不执行工具；
8. time预算在下一轮规划前终止；
9. 权限失败转人工；
10. Planner错误保留terminal error code；
11. 错订单Observation不能满足当前订单Verifier；
12. ModelGateway Planner驱动完整结构化循环；
13. 30例合成评估回归断言。

专项结果：

```text
13 passed
```

## 3. 全量回归

最终命令：

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test"
.\.venv\Scripts\python.exe -m pytest -W error
```

最终结果：

```text
107 passed in 10.59s
```

同时完成以下检查：

```text
python -m compileall -q app tests evaluation
通过，无输出

alembic check
No new upgrade operations detected.
```

## 4. 合成评估

数据集：

`evaluation/datasets/day08_agent_loop_v1.json`

运行器：

`evaluation/run_day08_agent_loop.py`

报告：

`evaluation/reports/day08_agent_loop_v1.json`

命令：

```powershell
.\.venv\Scripts\python.exe evaluation\run_day08_agent_loop.py `
  --output evaluation\reports\day08_agent_loop_v1.json
```

30例合成场景：

- 取消订单12；
- 已签收且有证明8；
- 运输中5；
- 物流第一次临时失败5。

对比基线：

```text
固定order → logistics → policy
```

当前版本：

```text
Observation-driven Agent Loop + Verifier + Budget
```

| 指标 | 固定流程 | 最小Agent Loop |
|---|---:|---:|
| 完成Run | 13/30 | 30/30 |
| 正确Outcome | 13/30 | 30/30 |
| Tool calls | 73 | 71 |
| 终止订单后的无效调用 | 12 | 0 |
| 临时故障恢复 | 0/5 | 5/5 |
| Agent steps | 不适用 | 101 |

## 5. 解释边界

这些数据证明：

- 同一代码可以根据Observation分支；
- 取消订单不会继续固定查询物流；
- 临时工具故障可以在预算内恢复；
- Verifier和终止条件可自动回归。

不证明：

- 真实LLM达到100%工具选择率；
- 真实客户任务完成率；
- 生产延迟、token或成本；
- 真实业务收益；
- 进程重启恢复。

所有业务事实、Planner Decision和失败均为合成数据。

## 6. 实现中修正的问题

### 6.1 Verifier必须绑定主事实

初版按Observation类型查找，可能错误接受订单10087的Observation来完成订单10086。修正为同时匹配当前`order_id`，Policy同时匹配TicketCategory。

### 6.2 轨迹记录失败不能继续读取Observation

Tool Recorder失败时Runner收到已终止State，必须立即返回，不能再访问不存在的最后Observation。

### 6.3 Planner具体错误码不能丢失

只有`planner_error`终止原因不足以定位模型输出非法、Provider不可用或记录失败。State新增`terminal_error_code`。

### 6.4 Token不能为负数

PlannerResult拒绝负token，防止错误Adapter绕过总token预算。

## 7. 下一阶段

Day9把当前State和节点映射到LangGraph：

- 条件边；
- checkpoint；
- 暂停、恢复和取消；
- 进程重启后继续；
- 已成功步骤不重复。
