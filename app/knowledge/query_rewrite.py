import re
from typing import Protocol

from app.domain.retrieval import RewrittenKnowledgeQuery

_ORDER_REFERENCE = re.compile(
    r"(?:订单|工单|order|ticket)\s*[#：:]?\s*[A-Za-z0-9_-]+",
    re.IGNORECASE,
)
_WHITESPACE = re.compile(r"\s+")
_ASCII_TERM = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,31}")
_PUNCTUATION = re.compile(r"[，。！？；、,!?;：:\s]+")

_ALIASES = (
    ("没收到包裹", "未收到货"),
    ("没有收到包裹", "未收到货"),
    ("没有收到货", "未收到货"),
    ("快递没到", "未收到货"),
    ("签收单", "签收证明"),
    ("签收照片", "签收证明"),
    ("退钱", "退款"),
    ("人工客服", "人工审核"),
)
_CONCEPT_TERMS = (
    "未收到货",
    "签收证明",
    "物流",
    "签收",
    "退款",
    "证据冲突",
    "人工审核",
    "取消",
    "未付款",
    "已付款",
    "已发货",
    "有效期",
)
_STOP_CLAUSES = frozenset(
    {"怎么办", "怎么处理", "应该怎么处理", "请问", "需要什么"}
)


class KnowledgeQueryRewriter(Protocol):
    def rewrite(self, text: str) -> RewrittenKnowledgeQuery: ...


class RuleBasedKnowledgeQueryRewriter:
    """Deterministic domain rewrite; never creates security filters."""

    strategy = "after_sales_rules_v1"

    def rewrite(self, text: str) -> RewrittenKnowledgeQuery:
        normalized = _ORDER_REFERENCE.sub(" ", text.strip())
        for source, target in _ALIASES:
            normalized = normalized.replace(source, target)
        normalized = _WHITESPACE.sub(" ", normalized).strip()
        terms: list[str] = []
        for concept in _CONCEPT_TERMS:
            if concept in normalized and concept not in terms:
                terms.append(concept)
        for term in _ASCII_TERM.findall(normalized):
            lowered = term.lower()
            if lowered not in terms:
                terms.append(lowered)
        if not terms:
            for clause in _PUNCTUATION.split(normalized):
                clause = clause.strip()
                if (
                    2 <= len(clause) <= 16
                    and clause not in _STOP_CLAUSES
                    and clause not in terms
                ):
                    terms.append(clause)
        return RewrittenKnowledgeQuery(
            semantic_text=normalized or text.strip(),
            keyword_terms=tuple(terms[:12]),
            strategy=self.strategy,
        )
