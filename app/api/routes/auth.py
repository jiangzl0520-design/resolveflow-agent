from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from app.api.dependencies import (
    get_authorization_service,
    get_current_actor,
    get_request_id,
    get_token_service,
)
from app.core.security import JwtTokenService
from app.domain.auth import AuthenticatedActor
from app.schemas.auth import (
    AccessTokenResponse,
    AuthorizationAuditListResponse,
    AuthorizationAuditRead,
    DevelopmentTokenRequest,
)
from app.schemas.common import ResponseMeta
from app.services.authorization_service import AuthorizationService

development_router = APIRouter(
    prefix="/auth",
    tags=["development-authentication"],
)
authorization_router = APIRouter(
    prefix="/authorization",
    tags=["authorization"],
)

TokenServiceDependency = Annotated[
    JwtTokenService,
    Depends(get_token_service),
]
ActorDependency = Annotated[
    AuthenticatedActor,
    Depends(get_current_actor),
]
AuthorizationServiceDependency = Annotated[
    AuthorizationService,
    Depends(get_authorization_service),
]


@development_router.post(
    "/development-token",
    response_model=AccessTokenResponse,
)
def issue_development_token(
    payload: DevelopmentTokenRequest,
    response: Response,
    token_service: TokenServiceDependency,
) -> AccessTokenResponse:
    response.headers["Cache-Control"] = "no-store"
    return AccessTokenResponse(
        access_token=token_service.issue_access_token(
            actor_id=payload.actor_id,
            tenant_id=payload.tenant_id,
            roles=frozenset(payload.roles),
        ),
        expires_in=token_service.access_token_expires_in_seconds,
    )


@authorization_router.get(
    "/audit-events",
    response_model=AuthorizationAuditListResponse,
)
def list_authorization_audit_events(
    request: Request,
    actor: ActorDependency,
    service: AuthorizationServiceDependency,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> AuthorizationAuditListResponse:
    events = service.list_audit_events(
        actor,
        request_id=get_request_id(request),
        limit=limit,
    )
    return AuthorizationAuditListResponse(
        data=[
            AuthorizationAuditRead.model_validate(event)
            for event in events
        ],
        meta=ResponseMeta(request_id=get_request_id(request)),
    )
