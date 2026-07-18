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

Store the two reviewed 64-lowercase-hex registry digests, the non-secret secret
directory path, and all ledger caps in the canonical root-owned runtime file.
The production wrapper reads this file on every invocation, rejects mutable or
malformed image values, and ignores transient environment overrides for image
and cap fields. This keeps later recreations on the same durable inputs.

```console
set +x
sudo install -d -o root -g root -m 0700 /opt/nvidia-build-lb/secrets
sudo sh -c 'umask 077; printf nblb_admin_ > /opt/nvidia-build-lb/secrets/admin_token; openssl rand -hex 32 | tr -d "\n" >> /opt/nvidia-build-lb/secrets/admin_token'
sudo sh -c 'umask 077; openssl rand 32 > /opt/nvidia-build-lb/secrets/vault_master_key'
sudo sh -c 'umask 077; openssl rand -hex 32 | tr -d "\n" > /opt/nvidia-build-lb/secrets/db_password'
sudo chown root:root /opt/nvidia-build-lb/secrets/*
sudo chmod 0600 /opt/nvidia-build-lb/secrets/*
export NBLB_RUNTIME_CONFIG_FILE=/etc/nvidia-build-lb/runtime.env
sudo install -d -o root -g root -m 0755 /etc/nvidia-build-lb
sudo test ! -e /etc/nvidia-build-lb/runtime.env.lock
sudo install -o root -g root -m 0644 /dev/null /etc/nvidia-build-lb/runtime.env.lock
sudo env APP_DIGEST="$NBLB_APP_REGISTRY_DIGEST" POSTGRES_DIGEST="$NBLB_POSTGRES_REGISTRY_DIGEST" sh -c '
  set -eu
  tmp=$(mktemp /etc/nvidia-build-lb/.runtime.env.XXXXXX)
  trap '\''rm -f -- "$tmp"'\'' EXIT HUP INT TERM
  printf "%s\n" \
    "NBLB_APP_REGISTRY_DIGEST=$APP_DIGEST" \
    "NBLB_POSTGRES_REGISTRY_DIGEST=$POSTGRES_DIGEST" \
    "NBLB_SECRET_DIR=/opt/nvidia-build-lb/secrets" \
    "NBLB_ADMIN_EVENT_MAX_ROWS=100000" \
    "NBLB_ADMIN_ATTEMPT_MAX_ROWS=40000" \
    "NBLB_ADMIN_LEDGER_PRUNE_BATCH_SIZE=1000" > "$tmp"
  chown root:root "$tmp"
  chmod 0644 "$tmp"
  sync -f "$tmp"
  mv -f -- "$tmp" /etc/nvidia-build-lb/runtime.env
  sync -f /etc/nvidia-build-lb
  trap - EXIT HUP INT TERM
'
scripts/ops/production-compose.sh config --quiet
scripts/ops/production-compose.sh up -d
```

Compose waits for PostgreSQL health, runs the migration container, then starts
the one-worker gateway. Migrations take a dedicated PostgreSQL session advisory
lock, so concurrent rollout attempts serialize. Lock acquisition has a bounded
statement timeout and failure is surfaced only as `migration_failed`. The
required Alembic head is `0005_admin_dashboard_ledger`. The ordered chain is
`0001_baseline` → `0002_vault_auth` → `0003_nvidia_routing` →
`0004_vault_key_verifier` → `0005_admin_dashboard_ledger`. Complete 0004 is
supported only as the legacy V2 backup/restore input; a deployed app reaches
complete 0005 and emits V3 backup state. On first startup after the 0004
migration, the app initializes the singleton verifier only after every existing
upstream ciphertext decrypts with the configured vault key; every later startup
requires the salted verifier HMAC to match or fails closed.

For an existing 0004 deployment, use one controlled forward operation: stop the
app, create and verify a paired V2 backup while PostgreSQL remains running, run
the same reviewed forward image's migration to 0005, start the app, then verify
`/health` and the canonical admin dashboard. If backup fails, restart the 0004
app and abort; never migrate after a failed or unverified backup. Do not run 0004
and 0005 app processes against the database concurrently.

## Fresh bootstrap and readiness

On a new empty database, the process is reachable before the gateway is ready.
With no eligible upstream key, `/health` intentionally returns HTTP 503 with
`{"status":"degraded","ready":false}`. Do not wait for `ready:true` before
registering the first key.

