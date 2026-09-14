from dataclasses import dataclass
from datetime import timedelta
from re import fullmatch

from app.domain.memory import MemoryCategory, MemorySourceType


@dataclass(frozen=True, slots=True)
class MemoryKeyPolicy:
    category: MemoryCategory
    allowed_sources: frozenset[MemorySourceType]
    minimum_confidence: float
    maximum_ttl: timedelta


MEMORY_KEY_POLICIES: dict[str, MemoryKeyPolicy] = {
    "preference.language": MemoryKeyPolicy(
        category=MemoryCategory.USER_PREFERENCE,
        allowed_sources=frozenset(
            {
                MemorySourceType.EXPLICIT_USER,
                MemorySourceType.HUMAN_REVIEWER,
            }
        ),
        minimum_confidence=0.9,
        maximum_ttl=timedelta(days=365),
    ),
    "preference.response_style": MemoryKeyPolicy(
        category=MemoryCategory.USER_PREFERENCE,
        allowed_sources=frozenset(
            {
                MemorySourceType.EXPLICIT_USER,
                MemorySourceType.HUMAN_REVIEWER,
            }
        ),
        minimum_confidence=0.9,
        maximum_ttl=timedelta(days=180),
    ),
    "preference.contact_channel": MemoryKeyPolicy(
        category=MemoryCategory.USER_PREFERENCE,
        allowed_sources=frozenset(
            {
                MemorySourceType.EXPLICIT_USER,
                MemorySourceType.HUMAN_REVIEWER,
            }
        ),
        minimum_confidence=0.9,
        maximum_ttl=timedelta(days=90),
    ),
    "profile.locale": MemoryKeyPolicy(
        category=MemoryCategory.CUSTOMER_PROFILE,
        allowed_sources=frozenset(
            {
                MemorySourceType.EXPLICIT_USER,
                MemorySourceType.VERIFIED_SYSTEM,
                MemorySourceType.HUMAN_REVIEWER,
            }
        ),
        minimum_confidence=0.95,
        maximum_ttl=timedelta(days=365),
    ),
}


def normalize_memory_value(memory_key: str, value: str) -> str:
    normalized = " ".join(value.strip().split())
    if memory_key == "preference.language":
        normalized = normalized.lower()
        if not fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?", normalized):
            raise ValueError("Language preference must be a language tag.")
    elif memory_key == "preference.response_style":
        allowed = {"concise", "detailed", "step_by_step"}
        normalized = normalized.lower()
        if normalized not in allowed:
            raise ValueError("Unsupported response style preference.")
    elif memory_key == "preference.contact_channel":
        allowed = {"email", "sms", "phone", "in_app"}
        normalized = normalized.lower()
        if normalized not in allowed:
            raise ValueError("Unsupported contact channel preference.")
    elif memory_key == "profile.locale":
        if not fullmatch(r"[a-z]{2,3}(?:-[A-Z]{2})?", normalized):
            raise ValueError("Locale must be a normalized locale tag.")
    return normalized
