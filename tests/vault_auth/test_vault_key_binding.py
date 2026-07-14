"""Database-bound vault-key initialization and startup validation."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import SecretBytes
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nvidia_build_lb.admin.schemas import HealthState
from nvidia_build_lb.db_models import UpstreamKeyRow, VaultKeyVerifierRow
from nvidia_build_lb.vault import Vault
from nvidia_build_lb.vault_key_binding import (
    VaultKeyBindingError,
    ensure_vault_key_binding,
)

pytestmark = [pytest.mark.vault_auth, pytest.mark.anyio]

_NOW = datetime(2026, 7, 14, tzinfo=UTC)


async def test_empty_database_initializes_once_and_rejects_a_later_wrong_key(
    migrated_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    vault = Vault(SecretBytes(b"a" * 32))

    initialized = await ensure_vault_key_binding(migrated_session_factory, vault)
    validated = await ensure_vault_key_binding(migrated_session_factory, vault)

    assert initialized.initialized is True
    assert initialized.ciphertext_rows_validated == 0
    assert validated.initialized is False
    with pytest.raises(VaultKeyBindingError, match="vault_key_binding_failed"):
        _ = await ensure_vault_key_binding(
            migrated_session_factory,
            Vault(SecretBytes(b"b" * 32)),
        )


async def test_legacy_ciphertext_must_decrypt_before_verifier_initialization(
    migrated_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    correct = Vault(SecretBytes(b"c" * 32))
    row_id = uuid4()
    envelope = correct.encrypt(row_id, "nvapi-synthetic-legacy-key")
    async with migrated_session_factory.begin() as session:
        session.add(
            UpstreamKeyRow(
                id=row_id,
                fingerprint="1" * 64,
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
                created_at=_NOW,
                updated_at=_NOW,
            )
        )

    with pytest.raises(VaultKeyBindingError, match="vault_key_binding_failed"):
        _ = await ensure_vault_key_binding(
            migrated_session_factory,
            Vault(SecretBytes(b"d" * 32)),
        )
    async with migrated_session_factory() as session:
        unbound = await session.get(VaultKeyVerifierRow, 1)
    assert unbound is not None
    assert unbound.verifier_salt is None
    assert unbound.verifier_digest is None

    initialized = await ensure_vault_key_binding(migrated_session_factory, correct)

    assert initialized.initialized is True
    assert initialized.ciphertext_rows_validated == 1
    with pytest.raises(VaultKeyBindingError, match="vault_key_binding_failed"):
        _ = await ensure_vault_key_binding(
            migrated_session_factory,
            Vault(SecretBytes(b"d" * 32)),
        )
