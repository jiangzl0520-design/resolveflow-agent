class KnowledgeAnsweringError(Exception):
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


class EvidenceAssessmentContractError(KnowledgeAnsweringError):
    def __init__(self) -> None:
        super().__init__(
            "evidence_assessment_contract_error",
            "Evidence assessment did not match the supplied candidates.",
            retryable=False,
        )


class GroundingVerificationError(KnowledgeAnsweringError):
    def __init__(self) -> None:
        super().__init__(
            "grounding_verification_failed",
            "The generated answer could not be grounded safely.",
            retryable=False,
        )


class KnowledgeAnswerRecordingError(KnowledgeAnsweringError):
    def __init__(self) -> None:
        super().__init__(
            "knowledge_answer_recording_failed",
            "Knowledge answer execution could not be recorded safely.",
            retryable=True,
        )
