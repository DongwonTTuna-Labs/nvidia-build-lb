# NVIDIA Build LB

An NVIDIA Build hosted API load balancer targeting `z-ai/glm-5.2` at
`https://integrate.api.nvidia.com/v1`.

It provides an OpenAI-compatible chat surface, encrypted NVIDIA key custody,
rate-aware round-robin and bounded failover, digest-only downstream tokens, and
a loopback-only administration UI. PostgreSQL retains routing state and
encrypted credentials; the vault master key remains a separate root-owned host
secret.

Development uses Python 3.13 and `uv`:

```console
uv sync --locked --all-groups
uv run pytest -q
make help
```

`make help` lists the stable verification targets. `build-candidate` records one
byte-identical source manifest and one immutable application/PostgreSQL image
ID pair. `test-browser-prod`, `verify-local`, and `scan-release` must all reuse
that exact three-part identity before publication:

```console
make build-candidate EVIDENCE_DIR=.omo/evidence/task-6a-release
APP_IMAGE_ID="$(jq -er .image_digest .omo/evidence/task-6a-release/candidate.json)"
POSTGRES_IMAGE_ID="$(jq -er .postgres_image_digest .omo/evidence/task-6a-release/candidate.json)"
SOURCE_MANIFEST=.omo/evidence/task-6a-release/source-manifest.json
make verify-local IMAGE_DIGEST="$APP_IMAGE_ID" POSTGRES_IMAGE_DIGEST="$POSTGRES_IMAGE_ID" SOURCE_MANIFEST="$SOURCE_MANIFEST" EVIDENCE_DIR=.omo/evidence/task-7-verify
make scan-release IMAGE_DIGEST="$APP_IMAGE_ID" POSTGRES_IMAGE_DIGEST="$POSTGRES_IMAGE_ID" SOURCE_MANIFEST="$SOURCE_MANIFEST" EVIDENCE_DIR=.omo/evidence/task-7-scan
```

Operational documentation:

- `docs/ARCHITECTURE.md`: wire, persistence, routing, and security contracts
- `docs/RUNBOOK.md`: deploy, health, rotation, and incident operations
- `docs/BACKUP_RESTORE.md`: split-custody backup and isolated restore drill
- `docs/SECURITY.md`: threat model, secret handling, and release scans
- `docs/ROLLBACK.md`: image and state rollback procedure

The supplied Compose surface binds only `127.0.0.1:2456`; it intentionally
does not create public DNS, tunnel, or reverse-proxy ingress. This project is
invoked through `scripts/ops/production-compose.sh`, which reloads a canonical
root-owned runtime file on every call, accepts only raw 64-hex GHCR registry
digests, serializes that file through a stable lock, and rejects mutable image
tags before Compose runs. This project is independent and is not affiliated
with NVIDIA Corporation.