```console
scripts/ops/production-compose.sh ps
scripts/ops/production-compose.sh logs --no-color --since 10m app migrate db
curl --silent --show-error --output - --write-out '\nHTTP %{http_code}\n' --header 'Host: 127.0.0.1:2456' http://127.0.0.1:2456/health
```

Transfer the admin bearer to the local browser without printing it, placing it
in a command argument, or leaving it in persistent clipboard history. On a
Wayland desktop with `wl-copy`, use a one-paste clipboard handoff:

```console
set +x
sudo cat /opt/nvidia-build-lb/secrets/admin_token | wl-copy --paste-once
```

Open `http://127.0.0.1:2456/admin`, paste once into **Admin bearer**, submit,
then run `wl-copy --clear`. If `wl-copy --paste-once` is unavailable, use an
approved local password manager with one-time paste instead; never use `cat` to
the terminal, a shell argument, or a saved browser field as a fallback.

Complete the visible first-run sequence for both owned NVIDIA credentials:

1. add key 1; it is stored encrypted and disabled;
2. probe key 1, confirm `valid`, and enable it;
3. add key 2 through the same one-time field, then probe and enable it;
4. verify that the dashboard shows exactly two registered rows and two eligible
   rows before issuing any downstream credential or starting Hermes cutover.

For a planned key rotation, do not use the generic Add control. Start from
**Replace** on the exact old row identified by its safe handle, ID, and
fingerprint. Add, probe, and enable the replacement. The dashboard then owns the
temporary three-row cleanup: follow its single action to disable the selected
old row, then delete that same row, and stop unless the overview returns to
`2 registered · 2 eligible`. If a reload loses the local replacement
relationship, the dashboard marks 3+ rows as cleanup-required and directs
review to the safe row evidence instead of choosing a deletion target.

Now verify readiness:

```console
curl --fail --header 'Host: 127.0.0.1:2456' http://127.0.0.1:2456/health
```

The expected response after the first enable is `{"status":"ok","ready":true}`;
readiness alone does not prove the required two-key deployment. Finish the
second key sequence and confirm `2 registered / 2 eligible`. `MODE=one-key`
still requires those same two registered rows: deliberately disable exactly one
already-probed row before that matrix, then re-probe and enable it before
`MODE=two-key`. Each matrix restores the enabled projection it observed at
entry. A UI-issued downstream token is only for a named client that will
actually use it; store its safe Internal ID with that client, verify the client,
and revoke it when the client is retired. Do not issue a UI token for Hermes:
the journal-aware `cycle` helper creates and owns the Hermes token itself.

From the clean release checkout, bind all three live gates to the exact running
application digest. With exactly two registered rows and one deliberately
disabled row, run the one-key matrix first:

```console
RUNNING_REF=$(docker inspect --format '{{.Config.Image}}' nvidia-build-lb-app-1)
IMAGE_DIGEST=${RUNNING_REF##*@}
[[ "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || exit 1
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
sudo /usr/bin/make -C "$PWD" \
  MODE=one-key IMAGE_DIGEST="$IMAGE_DIGEST" \
  EVIDENCE_DIR="/opt/nvidia-build-lb/evidence/live-one-key-$STAMP" smoke-live
```

Re-probe and enable that exact disabled row, require `2 registered · 2
eligible`, then run the two-key matrix against the unchanged digest:

```console
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
sudo /usr/bin/make -C "$PWD" \
  MODE=two-key IMAGE_DIGEST="$IMAGE_DIGEST" \
  EVIDENCE_DIR="/opt/nvidia-build-lb/evidence/live-two-key-$STAMP" smoke-live
```

Only after both matrices pass, run the helper-issued Hermes cycle. It performs
pre-issuance reconciliation, cutover, rollback, final reapply, previous-token
revocation, restart, and the real tool-using agent check under one lock:

```console
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
sudo /usr/bin/make -C "$PWD" \
  IMAGE_DIGEST="$IMAGE_DIGEST" \
  EVIDENCE_DIR="/opt/nvidia-build-lb/evidence/hermes-cycle-$STAMP" smoke-hermes
```

If that command fails or is interrupted, do not issue another token or edit a
Hermes file. Run only `sudo /usr/bin/python3
scripts/ops/hermes_cutover.py recover`, follow its safe `next_action`, and
repeat recovery until terminal. Retire backup generations only after the
receipt-required downstream and provider-side revocations; the infra stack
README contains the exact ID comparison and retirement commands.

Use only
filtered container inspection for UID, capability, health, labels, and mount
metadata. Never run unfiltered `env`, `printenv`, or `docker inspect`, and never
log HTTP headers or request bodies.

