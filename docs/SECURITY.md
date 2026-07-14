# Security Model

## Trust boundaries

- The admin UI and API are host-loopback only. Exact Host and Origin checks run
  before credential parsing; CORS is not enabled.
- Downstream clients receive scoped digest-only credentials. `models:read` and
  `chat:write` are independently enforced before request counters or upstream
  I/O.
- NVIDIA credentials are encrypted with AES-256-GCM. PostgreSQL stores only the
  nonce, ciphertext, version, and SHA-256 fingerprint. The master key is a
  separate 32-byte root-owned host file.
- Admin and downstream bearer types have disjoint prefixes and authenticators.
  Plaintext is never exposed by list, event, log, health, or backup receipts.
- The application and database run without effective/bounding capabilities,
  supplementary groups, or privilege escalation after bounded prestart.

Trivy `DS-0002` is suppressed inline only on the two intentional root
entrypoints. The exception does not permit a root steady state: container QA
must prove the bounded prestart disappeared and every remaining app/PostgreSQL
process has the fixed nonroot UID, empty groups, zero effective/bounding
capabilities, and `NoNewPrivs=1`.

## Explicit threats and controls

- Secret in source/history/layer: release scanning combines exact product-key
  shape scanning, Gitleaks filesystem/history/config/history scanning, Trivy
  filesystem/image secret scanning, and filtered image metadata inspection.
- The pinned official Python base publishes its signing `GPG_KEY` in image config
  and inherited history. Before metadata Gitleaks, the scanner requires both
  Python stages to use the same digest-pinned base, proves the candidate value
  and every matching history line are byte-identical to that base, and replaces
  only that verified public value in a temporary scan copy. Missing, additional,
  or changed values fail closed; every other metadata byte remains scanned.
- Vulnerable dependency/image: the locked Python graph is audited by
  `pip-audit`; both immutable application and PostgreSQL images are scanned by
  Trivy for all high/critical vulnerabilities before publication. Unfixed
  findings are not ignored.
- The application builder/runtime share one digest-pinned official Python 3.13
  Alpine base that is scanned before release. The PostgreSQL image keeps the
  pinned 17.9 major/minor base, upgrades only exact patched Alpine crypto/XML
  packages, and removes the unused `gosu` binary; the root prestart performs
  the only UID handoff with `setpriv` before the official entrypoint runs.
- Mutable supply chain: GitHub Actions use full 40-hex commit pins, Docker base
  images and Dockerfile frontend use digests, workflows have minimum explicit
  permissions, and GHCR publishes commit-addressed tags without `latest`.
- DNS rebinding/browser cross-origin access: exact authority/origin validation,
  no CORS grant, bearer headers, no cookies, strict CSP, no-store, and no-referrer.
- Replay after ambiguous POST outcome: failover is allowed only before a
  response byte or ambiguous write/read outcome. Mid-stream or ambiguous
  requests terminate and are never retried on a second key.
- Backup/key mismatch: manifest hashes bind separate artifacts to a safe
  database identity; restore is accepted only into a labeled empty isolated DB
  and must reproduce that identity exactly.
- Migration race: a dedicated two-int PostgreSQL session advisory lock serializes
  Alembic. The service epoch uses a different second lock key.

## Secret handling rules

- Use `set +x` before any command that consumes or creates a secret.
- Never use unfiltered `env`, `printenv`, `docker inspect`, HTTP header capture,
  HAR, trace, video, or credential-bearing screenshot.
- Never put secret values in `.env`, Compose YAML, GitHub Actions inputs,
  command-line arguments, PR text, logs, or evidence.
- Runtime inspection reports only internal IDs, irreversible fingerprints,
  modes, owners, UIDs, capability sets, status classes, and counts.
- Delete a disabled upstream key only after confirming it is not live-pinned.
  Revoke downstream tokens through the admin API; never edit digests directly.

## Release gate

The exact application/PostgreSQL image ID pair and byte-identical source
manifest produced by `build-candidate` must pass every later gate:

```console
make test-browser-prod IMAGE_DIGEST=sha256:... POSTGRES_IMAGE_DIGEST=sha256:... SOURCE_MANIFEST=.omo/evidence/task-6a-release/source-manifest.json EVIDENCE_DIR=.omo/evidence/task-6b-release
make verify-local IMAGE_DIGEST=sha256:... POSTGRES_IMAGE_DIGEST=sha256:... SOURCE_MANIFEST=.omo/evidence/task-6a-release/source-manifest.json EVIDENCE_DIR=.omo/evidence/task-7-verify
make scan-release IMAGE_DIGEST=sha256:... POSTGRES_IMAGE_DIGEST=sha256:... SOURCE_MANIFEST=.omo/evidence/task-6a-release/source-manifest.json EVIDENCE_DIR=.omo/evidence/task-7-scan
```

Each script claims a new evidence directory, fails closed, labels every Docker
resource it creates, and writes bound receipts. The release scan covers both
images and the source tree without `--ignore-unfixed`. A scanner outage,
unavailable vulnerability database, mutable Action reference, finding, missing
cleanup, source-manifest byte drift, or either image-ID drift is FAIL.

After the runtime audit records at least three distinct disproved hypotheses,
each hypothesis must cite a SHA-256 already present in the four gate artifact
sets. `scripts/qa/canonical_evidence.py create` binds those artifacts, both
image IDs, the source manifest, backup/restore receipts, visual reviews, and the
runtime audit into one exclusive mode-`0600` receipt. Re-run the same invocation
with `verify` instead of `create` before treating that receipt as current.

## Residual risks

- The vault master key has no online re-encryption path in this release. Rotation
  requires a separately designed transaction; paired restore is supported.
- Host root and Docker-daemon access remain trusted. A Docker-group principal is
  root-equivalent and must be administered accordingly.
- Provider availability, credits, and rate limits are external. Cooldown and
  failover improve availability but do not bypass provider limits.
