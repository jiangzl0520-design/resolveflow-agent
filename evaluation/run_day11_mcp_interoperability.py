from argparse import ArgumentParser
from contextlib import contextmanager
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
from time import monotonic, sleep
from uuid import UUID

from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, Tool, ToolAnnotations

from app.core.config import AppEnvironment, Settings
from app.core.security import JwtTokenService
from app.domain.auth import AuthenticatedActor, Permission, Role
from app.mcp.client import (
    CircuitBreaker,
    MCPOrderToolAdapter,
    StreamableHttpMCPGateway,
)
from app.mcp.contracts import (
    MCPToolContractError,
    ORDER_TOOL_NAME,
    order_tool_metadata,
)
from app.mcp.security import MCPServiceTokenIssuer
from app.tools.after_sales import (
    InMemoryAfterSalesDataSource,
    OrderLookupArguments,
    OrderObservation,
    OrderStatus,
    SimulatedOrder,
    build_after_sales_tools,
)
from app.tools.contracts import ToolCallRequest, ToolExecutionContext
from app.tools.executor import InMemoryToolCallRecorder, ToolExecutor
from app.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = (
    ROOT
    / "evaluation"
    / "datasets"
    / "day11_mcp_interoperability_v1.json"
)
TENANT_ID = UUID("e1000000-0000-0000-0000-000000000001")
OTHER_TENANT_ID = UUID("e1000000-0000-0000-0000-000000000002")
OBSERVED_AT = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
MCP_SECRET = "day11-evaluation-mcp-secret-longer-than-thirty-two"
APP_SECRET = "day11-evaluation-app-secret-longer-than-thirty-two"


class StaticGateway:
    def __init__(self, tool: Tool) -> None:
        self.tool = tool

    def list_tools(self, token: str) -> list[Tool]:
        return [self.tool]

    def call_tool(self, token: str, *, name: str, arguments: dict):
        raise AssertionError("A drifted tool must never become callable.")


class FailingGateway:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def list_tools(self, token: str) -> list[Tool]:
        return [_valid_tool()]

    def call_tool(
        self,
        token: str,
        *,
        name: str,
        arguments: dict,
    ) -> CallToolResult:
        self.calls += 1
        if self.calls <= self.failures:
            raise MCPError(
                -32042,
                "dependency unavailable",
                {
                    "resolveflow_code": "tool_dependency_unavailable",
                    "retryable": True,
                },
            )
        return CallToolResult(
            content=[],
            structuredContent=_remote_output("EVAL-000"),
        )


def _settings(url: str) -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        environment=AppEnvironment.TEST,
        jwt_secret=APP_SECRET,
        mcp_jwt_secret=MCP_SECRET,
        mcp_order_server_url=url,
        mcp_timeout_seconds=3,
    )


def _context(
    *,
    tenant_id: UUID = TENANT_ID,
    case_id: str = "default",
) -> ToolExecutionContext:
    return ToolExecutionContext(
        actor=AuthenticatedActor(
            actor_id="evaluation-agent",
            tenant_id=tenant_id,
            roles=frozenset({Role.AGENT}),
        ),
        request_id=f"request-{case_id}",
        trace_id=f"trace-{case_id}",
        agent_run_id=f"run-{case_id}",
        agent_step_id=f"step-{case_id}",
    )


def _orders(count: int) -> list[SimulatedOrder]:
    statuses = list(OrderStatus)
    return [
        SimulatedOrder(
            tenant_id=TENANT_ID,
            order_id=f"EVAL-{index:03d}",
            status=statuses[index % len(statuses)],
            amount_minor=1000 + index * 137,
            currency="CNY",
            observed_at=OBSERVED_AT,
        )
        for index in range(count)
    ]


