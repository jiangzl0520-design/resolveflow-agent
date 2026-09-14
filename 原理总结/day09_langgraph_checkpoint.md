# Day 09：LangGraph 状态图与 checkpoint

## 完成内容

把Day08自研循环映射为LangGraph状态图，引入PostgreSQL checkpoint、持久化暂停、进程重启恢复、取消和工具版本锁定。

## 完整流程

```text
用thread_id创建或加载运行
→ control节点检查暂停、恢复和取消
→ plan节点生成结构化Decision
→ execute_tool节点调用已锁定版本的工具并写Observation
→ verify节点检查finish资格
→ 按条件边回到plan、结束或interrupt
→ 每个节点边界由checkpointer持久化State
```

Graph State只保存JSON安全值，例如目标、工具名称/版本、参数、Observation和预算，不直接保存Session、Repository、函数和Pydantic运行时对象。节点执行时根据已锁定的工具版本重新构造领域对象；恢复时找不到原版本必须拒绝，不能悄悄换新工具改变历史语义。

`thread_id`标识一条可恢复执行线。`interrupt()`把状态写入checkpoint后暂停；恢复必须使用同一thread id和`Command(resume=...)`。新进程连接同一PostgreSQL checkpointer后能从准确节点继续，checkpoint前已记录的成功只读工具不会重新执行。

## checkpoint边界

Checkpoint保证工作流状态恢复，不保证外部副作用Exactly-once。写工具可能在“外部操作成功、下一checkpoint尚未提交”之间崩溃，因此Day10仍必须使用业务幂等键和执行后验证。

LangGraph自己的`checkpoint_*`表由框架setup管理；ResolveFlow业务表由Alembic管理，两套迁移责任不能混用。

## 测试证据

Day09非PostgreSQL专项9项、真实PostgreSQL重建Runtime专项1项通过；全量117项通过。30个评估任务全部从正确位置恢复并得到正确Outcome。

## 与后续Day的关系

Day09提供可暂停和恢复的Agent Runtime；Day10利用interrupt建立真正人工审批门，后续长期任务和上下文恢复继续复用同一状态图原则。

