from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import StorageUnavailableError
from app.db.models import (
    ContextBuildRunRecordModel,
    ContextFragmentTraceRecordModel,
)
from app.domain.context import ContextBuildRun, ContextFragmentTrace


class SqlAlchemyContextTraceRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def record(
        self,
        run: ContextBuildRun,
        traces: tuple[ContextFragmentTrace, ...],
    ) -> None:
        with self._session_factory() as session:
            session.add(
                ContextBuildRunRecordModel(
                    id=run.id,
                    tenant_id=run.tenant_id,
                    agent_run_id=run.agent_run_id,
                    step_number=run.step_number,
                    model=run.model,
                    status=run.status.value,
                    context_window_tokens=run.context_window_tokens,
                    reserved_output_tokens=run.reserved_output_tokens,
                    reserved_reasoning_tokens=run.reserved_reasoning_tokens,
                    safety_margin_tokens=run.safety_margin_tokens,
                    input_budget_tokens=run.input_budget_tokens,
                    actual_input_tokens=run.actual_input_tokens,
                    included_fragment_count=run.included_fragment_count,
                    dropped_fragment_count=run.dropped_fragment_count,
                    error_code=run.error_code,
                    request_id=run.request_id,
                    trace_id=run.trace_id,
                    created_at=run.created_at,
                )
            )
            session.add_all(
                ContextFragmentTraceRecordModel(
                    build_run_id=trace.build_run_id,
                    fragment_id=trace.fragment_id,
                    semantic_key=trace.semantic_key,
                    source=trace.source.value,
                    trust_level=trace.trust_level.value,
                    content_hash=trace.content_hash,
                    priority=trace.priority,
                    relevance=trace.relevance,
                    ordinal=trace.ordinal,
                    estimated_tokens=trace.estimated_tokens,
                    included=trace.included,
                    decision_reason=trace.decision_reason.value,
                )
                for trace in traces
            )
            try:
                session.commit()
            except SQLAlchemyError as exc:
                session.rollback()
                raise StorageUnavailableError(
                    "Context trace write failed."
                ) from exc
