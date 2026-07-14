"""Single typed logging seam with development and production rendering."""

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, NewType, TextIO, assert_never
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from nvidia_build_lb.admin.schemas import LastStatusClass
from nvidia_build_lb.config import DeploymentStage, LogLevel
from nvidia_build_lb.errors import SafeError
from nvidia_build_lb.scheduler_state import TerminalOutcome

RequestId = NewType("RequestId", str)


class LogEventName(StrEnum):
    """Whitelisted stable event names."""

    CONFIGURATION_REJECTED = "configuration.rejected"
    SERVICE_STARTED = "service.started"
    REQUEST_COMPLETED = "request.completed"
    UPSTREAM_STATE_CHANGED = "upstream.state_changed"


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """Logger initialization inputs grouped as one reusable value."""

    name: str
    stage: DeploymentStage
    level: LogLevel
    stream: TextIO


@dataclass(frozen=True, slots=True)
class SafeLogEvent:
    """The complete whitelist of data accepted by the logging boundary."""

    name: LogEventName
    level: LogLevel
    request_id: RequestId | None = None
    internal_key_id: UUID | None = None
    attempt_ordinal: int | None = None
    safe_status_class: LastStatusClass | None = None
    terminal_outcome: TerminalOutcome | None = None
    error: SafeError | None = None


class StructuredLogLine(BaseModel):
    """Machine-parseable production log schema."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    event: LogEventName
    level: LogLevel
    request_id: str | None = None
    internal_key_id: UUID | None = None
    attempt_ordinal: int | None = None
    safe_status_class: LastStatusClass | None = None
    terminal_outcome: TerminalOutcome | None = None
    error_type: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class ServiceLogger:
    """Logger wrapper that accepts only safe typed events."""

    _logger: logging.Logger
    _stage: DeploymentStage

    def emit(self, event: SafeLogEvent) -> None:
        """Emit one event through the configured stage renderer."""
        self._logger.log(_stdlib_level(event.level), _render_event(event, self._stage))


def _stdlib_level(level: LogLevel) -> int:
    match level:
        case LogLevel.DEBUG:
            return logging.DEBUG
        case LogLevel.INFO:
            return logging.INFO
        case LogLevel.WARNING:
            return logging.WARNING
        case LogLevel.ERROR:
            return logging.ERROR
        case _:
            assert_never(level)


def _structured_line(event: SafeLogEvent) -> StructuredLogLine:
    error = event.error
    return StructuredLogLine(
        event=event.name,
        level=event.level,
        request_id=event.request_id,
        internal_key_id=event.internal_key_id,
        attempt_ordinal=event.attempt_ordinal,
        safe_status_class=event.safe_status_class,
        terminal_outcome=event.terminal_outcome,
        error_type=error.error_type if error is not None else None,
        error_code=error.code.value if error is not None else None,
        error_message=error.message if error is not None else None,
    )


def _render_event(event: SafeLogEvent, stage: DeploymentStage) -> str:
    match stage:
        case DeploymentStage.PRODUCTION:
            return _structured_line(event).model_dump_json(exclude_none=True)
        case DeploymentStage.DEVELOPMENT:
            fields = [event.level.value, event.name.value]
            if event.request_id is not None:
                fields.append(f"request_id={event.request_id}")
            if event.internal_key_id is not None:
                fields.append(f"internal_key_id={event.internal_key_id}")
            if event.attempt_ordinal is not None:
                fields.append(f"attempt_ordinal={event.attempt_ordinal}")
            if event.safe_status_class is not None:
                fields.append(f"safe_status_class={event.safe_status_class.value}")
            if event.terminal_outcome is not None:
                fields.append(f"terminal_outcome={event.terminal_outcome.value}")
            if event.error is not None:
                fields.append(f"error_code={event.error.code.value}")
            return " ".join(fields)
        case _:
            assert_never(stage)


def init_logging(config: LoggingConfig) -> ServiceLogger:
    """Initialize exactly one non-propagating service logger."""
    logger = logging.getLogger(config.name)
    logger.handlers.clear()
    logger.setLevel(_stdlib_level(config.level))
    handler = logging.StreamHandler(config.stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return ServiceLogger(_logger=logger, _stage=config.stage)
