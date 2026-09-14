# Day 11 测试与评估证据

## 1. 环境

- 日期：2026-07-30
- Python：3.12.13
- MCP Python SDK：2.0.0
- 协议传输：Streamable HTTP
- 独立服务进程：Uvicorn
- 真实付费模型调用：0

## 2. Day11专项测试

文件：

- `tests/test_mcp_security.py`
- `tests/test_mcp_order_server.py`
- `tests/test_mcp_tool_adapter.py`
- `tests/test_mcp_process_integration.py`
- `tests/test_day11_evaluation.py`

覆盖：

1. 短期MCP Token包含精确audience、最小scope、tenant和trace；
2. 过期Token与错误audience被拒绝；
3. Actor不能下放自己没有的权限；
4. MCP Server返回结构化`OrderObservation`并记录远程审计；
5. Agent发现后仍得到原`ToolDefinition`和`ToolObservation`；
6. 版本和输出Schema漂移在Agent暴露前被拒绝；
7. 非法远程输出不能进入Agent State；
8. 连续依赖错误触发熔断，恢复窗口后探测成功；
9. 半开状态只允许一个恢复探测，避免并发请求同时冲击下游；
10. 真实Uvicorn子进程上的`tools/list`与`tools/call`成功；
11. 同订单号跨租户查询不可见；
12. 主应用JWT不能直接用作MCP服务Token。

专项结果：

```text
14 passed in 5.96s
```

## 3. 全量回归

连接真实`resolveflow_test` PostgreSQL测试库后的完整结果：

```text
152 passed in 23.63s
```

真实PostgreSQL专项结果：

```text
9 passed, 141 deselected in 5.64s
```

其他检查：

```text
compileall: passed
pip check: No broken requirements found.
alembic check: No new upgrade operations detected.
mcp: 2.0.0
```

Day11没有修改数据库表；PostgreSQL回归用于证明MCP依赖和远程错误分类没有破坏Day1–Day10的持久化、checkpoint和退款审批链路。

## 4. 60例合成评估

数据集：

`evaluation/datasets/day11_mcp_interoperability_v1.json`

运行器：

`evaluation/run_day11_mcp_interoperability.py`

报告：

`evaluation/reports/day11_mcp_interoperability_v1.json`

场景：

| 场景 | 数量 | 执行方式 |
|---|---:|---|
| 进程内工具与MCP工具结果一致性 | 30 | 真实Uvicorn + Streamable HTTP |
| 越权Token或跨租户 | 10 | 真实Uvicorn + Streamable HTTP |
| 远程契约漂移 | 10 | 确定性契约Gateway |
| 依赖连续故障 | 10 | 确定性故障Gateway |

结果：

| 指标 | 结果 |
|---|---:|
| 本地/MCP结果完全一致 | 30/30 |
| 互操作一致率 | 100% |
| 越权或跨租户成功调用 | 0/10 |
| 上线前发现并阻断契约漂移 | 10/10 |
| 故障期总请求 | 10 |
| 到达远程故障服务的请求 | 3 |
| 熔断避免的下游调用 | 7 |
| 恢复窗口后的探测 | 成功 |
| 付费API调用 | 0 |

这组数据体现的项目价值是：

- 工具拆成独立进程后，30个订单样本仍保持100%业务输出一致；
- 10个身份/租户攻击样本没有成功读取；
- 10个不兼容升级全部在Agent使用前被发现；
- 10次故障期请求中有7次被本地熔断，减少70%的无效下游压力。

## 5. 数据解释边界

这些数据证明：

- 同一个内部Tool Contract可以由进程内handler或独立MCP服务实现；
- 真实本地HTTP进程上的发现和调用能够工作；
- 远程结果仍经过本地Schema校验；
- 主JWT透传、错误audience和跨租户读取被拒绝；
- 契约漂移不会静默进入Agent；
- 熔断器能在固定故障模型下减少下游调用并恢复。

这些数据不证明：

- 生产订单分布和真实网络延迟；
- 生产P95/P99、吞吐或可用性SLO；
- MCP自动提供业务权限和多租户安全；
- 真实订单Provider已经接入；
- 服务令牌密钥轮换、mTLS和企业OAuth已经完成；
- MCP让模型推理质量提升。

所有订单和故障均为合成数据。契约漂移和熔断样本使用确定性Gateway；一致性和身份样本使用真实本地Uvicorn进程。

## 6. 复现命令

```powershell
.\.venv\Scripts\python.exe -m evaluation.run_day11_mcp_interoperability `
  --output evaluation/reports/day11_mcp_interoperability_v1.json
```

独立启动MCP服务：

```powershell
$env:MCP_ORDER_FIXTURES_JSON='[]'
.\.venv\Scripts\python.exe -m uvicorn `
  app.mcp.runtime:app `
  --host 127.0.0.1 `
  --port 8011
```

开发/测试运行时可使用JSON订单夹具。生产环境会拒绝夹具Runtime，必须注入真实订单数据源，并使用HTTPS和正式身份基础设施。
