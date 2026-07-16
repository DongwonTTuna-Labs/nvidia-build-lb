# Rollback

## Preconditions

Record the currently deployed application and PostgreSQL image digests, source
commit, backup `pair_id`, health response, and active Compose project before any
rollout. Keep at least one verified split backup and isolated restore receipt.
Never use a mutable tag as a rollback target.

The canonical runtime file and its stable root-owned `runtime.env.lock` must
already exist as described in `docs/RUNBOOK.md`. `update-app-digest` holds that
lock exclusively and compare-checks the loaded file before atomic replacement;
ordinary Compose reads hold it shared. The multi-command rollback procedure is
still an operator-controlled sequence, so exclude competing rollout operators
until all health checks finish rather than treating separate invocations as one
transaction.

## Application image rollback

If the prior application image is compatible with the current Alembic head:

1. Atomically replace only the app digest in the canonical runtime file with the
   reviewed prior registry digest's 64 lowercase hex characters after `@sha256:`.
2. Render Compose and confirm the database image, secret directory, volume, port,
   and project name are unchanged.
3. Run the migration job; it is idempotent and serialized.
4. Recreate only `app` after migration succeeds.
5. Verify health, models, non-streaming, streaming, admin list, downstream scope,
   and secret-safe logs. Verify `codex-lb` port 2455 before and after.

```console
set -Eeuo pipefail
set +x
: "${PRIOR_APP_REGISTRY_DIGEST:?set the reviewed prior 64-hex app digest}"
export NBLB_RUNTIME_CONFIG_FILE=/etc/nvidia-build-lb/runtime.env
ROLLBACK_APP_MUST_WITHDRAW=0
rollback_exit() {
  original_status=$?
  trap - EXIT HUP INT TERM
  set +e
  cleanup_status=0
  if [ "$ROLLBACK_APP_MUST_WITHDRAW" -eq 1 ]; then
    scripts/ops/production-compose.sh stop app >/dev/null 2>&1 || cleanup_status=1
  fi
  if [ "$original_status" -ne 0 ]; then
    [ "$cleanup_status" -eq 0 ] || printf '%s\n' rollback_candidate_withdraw_failed >&2
    exit "$original_status"
  fi
  [ "$cleanup_status" -eq 0 ] \
    || { printf '%s\n' rollback_candidate_withdraw_failed >&2; exit 1; }
  exit 0
}
trap rollback_exit EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
sudo env NBLB_RUNTIME_CONFIG_FILE="$NBLB_RUNTIME_CONFIG_FILE" \
  scripts/ops/production-compose.sh update-app-digest "$PRIOR_APP_REGISTRY_DIGEST"
scripts/ops/production-compose.sh config --quiet
scripts/ops/production-compose.sh run --rm migrate
ROLLBACK_APP_MUST_WITHDRAW=1
scripts/ops/production-compose.sh up -d --force-recreate --no-deps app
ROLLBACK_APP_CONTAINER="$(scripts/ops/production-compose.sh ps -q app)"
ROLLBACK_RUNTIME_READY=0
ROLLBACK_RUNTIME_ATTEMPT=0
while [ "$ROLLBACK_RUNTIME_ATTEMPT" -lt 30 ]; do
  ROLLBACK_RUNTIME_ATTEMPT=$((ROLLBACK_RUNTIME_ATTEMPT + 1))
  if sudo /usr/bin/python3 scripts/operator_readiness_probe.py \
    runtime 2456 /opt/nvidia-build-lb/secrets/admin_token \
    "$ROLLBACK_APP_CONTAINER" >/dev/null 2>&1; then
    ROLLBACK_RUNTIME_READY=1
    break
  fi
  sleep 1
done
[ "$ROLLBACK_RUNTIME_READY" -eq 1 ] \
  || { printf '%s\n' rollback_runtime_timeout >&2; exit 1; }
curl --fail http://127.0.0.1:2455/health
ROLLBACK_APP_MUST_WITHDRAW=0
trap - EXIT HUP INT TERM
```

The authenticated runtime probe sends the canonical service `Host` even when a
different host port is used. A current image must return the exact bounded
`operator-readiness` DTO. Only an `operator-readiness` `404` permits the exact
legacy-overview path; a legacy ready result passes immediately, while a
degraded/no-key result must remain exact on the same container ID and
`StartedAt` generation beyond the prior image's complete fail-stop budget.
Any timeout leaves operational evidence unconfirmed, stops the recreated
rollback candidate through the armed trap, and forbids the remaining rollout
steps. The `ledger-capacity` mode used by forward recovery has no legacy
fallback and additionally requires a nonblocked ledger.

Do not run Alembic downgrade against the live database as an incident shortcut.
The current migrations are additive, but future compatibility must be assessed
from the exact prior image and current schema before rollback.

## State rollback

State rollback is restore-and-cutover, never in-place overwrite:

1. Keep the live project stopped but intact after preserving fresh evidence.
2. Restore the selected database/key pair into a distinct labeled isolated
   project using `docs/BACKUP_RESTORE.md`.
3. Verify the pair receipt, application health, credential decryptability,
   downstream authentication, and scoped chat on the isolated port.
4. Only after an explicit deployment decision, switch the service to the
   verified restored volume/key pair as one controlled generation.
5. Keep the previous volume and key pair intact until post-cutover QA passes.

The provided restore script refuses the live database label and a nonempty
target. It does not implement the final production volume cutover; that host
mutation belongs to the separately reviewed `home-server-infra` rollout.

## Failed rollback

If image compatibility, pair verification, migration, health, or decryptability
fails, do not delete either generation and do not guess which key belongs to the
database. Leave the gateway unavailable or on the last known-good generation,
preserve safe logs/receipts, and diagnose the exact source/image/schema/pair
tuple. Existing `codex-lb` is outside this rollback and must not be restarted.
