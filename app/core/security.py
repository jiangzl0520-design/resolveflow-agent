from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
from jwt.exceptions import InvalidTokenError

from app.core.config import Settings
from app.core.errors import AuthenticationError
from app.domain.auth import AuthenticatedActor, Role

JWT_ALGORITHM = "HS256"


class JwtTokenService:
    """Issue local development tokens and verify trusted bearer JWTs."""

    def __init__(self, settings: Settings) -> None:
        self._secret = settings.jwt_secret
        self._issuer = settings.jwt_issuer
        self._audience = settings.jwt_audience
        self._access_token_lifetime = timedelta(
            minutes=settings.access_token_expire_minutes
        )

    def issue_access_token(
        self,
        *,
        actor_id: str,
        tenant_id: UUID,
        roles: frozenset[Role],
        now: datetime | None = None,
        expires_delta: timedelta | None = None,
    ) -> str:
        if not 1 <= len(actor_id) <= 128:
            raise ValueError("actor_id must contain 1 to 128 characters.")
        if tenant_id.int == 0:
            raise ValueError("The legacy quarantine tenant cannot authenticate.")
        if not roles:
            raise ValueError("At least one role is required.")
        issued_at = now or datetime.now(UTC)
        expires_at = issued_at + (
            expires_delta
            if expires_delta is not None
            else self._access_token_lifetime
        )
        claims = {
            "iss": self._issuer,
            "aud": self._audience,
            "sub": actor_id,
            "tenant_id": str(tenant_id),
            "roles": sorted(role.value for role in roles),
            "iat": issued_at,
            "nbf": issued_at,
            "exp": expires_at,
            "jti": str(uuid4()),
        }
        return jwt.encode(
            claims,
            self._secret,
            algorithm=JWT_ALGORITHM,
        )

    @property
    def access_token_expires_in_seconds(self) -> int:
        return int(self._access_token_lifetime.total_seconds())

    def verify_access_token(self, token: str) -> AuthenticatedActor:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[JWT_ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                options={
                    "require": [
                        "iss",
                        "aud",
                        "sub",
                        "tenant_id",
                        "roles",
                        "iat",
                        "nbf",
                        "exp",
                        "jti",
                    ]
                },
            )
            actor_id = claims["sub"]
            raw_roles = claims["roles"]
            if not isinstance(actor_id, str) or not 1 <= len(actor_id) <= 128:
                raise ValueError("Invalid subject claim.")
            if (
                not isinstance(raw_roles, list)
                or not raw_roles
                or not all(isinstance(role, str) for role in raw_roles)
            ):
                raise ValueError("Invalid roles claim.")
            roles = frozenset(Role(role) for role in raw_roles)
            tenant_id = UUID(str(claims["tenant_id"]))
            if tenant_id.int == 0:
                raise ValueError("Invalid tenant claim.")
        except (InvalidTokenError, KeyError, TypeError, ValueError) as exc:
            raise AuthenticationError(
                "Bearer token could not be validated."
            ) from exc

        return AuthenticatedActor(
            actor_id=actor_id,
            tenant_id=tenant_id,
            roles=roles,
        )
