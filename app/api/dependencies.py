from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.errors import AuthenticationError
from app.core.security import JwtTokenService
from app.domain.auth import AuthenticatedActor
from app.services.authorization_service import AuthorizationService
from app.services.investigation_job_service import InvestigationJobService
from app.services.memory_service import MemoryService
from app.services.ticket_service import TicketService

bearer_scheme = HTTPBearer(auto_error=False)


def get_ticket_service(request: Request) -> TicketService:
    """Resolve the application-scoped ticket service for one request."""
    return request.app.state.ticket_service


def get_request_id(request: Request) -> str:
    """Return the correlation identifier created by HTTP middleware."""
    return request.state.request_id


def get_trace_id(request: Request) -> str:
    return request.state.trace_id


def get_token_service(request: Request) -> JwtTokenService:
    return request.app.state.token_service


def get_authorization_service(request: Request) -> AuthorizationService:
    return request.app.state.authorization_service


def get_investigation_job_service(
    request: Request,
) -> InvestigationJobService:
    return request.app.state.investigation_job_service


def get_memory_service(request: Request) -> MemoryService:
    return request.app.state.memory_service


def get_current_actor(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
    token_service: Annotated[JwtTokenService, Depends(get_token_service)],
) -> AuthenticatedActor:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("A bearer access token is required.")
    return token_service.verify_access_token(credentials.credentials)
