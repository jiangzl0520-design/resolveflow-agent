# Day 8 关键代码

## 1. 文件入口

```text
app/agent/models.py
app/agent/planner.py
app/agent/fake_planner.py
app/agent/verifier.py
app/agent/runner.py
```

## 2. models.py

### 输入和输出

输入模型包括`AgentRunCommand`、`AgentBudget`和`AgentDecision`；运行输出是完整`AgentRunState`。

### 执行流程

```text
Command
→ 初始化State
→ 每轮追加Step、Observation和token
→ 最终写入status、termination reason和outcome
```

### 设计原因

把目标、预算、Observation、轨迹和终止原因显式放入State，避免Planner依赖不可追踪的隐藏状态。

### 架构位置

这是Agent Runtime领域模型，不依赖OpenAI、FastAPI或数据库。

### 状态变化

State使用不可变dataclass，每次通过`replace`创建新状态。

### 异常

Command拒绝非法order_id、空goal和缺少trace；Planner token不能为负数。

## 3. planner.py

### 输入和输出

输入当前State和Actor有权访问的ToolDescriptor；输出`PlannerResult`，包含Decision、token和model call ID。

### 执行流程

```text
State
→ 构建最小runtime_data
→ 版本化Prompt
→ ModelGateway
→ Pydantic AgentDecision
→ PlannerResult
```

### 设计原因

业务循环依赖`AgentPlanner`协议，不直接依赖模型SDK。模型错误映射为`AgentPlannerError`。

### 架构位置

这是Agent decide阶段与Day6模型网关的适配器。

### 状态变化

Planner不直接修改State，由Runner统计token和追加轨迹。

### 异常

任何ModelGatewayError保留错误码并交给Runner终止。

## 4. verifier.py

### 输入和输出

输入当前State和finish Decision；输出`CompletionVerification`。

### 执行流程

```text
查找匹配当前order_id/category的最新成功Observation
→ 检查证据完整度
→ 检查Outcome与事实一致性
→ accepted或拒绝代码
```

### 设计原因

模型不能自证任务完成。完成条件由确定性代码检查。

### 架构位置

Verifier位于Planner和最终状态转换之间。

### 状态变化

拒绝结果进入`verification_failures`，下一轮Planner可以补证据；接受后Runner才写final outcome。

### 异常

缺失证据和结论冲突都返回明确code，不抛出普通异常。

## 5. runner.py

### 输入和输出

输入Command、Planner、Registry、Executor、Verifier和Budget；输出终态AgentRunState。

### 执行流程

```text
初始化
→ 预算检查
→ Planner
→ token更新
→ Decision循环检测
→ call_tool / finish / escalate
→ Observation或Verification写State
→ 循环或终止
```

### 设计原因

Runner只负责编排和硬边界，不包含供应商SDK或具体订单数据源。

### 架构位置

它是Day8最小Agent Runtime核心。

### 状态变化

每轮增加step；工具调用增加Observation；完成验证失败增加failure；终止写status/reason/error code/time。

### 异常

- Planner失败：failed/planner_error；
- 权限失败：escalated；
- 工具输出契约或内部错误：failed；
- selection/argument/resource/临时依赖错误：作为Observation返回下一轮；
- 预算或循环：硬终止。

## 6. fake_planner.py

### 输入和输出

输入预设Decision/PlannerResult/Exception序列；输出确定性Planner结果并保存看到的State和工具目录。

### 用途

用于稳定覆盖提前finish、错误修复、预算和循环，不消耗真实模型Token。

它只证明Agent Runner行为，不证明真实LLM规划质量。