## Credential operations

Open `http://127.0.0.1:2456/admin` on the host and transfer the admin bearer with
the one-paste handoff above. The browser keeps it only for that tab. Add NVIDIA keys through the
admin API/UI; every new key must pass a probe before enable. Plaintext NVIDIA
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

## Ledger-capacity forward recovery

The canonical runtime file persists these Compose inputs across every wrapper
invocation and app recreation. Every wrapper invocation holds a shared lock on
the stable root-owned `/etc/nvidia-build-lb/runtime.env.lock` inode; updates and
capacity recovery hold it exclusively. The wrapper also compares the loaded
runtime-file SHA-256 immediately before replacement, so an out-of-contract
writer cannot be silently overwritten:

- `NBLB_ADMIN_EVENT_MAX_ROWS` → `NVIDIA_BUILD_LB_ADMIN_EVENT_MAX_ROWS`, allowed
  `1,000..1,000,000`, default `100,000`;
- `NBLB_ADMIN_ATTEMPT_MAX_ROWS` → `NVIDIA_BUILD_LB_ADMIN_ATTEMPT_MAX_ROWS`,
  allowed `100..400,000`, default `40,000`;
- `NBLB_ADMIN_LEDGER_PRUNE_BATCH_SIZE` →
  `NVIDIA_BUILD_LB_ADMIN_LEDGER_PRUNE_BATCH_SIZE`, allowed `6..5,000`, default
  `1,000`.

Every configuration must satisfy
`2 * NBLB_ADMIN_ATTEMPT_MAX_ROWS + 100 <= NBLB_ADMIN_EVENT_MAX_ROWS`; the prune
batch must also remain at least three times the public-attempt limit. Invalid
values fail before Compose or at app startup. A transient
`capacity_exhausted_recovering` incident may require a reviewed cap increase
after protected work settles and automatic cleanup cannot reopen intake. First
complete and verify a paired backup using `docs/BACKUP_RESTORE.md`, then choose
the smallest reviewed increase. The following single wrapper operation keeps the
candidate cap change, same-image recreation, bounded authenticated capacity
check, and runtime-file commit under one exclusive lock. It checks the app image
reference and image ID before and after recreation. The capacity check uses
host loopback and the root-owned canonical token file to read the authenticated
bounded `operator-readiness` endpoint with the canonical service `Host`. The
fixed host-owned `scripts/operator_readiness_probe.py` emits no payload or bearer, enforces a
two-second absolute request deadline and 2 MiB body cap, and validates the exact
four-field DTO plus runtime/readiness/ledger coherence. This `ledger-capacity` mode
never uses the legacy-overview fallback. It requires operational runtime
and nonblocked ledger state while accepting either `ready` or
`no_eligible_upstream`, so first-key setup remains available after capacity
recovery. A recreation, image, capacity, CAS, signal, or commit
failure stops `app` and leaves intake withdrawn; it never publishes the candidate
caps as a successful runtime generation. The same command may safely reenter a
stopped same-image app left by a prior failed recovery and force-recreate it from
the still-durable runtime generation plus the newly reviewed caps:

```console
set -e
set +x
export NBLB_RUNTIME_CONFIG_FILE=/etc/nvidia-build-lb/runtime.env
sudo env NBLB_RUNTIME_CONFIG_FILE="$NBLB_RUNTIME_CONFIG_FILE" \
  scripts/ops/production-compose.sh recover-ledger-capacity 200000 80000 2000
scripts/ops/production-compose.sh config --format json | jq -e \
  '.services.app.environment.NVIDIA_BUILD_LB_ADMIN_EVENT_MAX_ROWS == "200000" and
   .services.app.environment.NVIDIA_BUILD_LB_ADMIN_ATTEMPT_MAX_ROWS == "80000" and
   .services.app.environment.NVIDIA_BUILD_LB_ADMIN_LEDGER_PRUNE_BATCH_SIZE == "2000"' >/dev/null
```

Refresh the canonical admin dashboard and verify the shown event/attempt
capacities equal the configured values, maintenance has completed, and readiness
is no longer capacity-blocked. `orphaned_pending` and `legacy_unlinked` are
permanent evidence blockers, not cap incidents: preserve a paired backup, keep
intake withdrawn, and deploy a separately reviewed forward repair that preserves
receipt, event, and pin evidence. Never increase caps merely to hide either
blocker, delete retained rows manually, or downgrade the image or schema.

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
