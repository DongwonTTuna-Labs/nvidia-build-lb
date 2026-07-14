# Rollback

## Preconditions

Record the currently deployed application and PostgreSQL image digests, source
commit, backup `pair_id`, health response, and active Compose project before any
rollout. Keep at least one verified split backup and isolated restore receipt.
Never use a mutable tag as a rollback target.

## Application image rollback

If the prior application image is compatible with the current Alembic head:

1. Set `NBLB_APP_REGISTRY_DIGEST` to only the reviewed prior registry digest's
   64 lowercase hex characters after `@sha256:`.
2. Render Compose and confirm the database image, secret directory, volume, port,
   and project name are unchanged.
3. Run the migration job; it is idempotent and serialized.
4. Recreate only `app` after migration succeeds.
5. Verify health, models, non-streaming, streaming, admin list, downstream scope,
   and secret-safe logs. Verify `codex-lb` port 2455 before and after.

```console
scripts/ops/production-compose.sh config --quiet
scripts/ops/production-compose.sh run --rm migrate
scripts/ops/production-compose.sh up -d --force-recreate --no-deps app
curl --fail --header 'Host: 127.0.0.1:2456' http://127.0.0.1:2456/health
curl --fail http://127.0.0.1:2455/health
```

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
