from fastapi import APIRouter, Response

from app.observability.metrics import get_metrics


router = APIRouter(tags=["observability"])


@router.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    body, content_type = get_metrics().render()
    return Response(content=body, media_type=content_type)
