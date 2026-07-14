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

`make help` lists the stable verification targets. A release candidate must pass
`build-candidate`, `test-browser-prod`, `verify-local`, and `scan-release` on the
same immutable image ID before publication.

Operational documentation:

- `docs/ARCHITECTURE.md`: wire, persistence, routing, and security contracts
- `docs/RUNBOOK.md`: deploy, health, rotation, and incident operations
- `docs/BACKUP_RESTORE.md`: split-custody backup and isolated restore drill
- `docs/SECURITY.md`: threat model, secret handling, and release scans
- `docs/ROLLBACK.md`: image and state rollback procedure

The supplied Compose surface binds only `127.0.0.1:2456`; it intentionally
does not create public DNS, tunnel, or reverse-proxy ingress. This project is
independent and is not affiliated with NVIDIA Corporation.
