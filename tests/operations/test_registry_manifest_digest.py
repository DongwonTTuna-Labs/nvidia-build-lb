"""Registry publication receipts bind deployable digests to scanned image IDs."""

import json
from hashlib import sha256

import pytest
from scripts.qa.registry_manifest_digest import RegistryManifestError, raw_registry_digest

_IMAGE_ID = "sha256:" + ("a" * 64)


def _manifest() -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": _IMAGE_ID,
            "size": 123,
        },
        "layers": [],
    }


def _payload(*parts: bytes) -> bytes:
    return b"".join(parts)


def test_registry_manifest_emits_only_raw_digest_bound_to_scanned_image() -> None:
    payload = (
        b'{ "layers" : [], "config" : {"size":123,'
        + f'"digest":"{_IMAGE_ID}",'.encode()
        + b'"mediaType":"application/vnd.oci.image.config.v1+json"},'
        + b'"mediaType":"application/vnd.oci.image.manifest.v1+json",'
        + b'"schemaVersion":2 }\n'
    )
    reserialized = json.dumps(_manifest()).encode("utf-8")

    assert sha256(payload).digest() != sha256(reserialized).digest()
    assert raw_registry_digest(payload, _IMAGE_ID) == sha256(payload).hexdigest()


@pytest.mark.parametrize(
    "mutation",
    [
        {"mediaType": "application/vnd.oci.image.index.v1+json"},
        {"mediaType": "application/vnd.docker.distribution.manifest.list.v2+json"},
        {"schemaVersion": 1},
        {"schemaVersion": 2.0},
        {"schemaVersion": True},
        {"schemaVersion": "2"},
        {"config": {"digest": "sha256:" + ("c" * 64)}},
        {"config": None},
    ],
)
def test_registry_manifest_rejects_unbound_or_noncanonical_receipt(
    mutation: dict[str, object],
) -> None:
    manifest = _manifest()
    manifest.update(mutation)

    with pytest.raises(RegistryManifestError):
        _ = raw_registry_digest(json.dumps(manifest).encode("utf-8"), _IMAGE_ID)


@pytest.mark.parametrize(
    "payload",
    [
        _payload(
            b'{"schemaVersion":2,"schemaVersion":2,',
            b'"mediaType":"application/vnd.oci.image.manifest.v1+json",',
            f'"config":{{"digest":"{_IMAGE_ID}"}}}}'.encode(),
        ),
        _payload(
            b'{"schemaVersion":2,',
            b'"mediaType":"application/vnd.oci.image.manifest.v1+json",',
            b'"mediaType":"application/vnd.oci.image.manifest.v1+json",',
            f'"config":{{"digest":"{_IMAGE_ID}"}}}}'.encode(),
        ),
        _payload(
            b'{"schemaVersion":2,',
            b'"mediaType":"application/vnd.oci.image.manifest.v1+json",',
            f'"config":{{"digest":"{_IMAGE_ID}"}},'.encode(),
            f'"config":{{"digest":"{_IMAGE_ID}"}}}}'.encode(),
        ),
        _payload(
            b'{"schemaVersion":2,',
            b'"mediaType":"application/vnd.oci.image.manifest.v1+json",',
            f'"config":{{"digest":"{_IMAGE_ID}","digest":"{_IMAGE_ID}"}}}}'.encode(),
        ),
    ],
    ids=("schema-version", "media-type", "config", "config-digest"),
)
def test_registry_manifest_rejects_duplicate_identity_fields(payload: bytes) -> None:
    with pytest.raises(RegistryManifestError):
        _ = raw_registry_digest(payload, _IMAGE_ID)
