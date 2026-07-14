# Backup and Isolated Restore

## Custody contract

A usable backup is a three-location tuple:

1. PostgreSQL custom-format `database.dump` in the database backup root.
2. The exact 32-byte `vault_master_key` in a separate key backup root.
3. `manifest.json` in a third root, binding both artifact hashes to the Alembic
   revision, upstream key IDs/fingerprints, and an aggregate of all downstream
   token IDs/digests.

Each generated directory is root-owned mode `0700`; each file is root-owned
mode `0600`. The manifest never contains plaintext keys, tokens, ciphertext, or
database passwords. Its downstream aggregate proves exact equality without
publishing token digests. Store the database and key roots in separate custody
domains. Losing either half makes recovery impossible.

## Quiesced backup

The backup contract is deliberately quiesced: stop the application first so
the dump and safe state oracle cannot diverge. PostgreSQL stays running. The
script refuses a running source app and refuses a database without the
`nvidia-build-lb.backup-source=true` label.

```console
set +x
export BACKUP_ID="backup-$(date -u +%Y%m%dt%H%M%Sz)"
export APP_CONTAINER="$(docker compose ps -q app)"
export DB_CONTAINER="$(docker compose ps -q db)"
docker compose stop app
sudo scripts/ops/backup.sh \
  --db-container "$DB_CONTAINER" \
  --app-container "$APP_CONTAINER" \
  --helper-image "$NBLB_APP_IMAGE" \
  --vault-key-file /opt/nvidia-build-lb/secrets/vault_master_key \
  --database-root /srv/nvidia-build-lb-backup/database \
  --key-root /srv/nvidia-build-lb-key-backup/key \
  --manifest-root /srv/nvidia-build-lb-manifest/manifest \
  --backup-id "$BACKUP_ID"
docker compose start app
curl --fail --header 'Host: 127.0.0.1:2456' http://127.0.0.1:2456/health
```

The stdout receipt contains only safe digests, counts, IDs, and `pair_id`.
Preserve it without changing either artifact.

## Mandatory isolated restore drill

Never restore over the live volume. Use a distinct Compose project, distinct
volume, alternate loopback port, and a database labeled
`nvidia-build-lb.restore-isolated=true`. Prepare a new root-only secret directory
with copies of the database password and admin token; do not precreate its vault
key.

```console
set +x
sudo install -d -o root -g root -m 0700 /opt/nvidia-build-lb/restore-secrets
sudo cp --no-preserve=mode,ownership /opt/nvidia-build-lb/secrets/db_password /opt/nvidia-build-lb/restore-secrets/db_password
sudo cp --no-preserve=mode,ownership /opt/nvidia-build-lb/secrets/admin_token /opt/nvidia-build-lb/restore-secrets/admin_token
sudo chown root:root /opt/nvidia-build-lb/restore-secrets/*
sudo chmod 0600 /opt/nvidia-build-lb/restore-secrets/*
export NBLB_SECRET_DIR=/opt/nvidia-build-lb/restore-secrets
export NBLB_RESTORE_ISOLATED=true
export NBLB_BACKUP_SOURCE=false
export NBLB_PORT=32458
docker compose -p nvidia-build-lb-restore-drill up -d db
export RESTORE_DB="$(docker compose -p nvidia-build-lb-restore-drill ps -q db)"
sudo scripts/ops/restore.sh \
  --db-container "$RESTORE_DB" \
  --helper-image "$NBLB_APP_IMAGE" \
  --database-directory "/srv/nvidia-build-lb-backup/database/$BACKUP_ID" \
  --key-directory "/srv/nvidia-build-lb-key-backup/key/$BACKUP_ID" \
  --manifest "/srv/nvidia-build-lb-manifest/manifest/$BACKUP_ID/manifest.json" \
  --target-secret-dir /opt/nvidia-build-lb/restore-secrets
docker compose -p nvidia-build-lb-restore-drill up -d migrate app
curl --fail --header 'Host: 127.0.0.1:2456' http://127.0.0.1:32458/health
```

Restore refuses a nonempty target database, a non-isolated label, mismatched
artifact hash, wrong key length, unsafe root artifact, unknown manifest field,
or any safe-state mismatch. The PASS receipt proves the same Alembic revision,
upstream key IDs/fingerprints, downstream digest aggregate, and vault key
fingerprint. The network-disabled one-shot key installer receives only
`DAC_OVERRIDE` so a Docker-group operator can atomically create the root-owned
mode-0600 file in the prepared directory; it has no database or network access.
After the drill:

```console
docker compose -p nvidia-build-lb-restore-drill down --volumes --remove-orphans
sudo rm -rf -- /opt/nvidia-build-lb/restore-secrets
```

`down --volumes` is permitted only for this explicitly isolated drill project,
never for the live project.
