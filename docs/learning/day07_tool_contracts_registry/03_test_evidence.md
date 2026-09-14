# Day 7 测试、评估与运行证据

## 1. 环境

- 日期：2026-07-25
- Python：3.12.13
- Pydantic：随FastAPI项目当前锁定依赖
- PostgreSQL：18.3-alpine
- Day 7没有新增数据库迁移
- 操作系统：Windows 11

## 2. 自动化测试范围

### 2.1 Registry与Executor

`tests/test_tool_registry.py`覆盖：

1. 只暴露Actor有权限的工具；
2. JSON Schema禁止额外字段；
3. 按name/version精确解析；
4. 重复name/version启动失败；
5. 工具名字符和semver校验；
6. 成功返回类型化Observation；
7. tenant、actor、trace、run、step和Schema hash被记录；
8. 未知工具是selection error；
9. 非法参数在Handler前被阻止；
10. 未授权Actor在Handler前被阻止；
11. Handler非法输出是output contract error；
12. Observation记录失败时fail closed。

### 2.2 三个售后工具

`tests/test_after_sales_tools.py`覆盖：

1. 订单、物流、政策工具正常返回；
2. 三个工具都是low risk + read only；
3. tenant从可信Context注入；
4. 跨tenant资源隐藏为not found；
5. 模型额外传tenant_id被拒绝；
6. 依赖失败单独分类且可重试；
7. 真实线程等待超时被标准化；
8. Customer既看不到目录也不能执行；
9. 参数格式错误与资源不存在明确区分。

专项结果：

```text
16 passed
```

## 3. 全量回归

命令：

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow_test"
.\.venv\Scripts\python.exe -m pytest -W error
```

最终结果：

```text
94 passed in 10.15s
```

同时：

```text
python -m compileall -q app tests evaluation
→ passed

alembic check
→ No new upgrade operations detected.
```

## 4. 可复现量化评估

数据集：

`evaluation/datasets/day07_tool_contracts_v1.json`

运行器：

`evaluation/run_day07_tool_contracts.py`

原始报告：

`evaluation/reports/day07_tool_contracts_v1.json`

运行：

```powershell
.\.venv\Scripts\python.exe evaluation\run_day07_tool_contracts.py `
  --output evaluation\reports\day07_tool_contracts_v1.json
```

30个合成案例：

- 正常工具调用10；
- 工具名/版本错误4；
- 参数错误4；
- 权限错误3；
- 依赖错误3；
- 超时3；
- 资源不存在3。

基线是一个明确的“无类型通用Dispatcher”：

- 没有JSON Schema/Pydantic参数门禁；
- 没有权限门禁；
- 没有timeout；
- 所有失败只返回generic error。

| 指标 | 无类型基线 | ResolveFlow |
|---|---:|---:|
| 精确结果分类 | 10/30 | 30/30 |
| 未授权案例到达Handler | 3 | 0 |
| 非法参数案例到达Handler | 2 | 0 |
| 未识别超时 | 3 | 0 |
| 可区分失败种类 | 1 | 6 |

这里的“30/30”只说明这组脚本化工具调用被正确执行或分类，不是LLM工具选择准确率，也不是完整Agent任务成功率。

## 5. 评估为什么使用确定性超时注入

第一次评估使用`10ms delay + 1ms timeout`真实线程等待，Windows调度使一个任务在线程提交后、主线程开始等待前已经完成，结果出现29/30。

没有修改断言接受不稳定结果。最终分为：

- 单元测试：使用真实线程delay验证Executor timeout路径；
- 离线评估：使用确定性Future timeout注入，保证同一数据集可复现。

这个区分很重要：

```text
真实机制测试
  证明代码能处理实际timeout

确定性评估
  证明版本比较不会被调度抖动污染
```

## 6. 实现中发现的问题

### 6.1 Pydantic全局strict误拒绝合法枚举JSON

最初`PolicyLookupArguments`使用`strict=True`，模型JSON中的`"not_received"`不会转换为内部枚举，导致合法调用被拒绝。

修正为：

- 保留`extra="forbid"`；
- 保留枚举、pattern、长度校验；
- 允许JSON字符串按声明解析为领域枚举。

专项测试最初1项失败，修复后全部通过。没有把“越严格越安全”当作绝对原则；契约必须与真实JSON协议一致。

### 6.2 Thread timeout不等于终止线程

`Future.result(timeout)`只停止等待。已运行线程可能无法cancel。因此当前三个工具明确只读。这个实现不能直接复制给退款写工具。

### 6.3 Schema版本可能被误改

只记录版本号无法发现“改Schema但忘记升级版本”。最终Observation增加input/output Schema hash。

## 7. 当前没有过度宣称的能力

Day 7尚未证明：

- LLM能正确选择工具；
- Agent能根据Observation动态修订计划；
- 真实订单/物流API可靠性；
- 真实生产延迟和吞吐；
- 工具调用持久化到完整Agent checkpoint；
- 写工具幂等、审批和执行后验证；
- MCP互操作；
- 多步任务完成率。

这些属于Day8–Day11以及可观测性和评估阶段。
