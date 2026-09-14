from typing import Literal

from pydantic import BaseModel

from app.schemas.common import ResponseMeta


class HealthData(BaseModel):
    status: Literal["ok"]
    service: str


class HealthResponse(BaseModel):
    data: HealthData
    meta: ResponseMeta

