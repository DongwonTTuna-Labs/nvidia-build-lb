# Operations Runbook

## Runtime invariants

- The gateway is published only on `127.0.0.1:2456`.
- Application and PostgreSQL images are immutable `@sha256:` references.
- `admin_token`, `vault_master_key`, and `db_password` are separate root-owned
  regular files with mode `0600`; they never enter Git or Compose environment
  values.
- The application runs as UID/GID 65532 and PostgreSQL as UID/GID 70 after a
  bounded root prestart copies only each process's required secret into tmpfs.
- Existing `codex-lb` on port 2455 is independent and must remain healthy.

## First deployment

Set the two registry digest variables to only the reviewed 64 lowercase hex
characters after `@sha256:`. The production wrapper rejects tags, complete image
references, uppercase values, and malformed digests before Compose runs.

```console
set +x
sudo install -d -o root -g root -m 0700 /opt/nvidia-build-lb/secrets
sudo sh -c 'umask 077; printf nblb_admin_ > /opt/nvidia-build-lb/secrets/admin_token; openssl rand -hex 32 | tr -d "\n" >> /opt/nvidia-build-lb/secrets/admin_token'
sudo sh -c 'umask 077; openssl rand 32 > /opt/nvidia-build-lb/secrets/vault_master_key'
sudo sh -c 'umask 077; openssl rand -hex 32 | tr -d "\n" > /opt/nvidia-build-lb/secrets/db_password'
sudo chown root:root /opt/nvidia-build-lb/secrets/*
sudo chmod 0600 /opt/nvidia-build-lb/secrets/*
export NBLB_APP_REGISTRY_DIGEST="$APP_REGISTRY_DIGEST"
export NBLB_POSTGRES_REGISTRY_DIGEST="$POSTGRES_REGISTRY_DIGEST"
export NBLB_SECRET_DIR=/opt/nvidia-build-lb/secrets
scripts/ops/production-compose.sh config --quiet
scripts/ops/production-compose.sh up -d
```

Compose waits for PostgreSQL health, runs the migration container, then starts
the one-worker gateway. Migrations take a dedicated PostgreSQL session advisory
lock, so concurrent rollout attempts serialize. Lock acquisition has a bounded
statement timeout and failure is surfaced only as `migration_failed`. The
required Alembic head is `0004_vault_key_verifier`. On first startup after that
migration, the app initializes the singleton verifier only after every existing
upstream ciphertext decrypts with the configured vault key; every later startup
requires the salted verifier HMAC to match or fails closed.

## Health and safe inspection

```console
curl --fail --header 'Host: 127.0.0.1:2456' http://127.0.0.1:2456/health
scripts/ops/production-compose.sh ps
scripts/ops/production-compose.sh logs --no-color --since 10m app migrate db
```

Expected ready response is `{"status":"ok","ready":true}`. Use only filtered
container inspection for UID, capability, health, labels, and mount metadata.
Never run unfiltered `env`, `printenv`, or `docker inspect`, and never log HTTP
headers or request bodies.

## Credential operations

Open `http://127.0.0.1:2456/admin` on the host and enter the admin bearer. The
browser keeps it only in a per-tab module variable. Add NVIDIA keys through the
admin API/UI; new keys are disabled until explicitly enabled. Plaintext NVIDIA
keys are never returned. Downstream bearer plaintext is returned only once.

Admin bearer rotation is an atomic host-file replacement followed by recreation
of only the app container. A stop/start is insufficient because the canonical
secret bind may remain pinned to the old inode.

```console
set +x
sudo sh -c 'umask 077; printf nblb_admin_ > /opt/nvidia-build-lb/secrets/.admin_token.next; openssl rand -hex 32 | tr -d "\n" >> /opt/nvidia-build-lb/secrets/.admin_token.next; chown root:root /opt/nvidia-build-lb/secrets/.admin_token.next; chmod 0600 /opt/nvidia-build-lb/secrets/.admin_token.next; mv /opt/nvidia-build-lb/secrets/.admin_token.next /opt/nvidia-build-lb/secrets/admin_token; sync -f /opt/nvidia-build-lb/secrets/admin_token; sync -f /opt/nvidia-build-lb/secrets'
scripts/ops/production-compose.sh up -d --force-recreate --no-deps app
```

Do not rotate `vault_master_key` in isolation. Existing NVIDIA ciphertext is
bound to that key. A future online re-encryption feature requires its own design
and migration; until then the key is restored only as the matching half of a
verified backup pair.

## Incident classification

- `prestart_failed`: missing, malformed, wrongly owned, symlinked, or wrongly
  permissioned canonical secret. Correct custody and recreate only the consumer.
- `migration_failed`: unavailable database, advisory-lock timeout, unknown
  Alembic revision, or migration failure. Preserve DB state and diagnose before
  retrying.
- `runtime_failed`: database lifecycle, startup reconciliation, or service epoch
  failure. The process fails closed; inspect safe logs and database health.
- HTTP 503 health: the database or eligible-key readiness contract is not met.
- NVIDIA 429/5xx: per-key cooldown/failover applies only before visible output;
  ambiguous or partially delivered POSTs are never replayed.

Use `docs/ROLLBACK.md` rather than deleting a volume or downgrading a live
schema ad hoc.
