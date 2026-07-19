# NVIDIA Build LB

An NVIDIA Build hosted API load balancer targeting `z-ai/glm-5.2` at
`https://integrate.api.nvidia.com/v1`.

It provides an OpenAI-compatible chat surface, encrypted NVIDIA key custody,
rate-aware round-robin and bounded failover, digest-only downstream tokens, and
a loopback-only administration UI. PostgreSQL retains routing state and
encrypted credentials; the vault master key remains a separate root-owned host
secret.

The hosted gateway is implemented in Rust/Actix with SQLx/PostgreSQL and the
owner console is Svelte. The retained Python QA harness remains available for
the legacy contract surface.

Fast affected-scope checks for the hosted stack are:

```console
make rust-check       # core tests + gateway compile
make admin-check      # Svelte type/build check
make rust-smoke       # mock two-key, failover, scope, stream, modalities
make rust-smoke-postgres
```

The retired Python contract suite is kept only for historical comparison; it
is not a shipping gate for the Rust/Svelte stack. Run it only when explicitly
auditing legacy behavior:

```console
uv sync --locked --all-groups
uv run pytest -q
make help
```

`make help` lists the stable verification targets. `build-candidate` records one
source-bound manifest and one immutable application/PostgreSQL/QA-fixture image
triplet. The Rust candidate gate and its targeted smoke receipts are the
shipping identity; the older `test-browser-prod`, `verify-local`, and
`scan-release` scripts are retained only as retired compatibility tooling:

```console
make build-candidate EVIDENCE_DIR=.omo/evidence/task-6a-release
APP_IMAGE_ID="$(jq -er .image_digest .omo/evidence/task-6a-release/candidate.json)"
POSTGRES_IMAGE_ID="$(jq -er .postgres_image_digest .omo/evidence/task-6a-release/candidate.json)"
FIXTURE_IMAGE_ID="$(jq -er .fixture_image_digest .omo/evidence/task-6a-release/candidate.json)"
SOURCE_MANIFEST=.omo/evidence/task-6a-release/source-manifest.json
IMAGE_DIGEST="$APP_IMAGE_ID" POSTGRES_IMAGE_DIGEST="$POSTGRES_IMAGE_ID" FIXTURE_IMAGE_DIGEST="$FIXTURE_IMAGE_ID" \
  SOURCE_MANIFEST="$SOURCE_MANIFEST" EVIDENCE_DIR=.omo/evidence/task-6b-release \
  scripts/qa/test-browser-prod.sh
make verify-local IMAGE_DIGEST="$APP_IMAGE_ID" POSTGRES_IMAGE_DIGEST="$POSTGRES_IMAGE_ID" FIXTURE_IMAGE_DIGEST="$FIXTURE_IMAGE_ID" SOURCE_MANIFEST="$SOURCE_MANIFEST" EVIDENCE_DIR=.omo/evidence/task-7-verify
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
