"""Select and revalidate one query-bounded, exact-group maintenance batch."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import String, case, func, literal, or_, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession

from nvidia_build_lb.admin_ledger import AdminLedger, LedgerCounts
from nvidia_build_lb.admin_maintenance_coherence import (
    ATTEMPT_EVENT_TYPES,
    candidate_matches_rows,
)
from nvidia_build_lb.admin_maintenance_types import (
    AttemptCandidate,
    EventCandidate,
    MaintenanceBatch,
    MaintenanceGroup,
)
from nvidia_build_lb.db_models import (
    AdminEventRow,
    UpstreamAttemptReceiptRow,
    UpstreamLivePinRow,
)

_GROUP_ROW = TypeAdapter(tuple[str, str, datetime, int])


@dataclass(frozen=True, slots=True)
class MaintenanceCandidateSelector:
    """Select multiple oldest whole groups without an unbounded row scan or N+1."""

    ledger: AdminLedger

    async def batch(
        self,
        session: AsyncSession,
        protected: frozenset[UUID],
        counts: LedgerCounts,
        now: datetime,
    ) -> MaintenanceBatch:
        """Fill at most the configured row budget with complete safe candidates."""
        attempts = await self._attempts(session, protected, counts, now)
        remaining = self.ledger.policy.prune_batch_size - sum(
            candidate.row_cost for candidate in attempts
        )
        events = await self._events(session, protected, counts, now, remaining)
        return MaintenanceBatch(attempts=attempts, events=events)

    async def _attempts(
        self,
        session: AsyncSession,
        protected: frozenset[UUID],
        counts: LedgerCounts,
        now: datetime,
    ) -> tuple[AttemptCandidate, ...]:
        budget = self.ledger.policy.prune_batch_size
        routed = UpstreamAttemptReceiptRow.explicit_probe_key_id.is_(None)
        kind = case((routed, literal("routed")), else_=literal("probe"))
        identity = case(
            (routed, UpstreamAttemptReceiptRow.request_id),
            else_=sql_cast(UpstreamAttemptReceiptRow.started_event_id, String),
        )
        cutoff = (
            now - self.ledger.policy.reconciliation_grace
            if not counts.attempt_admission_available(self.ledger.policy)
            else now - self.ledger.policy.retention_delta
        )
        group_result = await session.execute(
            select(
                kind,
                identity,
                func.max(UpstreamAttemptReceiptRow.terminal_committed_at),
                func.count(),
            )
            .group_by(kind, identity)
            .having(
                func.count().filter(UpstreamAttemptReceiptRow.terminal_committed_at.is_(None)) == 0,
                func.count() * 3 <= budget,
                func.max(UpstreamAttemptReceiptRow.terminal_committed_at) <= cutoff,
            )
            .order_by(
                func.max(UpstreamAttemptReceiptRow.terminal_committed_at).asc(),
                identity.asc(),
            )
            .limit(max(1, budget // 3))
        )
        groups = tuple(
            MaintenanceGroup(kind_value, identity_value, latest, count)
            for row in group_result.all()
            for kind_value, identity_value, latest, count in (_GROUP_ROW.validate_python(row),)
        )
        selected: list[MaintenanceGroup] = []
        cost = 0
        for group in groups:
            group_cost = group.receipt_count * 3
            if cost + group_cost <= budget and not (
                group.kind == "routed" and self.ledger.active_requests.contains(group.identity)
            ):
                selected.append(group)
                cost += group_cost
        candidates = await self._materialize(session, tuple(selected))
        return await self.safe_attempts(session, candidates, protected, now)

    async def _materialize(
        self,
        session: AsyncSession,
        groups: tuple[MaintenanceGroup, ...],
    ) -> tuple[AttemptCandidate, ...]:
        routed_ids = tuple(group.identity for group in groups if group.kind == "routed")
        probe_ids = tuple(UUID(group.identity) for group in groups if group.kind == "probe")
        routed_condition = UpstreamAttemptReceiptRow.explicit_probe_key_id.is_(
            None
        ) & UpstreamAttemptReceiptRow.request_id.in_(routed_ids)
        probe_condition = UpstreamAttemptReceiptRow.started_event_id.in_(probe_ids)
        if routed_ids and probe_ids:
            condition = or_(routed_condition, probe_condition)
        elif routed_ids:
            condition = routed_condition
        elif probe_ids:
            condition = probe_condition
        else:
            return ()
        rows = tuple(
            (await session.scalars(select(UpstreamAttemptReceiptRow).where(condition))).all()
        )
        grouped: dict[tuple[str, str], list[UpstreamAttemptReceiptRow]] = defaultdict(list)
        for row in rows:
            key = (
                ("routed", row.request_id)
                if row.explicit_probe_key_id is None
                else ("probe", str(row.started_event_id))
            )
            grouped[key].append(row)
        candidates: list[AttemptCandidate] = []
        for group in groups:
            receipts = grouped[(group.kind, group.identity)]
            if len(receipts) != group.receipt_count:
                continue
            candidates.append(
                AttemptCandidate(
                    request_id=receipts[0].request_id,
                    receipt_ids=tuple(sorted((row.started_event_id for row in receipts), key=str)),
                    start_event_ids=tuple(
                        sorted((row.started_event_id for row in receipts), key=str)
                    ),
                    terminal_event_ids=tuple(
                        sorted((row.terminal_event_id for row in receipts), key=str)
                    ),
                    latest_terminal_at=group.latest_terminal_at,
                    routed=group.kind == "routed",
                )
            )
        return tuple(candidates)

    async def safe_attempts(
        self,
        session: AsyncSession,
        candidates: tuple[AttemptCandidate, ...],
        protected: frozenset[UUID],
        now: datetime,
    ) -> tuple[AttemptCandidate, ...]:
        """Return candidates still exact, unpinned, old enough, and inactive."""
        receipt_ids = tuple(identity for item in candidates for identity in item.receipt_ids)
        request_ids = tuple({item.request_id for item in candidates})
        if not receipt_ids:
            return ()
        receipts = tuple(
            (
                await session.scalars(
                    select(UpstreamAttemptReceiptRow).where(
                        UpstreamAttemptReceiptRow.started_event_id.in_(receipt_ids)
                    )
                )
            ).all()
        )
        events = tuple(
            (
                await session.scalars(
                    select(AdminEventRow).where(
                        AdminEventRow.request_id.in_(request_ids),
                        AdminEventRow.event_type.in_(ATTEMPT_EVENT_TYPES),
                    )
                )
            ).all()
        )
        pins = frozenset(
            (
                await session.scalars(
                    select(UpstreamLivePinRow.started_event_id).where(
                        UpstreamLivePinRow.started_event_id.in_(receipt_ids)
                    )
                )
            ).all()
        )
        grace = now - self.ledger.policy.reconciliation_grace
        safe: list[AttemptCandidate] = []
        for candidate in candidates:
            candidate_receipts = tuple(
                row for row in receipts if row.started_event_id in candidate.receipt_ids
            )
            candidate_events = tuple(
                row for row in events if row.request_id == candidate.request_id
            )
            if (
                not protected.intersection(candidate.event_ids)
                and not pins.intersection(candidate.receipt_ids)
                and candidate.latest_terminal_at <= grace
                and not (
                    candidate.routed and self.ledger.active_requests.contains(candidate.request_id)
                )
                and candidate_matches_rows(candidate, candidate_receipts, candidate_events)
            ):
                safe.append(candidate)
        return tuple(safe)

    async def _events(
        self,
        session: AsyncSession,
        protected: frozenset[UUID],
        counts: LedgerCounts,
        now: datetime,
        limit: int,
    ) -> tuple[EventCandidate, ...]:
        if limit <= 0:
            return ()
        statement = select(AdminEventRow.id).where(
            AdminEventRow.event_type.not_in(ATTEMPT_EVENT_TYPES),
            AdminEventRow.id.not_in(protected),
        )
        if counts.attempt_admission_available(self.ledger.policy):
            statement = statement.where(
                AdminEventRow.occurred_at <= now - self.ledger.policy.retention_delta
            )
        identities = tuple(
            (
                await session.scalars(
                    statement.order_by(AdminEventRow.occurred_at, AdminEventRow.id).limit(limit)
                )
            ).all()
        )
        return tuple(EventCandidate(identity) for identity in identities)
