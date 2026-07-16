"""Production explicit-probe execution over the durable routing coordinator."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import anyio
import orjson

from nvidia_build_lb.admin.schemas import LastStatusClass, ProbeStatus, UpstreamProbeResponse
from nvidia_build_lb.api_types import ChatResponderFactory, PublicChatRouter, StreamLogContext
from nvidia_build_lb.routing import RoutedFailure, RoutedJson, RoutedStream

_PROBE_BODY = orjson.dumps(
    {
        "model": "z-ai/glm-5.2",
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_tokens": 1,
        "stream": False,
    }
)


class ProbeResultRepository(Protocol):
    """Project one already-persisted probe status without changing enabled state."""

    async def probe_result(self, key_id: UUID, status: ProbeStatus) -> UpstreamProbeResponse:
        """Return the safe same-key probe response."""
        ...


@dataclass(frozen=True, slots=True)
class RoutingProbeExecutor:
    """Route exactly one explicit key and reduce its terminal to four probe states."""

    router: PublicChatRouter
    upstream: ProbeResultRepository
    responders: ChatResponderFactory | None = None

    async def probe(self, key_id: UUID, request_id: str) -> UpstreamProbeResponse:
        """Run one no-alternate probe whose terminal transition is already durable."""
        routed = await self.router.execute(
            request_id=request_id,
            body=_PROBE_BODY,
            requested_stream=False,
            explicit_probe_key_id=key_id,
        )
        status = await self._status(routed)
        return await self.upstream.probe_result(key_id, status)

    async def _status(self, routed: RoutedJson | RoutedStream | RoutedFailure) -> ProbeStatus:
        if isinstance(routed, RoutedJson):
            return ProbeStatus.VALID
        if isinstance(routed, RoutedStream):
            responders = self.responders
            if responders is None:
                raise RuntimeError

            async def receive() -> dict[str, object]:
                await anyio.sleep_forever()
                raise RuntimeError

            async def send(message: dict[str, object]) -> None:
                del message

            responder = responders.create(
                StreamLogContext(routed.lease.key_id, routed.attempt_count)
            )
            persisted = await responder.run_stream_status(
                routed=routed,
                receive=receive,
                send=send,
            )
            return self._probe_status(persisted)
        return self._probe_status(routed.terminal.outcome.persisted_status)

    @staticmethod
    def _probe_status(persisted: LastStatusClass | None) -> ProbeStatus:
        """Reduce one durably committed safe status to the closed probe projection."""
        if persisted is LastStatusClass.SUCCESS:
            return ProbeStatus.VALID
        if persisted is LastStatusClass.INVALID_CREDENTIAL:
            return ProbeStatus.INVALID_CREDENTIAL
        if persisted is LastStatusClass.RATE_LIMITED:
            return ProbeStatus.RATE_LIMITED
        return ProbeStatus.UPSTREAM_UNAVAILABLE
