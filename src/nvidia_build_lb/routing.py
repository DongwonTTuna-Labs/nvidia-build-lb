"""Sequential two-key routing with durable terminal-before-failover ordering."""

from contextlib import suppress
from dataclasses import dataclass, replace
from uuid import UUID

import anyio
from anyio.lowlevel import checkpoint_if_cancelled

from nvidia_build_lb.attempt_types import AttemptLease
from nvidia_build_lb.pinned_runtime import PinnedRuntimeDriftError
from nvidia_build_lb.polling import (
    FailureTerminal,
    JsonTerminal,
    ResponseRetirementUnresolvedError,
    StreamTerminal,
)
from nvidia_build_lb.representations import RepresentationProtocolError, json_to_sse_frames
from nvidia_build_lb.routing_failures import (
    aggregate_failures,
    is_aggregate_member,
    protocol_failure,
)
from nvidia_build_lb.routing_limits import MAX_PUBLIC_ATTEMPTS
from nvidia_build_lb.routing_models import (
    RoutedFailure,
    RoutedJson,
    RoutedResult,
    RoutedStream,
    RoutingCoordinatorDependencies,
)
from nvidia_build_lb.routing_observations import RoutedObservations
from nvidia_build_lb.routing_persistence import (
    checkpoint_before_network,
    finalize_cancelled_shielded,
    finalize_failure_shielded,
    finalize_success_shielded,
)
from nvidia_build_lb.routing_reservations import build_start_command, reserve_attempt
from nvidia_build_lb.terminal_retirement_fail_stop import fail_stop_after_terminal
from nvidia_build_lb.transport_common import PinnedTransportDriftError

__all__ = [
    "RoutedFailure",
    "RoutedJson",
    "RoutedResult",
    "RoutedStream",
    "RoutingCoordinator",
    "RoutingCoordinatorDependencies",
]


