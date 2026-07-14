"""Request-scoped responder factory with durable stream-terminal logging."""

from dataclasses import dataclass

from nvidia_build_lb.api_types import StreamLogContext
from nvidia_build_lb.attempt_types import AttemptFinalizeCommand, TerminalCommitted
from nvidia_build_lb.config import LogLevel
from nvidia_build_lb.credential_types import Clock
from nvidia_build_lb.logging import LogEventName, RequestId, SafeLogEvent, ServiceLogger
from nvidia_build_lb.streaming import ChatStreamResponder
from nvidia_build_lb.terminal import (
    ChatSupervisor,
    ChatSupervisorDependencies,
    MonotonicClock,
    TerminalAttemptStore,
)


@dataclass(frozen=True, slots=True)
class LoggingTerminalAttemptStore:
    """Log only the safe terminal command after its durable commit succeeds."""

    inner: TerminalAttemptStore
    logger: ServiceLogger
    context: StreamLogContext

    async def finalize_attempt(self, command: AttemptFinalizeCommand) -> TerminalCommitted:
        """Commit exactly once, then emit its closed safe terminal fields."""
        committed = await self.inner.finalize_attempt(command)
        self.logger.emit(
            SafeLogEvent(
                name=LogEventName.REQUEST_COMPLETED,
                level=LogLevel.INFO,
                request_id=RequestId(command.identity.request_id),
                internal_key_id=self.context.key_id,
                attempt_ordinal=self.context.attempt_ordinal,
                safe_status_class=command.status_class,
                terminal_outcome=command.outcome,
            )
        )
        return committed


@dataclass(frozen=True, slots=True)
class LoggedResponderFactory:
    """Create JSON responders or stream supervisors with safe terminal logging."""

    attempts: TerminalAttemptStore
    clock: Clock
    monotonic_clock: MonotonicClock
    logger: ServiceLogger

    def create(self, stream_log: StreamLogContext | None = None) -> ChatStreamResponder:
        """Return one request-scoped responder for the routed representation."""
        if stream_log is None:
            return ChatStreamResponder(None)
        logged_attempts = LoggingTerminalAttemptStore(self.attempts, self.logger, stream_log)
        return ChatStreamResponder(
            ChatSupervisor(
                ChatSupervisorDependencies(
                    attempts=logged_attempts,
                    clock=self.clock,
                    monotonic_clock=self.monotonic_clock,
                )
            )
        )
