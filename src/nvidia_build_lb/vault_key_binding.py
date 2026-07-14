"""Database-bound initialization and validation of the vault master key."""

from dataclasses import dataclass
from typing import override

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nvidia_build_lb.db_models import UpstreamKeyRow, VaultKeyVerifierRow, utc_now
from nvidia_build_lb.vault import (
    Vault,
    VaultDecryptionError,
    VaultEnvelope,
    VaultKeyVerifier,
)


class VaultKeyBindingError(Exception):
    """Fail startup with one stable value-silent vault binding error."""

    @override
    def __str__(self) -> str:
        return "vault_key_binding_failed"


@dataclass(frozen=True, slots=True)
class VaultKeyBindingReceipt:
    """Safe startup evidence for verifier validation or initialization."""

    initialized: bool
    ciphertext_rows_validated: int


async def ensure_vault_key_binding(
    sessions: async_sessionmaker[AsyncSession],
    vault: Vault,
) -> VaultKeyBindingReceipt:
    """Validate the configured key or initialize a verifier after decrypting legacy rows."""
    try:
        async with sessions.begin() as session:
            row = _require_verifier_row(
                await session.get(VaultKeyVerifierRow, 1, with_for_update=True)
            )
            if row.verifier_salt is not None or row.verifier_digest is not None:
                return _validate_bound_row(row, vault)
            return await _initialize_unbound_row(session, row, vault)
    except VaultKeyBindingError:
        raise
    except (SQLAlchemyError, VaultDecryptionError, ValueError):
        raise VaultKeyBindingError from None


def _require_verifier_row(row: VaultKeyVerifierRow | None) -> VaultKeyVerifierRow:
    if row is None:
        raise VaultKeyBindingError
    return row


def _validate_bound_row(row: VaultKeyVerifierRow, vault: Vault) -> VaultKeyBindingReceipt:
    if row.verifier_salt is None or row.verifier_digest is None or row.initialized_at is None:
        raise VaultKeyBindingError
    try:
        verifier = VaultKeyVerifier(row.verifier_salt, row.verifier_digest)
    except ValueError:
        raise VaultKeyBindingError from None
    if not vault.matches_key_verifier(verifier):
        raise VaultKeyBindingError
    return VaultKeyBindingReceipt(initialized=False, ciphertext_rows_validated=0)


async def _initialize_unbound_row(
    session: AsyncSession,
    row: VaultKeyVerifierRow,
    vault: Vault,
) -> VaultKeyBindingReceipt:
    if row.initialized_at is not None:
        raise VaultKeyBindingError
    upstream_rows = tuple(
        (
            await session.scalars(
                select(UpstreamKeyRow).order_by(UpstreamKeyRow.created_at, UpstreamKeyRow.id)
            )
        ).all()
    )
    for upstream in upstream_rows:
        _ = vault.decrypt(
            upstream.id,
            VaultEnvelope(
                version=upstream.vault_version,
                nonce=upstream.vault_nonce,
                ciphertext=upstream.vault_ciphertext,
            ),
        )
    verifier = vault.build_key_verifier()
    row.verifier_salt = verifier.salt
    row.verifier_digest = verifier.digest
    row.initialized_at = utc_now()
    return VaultKeyBindingReceipt(
        initialized=True,
        ciphertext_rows_validated=len(upstream_rows),
    )