def _fixtures(orders: list[SimulatedOrder]) -> list[dict]:
    return [
        {
            "tenant_id": str(item.tenant_id),
            "order_id": item.order_id,
            "status": item.status.value,
            "amount_minor": item.amount_minor,
            "currency": item.currency,
            "observed_at": item.observed_at.isoformat(),
        }
        for item in orders
    ]


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextmanager
def _mcp_process(orders: list[SimulatedOrder]):
    port = _free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    env = os.environ.copy()
    env.update(
        {
            "APP_ENV": "test",
            "MCP_JWT_SECRET": MCP_SECRET,
            "MCP_ORDER_SERVER_URL": url,
            "MCP_ORDER_FIXTURES_JSON": json.dumps(_fixtures(orders)),
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.mcp.runtime:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = monotonic() + 15
        while monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("MCP evaluation server exited.")
            try:
                with socket.create_connection(
                    ("127.0.0.1", port),
                    timeout=0.1,
                ):
                    break
            except OSError:
                sleep(0.05)
        else:
            raise TimeoutError("MCP evaluation server was not ready.")
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _execute(
    executor: ToolExecutor,
    *,
    order_id: str,
    context: ToolExecutionContext,
):
    return executor.execute(
        ToolCallRequest(
            tool_name=ORDER_TOOL_NAME,
            tool_version="1.0.0",
            arguments={"order_id": order_id},
            context=context,
        )
    )


def _valid_tool() -> Tool:
    return Tool(
        name=ORDER_TOOL_NAME,
        description="Synthetic remote order lookup.",
        inputSchema=OrderLookupArguments.model_json_schema(),
        outputSchema=OrderObservation.model_json_schema(),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        _meta=order_tool_metadata(),
    )


def _remote_output(order_id: str) -> dict:
    return {
        "order_id": order_id,
        "status": "shipped",
        "paid": True,
        "shipped": True,
        "amount_minor": 1000,
        "currency": "CNY",
        "observed_at": OBSERVED_AT.isoformat(),
    }


def _parity_and_authorization(
    parity_count: int,
    authorization_count: int,
) -> dict[str, int]:
    orders = _orders(parity_count)
    parity_matches = 0
    unauthorized_successes = 0
    cross_tenant_cases = authorization_count // 2
    invalid_token_cases = authorization_count - cross_tenant_cases
    with _mcp_process(orders) as url:
        runtime_settings = _settings(url)
        local_executor = ToolExecutor(
            ToolRegistry(
                build_after_sales_tools(
                    InMemoryAfterSalesDataSource(orders=orders)
                )
            ),
            InMemoryToolCallRecorder(),
        )
        adapter = MCPOrderToolAdapter.from_settings(runtime_settings)
        remote_executor = ToolExecutor(
            ToolRegistry([adapter.discover(_context())]),
            InMemoryToolCallRecorder(),
        )
        try:
            for index, order in enumerate(orders):
                context = _context(case_id=f"parity-{index}")
                local = _execute(
                    local_executor,
                    order_id=order.order_id,
                    context=context,
                )
                remote = _execute(
                    remote_executor,
                    order_id=order.order_id,
                    context=context,
                )
                parity_matches += int(
                    local.succeeded
                    and remote.succeeded
                    and local.output is not None
                    and remote.output is not None
                    and local.output.model_dump()
                    == remote.output.model_dump()
                )

            for index in range(cross_tenant_cases):
                result = _execute(
                    remote_executor,
                    order_id=orders[index].order_id,
                    context=_context(
                        tenant_id=OTHER_TENANT_ID,
                        case_id=f"cross-tenant-{index}",
                    ),
                )
                unauthorized_successes += int(result.succeeded)

            gateway = StreamableHttpMCPGateway(
                url,
                timeout_seconds=3,
            )
            app_token = JwtTokenService(
                runtime_settings
            ).issue_access_token(
                actor_id="evaluation-agent",
                tenant_id=TENANT_ID,
                roles=frozenset({Role.AGENT}),
            )
            wrong_audience_token = MCPServiceTokenIssuer(
                runtime_settings,
                audience="http://127.0.0.1:9/mcp",
            ).issue(
                _context(),
                permissions=frozenset({Permission.TOOL_ORDER_READ}),
            )
            for index in range(invalid_token_cases):
                token = (
                    app_token
                    if index % 2 == 0
                    else wrong_audience_token
                )
                try:
                    gateway.list_tools(token)
                except Exception:
                    pass
                else:
                    unauthorized_successes += 1
        finally:
            local_executor.close()
            remote_executor.close()

    return {
        "parity_matches": parity_matches,
        "unauthorized_successes": unauthorized_successes,
        "cross_tenant_cases": cross_tenant_cases,
        "invalid_token_cases": invalid_token_cases,
    }


def _contract_drift_cases(count: int, url: str) -> int:
    detected = 0
    runtime_settings = _settings(url)
    for index in range(count):
        tool = _valid_tool()
        if index % 2 == 0:
            assert tool.meta is not None
            tool.meta["resolveflow/toolVersion"] = "2.0.0"
        else:
            tool.output_schema = {"type": "object"}
        adapter = MCPOrderToolAdapter(
            StaticGateway(tool),
            MCPServiceTokenIssuer(runtime_settings),
            timeout_seconds=1,
            circuit_breaker=CircuitBreaker(
                failure_threshold=3,
                recovery_seconds=5,
            ),
        )
        try:
            adapter.discover(_context(case_id=f"drift-{index}"))
        except MCPToolContractError:
            detected += 1
    return detected


def _circuit_cases(
    count: int,
    threshold: int,
    recovery_seconds: float,
    url: str,
) -> dict[str, int | bool]:
    now = [100.0]
    gateway = FailingGateway(threshold)
    breaker = CircuitBreaker(
        failure_threshold=threshold,
        recovery_seconds=recovery_seconds,
        clock=lambda: now[0],
    )
    adapter = MCPOrderToolAdapter(
        gateway,
        MCPServiceTokenIssuer(_settings(url)),
        timeout_seconds=1,
        circuit_breaker=breaker,
    )
    executor = ToolExecutor(
        ToolRegistry([adapter.discover(_context())]),
        InMemoryToolCallRecorder(),
    )
    circuit_open_results = 0
    try:
        for index in range(count):
            result = _execute(
                executor,
                order_id="EVAL-000",
                context=_context(case_id=f"outage-{index}"),
            )
            circuit_open_results += int(
                result.error is not None
                and result.error.code == "tool_circuit_open"
            )
        remote_calls_during_outage = gateway.calls
        now[0] += recovery_seconds
        recovered = _execute(
            executor,
            order_id="EVAL-000",
            context=_context(case_id="recovery-probe"),
        ).succeeded
    finally:
        executor.close()
    return {
        "remote_calls_during_outage": remote_calls_during_outage,
        "circuit_open_results": circuit_open_results,
        "downstream_calls_avoided": (
            count - remote_calls_during_outage
        ),
        "recovery_probe_succeeded": recovered,
    }


def run(dataset_path: Path) -> dict[str, object]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    counts = dataset["scenario_counts"]
    parity_auth = _parity_and_authorization(
        counts["local_remote_parity"],
        counts["unauthorized_or_cross_tenant"],
    )
    placeholder_url = "http://127.0.0.1:18011/mcp"
    drift_detected = _contract_drift_cases(
        counts["contract_drift"],
        placeholder_url,
    )
    circuit = _circuit_cases(
        counts["dependency_outage"],
        dataset["circuit_failure_threshold"],
        dataset["circuit_recovery_seconds"],
        placeholder_url,
    )
    case_count = sum(counts.values())
    parity_count = counts["local_remote_parity"]
    auth_count = counts["unauthorized_or_cross_tenant"]
    drift_count = counts["contract_drift"]
    return {
        "report_id": "day11-mcp-interoperability-v1",
        "dataset": {**dataset, "case_count": case_count},
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "mcp_sdk": "2.0.0",
            "transport": "Streamable HTTP for parity/auth cases",
            "paid_api_calls": 0,
        },
        "interoperability": {
            "local_remote_parity_matches": parity_auth[
                "parity_matches"
            ],
            "parity_cases": parity_count,
            "parity_percent": round(
                parity_auth["parity_matches"] / parity_count * 100,
                2,
            ),
        },
        "authorization_and_tenant_isolation": {
            "cases": auth_count,
            "cross_tenant_cases": parity_auth["cross_tenant_cases"],
            "invalid_token_cases": parity_auth["invalid_token_cases"],
            "unauthorized_successes": parity_auth[
                "unauthorized_successes"
            ],
        },
        "contract_drift": {
            "cases": drift_count,
            "detected_before_agent_exposure": drift_detected,
            "detection_percent": round(
                drift_detected / drift_count * 100,
                2,
            ),
        },
        "dependency_outage": {
            "requests": counts["dependency_outage"],
            "failure_threshold": dataset[
                "circuit_failure_threshold"
            ],
            **circuit,
        },
        "measured_value": {
            "tool_outcome_parity_percent": round(
                parity_auth["parity_matches"] / parity_count * 100,
                2,
            ),
            "unauthorized_successful_calls": parity_auth[
                "unauthorized_successes"
            ],
            "contract_drifts_blocked": drift_detected,
            "outage_downstream_calls_avoided": circuit[
                "downstream_calls_avoided"
            ],
        },
        "interpretation_limits": [
            "Orders and outage cases are synthetic and deterministic.",
            "Parity/auth cases use a real local Uvicorn process and MCP Streamable HTTP; contract drift and circuit cases use deterministic gateways.",
            "MCP demonstrates tool interoperability and process isolation, not business authorization by itself.",
            "No production traffic, latency SLO, revenue, or model-quality improvement is claimed.",
        ],
    }


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DEFAULT_DATASET,
    )
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    report = run(arguments.dataset)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            serialized + "\n",
            encoding="utf-8",
        )
    print(serialized)


if __name__ == "__main__":
    main()

