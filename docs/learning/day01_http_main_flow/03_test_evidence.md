# Day 1 自动化测试证据

## 1. 测试环境

- 操作系统：Windows
- Python：3.12.13
- FastAPI：0.139.0
- Pydantic：2.13.4
- Starlette：1.3.1
- pytest：9.1.1
- HTTPX2：2.7.0

依赖声明位于 `pyproject.toml`，隔离环境位于项目 `.venv`。

## 2. 最终验证命令

```powershell
.\.venv\Scripts\python.exe -m pytest -W error
```

该命令执行 `tests/` 下全部测试，并把警告提升为错误。这样不只是检查断言，还能阻止已弃用依赖路径被悄悄保留。

## 3. 覆盖范围

| 测试 | 类型 | 验证内容 |
|---|---|---|
| health check | 正常路径 | 200、服务状态、响应体和响应头使用同一 request_id |
| create then get | 正常路径 | POST 201、生成 UUID、初始状态 open、GET 可查到同一工单 |
| missing ticket | 失败路径 | 不存在的合法 UUID 返回统一 404 |
| blank subject | 失败/边界 | 只有空格的标题在进入 Service 前返回统一 422 |
| invalid category | 失败路径 | 未定义分类返回统一 422 |
| description 2001 | 越界 | 超过最大长度被拒绝 |
| description 2000 | 边界成功 | 刚好达到最大长度仍能创建 |
| client-supplied system fields | 安全/失败路径 | 客户端提交 `status`、`id`、`created_at` 时统一返回 422，且不创建工单 |

## 4. 最终原始结果

```text
........                                                                 [100%]
8 passed in 0.09s
```

结果说明八个收集到的测试用例全部通过，并且 `-W error` 下没有遗留警告。

## 5. 测试发现并修复的问题

第一次运行得到：

```text
7 passed, 1 warning
StarletteDeprecationWarning: Using `httpx` with `starlette.testclient`
is deprecated; install `httpx2` instead.
```

检查 Starlette 1.3.1 的 `testclient.py` 后确认它优先导入 `httpx2`，缺失时才回退到旧 `httpx`。随后把开发依赖改为 `httpx2>=2.7,<3.0`，重新运行严格测试，得到无警告的 7 passed。

这说明测试不仅用于证明代码正确，也能暴露依赖兼容和未来升级风险。

Day 1 面试讨论 Schema 与 Domain 信任边界时，又发现 Pydantic 默认会忽略额外字段。虽然 Router 的显式映射和 Service 自行生成系统字段已经阻止了状态覆盖，但静默忽略不利于发现错误或越权尝试。因此给 `TicketCreate` 增加 `extra="forbid"`，并新增测试证明客户端提交 `status`、`id`、`created_at` 时会返回统一 422。最终测试数从 7 增加为 8。

## 6. 当前没有覆盖的内容

- 真实 Uvicorn 进程和网络端口；
- PostgreSQL 持久化和事务；
- 多进程数据共享；
- 身份认证和租户隔离；
- Agent 运行和人工审批。

这些内容超出 Day 1 范围，不应把当前 7 个测试描述成完整生产验证。
