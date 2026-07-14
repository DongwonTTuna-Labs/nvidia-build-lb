from enum import StrEnum, unique
from importlib import import_module
from importlib.util import find_spec
from types import MappingProxyType
from uuid import UUID, uuid4

import anyio
import pytest
from anyio.lowlevel import checkpoint
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretBytes

from nvidia_build_lb.vault import (
    Vault,
    VaultDecryptionError,
    VaultEnvelope,
    VaultFailureCode,
    VaultKeyLengthError,
)

pytestmark = pytest.mark.vault_auth


@unique
class _Tamper(StrEnum):
    NONCE = "nonce"
    CIPHERTEXT = "ciphertext"


def test_vault_module_is_packaged() -> None:
    # Given: the installed nvidia_build_lb package.

    # When: the encrypted-vault module is resolved.
    spec = find_spec("nvidia_build_lb.vault")

    # Then: Todo 2 supplies an importable production module.
    assert spec is not None


def test_vault_module_exports_closed_crypto_surface() -> None:
    # Given: the packaged encrypted-vault module.
    module = import_module("nvidia_build_lb.vault")

    # When: its contract-owned symbols are enumerated.
    exported = {"Vault", "VaultDecryptionError", "VaultEnvelope", "VaultKeyLengthError"}

    # Then: every typed crypto boundary is present before behavior is added.
    assert all(hasattr(module, name) for name in exported)


def test_vault_rejects_every_non_256_bit_master_key() -> None:
    # Given: two synthetic master keys adjacent to the exact 32-byte boundary.
    malformed = (b"x" * 31, b"x" * 33)

    # When: each key is parsed into a vault.
    results: list[str] = []
    for candidate in malformed:
        with pytest.raises(VaultKeyLengthError) as captured:
            _ = Vault(SecretBytes(candidate))
        results.append(str(captured.value))

    # Then: both fail with one stable secret-free code.
    assert results == ["vault_key_invalid", "vault_key_invalid"]


def test_vault_uses_exact_row_bound_v1_aad_and_randomized_ciphertext() -> None:
    # Given: one synthetic credential, master key, and fixed row UUID.
    master_key = b"m" * 32
    row_id = UUID("00000000-0000-4000-8000-000000000001")
    credential = " opaque-é-credential "
    vault = Vault(SecretBytes(master_key))

    # When: the same plaintext is encrypted twice for the same row.
    first = vault.encrypt(row_id, credential)
    second = vault.encrypt(row_id, credential)

    # Then: both envelopes decrypt under the exact AAD but use distinct nonce/ciphertext pairs.
    aad = b"nvidia-build-lb:vault:v1\x00" + row_id.bytes
    assert AESGCM(master_key).decrypt(first.nonce, first.ciphertext, aad).decode() == credential
    assert first.version == 1
    assert len(first.nonce) == 12
    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext


def test_vault_decrypts_only_the_original_row_envelope() -> None:
    # Given: one valid envelope for a synthetic row.
    row_id = uuid4()
    credential = "synthetic-upstream-key"
    vault = Vault(SecretBytes(b"v" * 32))
    envelope = vault.encrypt(row_id, credential)

    # When: the service decrypts that exact row envelope.
    decrypted = vault.decrypt(row_id, envelope)

    # Then: the masked result contains the unchanged credential only on explicit access.
    assert decrypted.get_secret_value() == credential
    assert credential not in repr(decrypted)
    assert credential not in repr(envelope)


def test_vault_envelope_repr_never_exposes_nonce_or_ciphertext() -> None:
    # Given: one encrypted synthetic credential.
    envelope = Vault(SecretBytes(b"r" * 32)).encrypt(uuid4(), "repr-secret")

    # When: an exception or diagnostic renders the envelope.
    rendered = repr(envelope)

    # Then: neither opaque vault storage field is printable by accident.
    assert repr(envelope.nonce) not in rendered
    assert repr(envelope.ciphertext) not in rendered


@pytest.mark.parametrize("tamper", [_Tamper.NONCE, _Tamper.CIPHERTEXT])
def test_vault_rejects_authenticated_envelope_tamper(tamper: _Tamper) -> None:
    # Given: one valid synthetic envelope.
    row_id = uuid4()
    vault = Vault(SecretBytes(b"t" * 32))
    envelope = vault.encrypt(row_id, "synthetic-upstream-key")
    tampered_parts = MappingProxyType(
        {
            _Tamper.NONCE: (
                bytes([envelope.nonce[0] ^ 1]) + envelope.nonce[1:],
                envelope.ciphertext,
            ),
            _Tamper.CIPHERTEXT: (
                envelope.nonce,
                bytes([envelope.ciphertext[0] ^ 1]) + envelope.ciphertext[1:],
            ),
        }
    )
    nonce, ciphertext = tampered_parts[tamper]
    changed = VaultEnvelope(version=envelope.version, nonce=nonce, ciphertext=ciphertext)

    # When: the changed envelope is decrypted.
    with pytest.raises(VaultDecryptionError) as captured:
        _ = vault.decrypt(row_id, changed)

    # Then: authentication fails closed without plaintext context.
    assert captured.value.code is VaultFailureCode.AUTHENTICATION_FAILED
    assert str(captured.value) == "vault_authentication_failed"


def test_vault_rejects_wrong_key_and_row_swap() -> None:
    # Given: one envelope and two mismatched decryption contexts.
    row_id = uuid4()
    envelope = Vault(SecretBytes(b"a" * 32)).encrypt(row_id, "synthetic-upstream-key")

    # When: a different key and a different row each attempt decryption.
    with pytest.raises(VaultDecryptionError) as wrong_key:
        _ = Vault(SecretBytes(b"b" * 32)).decrypt(row_id, envelope)
    with pytest.raises(VaultDecryptionError) as row_swap:
        _ = Vault(SecretBytes(b"a" * 32)).decrypt(uuid4(), envelope)

    # Then: both contexts fail through the same authenticated-envelope class.
    assert wrong_key.value.code is VaultFailureCode.AUTHENTICATION_FAILED
    assert row_swap.value.code is VaultFailureCode.AUTHENTICATION_FAILED


def test_vault_rejects_unknown_envelope_version_before_decryption() -> None:
    # Given: a structurally valid envelope carrying an unsupported version.
    row_id = uuid4()
    vault = Vault(SecretBytes(b"u" * 32))
    current = vault.encrypt(row_id, "synthetic-upstream-key")
    unknown = VaultEnvelope(version=2, nonce=current.nonce, ciphertext=current.ciphertext)

    # When: the unknown version reaches the decrypt boundary.
    with pytest.raises(VaultDecryptionError) as captured:
        _ = vault.decrypt(row_id, unknown)

    # Then: it fails closed as an explicit version error.
    assert captured.value.code is VaultFailureCode.UNKNOWN_VERSION


@pytest.mark.anyio
async def test_vault_error_survives_anyio_deadline_boundary() -> None:
    row_id = uuid4()
    vault = Vault(SecretBytes(b"u" * 32))
    current = vault.encrypt(row_id, "synthetic-upstream-key")
    unknown = VaultEnvelope(version=2, nonce=current.nonce, ciphertext=current.ciphertext)

    async def decrypt_with_deadline() -> None:
        with anyio.fail_after(1):
            await checkpoint()
            _ = vault.decrypt(row_id, unknown)

    with pytest.raises(VaultDecryptionError) as captured:
        await decrypt_with_deadline()

    assert captured.value.code is VaultFailureCode.UNKNOWN_VERSION
