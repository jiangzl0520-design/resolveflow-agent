class AgentPlannerError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class AgentRunNotFoundError(LookupError):
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"Agent run {run_id} has no checkpoint.")


class AgentRunAlreadyExistsError(RuntimeError):
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"Agent run {run_id} already has checkpoints.")


class AgentCheckpointCompatibilityError(RuntimeError):
    """Raised when persisted state cannot be safely reconstructed."""


class RefundApprovalRequiredError(RuntimeError):
    """Raised when generic resume is attempted at a refund gate."""


class RefundReviewDeniedError(PermissionError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)
