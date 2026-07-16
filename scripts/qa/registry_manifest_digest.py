"""Validate a pushed single-image manifest and emit its raw registry digest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_JSON_OBJECT = TypeAdapter(dict[str, object])
_SCHEMA_VERSION = 2
_MANIFEST_MEDIA_TYPES = frozenset(
    {
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    }
)


class RegistryManifestError(ValueError):
    """Reject a registry receipt that is not bound to the scanned image ID."""


class _Arguments(argparse.Namespace):
    manifest: Path = Path()
    expected_image_id: str = ""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RegistryManifestError
        result[key] = value
    return result


def raw_registry_digest(payload: bytes, expected_image_id: str) -> str:
    """Return a raw manifest digest only when its config is the expected image ID."""
    if _DIGEST.fullmatch(expected_image_id) is None:
        raise RegistryManifestError
    try:
        loaded: object = json.loads(  # pyright: ignore[reportAny]
            payload,
            object_pairs_hook=_unique_object,
        )
        manifest = _JSON_OBJECT.validate_python(loaded, strict=True)
    except (
        RegistryManifestError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
    ):
        raise RegistryManifestError from None
    schema_version = manifest.get("schemaVersion")
    if type(schema_version) is not int or schema_version != _SCHEMA_VERSION:
        raise RegistryManifestError
    if manifest.get("mediaType") not in _MANIFEST_MEDIA_TYPES:
        raise RegistryManifestError
    try:
        config = _JSON_OBJECT.validate_python(manifest.get("config"), strict=True)
    except ValidationError:
        raise RegistryManifestError from None
    if config.get("digest") != expected_image_id:
        raise RegistryManifestError
    return hashlib.sha256(payload).hexdigest()


def main() -> None:
    """Parse one buildx manifest receipt without leaking unvalidated content."""
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--manifest", type=Path, required=True)
    _ = parser.add_argument("--expected-image-id", required=True)
    arguments = parser.parse_args(namespace=_Arguments())
    try:
        result = raw_registry_digest(
            arguments.manifest.read_bytes(),
            arguments.expected_image_id,
        )
    except (OSError, RegistryManifestError):
        _ = sys.stderr.write("registry_manifest_invalid\n")
        raise SystemExit(1) from None
    _ = sys.stdout.write(result + "\n")


if __name__ == "__main__":
    main()
