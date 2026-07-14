"""Encrypted upstream credential persistence and safe read models."""

from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID, uuid4

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nvidia_build_lb.admin.schemas import (
    EventOutcome,
    EventType,
    HealthState,
    LastStatusClass,
    ProbeStatus,
    UpstreamKeyCreateRequest,
    UpstreamKeyListResponse,
    UpstreamKeyRead,
    UpstreamProbeResponse,
)
from nvidia_build_lb.credential_types import Clock, ResourceConflictError, ResourceNotFoundError
from nvidia_build_lb.db_models import (
    AdminEventRow,
    UpstreamKeyRow,
    UpstreamLivePinRow,
)
from nvidia_build_lb.scheduler_lock import lock_scheduler_state
from nvidia_build_lb.vault import Vault, VaultEnvelope


@dataclass(frozen=True, slots=True)
class UpstreamKeyDependencies:
    """Concrete persistence, encryption, and time dependencies."""

    sessions: async_sessionmaker[AsyncSession]
    vault: Vault
    clock: Clock


def _to_read(row: UpstreamKeyRow) -> UpstreamKeyRead:
    last_status = None if row.last_status_class is None else LastStatusClass(row.last_status_class)
    return UpstreamKeyRead(
        id=row.id,
        fingerprint=f"sha256:{row.fingerprint}",
        enabled=row.enabled,
        health_state=HealthState(row.health_state),
        cooldown_until=row.cooldown_until,
        request_count=row.request_count,
        success_count=row.success_count,
        failure_count=row.failure_count,
        last_status_class=last_status,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@dataclass(frozen=True, slots=True)
class UpstreamKeyRepository:
    """Persist encrypted keys and expose only closed safe DTOs."""

    dependencies: UpstreamKeyDependencies

    async def create(
        self,
        payload: UpstreamKeyCreateRequest,
        request_id: str,
    ) -> UpstreamKeyRead:
        """Create one disabled encrypted row and its safe event atomically."""
        row_id = uuid4()
        now = self.dependencies.clock.now()
        fingerprint = sha256(payload.key.encode()).hexdigest()
        envelope = self.dependencies.vault.encrypt(row_id, payload.key)
        row = UpstreamKeyRow(
            id=row_id,
            fingerprint=fingerprint,
            vault_version=envelope.version,
            vault_nonce=envelope.nonce,
            vault_ciphertext=envelope.ciphertext,
            enabled=False,
            health_state=HealthState.UNKNOWN.value,
            cooldown_until=None,
            cooldown_kind=None,
            quarantined=False,
            request_count=0,
            success_count=0,
            failure_count=0,
            consecutive_rate_limits=0,
            consecutive_transient_failures=0,
            last_status_class=None,
            last_used_at=None,
            created_at=now,
            updated_at=now,
        )
        try:
            async with self.dependencies.sessions.begin() as session:
                session.add(row)
                session.add(
                    AdminEventRow(
                        id=uuid4(),
                        request_id=request_id,
                        event_type=EventType.UPSTREAM_KEY_CREATED.value,
                        upstream_key_id=row_id,
                        downstream_token_id=None,
                        outcome_class=EventOutcome.SUCCEEDED.value,
                        status_class=None,
                        latency_ms=None,
                        occurred_at=now,
                    )
                )
        except IntegrityError:
            raise ResourceConflictError(resource="upstream_key") from None
        return _to_read(row)

    async def list_all(self) -> UpstreamKeyListResponse:
        """Return every current key in the contract's stable order."""
        async with self.dependencies.sessions() as session:
            rows = tuple(
                (
                    await session.scalars(
                        select(UpstreamKeyRow).order_by(
                            UpstreamKeyRow.created_at.asc(),
                            UpstreamKeyRow.id.asc(),
                        )
                    )
                ).all()
            )
        return UpstreamKeyListResponse(items=tuple(_to_read(row) for row in rows))

    async def secret_for(self, key_id: UUID) -> SecretStr:
        """Decrypt one key only for the internal NVIDIA adapter boundary."""
        async with self.dependencies.sessions() as session:
            row = await session.get(UpstreamKeyRow, key_id)
        if row is None:
            raise ResourceNotFoundError(resource="upstream_key", resource_id=key_id)
        return self.dependencies.vault.decrypt(
            key_id,
            VaultEnvelope(
                version=row.vault_version,
                nonce=row.vault_nonce,
                ciphertext=row.vault_ciphertext,
            ),
        )

    async def probe_result(
        self,
        key_id: UUID,
        status: ProbeStatus,
    ) -> UpstreamProbeResponse:
        """Project a closed probe observation without changing enabled state."""
        async with self.dependencies.sessions() as session:
            row = await session.get(UpstreamKeyRow, key_id)
        if row is None:
            raise ResourceNotFoundError(resource="upstream_key", resource_id=key_id)
        return UpstreamProbeResponse(
            id=row.id,
            enabled=row.enabled,
            probe_status=status,
            observed_at=self.dependencies.clock.now(),
        )

    async def enable(self, key_id: UUID, request_id: str) -> None:
        """Enable a known key idempotently without clearing health state."""
        async with self.dependencies.sessions.begin() as session:
            _ = await lock_scheduler_state(session)
            row = await session.get(UpstreamKeyRow, key_id, with_for_update=True)
            if row is None:
                raise ResourceNotFoundError(resource="upstream_key", resource_id=key_id)
            if row.enabled:
                return
            row.enabled = True
            row.updated_at = self.dependencies.clock.now()
            session.add(
                AdminEventRow(
                    id=uuid4(),
                    request_id=request_id,
                    event_type=EventType.UPSTREAM_KEY_ENABLED.value,
                    upstream_key_id=key_id,
                    downstream_token_id=None,
                    outcome_class=EventOutcome.SUCCEEDED.value,
                    status_class=None,
                    latency_ms=None,
                    occurred_at=row.updated_at,
                )
            )

    async def disable(self, key_id: UUID, request_id: str) -> None:
        """Disable a known key idempotently."""
        async with self.dependencies.sessions.begin() as session:
            _ = await lock_scheduler_state(session)
            row = await session.get(UpstreamKeyRow, key_id, with_for_update=True)
            if row is None:
                raise ResourceNotFoundError(resource="upstream_key", resource_id=key_id)
            if not row.enabled:
                return
            row.enabled = False
            row.updated_at = self.dependencies.clock.now()
            session.add(
                AdminEventRow(
                    id=uuid4(),
                    request_id=request_id,
                    event_type=EventType.UPSTREAM_KEY_DISABLED.value,
                    upstream_key_id=key_id,
                    downstream_token_id=None,
                    outcome_class=EventOutcome.SUCCEEDED.value,
                    status_class=None,
                    latency_ms=None,
                    occurred_at=row.updated_at,
                )
            )

    async def delete(self, key_id: UUID, request_id: str) -> None:
        """Delete a disabled key or reject an enabled or unknown identity."""
        async with self.dependencies.sessions.begin() as session:
            _ = await lock_scheduler_state(session)
            row = await session.get(UpstreamKeyRow, key_id, with_for_update=True)
            if row is None:
                raise ResourceNotFoundError(resource="upstream_key", resource_id=key_id)
            if row.enabled:
                raise ResourceConflictError(resource="upstream_key")
            live_pin = await session.scalar(
                select(UpstreamLivePinRow.started_event_id)
                .where(UpstreamLivePinRow.upstream_key_id == key_id)
                .limit(1)
            )
            if live_pin is not None:
                raise ResourceConflictError(resource="upstream_key")
            session.add(
                AdminEventRow(
                    id=uuid4(),
                    request_id=request_id,
                    event_type=EventType.UPSTREAM_KEY_DELETED.value,
                    upstream_key_id=key_id,
                    downstream_token_id=None,
                    outcome_class=EventOutcome.SUCCEEDED.value,
                    status_class=None,
                    latency_ms=None,
                    occurred_at=self.dependencies.clock.now(),
                )
            )
            await session.delete(row)
