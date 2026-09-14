class ModelProviderError(Exception):
    def __init__(self, error_code: str, *, retryable: bool) -> None:
        self.error_code = error_code
        self.retryable = retryable
        super().__init__(error_code)


class ModelProviderTimeoutError(ModelProviderError):
    def __init__(self) -> None:
        super().__init__("provider_timeout", retryable=True)


class ModelProviderConnectionError(ModelProviderError):
    def __init__(self) -> None:
        super().__init__("provider_connection_error", retryable=True)


class ModelProviderRateLimitError(ModelProviderError):
    def __init__(self) -> None:
        super().__init__("provider_rate_limited", retryable=True)


class ModelProviderServerError(ModelProviderError):
    def __init__(self) -> None:
        super().__init__("provider_server_error", retryable=True)


class ModelProviderAuthenticationError(ModelProviderError):
    def __init__(self) -> None:
        super().__init__("provider_authentication_failed", retryable=False)


class ModelProviderInvalidRequestError(ModelProviderError):
    def __init__(self) -> None:
        super().__init__("provider_invalid_request", retryable=False)


class ModelProviderRefusalError(ModelProviderError):
    def __init__(self) -> None:
        super().__init__("provider_refusal", retryable=False)


class ModelProviderOutputTruncatedError(ModelProviderError):
    def __init__(self) -> None:
        super().__init__("provider_output_truncated", retryable=False)


class ModelGatewayError(Exception):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class ModelOutputValidationError(ModelGatewayError):
    def __init__(self) -> None:
        super().__init__("model_output_invalid")


class ModelProviderUnavailableError(ModelGatewayError):
    pass


class ModelRequestRejectedError(ModelGatewayError):
    pass


class ModelCallRecordingError(ModelGatewayError):
    def __init__(self) -> None:
        super().__init__("model_call_recording_failed")


class ModelConfigurationError(ModelGatewayError):
    pass