@dataclass(frozen=True, slots=True)
class RoutingCoordinator:
    """Run at most two public attempts sequentially and never alternate probes."""

    dependencies: RoutingCoordinatorDependencies

    async def execute(
        self,
        *,
        request_id: str,
        body: bytes,
        requested_stream: bool = False,
        explicit_probe_key_id: UUID | None = None,
    ) -> RoutedResult:
        """Reserve, execute, persist, and optionally fail over exactly once."""
        failures: list[FailureTerminal] = []
        observations = RoutedObservations()
        attempt_count = 0
        excluded_key_ids: frozenset[UUID] = frozenset()
        maximum_attempts = 1 if explicit_probe_key_id is not None else MAX_PUBLIC_ATTEMPTS
        for ordinal in range(1, maximum_attempts + 1):
            command = build_start_command(
                self.dependencies,
                request_id,
                explicit_probe_key_id,
                excluded_key_ids,
            )
            reservation = await reserve_attempt(
                self.dependencies,
                command,
                has_failure=bool(failures),
                attempt_count=attempt_count,
            )
            if reservation is None:
                break
            if isinstance(reservation, RoutedFailure):
                return observations.failure(reservation.terminal, reservation.attempt_count)
            lease = reservation
            attempt_count += 1
            started_monotonic = self.dependencies.monotonic_clock.monotonic()
            await checkpoint_before_network(self.dependencies, lease, started_monotonic)
            terminal = await self._execute_attempt(lease, body, started_monotonic)
            handled = await self._handle_terminal(
                terminal,
                lease,
                attempt_count,
                started_monotonic,
                requested_stream,
            )
            try:
                await checkpoint_if_cancelled()
            except anyio.get_cancelled_exc_class():
                if isinstance(handled, RoutedStream):
                    await self.cancel_unhanded_stream(handled)
                raise
            if not isinstance(handled, FailureTerminal):
                return observations.completed(handled, lease, attempt_count)
            observations.record_failure(handled, lease, attempt_count)
            failures.append(handled)
            if ordinal > 1 and not is_aggregate_member(handled):
                return observations.failure(handled, attempt_count)
            if not handled.outcome.alternate_eligible or ordinal == maximum_attempts:
                break
            excluded_key_ids = frozenset({lease.key_id})
        return observations.failure(aggregate_failures(failures), attempt_count)

    async def _handle_terminal(
        self,
        terminal: JsonTerminal | StreamTerminal | FailureTerminal,
        lease: AttemptLease,
        attempt_count: int,
        started_monotonic: float,
        requested_stream: bool,
    ) -> RoutedJson | RoutedStream | FailureTerminal:
        if isinstance(terminal, JsonTerminal):
            return await self._handle_json(
                terminal,
                lease,
                attempt_count,
                started_monotonic,
                requested_stream,
            )
        if isinstance(terminal, StreamTerminal):
            return RoutedStream(terminal, lease, attempt_count, started_monotonic)
        return await self._finalize_failure(lease, terminal, started_monotonic)

    async def _execute_attempt(
        self,
        lease: AttemptLease,
        body: bytes,
        started_monotonic: float,
    ) -> JsonTerminal | StreamTerminal | FailureTerminal:
        """Execute one reserved attempt and close fatal pre-handoff states."""
        try:
            return await self.dependencies.adapter.execute(
                credential=lease.credential,
                body=body,
            )
        except anyio.get_cancelled_exc_class():
            await self._finalize_cancelled(lease, started_monotonic)
            raise
        except (
            PinnedRuntimeDriftError,
            PinnedTransportDriftError,
            ResponseRetirementUnresolvedError,
        ):
            await self._retire_fatal_attempt(lease, started_monotonic)
            raise
        except BaseException:
            await self._retire_fatal_attempt(lease, started_monotonic)
            raise

    async def _handle_json(
        self,
        terminal: JsonTerminal,
        lease: AttemptLease,
        attempt_count: int,
        started_monotonic: float,
        requested_stream: bool,
    ) -> RoutedJson | FailureTerminal:
        stream_frames: tuple[bytes, bytes] | None = None
        if requested_stream:
            try:
                stream_frames = json_to_sse_frames(terminal.representation)
            except RepresentationProtocolError:
                failure = replace(
                    protocol_failure(),
                    retirement_unresolved=terminal.retirement_unresolved,
                )
                return await self._finalize_failure(
                    lease,
                    failure,
                    started_monotonic,
                )
        await self._finalize_success(lease, started_monotonic)
        fail_stop_after_terminal(self.dependencies, terminal)
        return RoutedJson(terminal, lease.key_id, attempt_count, stream_frames)

    async def _finalize_success(self, lease: AttemptLease, started_monotonic: float) -> None:
        await finalize_success_shielded(self.dependencies, lease, started_monotonic)

    async def _finalize_failure(
        self,
        lease: AttemptLease,
        failure: FailureTerminal,
        started_monotonic: float,
    ) -> FailureTerminal:
        committed = await finalize_failure_shielded(
            self.dependencies,
            lease,
            failure,
            started_monotonic,
        )
        fail_stop_after_terminal(self.dependencies, committed)
        return committed

    async def cancel_unhanded_stream(self, routed: RoutedStream) -> None:
        """Persist cancellation and retire a stream that was never handed off."""
        try:
            await self._finalize_cancelled(routed.lease, routed.started_monotonic)
        finally:
            with suppress(BaseException):
                await routed.terminal.stream.aclose()

    async def _finalize_cancelled(
        self,
        lease: AttemptLease,
        started_monotonic: float,
    ) -> None:
        _ = await finalize_cancelled_shielded(
            self.dependencies.attempts,
            lease,
            self.dependencies.clock,
            self.dependencies.monotonic_clock,
            started_monotonic,
        )

    async def _retire_fatal_attempt(
        self,
        lease: AttemptLease,
        started_monotonic: float,
    ) -> None:
        """Close a fatal pre-handoff attempt before forcing the process to stop."""
        with suppress(BaseException):
            await self._finalize_cancelled(lease, started_monotonic)
        self.dependencies.fail_stop.trigger()
