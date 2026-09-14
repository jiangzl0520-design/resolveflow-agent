from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import StorageUnavailableError
from app.db.models import ModelCallRecordModel
from app.domain.model_call import ModelCallRecord, ModelCallStatus


class SqlAlchemyModelCallRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def add(self, record: ModelCallRecord) -> None:
        with self._session_factory() as session:
            session.add(
                ModelCallRecordModel(
                    id=record.id,
                    context_build_id=record.context_build_id,
                    tenant_id=record.tenant_id,
                    operation=record.operation,
                    provider=record.provider,
                    model=record.model,
                    prompt_name=record.prompt_name,
                    prompt_version=record.prompt_version,
                    prompt_hash=record.prompt_hash,
                    response_schema_name=record.response_schema_name,
                    response_schema_hash=record.response_schema_hash,
                    resource_type=record.resource_type,
                    resource_id=record.resource_id,
                    status=record.status.value,
                    attempts=record.attempts,
                    attempt_error_codes=list(
                        record.attempt_error_codes
                    ),
                    latency_ms=record.latency_ms,
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                    provider_request_id=record.provider_request_id,
                    provider_response_id=record.provider_response_id,
                    error_code=record.error_code,
                    request_id=record.request_id,
                    trace_id=record.trace_id,
                    started_at=record.started_at,
                    completed_at=record.completed_at,
                )
            )
            try:
                session.commit()
            except SQLAlchemyError as exc:
                session.rollback()
                raise StorageUnavailableError(
                    "Model call record write failed."
                ) from exc

    def list_for_trace(
        self,
        tenant_id: UUID,
        trace_id: str,
    ) -> list[ModelCallRecord]:
        with self._session_factory() as session:
            try:
                records = session.scalars(
                    select(ModelCallRecordModel)
                    .where(
                        ModelCallRecordModel.tenant_id == tenant_id,
                        ModelCallRecordModel.trace_id == trace_id,
                    )
                    .order_by(
                        ModelCallRecordModel.started_at,
                        ModelCallRecordModel.id,
                    )
                ).all()
            except SQLAlchemyError as exc:
                raise StorageUnavailableError(
                    "Model call record query failed."
                ) from exc
        return [_to_domain(record) for record in records]


def _to_domain(record: ModelCallRecordModel) -> ModelCallRecord:
    return ModelCallRecord(
        id=record.id,
        context_build_id=record.context_build_id,
        tenant_id=record.tenant_id,
        operation=record.operation,
        provider=record.provider,
        model=record.model,
        prompt_name=record.prompt_name,
        prompt_version=record.prompt_version,
        prompt_hash=record.prompt_hash,
        response_schema_name=record.response_schema_name,
        response_schema_hash=record.response_schema_hash,
        resource_type=record.resource_type,
        resource_id=record.resource_id,
        status=ModelCallStatus(record.status),
        attempts=record.attempts,
        attempt_error_codes=tuple(record.attempt_error_codes),
        latency_ms=record.latency_ms,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        provider_request_id=record.provider_request_id,
        provider_response_id=record.provider_response_id,
        error_code=record.error_code,
        request_id=record.request_id,
        trace_id=record.trace_id,
        started_at=_as_utc(record.started_at),
        completed_at=_as_utc(record.completed_at),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
