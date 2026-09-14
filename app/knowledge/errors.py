class KnowledgeIngestionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class KnowledgeParseError(KnowledgeIngestionError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, retryable=False)


class KnowledgeVersionConflictError(KnowledgeIngestionError):
    def __init__(self) -> None:
        super().__init__(
            "knowledge_version_content_conflict",
            "The same source version already has different content.",
            retryable=False,
        )


class KnowledgeIdempotencyConflictError(KnowledgeIngestionError):
    def __init__(self) -> None:
        super().__init__(
            "knowledge_idempotency_conflict",
            "The idempotency key was reused with different input.",
            retryable=False,
        )


class KnowledgeRunInProgressError(KnowledgeIngestionError):
    def __init__(self) -> None:
        super().__init__(
            "knowledge_ingestion_in_progress",
            "The ingestion run is already in progress.",
            retryable=True,
        )


class KnowledgeRetryBudgetExceededError(KnowledgeIngestionError):
    def __init__(self) -> None:
        super().__init__(
            "knowledge_retry_budget_exhausted",
            "The ingestion retry budget has been exhausted.",
            retryable=False,
        )

