import json
from datetime import datetime
from os import environ
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.core.config import AppEnvironment, Settings
from app.mcp.order_server import build_order_mcp_server
from app.tools.after_sales import (
    InMemoryAfterSalesDataSource,
    OrderStatus,
    SimulatedOrder,
)


class DevelopmentOrderFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    order_id: str
    status: OrderStatus
    amount_minor: int
    currency: str
    observed_at: datetime


def create_mcp_app(
    *,
    settings: Settings | None = None,
    source: InMemoryAfterSalesDataSource | None = None,
):
    runtime_settings = settings or Settings.from_env()
    runtime_source = source or _development_source(runtime_settings)
    server = build_order_mcp_server(
        runtime_source,
        runtime_settings,
    )
    return server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        host="127.0.0.1",
    )


def _development_source(
    settings: Settings,
) -> InMemoryAfterSalesDataSource:
    if settings.environment is AppEnvironment.PRODUCTION:
        raise RuntimeError(
            "Production MCP runtime requires an injected real order data source."
        )
    raw = environ.get("MCP_ORDER_FIXTURES_JSON", "[]")
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError("MCP_ORDER_FIXTURES_JSON must contain a JSON array.")
    fixtures = [
        DevelopmentOrderFixture.model_validate(item)
        for item in parsed
    ]
    return InMemoryAfterSalesDataSource(
        orders=[
            SimulatedOrder(
                tenant_id=item.tenant_id,
                order_id=item.order_id,
                status=item.status,
                amount_minor=item.amount_minor,
                currency=item.currency,
                observed_at=item.observed_at,
            )
            for item in fixtures
        ]
    )


app = create_mcp_app()

