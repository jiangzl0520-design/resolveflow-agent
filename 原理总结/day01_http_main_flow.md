# Day 01：需求建模与 HTTP 主链路

## 完成内容

建立FastAPI工单创建和查询接口，完成从HTTP请求到Service、Repository、领域对象再到HTTP响应的第一条后端主链路。

## 完整流程

```text
客户端
→ Uvicorn接收网络连接并运行ASGI应用
→ Middleware生成request_id
→ FastAPI/Router按方法和路径匹配接口
→ Pydantic输入Schema校验HTTP数据
→ Router翻译为CreateTicketCommand
→ TicketService执行创建用例
→ Repository保存Ticket
→ 输出Schema序列化
→ Middleware写入X-Request-ID
→ 客户端收到201或错误响应
```

Router只负责HTTP协议适配：匹配路径、接收Schema、调用Service、声明成功状态；它不实现创建工单。Service保存可复用业务用例，Repository隔离存储。输入Schema只包含客户端允许提交的字段；`ticket_id`、初始状态和时间由Service生成，防止客户端伪造系统字段。

## 状态与错误

新Ticket由Service设为系统规定的初始状态并交给Repository。资源不存在时Service抛`TicketNotFoundError`，统一HTTP异常处理器将它映射成404；Service本身不依赖HTTP，因此后台Worker也能复用。Router不存在的404和业务资源不存在的404来源不同。

`ticket_id`追踪工单生命周期；`request_id`追踪一次HTTP处理，同一工单被查询三次会有一个ticket id和三个request id。Service/Repository通过应用级依赖共享，避免每个请求创建互不相见的内存存储。

## 测试证据

8项主链路测试通过，覆盖创建、查询、输入拒绝、404、request id、依赖注入和响应序列化。

## 关键结论

FastAPI建立在Starlette和Pydantic之上，Uvicorn负责运行ASGI应用。分层的价值不是“方便管理”这么简单，而是隔离外部协议、可复用业务规则、可替换存储并统一错误语义。

