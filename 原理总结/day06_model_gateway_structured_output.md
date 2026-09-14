# Day 06：模型网关与结构化输出

## 完成内容

建立Provider-neutral ModelGateway、Prompt Registry、Pydantic结构化输出和模型调用记录，使业务服务不直接依赖厂商SDK，也不把模型自然语言直接当业务事实。

## 完整流程

```text
业务Service准备最小可信输入
→ ModelGateway选择Prompt名称/版本和输出Schema
→ Provider Adapter调用模型API
→ SDK返回结构化候选结果
→ Pydantic验证字段、类型和枚举
→ Gateway记录模型、Prompt、Schema、attempts、Token、耗时和错误
→ Service把结果当候选判断
→ 工具或后端规则继续验证事实和动作资格
```

业务层只依赖`ModelGateway`接口；Provider Adapter解决OpenAI API参数、超时和响应转换；Prompt Registry让Prompt有名称、版本和哈希；Pydantic输出模型把不稳定文本转换成受约束对象。

## 三道边界

1. 格式边界：Schema保证字段结构合法。
2. 事实边界：结构合法不代表内容真实，必须通过工具Observation验证。
3. 动作边界：事实真实也不代表允许退款，权限、Policy和人工审批仍由后端决定。

“Prompt要求返回JSON”不够，因为JSON可能缺字段、类型错误、出现未知动作或夹杂文本。结构化输出解决机器可消费性，不解决幻觉和业务授权。

## 重试与错误

连接中断、限流和服务端临时错误可以由Gateway使用相同输入有界重试，因为没有新业务事实产生。工具返回新Observation后，Agent应修改计划，而不是让Gateway原样重试模型。不可验证结果进入保守降级，不能伪装成成功。

模型调用记录保存可复现元数据，但不持久化原始敏感Prompt和工单正文。

## 测试证据

全量77项测试通过，模型网关专项7项通过；确定性评估覆盖30个候选结果，所有不可安全使用的结果都进入明确fallback，付费模型调用为0。

## 与后续Day的关系

Day06只产生候选判断；Day07把模型动作约束为工具契约，Day08将候选Decision放入Agent Loop，Day10再用Policy阻止模型直接执行高风险写操作。

