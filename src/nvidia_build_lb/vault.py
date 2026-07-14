"""Versioned AES-GCM envelope types for upstream credentials."""

from dataclasses import dataclass, field
from enum import StrEnum, unique
from os import urandom
from typing import ClassVar, Final, override
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretBytes, SecretStr

_MASTER_KEY_BYTES: Final = 32
_NONCE_BYTES: Final = 12
_VERSION: Final = 1
_AAD_PREFIX: Final = b"nvidia-build-lb:vault:v1\x00"


@unique
class VaultFailureCode(StrEnum):
    """Secret-free vault rejection classes."""

    UNKNOWN_VERSION = "vault_unknown_version"
    AUTHENTICATION_FAILED = "vault_authentication_failed"


@dataclass(slots=True)
class VaultKeyLengthError(Exception):
    """Reject a master key that is not exactly 32 raw bytes."""

    @override
    def __str__(self) -> str:
        return "vault_key_invalid"


@dataclass(slots=True)
class VaultDecryptionError(Exception):
    """Represent a fail-closed envelope rejection without sensitive context."""

    code: VaultFailureCode

    @override
    def __str__(self) -> str:
        return self.code.value


@dataclass(frozen=True, slots=True)
class VaultEnvelope:
    """The three non-plaintext fields stored for one encrypted credential."""

    version: int
    nonce: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)


class Vault:
    """Hold a masked master key for row-bound envelope operations."""

    __slots__: ClassVar[tuple[str, ...]] = ("_master_key",)

    _master_key: SecretBytes

    def __init__(self, master_key: SecretBytes) -> None:
        """Copy one exact 256-bit masked key into the vault boundary."""
        raw_key = master_key.get_secret_value()
        if len(raw_key) != _MASTER_KEY_BYTES:
            raise VaultKeyLengthError
        self._master_key = SecretBytes(bytes(raw_key))

    def encrypt(self, row_id: UUID, plaintext: str) -> VaultEnvelope:
        """Encrypt unchanged UTF-8 text with fresh randomness and row-bound AAD."""
        nonce = urandom(_NONCE_BYTES)
        ciphertext = AESGCM(self._master_key.get_secret_value()).encrypt(
            nonce,
            plaintext.encode(),
            _AAD_PREFIX + row_id.bytes,
        )
        return VaultEnvelope(version=_VERSION, nonce=nonce, ciphertext=ciphertext)

    def decrypt(self, row_id: UUID, envelope: VaultEnvelope) -> SecretStr:
        """Authenticate and decrypt one envelope only in its original row context."""
        if envelope.version != _VERSION:
            raise VaultDecryptionError(code=VaultFailureCode.UNKNOWN_VERSION)
        try:
            plaintext = AESGCM(self._master_key.get_secret_value()).decrypt(
                envelope.nonce,
                envelope.ciphertext,
                _AAD_PREFIX + row_id.bytes,
            )
            return SecretStr(plaintext.decode())
        except (InvalidTag, UnicodeDecodeError, ValueError):
            raise VaultDecryptionError(code=VaultFailureCode.AUTHENTICATION_FAILED) from None
