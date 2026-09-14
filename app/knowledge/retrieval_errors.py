class KnowledgeRetrievalError(Exception):
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


class KnowledgeEmbeddingConfigurationError(KnowledgeRetrievalError):
    def __init__(self, code: str = "knowledge_embedding_not_configured"):
        super().__init__(
            code,
            "Knowledge embedding provider is not configured.",
            retryable=False,
        )


class KnowledgeEmbeddingProviderError(KnowledgeRetrievalError):
    pass


class KnowledgeEmbeddingContractError(KnowledgeRetrievalError):
    def __init__(self) -> None:
        super().__init__(
            "knowledge_embedding_contract_error",
            "Knowledge embedding output violated its contract.",
            retryable=False,
        )


class KnowledgeSearchStorageError(KnowledgeRetrievalError):
    def __init__(self) -> None:
        super().__init__(
            "knowledge_search_storage_unavailable",
            "Knowledge search storage is unavailable.",
            retryable=True,
        )


class KnowledgeRetrievalRecordingError(KnowledgeRetrievalError):
    def __init__(self) -> None:
        super().__init__(
            "knowledge_retrieval_recording_failed",
            "Knowledge retrieval could not be recorded safely.",
            retryable=True,
        )

