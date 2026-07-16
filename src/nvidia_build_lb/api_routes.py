"""Public health, model-list, and OpenAI-compatible chat routes."""

import orjson
from fastapi import FastAPI, Request, Response

from nvidia_build_lb.api_errors import model_not_found, routed_failure
from nvidia_build_lb.api_response import RoutedChatResponse
from nvidia_build_lb.api_types import ApplicationServices, StreamLogContext
from nvidia_build_lb.config import NVIDIA_MODEL, LogLevel
from nvidia_build_lb.logging import LogEventName, RequestId, SafeLogEvent
from nvidia_build_lb.request_id import request_id_from
from nvidia_build_lb.routing import (
    RoutedFailure,
    RoutedJson,
    RoutedStream,
)
from nvidia_build_lb.routing_models import RoutedAttemptObservation
from nvidia_build_lb.scheduler_state import TerminalOutcome
from nvidia_build_lb.schemas import ChatCompletionsRequest, ModelListResponse


def register_public_routes(app: FastAPI, services: ApplicationServices) -> None:
    """Register the complete downstream API on one application."""

    @app.get("/health", response_class=Response)
    async def _health() -> Response:
        ready = await services.readiness.is_ready()
        return Response(
            status_code=200 if ready else 503,
            content=orjson.dumps({"status": "ok" if ready else "degraded", "ready": ready}),
            media_type="application/json",
        )

    @app.get("/v1/models", response_class=Response)
    async def _models() -> Response:
        return Response(
            content=ModelListResponse.fixed_model().model_dump_json(),
            media_type="application/json",
        )

    @app.post("/v1/chat/completions", response_class=Response)
    async def _chat(request: Request, payload: ChatCompletionsRequest) -> Response:
        request_id = request_id_from(request)
        if payload.model != NVIDIA_MODEL:
            return model_not_found(request_id)
        body = orjson.dumps(payload.model_dump(mode="json", exclude_unset=True))
        with services.active_requests.track(request_id):
            routed = await services.routing.execute(
                request_id=request_id,
                body=body,
                requested_stream=payload.stream,
            )
            _log_completed_attempts(services, request_id, routed)
            if isinstance(routed, RoutedFailure):
                return routed_failure(routed.terminal, request_id)
            stream_log = (
                StreamLogContext(routed.lease.key_id, routed.attempt_count)
                if isinstance(routed, RoutedStream)
                else None
            )
            return RoutedChatResponse(
                routed,
                services.responders.create(stream_log),
                requested_stream=payload.stream,
            )

    _ = (_health, _models, _chat)


def _log_completed_attempts(
    services: ApplicationServices,
    request_id: str,
    routed: RoutedJson | RoutedStream | RoutedFailure,
) -> None:
    observations = routed.attempt_observations
    if observations:
        for observation in observations:
            _log_attempt(services, request_id, observation)
        return
    if isinstance(routed, RoutedFailure):
        services.logger.emit(
            SafeLogEvent(
                name=LogEventName.REQUEST_COMPLETED,
                level=LogLevel.INFO,
                request_id=RequestId(request_id),
                attempt_ordinal=routed.attempt_count,
                safe_status_class=routed.terminal.outcome.persisted_status,
                terminal_outcome=TerminalOutcome.FAILED,
            )
        )


def _log_attempt(
    services: ApplicationServices,
    request_id: str,
    observation: RoutedAttemptObservation,
) -> None:
    services.logger.emit(
        SafeLogEvent(
            name=LogEventName.REQUEST_COMPLETED,
            level=LogLevel.INFO,
            request_id=RequestId(request_id),
            internal_key_id=observation.key_id,
            attempt_ordinal=observation.attempt_ordinal,
            safe_status_class=observation.safe_status_class,
            terminal_outcome=observation.terminal_outcome,
        )
    )
