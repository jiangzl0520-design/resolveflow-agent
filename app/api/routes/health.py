from fastapi import APIRouter, Request

from app.api.dependencies import get_request_id
from app.schemas.common import ResponseMeta
from app.schemas.health import HealthData, HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health_check(request: Request) -> HealthResponse:
    return HealthResponse(
        data=HealthData(status="ok", service="resolveflow-api"),
        meta=ResponseMeta(request_id=get_request_id(request)),
    )

