from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Any


class ContentSecurityFlag(StrEnum):
    PROMPT_INJECTION = "prompt_injection"
    SECRET = "secret"
    PII = "pii"


@dataclass(frozen=True, slots=True)
class ProtectedContent:
    value: Any
    flags: frozenset[ContentSecurityFlag]

    @property
    def prompt_injection_detected(self) -> bool:
        return ContentSecurityFlag.PROMPT_INJECTION in self.flags

    @property
    def sensitive_data_detected(self) -> bool:
        return bool(
            self.flags
            & {ContentSecurityFlag.SECRET, ContentSecurityFlag.PII}
        )


_INJECTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.DOTALL)
    for pattern in (
        (
            r"\b(?:ignore|disregard|forget|override|bypass)\b"
            r".{0,120}\b(?:previous|prior|system|developer|safety|policy)\b"
            r".{0,80}\b(?:instruction|prompt|message|rule)s?\b"
        ),
        (
            r"\b(?:reveal|print|repeat|expose|leak|extract)\b"
            r".{0,100}\b(?:system|developer|hidden|secret)\b"
            r".{0,60}\b(?:instruction|prompt|message|token|credential)s?\b"
        ),
        (
            r"\b(?:assistant|agent|model)\b.{0,100}"
            r"\b(?:call|invoke|execute|send|upload|delete|refund)\b"
            r".{0,80}\b(?:tool|function|secret|credential|conversation|data)\b"
        ),
        r"\b(?:system|developer)\s+(?:message|prompt|instruction)\s*[:=]",
        (
            r"(?:忽略|无视|覆盖|忘记|绕过).{0,60}"
            r"(?:之前|以上|系统|开发者|安全|策略).{0,40}"
            r"(?:指令|提示词|消息|规则)"
        ),
        (
            r"(?:泄露|输出|打印|重复|提取).{0,60}"
            r"(?:系统提示词|开发者消息|隐藏指令|密钥|令牌|凭证)"
        ),
        (
            r"(?:让|要求|命令).{0,30}(?:助手|Agent|模型).{0,50}"
            r"(?:调用|执行|上传|发送|删除|退款).{0,30}(?:工具|函数|数据)"
        ),
    )
)

_SECRET_PATTERNS = (
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
        r"-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
        r"[A-Za-z0-9_-]{8,}\b"
    ),
    re.compile(
        r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(password|passwd|api[_-]?key|secret|access[_-]?token)"
        r"\s*[:=]\s*[^\s,;]{6,}",
        re.IGNORECASE,
    ),
)
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@"
    r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])"
)
_CN_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


class UntrustedContentGuard:
    """Detect known instruction attacks and redact common secrets/PII.

    Detection is intentionally not treated as a complete prompt-injection
    defense. Callers must still enforce authorization, tool allowlists and
    business policy outside the model.
    """

    def protect(self, value: Any) -> ProtectedContent:
        protected, flags = self._protect(value)
        return ProtectedContent(protected, frozenset(flags))

    def inspect_text(self, value: str) -> ProtectedContent:
        return self.protect(value)

    def _protect(
        self,
        value: Any,
    ) -> tuple[Any, set[ContentSecurityFlag]]:
        if isinstance(value, str):
            return self._protect_text(value)
        if isinstance(value, dict):
            result: dict[Any, Any] = {}
            flags: set[ContentSecurityFlag] = set()
            for key, item in value.items():
                protected, item_flags = self._protect(item)
                result[key] = protected
                flags.update(item_flags)
            return result, flags
        if isinstance(value, (list, tuple)):
            result: list[Any] = []
            flags: set[ContentSecurityFlag] = set()
            for item in value:
                protected, item_flags = self._protect(item)
                result.append(protected)
                flags.update(item_flags)
            return result, flags
        return value, set()

    @staticmethod
    def _protect_text(
        value: str,
    ) -> tuple[str, set[ContentSecurityFlag]]:
        flags: set[ContentSecurityFlag] = set()
        if any(pattern.search(value) for pattern in _INJECTION_PATTERNS):
            flags.add(ContentSecurityFlag.PROMPT_INJECTION)

        protected = value
        for pattern in _SECRET_PATTERNS:
            replaced, count = pattern.subn("[REDACTED_SECRET]", protected)
            if count:
                flags.add(ContentSecurityFlag.SECRET)
                protected = replaced
        protected, email_count = _EMAIL.subn("[REDACTED_EMAIL]", protected)
        protected, phone_count = _CN_PHONE.subn(
            "[REDACTED_PHONE]",
            protected,
        )
        if email_count or phone_count:
            flags.add(ContentSecurityFlag.PII)
        return protected, flags
