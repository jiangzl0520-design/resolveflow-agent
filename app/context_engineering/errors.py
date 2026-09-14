class ContextEngineeringError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class RequiredContextOverflowError(ContextEngineeringError):
    def __init__(self) -> None:
        super().__init__(
            "required_context_overflow",
            "Required context does not fit inside the configured input budget.",
            retryable=False,
        )


class RequiredContextSecurityError(ContextEngineeringError):
    def __init__(self) -> None:
        super().__init__(
            "required_context_security_blocked",
            "Required untrusted context violated the context security policy.",
            retryable=False,
        )


class ContextTraceRecordingError(ContextEngineeringError):
    def __init__(self) -> None:
        super().__init__(
            "context_trace_recording_failed",
            "The context selection trace could not be recorded safely.",
            retryable=True,
        )
