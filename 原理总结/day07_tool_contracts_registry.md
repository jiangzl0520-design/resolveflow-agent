# Day 07：工具契约、注册中心与 Observation

## 完成内容

建立统一Tool Contract、Registry、Executor和Observation，把订单、物流、政策查询从普通Python函数提升为可校验、可授权、可观测的Agent工具。

## 完整流程

```text
模型输出tool_name + 业务参数
→ ToolRegistry按名称和版本查找契约
→ ToolExecutor从可信Context读取actor/tenant/run/step
→ 检查所需权限
→ Pydantic校验输入参数
→ 执行Tool handler
→ Pydantic再次校验工具输出
→ 记录耗时、结果或标准错误
→ 生成统一Tool Observation
→ Agent根据Observation决定下一步
```

Tool Contract声明名称、版本、输入/输出Schema、风险等级、副作用、所需权限和超时。Registry解决当前可用工具的发现与版本选择；Executor统一执行安全检查和记录，避免每个工具重复实现一套。

`tenant_id`、actor、request、trace、Agent run和step来自可信执行上下文，不能由模型生成。模型只提供完成动作所需的业务参数，例如`order_id`。

## Observation与错误

工具原始输出必须通过输出Schema后才能成为Observation。成功传输不等于输出可信；错误结构、错订单或过期事实都不能直接进入Agent State。

标准错误包括selection、argument、authorization、timeout、dependency、resource和output contract等类型。错误分类决定恢复动作：选错工具重新选择，参数错修参数，临时依赖错误有界重试，权限错误停止，资源不存在更新事实，输出契约错误拒绝结果并告警。

## 测试证据

专项16项、全量94项测试通过；30个确定性工具案例全部被正确执行或分类，跨租户参数注入和非法输出没有进入Observation。

## 与Agent的关系

Day07提供“可以安全执行的动作集合”；Day08的Agent Loop只负责根据目标和Observation选择这些动作，不直接绕过Executor调用业务函数。

