from typing import Any

from pydantic import BaseModel, Field


class ResponseMeta(BaseModel):
    request_id: str


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: list[dict[str, Any]] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    error: ErrorDetail
    meta: ResponseMeta

