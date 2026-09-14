from app.domain.context import (
    ContextDecisionReason,
    ContextFragmentCandidate,
    ContextTrustLevel,
)
from app.security.content import UntrustedContentGuard


class ContextSecurityPolicy:
    def __init__(
        self,
        guard: UntrustedContentGuard | None = None,
    ) -> None:
        self._guard = guard or UntrustedContentGuard()

    def protect(
        self,
        candidate: ContextFragmentCandidate,
    ) -> ContextFragmentCandidate:
        if candidate.trust_level is ContextTrustLevel.SYSTEM:
            return candidate
        protected = self._guard.protect(candidate.content)
        update = {"content": protected.value}
        if (
            candidate.eligible
            and candidate.trust_level
            in {
                ContextTrustLevel.USER_PROVIDED,
                ContextTrustLevel.EXTERNAL_DATA,
            }
            and protected.prompt_injection_detected
        ):
            update.update(
                {
                    "eligible": False,
                    "exclusion_reason": (
                        ContextDecisionReason.SECURITY_POLICY_BLOCKED
                    ),
                }
            )
        return candidate.model_copy(update=update)
