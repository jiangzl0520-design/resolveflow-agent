class MemoryError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class MemoryIdempotencyConflictError(MemoryError):
    def __init__(self) -> None:
        super().__init__(
            "memory_idempotency_conflict",
            "The idempotency key was already used for another memory request.",
        )


class MemoryStorageError(MemoryError):
    def __init__(self) -> None:
        super().__init__(
            "memory_storage_failed",
            "The long-term memory transaction could not be completed.",
        )


class MemoryReadError(MemoryError):
    def __init__(self) -> None:
        super().__init__(
            "memory_retrieval_failed",
            "Long-term memory could not be read safely.",
        )


class MemoryPolicyValidationError(MemoryError):
    def __init__(self, message: str) -> None:
        super().__init__("memory_value_invalid", message)
