class ToolRegistryError(LookupError):
    pass


class ToolNotFoundError(ToolRegistryError):
    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version
        super().__init__(f"Tool {name}@{version} was not found.")


class ToolHandlerError(Exception):
    error_code = "tool_execution_failed"


class ToolResourceNotFoundError(ToolHandlerError):
    error_code = "tool_resource_not_found"


class ToolDependencyUnavailableError(ToolHandlerError):
    error_code = "tool_dependency_unavailable"


class ToolCircuitOpenError(ToolDependencyUnavailableError):
    error_code = "tool_circuit_open"


class ToolRemoteAuthorizationError(ToolHandlerError):
    error_code = "tool_remote_authorization_denied"


class ToolRemoteContractError(ToolHandlerError):
    error_code = "tool_remote_contract_invalid"


class ToolRemoteTimeoutError(ToolHandlerError):
    error_code = "tool_remote_timeout"


class ToolIdempotencyConflictError(ToolHandlerError):
    error_code = "tool_idempotency_conflict"


class ToolExecutionGrantError(ToolHandlerError):
    error_code = "tool_execution_grant_invalid"


class ToolCallRecordingError(RuntimeError):
    pass
