#![forbid(unsafe_code)]
//! Actix gateway for the NVIDIA hosted API load balancer.

use actix_files::Files;
use actix_web::{
    App, HttpRequest, HttpResponse, HttpServer, Responder, guard, http::header,
    middleware::DefaultHeaders, web,
};
use anyhow::{Context, Result, anyhow, bail};
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use bytes::{Bytes, BytesMut};
use chrono::{Duration, Utc};
use futures_util::{Stream, StreamExt, stream};
use nvidia_build_lb_core::{
    DownstreamSummary, PROFILES, Router, Vault, VaultDownstreamRecord, VaultKeyRecord,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::{FromRow, PgPool, postgres::PgPoolOptions};
use std::{
    collections::{BTreeMap, HashMap, HashSet, VecDeque},
    env,
    pin::Pin,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration as StdDuration, Instant},
};
use uuid::Uuid;

const UPSTREAM_REQUEST_TIMEOUT: StdDuration = StdDuration::from_secs(60);
const UPSTREAM_CONNECT_TIMEOUT: StdDuration = StdDuration::from_secs(10);
const NVCF_POLL_TIMEOUT: StdDuration = StdDuration::from_secs(5);
const STREAM_PRIME_TIMEOUT: StdDuration = StdDuration::from_secs(10);

struct AppState {
    vault: VaultStore,
    router: Mutex<HashMap<String, Router>>,
    selection_lock: tokio::sync::Mutex<()>,
    client: reqwest::Client,
    admin_token: String,
    upstream_url: String,
    require_downstream_token: bool,
    public_port: u16,
    csp_hashes: Vec<String>,
}

#[derive(Debug, FromRow)]
struct DbKeyRow {
    id: Uuid,
    label: String,
    fingerprint: Vec<u8>,
    ciphertext: Vec<u8>,
    nonce: Vec<u8>,
    enabled: bool,
    verified: bool,
    retired: bool,
    cooldown_until: Option<chrono::DateTime<chrono::Utc>>,
    request_count: i64,
    failure_count: i64,
}

#[derive(Debug, FromRow)]
struct DbDownstreamRow {
    id: Uuid,
    label: String,
    digest: Vec<u8>,
    scopes: Vec<String>,
    active: bool,
    request_count: i64,
    last_used_at: Option<chrono::DateTime<chrono::Utc>>,
    created_at: chrono::DateTime<chrono::Utc>,
    revoked_at: Option<chrono::DateTime<chrono::Utc>>,
}

/// Owns the in-memory crypto boundary while PostgreSQL remains the durable
/// source of truth whenever a database is configured. The file vault is kept
/// as a local encrypted migration/rollback copy, never as the startup source
/// when database rows exist.
#[derive(Debug)]
struct VaultStore {
    vault: Mutex<Vault>,
    database: Option<PgPool>,
    sync_lock: tokio::sync::Mutex<()>,
    owner_id: Option<Uuid>,
}

impl VaultStore {
    async fn open(
        path: impl AsRef<std::path::Path>,
        master_key: [u8; 32],
        database: Option<PgPool>,
    ) -> Result<Self> {
        let path = path.as_ref().to_path_buf();
        let owner_id = if let Some(pool) = &database {
            let owner_id = Uuid::new_v4();
            let mut tx = pool.begin().await.context("begin gateway owner lease")?;
            sqlx::query("INSERT INTO nblb.gateway_instances (id) VALUES ($1)")
                .bind(owner_id)
                .execute(&mut *tx)
                .await
                .context("register gateway owner")?;
            // Only attempts whose owner lease has expired are abandoned. A
            // second process may still be streaming a live request, so a
            // blanket `finished_at IS NULL` update is unsafe.
            sqlx::query(
                "UPDATE nblb.request_attempts AS attempt SET outcome='abandoned_after_restart', finished_at=now() WHERE attempt.finished_at IS NULL AND attempt.owner_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM nblb.gateway_instances AS instance WHERE instance.id=attempt.owner_id AND instance.last_seen_at >= now() - interval '30 seconds')",
            )
            .execute(&mut *tx)
            .await
            .context("close attempts from expired gateway owners")?;
            sqlx::query(
                "DELETE FROM nblb.gateway_instances WHERE last_seen_at < now() - interval '30 seconds' AND id <> $1",
            )
            .bind(owner_id)
            .execute(&mut *tx)
            .await
            .context("prune expired gateway owners")?;
            tx.commit().await.context("commit gateway owner lease")?;
            Some(owner_id)
        } else {
            None
        };
        let vault = if let Some(pool) = &database {
            let keys = sqlx::query_as::<_, DbKeyRow>(
                "SELECT id, label, fingerprint, ciphertext, nonce, enabled, verified, retired, cooldown_until, request_count, failure_count FROM nblb.upstream_keys ORDER BY created_at, id",
            )
            .fetch_all(pool)
            .await
            .context("load upstream keys")?;
            let downstream = sqlx::query_as::<_, DbDownstreamRow>(
                "SELECT id, label, digest, scopes, active, request_count, last_used_at, created_at, revoked_at FROM nblb.downstream_credentials ORDER BY created_at, id",
            )
            .fetch_all(pool)
            .await
            .context("load downstream credentials")?;
            let rows = sqlx::query_as::<_, (String, i16)>(
                "SELECT profile_id, next_slot FROM nblb.routing_state ORDER BY profile_id",
            )
            .fetch_all(pool)
            .await
            .context("load routing cursors")?;
            let cursors = rows
                .into_iter()
                .map(|(profile, slot)| (profile, slot.saturating_sub(1) as usize))
                .collect::<BTreeMap<_, _>>();
            let file_vault = if keys.is_empty() && downstream.is_empty() {
                let file_vault = Vault::open(&path, master_key)?;
                if should_migrate_file_vault(
                    keys.len(),
                    downstream.len(),
                    file_vault.list().len(),
                    file_vault.list_downstream().len(),
                ) {
                    Some(file_vault)
                } else {
                    None
                }
            } else {
                None
            };
            if let Some(file_vault) = file_vault {
                // SQLx seeds routing_state rows during migration, so cursor
                // presence is not evidence that the database contains the
                // user's durable credentials.  Migrate a non-empty encrypted
                // file vault whenever both credential tables are empty.
                sync_database(pool, &file_vault).await?;
                file_vault
            } else {
                Vault::from_records_with_cursors(
                    &path,
                    master_key,
                    keys.into_iter().map(db_key_record).collect::<Result<_>>()?,
                    downstream
                        .into_iter()
                        .map(db_downstream_record)
                        .collect::<Result<_>>()?,
                    cursors,
                )?
            }
        } else {
            Vault::open(&path, master_key)?
        };
        Ok(Self {
            vault: Mutex::new(vault),
            database,
            sync_lock: tokio::sync::Mutex::new(()),
            owner_id,
        })
    }

    fn list(&self) -> Vec<nvidia_build_lb_core::KeySummary> {
        self.vault
            .lock()
            .map(|vault| vault.list())
            .unwrap_or_default()
    }

    fn list_downstream(&self) -> Vec<DownstreamSummary> {
        self.vault
            .lock()
            .map(|vault| vault.list_downstream())
            .unwrap_or_default()
    }

    fn credential(&self, id: Uuid) -> Result<String> {
        self.vault
            .lock()
            .map_err(|_| anyhow!("vault lock"))
            .and_then(|vault| vault.credential(id))
    }

    fn credential_for_probe(&self, id: Uuid) -> Result<String> {
        self.vault
            .lock()
            .map_err(|_| anyhow!("vault lock"))
            .and_then(|vault| {
                if vault.is_retired(id) {
                    bail!("retired upstream credential")
                }
                vault.credential(id)
            })
    }

    async fn set_cursor(&self, profile: &str, cursor: usize) -> Result<()> {
        let _sync_guard = self.sync_lock.lock().await;
        let previous = {
            let mut vault = self.vault.lock().map_err(|_| anyhow!("vault lock"))?;
            let previous = vault.clone();
            if let Err(error) = vault.set_router_cursor_for(profile, cursor) {
                *vault = previous.clone();
                return Err(error);
            }
            previous
        };
        if let Err(error) = self.sync_unlocked(Some(&previous)).await {
            let mut vault = self.vault.lock().map_err(|_| anyhow!("vault lock"))?;
            *vault = previous;
            vault.persist_for_rollback()?;
            return Err(error.context("rollback routing cursor after database sync failure"));
        }
        Ok(())
    }

    async fn mutate<F, T>(&self, operation: F) -> Result<T>
    where
        F: FnOnce(&mut Vault) -> Result<T>,
    {
        let _sync_guard = self.sync_lock.lock().await;
        let (previous, result) = {
            let mut vault = self.vault.lock().map_err(|_| anyhow!("vault lock"))?;
            let previous = vault.clone();
            let result = operation(&mut vault);
            (previous, result)
        };
        let result = match result {
            Ok(result) => result,
            Err(error) => {
                let mut vault = self.vault.lock().map_err(|_| anyhow!("vault lock"))?;
                *vault = previous;
                return Err(error);
            }
        };
        if let Err(error) = self.sync_unlocked(Some(&previous)).await {
            let mut vault = self.vault.lock().map_err(|_| anyhow!("vault lock"))?;
            *vault = previous;
            vault.persist_for_rollback()?;
            return Err(error.context("rollback vault mutation after database sync failure"));
        }
        Ok(result)
    }

    async fn authenticate(&self, token: &str, scope: &str) -> Result<DownstreamSummary> {
        self.mutate(|vault| vault.authenticate_downstream(token, scope))
            .await
    }

    async fn sync_unlocked(&self, previous: Option<&Vault>) -> Result<()> {
        let Some(pool) = &self.database else {
            return Ok(());
        };
        let (keys, downstream, cursors) = {
            let vault = self.vault.lock().map_err(|_| anyhow!("vault lock"))?;
            let cursors = PROFILES
                .iter()
                .map(|profile| ((*profile).to_owned(), vault.router_cursor_for(profile)))
                .collect::<BTreeMap<_, _>>();
            (vault.key_records()?, vault.downstream_records()?, cursors)
        };
        if let Some(previous) = previous {
            let previous_cursors = PROFILES
                .iter()
                .map(|profile| ((*profile).to_owned(), previous.router_cursor_for(profile)))
                .collect::<BTreeMap<_, _>>();
            sync_database_delta(
                pool,
                &previous.key_records()?,
                &previous.downstream_records()?,
                &previous_cursors,
                &keys,
                &downstream,
                &cursors,
            )
            .await
        } else {
            sync_database_rows(pool, &keys, &downstream, &cursors).await
        }
    }

    async fn evidence(&self) -> Result<Value> {
        if let Some(pool) = &self.database {
            let persisted_keys = sqlx::query_scalar::<_, i64>(
                "SELECT count(*) FROM nblb.upstream_keys WHERE retired = false",
            )
            .fetch_one(pool)
            .await?;
            let persisted_downstream =
                sqlx::query_scalar::<_, i64>("SELECT count(*) FROM nblb.downstream_credentials")
                    .fetch_one(pool)
                    .await?;
            let routing_profiles =
                sqlx::query_scalar::<_, i64>("SELECT count(*) FROM nblb.routing_state")
                    .fetch_one(pool)
                    .await?;
            let request_attempts =
                sqlx::query_scalar::<_, i64>("SELECT count(*) FROM nblb.request_attempts")
                    .fetch_one(pool)
                    .await?;
            return Ok(json!({
                "source_of_truth": "postgresql",
                "persisted_upstream_keys": persisted_keys,
                "persisted_downstream_credentials": persisted_downstream,
                "persisted_routing_profiles": routing_profiles,
                "persisted_request_attempts": request_attempts,
            }));
        }
        Ok(json!({
            "source_of_truth": "encrypted-file-fallback",
            "persisted_upstream_keys": self.list().len(),
            "persisted_downstream_credentials": self.list_downstream().len(),
            "persisted_routing_profiles": 0,
            "persisted_request_attempts": 0,
        }))
    }

    async fn attempt_started(&self, request_id: Uuid, profile: &str, key_id: Uuid) -> Result<()> {
        let Some(pool) = &self.database else {
            return Ok(());
        };
        sqlx::query(
            "INSERT INTO nblb.request_attempts (request_id, profile_id, key_id, owner_id, outcome) VALUES ($1,$2,$3,$4,'started')",
        )
        .bind(request_id)
        .bind(profile)
        .bind(key_id)
        .bind(self.owner_id)
        .execute(pool)
        .await
        .context("record request attempt")?;
        Ok(())
    }

    async fn attempt_finished(&self, request_id: Uuid, key_id: Uuid, outcome: &str) -> Result<()> {
        let Some(pool) = &self.database else {
            return Ok(());
        };
        let profile = sqlx::query_scalar::<_, String>(
            "SELECT profile_id FROM nblb.request_attempts WHERE request_id=$1 AND key_id=$2 AND owner_id=$3 AND finished_at IS NULL ORDER BY created_at DESC LIMIT 1",
        )
        .bind(request_id)
        .bind(key_id)
        .bind(self.owner_id)
        .fetch_optional(pool)
        .await
        .context("load request profile")?
        .ok_or_else(|| anyhow!("request attempt terminal row is missing"))?;
        let result = sqlx::query(
            "UPDATE nblb.request_attempts SET outcome=$3, finished_at=now() WHERE id = (SELECT id FROM nblb.request_attempts WHERE request_id=$1 AND key_id=$2 AND owner_id=$4 AND finished_at IS NULL ORDER BY created_at DESC LIMIT 1)",
        )
        .bind(request_id)
        .bind(key_id)
        .bind(outcome)
        .bind(self.owner_id)
        .execute(pool)
        .await
        .context("finish request attempt")?;
        if result.rows_affected() != 1 {
            bail!("request attempt terminal row is missing")
        }
        if outcome == "succeeded" {
            sqlx::query(
                "INSERT INTO nblb.profile_probe_receipts (profile_id, key_id) VALUES ($1, $2) ON CONFLICT (profile_id, key_id) DO UPDATE SET verified_at=now()",
            )
            .bind(profile)
            .bind(key_id)
            .execute(pool)
            .await
            .context("persist profile provider receipt")?;
        }
        Ok(())
    }

    async fn record_profile_proof(&self, profile: &str, key_id: Uuid) -> Result<()> {
        let Some(pool) = &self.database else {
            return Ok(());
        };
        sqlx::query(
            "INSERT INTO nblb.profile_probe_receipts (profile_id, key_id) VALUES ($1, $2) ON CONFLICT (profile_id, key_id) DO UPDATE SET verified_at=now()",
        )
        .bind(profile)
        .bind(key_id)
        .execute(pool)
        .await
        .context("persist profile provider receipt")?;
        Ok(())
    }

    async fn heartbeat_owner(&self) -> Result<()> {
        let Some(pool) = &self.database else {
            return Ok(());
        };
        let Some(owner_id) = self.owner_id else {
            return Ok(());
        };
        let result =
            sqlx::query("UPDATE nblb.gateway_instances SET last_seen_at=now() WHERE id=$1")
                .bind(owner_id)
                .execute(pool)
                .await
                .context("heartbeat gateway owner")?;
        if result.rows_affected() != 1 {
            bail!("gateway owner lease is missing")
        }
        Ok(())
    }

    async fn cleanup_stale_attempts(&self) -> Result<u64> {
        let Some(pool) = &self.database else {
            return Ok(0);
        };
        let result = sqlx::query(
            "UPDATE nblb.request_attempts AS attempt SET outcome='abandoned_after_restart', finished_at=now() WHERE attempt.finished_at IS NULL AND attempt.owner_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM nblb.gateway_instances AS instance WHERE instance.id=attempt.owner_id AND instance.last_seen_at >= now() - interval '30 seconds')",
        )
        .execute(pool)
        .await
        .context("close attempts from expired gateway owners")?;
        Ok(result.rows_affected())
    }
}

fn db_key_record(row: DbKeyRow) -> Result<VaultKeyRecord> {
    Ok(VaultKeyRecord {
        id: row.id,
        label: row.label,
        fingerprint: row.fingerprint,
        nonce: row.nonce,
        ciphertext: row.ciphertext,
        enabled: row.enabled,
        verified: row.verified,
        retired: row.retired,
        cooldown_until: row.cooldown_until,
        request_count: u64::try_from(row.request_count).context("invalid key request count")?,
        failure_count: u64::try_from(row.failure_count).context("invalid key failure count")?,
    })
}

fn db_downstream_record(row: DbDownstreamRow) -> Result<VaultDownstreamRecord> {
    Ok(VaultDownstreamRecord {
        id: row.id,
        label: row.label,
        scopes: row.scopes,
        token_digest: row.digest,
        active: row.active,
        request_count: u64::try_from(row.request_count)
            .context("invalid downstream request count")?,
        last_used_at: row.last_used_at,
        created_at: row.created_at,
        revoked_at: row.revoked_at,
    })
}

fn should_migrate_file_vault(
    database_keys: usize,
    database_downstream: usize,
    file_keys: usize,
    file_downstream: usize,
) -> bool {
    database_keys == 0 && database_downstream == 0 && (file_keys > 0 || file_downstream > 0)
}

async fn sync_database(pool: &PgPool, vault: &Vault) -> Result<()> {
    let cursors = PROFILES
        .iter()
        .map(|profile| ((*profile).to_owned(), vault.router_cursor_for(profile)))
        .collect::<BTreeMap<_, _>>();
    sync_database_rows(
        pool,
        &vault.key_records()?,
        &vault.downstream_records()?,
        &cursors,
    )
    .await
}

async fn sync_database_rows(
    pool: &PgPool,
    keys: &[VaultKeyRecord],
    downstream: &[VaultDownstreamRecord],
    cursors: &BTreeMap<String, usize>,
) -> Result<()> {
    let mut tx = pool.begin().await.context("begin vault sync")?;
    // Serialize full-snapshot reconciliation across gateway processes.  The
    // in-memory mutex protects one process; this transaction lock closes the
    // second-process stale-snapshot race without weakening the atomic commit.
    sqlx::query("SELECT pg_advisory_xact_lock(2147483647, 45291)")
        .execute(&mut *tx)
        .await
        .context("lock vault sync")?;
    // Upstream deletion and downstream revocation are durable state changes,
    // not row removal. Never delete rows from a possibly stale in-memory
    // snapshot: another gateway process may have created a credential while
    // this transaction waited for the advisory lock.
    for key in keys {
        sqlx::query(
            "INSERT INTO nblb.upstream_keys (id, label, fingerprint, ciphertext, nonce, enabled, verified, retired, cooldown_until, request_count, failure_count) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) ON CONFLICT (id) DO UPDATE SET label=EXCLUDED.label, fingerprint=EXCLUDED.fingerprint, ciphertext=EXCLUDED.ciphertext, nonce=EXCLUDED.nonce, enabled=EXCLUDED.enabled, verified=EXCLUDED.verified, retired=EXCLUDED.retired, cooldown_until=EXCLUDED.cooldown_until, request_count=EXCLUDED.request_count, failure_count=EXCLUDED.failure_count",
        )
        .bind(key.id)
        .bind(&key.label)
        .bind(&key.fingerprint)
        .bind(&key.ciphertext)
        .bind(&key.nonce)
        .bind(key.enabled)
        .bind(key.verified)
        .bind(key.retired)
        .bind(key.cooldown_until)
        .bind(i64::try_from(key.request_count).context("key request count overflow")?)
        .bind(i64::try_from(key.failure_count).context("key failure count overflow")?)
        .execute(&mut *tx)
        .await
        .context("persist upstream key")?;
    }
    for item in downstream {
        sqlx::query(
            "INSERT INTO nblb.downstream_credentials (id, label, digest, scopes, active, request_count, last_used_at, created_at, revoked_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT (id) DO UPDATE SET label=EXCLUDED.label, digest=EXCLUDED.digest, scopes=EXCLUDED.scopes, active=EXCLUDED.active, request_count=EXCLUDED.request_count, last_used_at=EXCLUDED.last_used_at, revoked_at=EXCLUDED.revoked_at",
        )
        .bind(item.id)
        .bind(&item.label)
        .bind(&item.token_digest)
        .bind(&item.scopes)
        .bind(item.active)
        .bind(i64::try_from(item.request_count).context("downstream request count overflow")?)
        .bind(item.last_used_at)
        .bind(item.created_at)
        .bind(item.revoked_at)
        .execute(&mut *tx)
        .await
        .context("persist downstream credential")?;
    }
    for profile in PROFILES {
        sqlx::query(
            "INSERT INTO nblb.routing_state (profile_id, next_slot, generation) VALUES ($1,$2,1) ON CONFLICT (profile_id) DO UPDATE SET next_slot=EXCLUDED.next_slot, generation=nblb.routing_state.generation + 1",
        )
        .bind(profile)
        .bind(i16::try_from(cursors.get(profile).copied().unwrap_or_default() % 2 + 1).context("routing cursor overflow")?)
        .execute(&mut *tx)
        .await
        .context("persist routing cursor")?;
    }
    tx.commit().await.context("commit vault sync")?;
    Ok(())
}

/// Applies only the fields changed by one local mutation.  A full in-memory
/// snapshot is unsafe when two gateway processes share PostgreSQL: the later
/// writer could restore an older enabled/verified/cooldown value or counter.
/// Each changed field is guarded by its previous value while the transaction
/// holds the advisory lock.  A concurrent change therefore fails closed and
/// lets the caller roll the local mutation back instead of overwriting the
/// fresher database state.
async fn sync_database_delta(
    pool: &PgPool,
    previous_keys: &[VaultKeyRecord],
    previous_downstream: &[VaultDownstreamRecord],
    previous_cursors: &BTreeMap<String, usize>,
    current_keys: &[VaultKeyRecord],
    current_downstream: &[VaultDownstreamRecord],
    current_cursors: &BTreeMap<String, usize>,
) -> Result<()> {
    let mut tx = pool.begin().await.context("begin incremental vault sync")?;
    sqlx::query("SELECT pg_advisory_xact_lock(2147483647, 45291)")
        .execute(&mut *tx)
        .await
        .context("lock incremental vault sync")?;

    let previous_keys = previous_keys
        .iter()
        .map(|key| (key.id, key))
        .collect::<HashMap<_, _>>();
    for current in current_keys {
        let Some(previous) = previous_keys.get(&current.id) else {
            let active_count = sqlx::query_scalar::<_, i64>(
                "SELECT count(*) FROM nblb.upstream_keys WHERE retired = false",
            )
            .fetch_one(&mut *tx)
            .await
            .context("count active upstream keys")?;
            if active_count >= nvidia_build_lb_core::MAX_UPSTREAM_KEYS as i64 {
                bail!("at most two upstream credentials are supported")
            }
            let result = sqlx::query(
                "INSERT INTO nblb.upstream_keys (id, label, fingerprint, ciphertext, nonce, enabled, verified, retired, cooldown_until, request_count, failure_count) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) ON CONFLICT (id) DO NOTHING",
            )
            .bind(current.id)
            .bind(&current.label)
            .bind(&current.fingerprint)
            .bind(&current.ciphertext)
            .bind(&current.nonce)
            .bind(current.enabled)
            .bind(current.verified)
            .bind(current.retired)
            .bind(current.cooldown_until)
            .bind(i64::try_from(current.request_count).context("key request count overflow")?)
            .bind(i64::try_from(current.failure_count).context("key failure count overflow")?)
            .execute(&mut *tx)
            .await
            .context("insert changed upstream key")?;
            if result.rows_affected() != 1 {
                bail!("concurrent upstream key insertion")
            }
            continue;
        };
        if current.enabled != previous.enabled {
            let result =
                sqlx::query("UPDATE nblb.upstream_keys SET enabled=$2 WHERE id=$1 AND enabled=$3")
                    .bind(current.id)
                    .bind(current.enabled)
                    .bind(previous.enabled)
                    .execute(&mut *tx)
                    .await
                    .context("update upstream enabled state")?;
            if result.rows_affected() != 1 {
                bail!("concurrent upstream enabled state")
            }
        }
        if current.verified != previous.verified {
            let result = sqlx::query(
                "UPDATE nblb.upstream_keys SET verified=$2 WHERE id=$1 AND verified=$3",
            )
            .bind(current.id)
            .bind(current.verified)
            .bind(previous.verified)
            .execute(&mut *tx)
            .await
            .context("update upstream verification state")?;
            if result.rows_affected() != 1 {
                bail!("concurrent upstream verification state")
            }
        }
        if current.retired != previous.retired {
            let result =
                sqlx::query("UPDATE nblb.upstream_keys SET retired=$2 WHERE id=$1 AND retired=$3")
                    .bind(current.id)
                    .bind(current.retired)
                    .bind(previous.retired)
                    .execute(&mut *tx)
                    .await
                    .context("update upstream retirement state")?;
            if result.rows_affected() != 1 {
                bail!("concurrent upstream retirement state")
            }
        }
        if current.cooldown_until != previous.cooldown_until {
            let result = sqlx::query(
                "UPDATE nblb.upstream_keys SET cooldown_until=$2 WHERE id=$1 AND cooldown_until IS NOT DISTINCT FROM $3",
            )
            .bind(current.id)
            .bind(current.cooldown_until)
            .bind(previous.cooldown_until)
            .execute(&mut *tx)
            .await
            .context("update upstream cooldown")?;
            if result.rows_affected() != 1 {
                bail!("concurrent upstream cooldown")
            }
        }
        if current.request_count != previous.request_count {
            let current_count =
                i64::try_from(current.request_count).context("key request count overflow")?;
            let previous_count =
                i64::try_from(previous.request_count).context("key request count overflow")?;
            let delta = current_count
                .checked_sub(previous_count)
                .context("key request count delta overflow")?;
            let result = sqlx::query(
                // Request counters are additive telemetry. Do not make a
                // successful provider response fail merely because another
                // gateway process incremented the same row first.
                "UPDATE nblb.upstream_keys SET request_count=GREATEST(0, request_count + $2) WHERE id=$1",
            )
            .bind(current.id)
            .bind(delta)
            .execute(&mut *tx)
            .await
            .context("update upstream request count")?;
            if result.rows_affected() != 1 {
                bail!("concurrent upstream request count")
            }
        }
        if current.failure_count != previous.failure_count {
            let current_count =
                i64::try_from(current.failure_count).context("key failure count overflow")?;
            let previous_count =
                i64::try_from(previous.failure_count).context("key failure count overflow")?;
            let delta = current_count
                .checked_sub(previous_count)
                .context("key failure count delta overflow")?;
            let result = sqlx::query(
                "UPDATE nblb.upstream_keys SET failure_count=GREATEST(0, failure_count + $2) WHERE id=$1",
            )
            .bind(current.id)
            .bind(delta)
            .execute(&mut *tx)
            .await
            .context("update upstream failure count")?;
            if result.rows_affected() != 1 {
                bail!("concurrent upstream failure count")
            }
        }
    }

    let previous_downstream = previous_downstream
        .iter()
        .map(|item| (item.id, item))
        .collect::<HashMap<_, _>>();
    for current in current_downstream {
        let Some(previous) = previous_downstream.get(&current.id) else {
            let result = sqlx::query(
                "INSERT INTO nblb.downstream_credentials (id, label, digest, scopes, active, request_count, last_used_at, created_at, revoked_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT (id) DO NOTHING",
            )
            .bind(current.id)
            .bind(&current.label)
            .bind(&current.token_digest)
            .bind(&current.scopes)
            .bind(current.active)
            .bind(i64::try_from(current.request_count).context("downstream request count overflow")?)
            .bind(current.last_used_at)
            .bind(current.created_at)
            .bind(current.revoked_at)
            .execute(&mut *tx)
            .await
            .context("insert changed downstream credential")?;
            if result.rows_affected() != 1 {
                bail!("concurrent downstream credential insertion")
            }
            continue;
        };
        if current.active != previous.active {
            let result = sqlx::query(
                "UPDATE nblb.downstream_credentials SET active=$2 WHERE id=$1 AND active=$3",
            )
            .bind(current.id)
            .bind(current.active)
            .bind(previous.active)
            .execute(&mut *tx)
            .await
            .context("update downstream active state")?;
            if result.rows_affected() != 1 {
                bail!("concurrent downstream active state")
            }
        }
        if current.revoked_at != previous.revoked_at {
            let result = sqlx::query(
                "UPDATE nblb.downstream_credentials SET revoked_at=$2 WHERE id=$1 AND revoked_at IS NOT DISTINCT FROM $3",
            )
            .bind(current.id)
            .bind(current.revoked_at)
            .bind(previous.revoked_at)
            .execute(&mut *tx)
            .await
            .context("update downstream revocation")?;
            if result.rows_affected() != 1 {
                bail!("concurrent downstream revocation")
            }
        }
        if current.request_count != previous.request_count {
            let current_count = i64::try_from(current.request_count)
                .context("downstream request count overflow")?;
            let previous_count = i64::try_from(previous.request_count)
                .context("downstream request count overflow")?;
            let delta = current_count
                .checked_sub(previous_count)
                .context("downstream request count delta overflow")?;
            let result = sqlx::query(
                "UPDATE nblb.downstream_credentials SET request_count=GREATEST(0, request_count + $2) WHERE id=$1",
            )
            .bind(current.id)
            .bind(delta)
            .execute(&mut *tx)
            .await
            .context("update downstream request count")?;
            if result.rows_affected() != 1 {
                bail!("concurrent downstream request count")
            }
        }
        if current.last_used_at != previous.last_used_at {
            let result = sqlx::query(
                "UPDATE nblb.downstream_credentials SET last_used_at=GREATEST(COALESCE(last_used_at, $2), $2) WHERE id=$1",
            )
            .bind(current.id)
            .bind(current.last_used_at)
            .execute(&mut *tx)
            .await
            .context("update downstream last-used timestamp")?;
            if result.rows_affected() != 1 {
                bail!("concurrent downstream last-used timestamp")
            }
        }
    }

    for profile in PROFILES {
        let previous = previous_cursors.get(profile).copied().unwrap_or_default();
        let current = current_cursors.get(profile).copied().unwrap_or_default();
        if previous.wrapping_sub(current) % 2 == 0 {
            continue;
        }
        let result = sqlx::query(
            // The advisory transaction lock serializes all cursor advances.
            // Toggle the current database value rather than requiring the
            // caller's possibly stale in-memory slot to still match it.
            "UPDATE nblb.routing_state SET next_slot=CASE WHEN next_slot=1 THEN 2 ELSE 1 END, generation=generation+1 WHERE profile_id=$1",
        )
        .bind(profile)
        .execute(&mut *tx)
        .await
        .context("update routing cursor")?;
        if result.rows_affected() != 1 {
            bail!("concurrent routing cursor")
        }
    }
    tx.commit().await.context("commit incremental vault sync")?;
    Ok(())
}

#[derive(Debug, Deserialize)]
struct KeyInput {
    label: String,
    credential: String,
}

#[derive(Debug, Deserialize)]
struct ToggleInput {
    enabled: bool,
}

#[derive(Debug, Deserialize)]
struct DownstreamInput {
    label: String,
    scopes: Vec<String>,
}

#[derive(Debug, Serialize)]
struct Health {
    status: &'static str,
    ready: bool,
    traffic_ready: bool,
    eligible_keys: usize,
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    // The container listener is an internal contract.  The published host
    // port is used only for Host/Origin validation and must never move the
    // listener away from the compose target port.
    let port = match env::var("NVIDIA_BUILD_LB_BIND_PORT") {
        Ok(value) => value.parse::<u16>().map_err(|error| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                format!("invalid NVIDIA_BUILD_LB_BIND_PORT: {error}"),
            )
        })?,
        Err(env::VarError::NotPresent) => 2456,
        Err(error) => {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                format!("cannot read NVIDIA_BUILD_LB_BIND_PORT: {error}"),
            ));
        }
    };
    let state = web::Data::new(build_state().await.map_err(|error| {
        eprintln!("gateway configuration failed: {error:#}");
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "gateway configuration failed",
        )
    })?);
    let script_hashes = state
        .csp_hashes
        .iter()
        .map(|hash| format!(" 'sha256-{hash}'"))
        .collect::<String>();
    let content_security_policy = format!(
        "default-src 'self'; script-src 'self'{script_hashes}; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
    );
    spawn_database_watchdog(state.clone());
    HttpServer::new(move || {
        App::new()
            .wrap(
                DefaultHeaders::new()
                    .add((header::CACHE_CONTROL, "no-store"))
                    .add((
                        header::CONTENT_SECURITY_POLICY,
                        content_security_policy.clone(),
                    ))
                    .add((header::REFERRER_POLICY, "no-referrer"))
                    .add((header::X_CONTENT_TYPE_OPTIONS, "nosniff")),
            )
            .app_data(state.clone())
            .app_data(web::PayloadConfig::new(64 * 1024 * 1024))
            .configure(routes)
    })
    .bind(("0.0.0.0", port))?
    .run()
    .await
}

fn spawn_database_watchdog(state: web::Data<AppState>) {
    let Some(pool) = state.vault.database.clone() else {
        return;
    };
    tokio::spawn(async move {
        let mut failed_since = None;
        loop {
            tokio::time::sleep(StdDuration::from_secs(2)).await;
            if let Err(error) = state.vault.heartbeat_owner().await {
                eprintln!("postgres owner lease heartbeat failed: {error:#}");
                std::process::exit(1);
            }
            if let Err(error) = state.vault.cleanup_stale_attempts().await {
                eprintln!("postgres stale attempt cleanup failed: {error:#}");
                std::process::exit(1);
            }
            let healthy = sqlx::query_scalar::<_, i32>("SELECT 1")
                .fetch_one(&pool)
                .await
                .is_ok();
            if healthy {
                failed_since = None;
                continue;
            }
            let first_failure = failed_since.get_or_insert_with(Instant::now);
            if first_failure.elapsed() >= StdDuration::from_secs(2) {
                eprintln!("postgres readiness lost; exiting for supervisor restart");
                std::process::exit(1);
            }
        }
    });
}

fn routes(cfg: &mut web::ServiceConfig) {
    cfg.route("/health", web::get().to(health))
        .route(
            "/admin/api/v1/operator-readiness",
            web::get().to(operator_readiness),
        )
        .route("/v1/models", web::get().to(models))
        .route("/v1/chat/completions", web::post().to(chat_completions))
        .route("/v1/embeddings", web::post().to(multimodal))
        .route("/v1/images/generations", web::post().to(multimodal))
        .route("/v1/videos/generations", web::post().to(multimodal))
        .route("/v1/audio/speech", web::post().to(multimodal))
        .route("/v1/audio/transcriptions", web::post().to(multimodal))
        .route("/v1/nvidia/inference", web::post().to(multimodal))
        .route("/admin/api/v1/upstream-keys", web::get().to(list_keys))
        .route("/admin/api/v1/overview", web::get().to(overview))
        .route("/admin/api/v1/evidence", web::get().to(evidence))
        .route(
            "/admin/api/v1/upstream-slots",
            web::get().to(upstream_slots),
        )
        .route(
            "/admin/api/v1/model-capabilities",
            web::get().to(model_capabilities),
        )
        .route(
            "/admin/api/v1/generation-readiness",
            web::get().to(generation_readiness),
        )
        .route("/admin/api/v1/operations", web::get().to(operations))
        .route(
            "/admin/api/v1/operations/{id}",
            web::get().to(operation_detail),
        )
        .route("/admin/api/v1/attentions", web::get().to(attentions))
        .route("/admin/api/v1/events", web::get().to(events))
        .route("/admin/api/v1/events/{id}", web::get().to(event_detail))
        .route(
            "/admin/api/v1/evidence/{id}",
            web::get().to(evidence_detail),
        )
        .route("/admin/api/v1/upstream-keys", web::post().to(add_key))
        .route(
            "/admin/api/v1/upstream-keys/{id}",
            web::delete().to(delete_key),
        )
        .route(
            "/admin/api/v1/upstream-keys/{id}/state",
            web::post().to(toggle_key),
        )
        .route(
            "/admin/api/v1/upstream-keys/{id}/enable",
            web::post().to(enable_key_alias),
        )
        .route(
            "/admin/api/v1/upstream-keys/{id}/disable",
            web::post().to(disable_key_alias),
        )
        .route(
            "/admin/api/v1/upstream-keys/{id}/probe",
            web::post().to(probe_key),
        )
        .route(
            "/admin/api/v1/downstream-credentials",
            web::get().to(list_downstream),
        )
        .route(
            "/admin/api/v1/downstream-credentials",
            web::post().to(add_downstream),
        )
        .route(
            "/admin/api/v1/downstream-credentials/{id}/revoke",
            web::post().to(revoke_downstream),
        )
        // Compatibility aliases retained for existing operators while the
        // canonical resource name is downstream-credentials.
        .route(
            "/admin/api/v1/downstream-tokens",
            web::get().to(list_downstream),
        )
        .route(
            "/admin/api/v1/downstream-tokens",
            web::post().to(add_downstream),
        )
        .route(
            "/admin/api/v1/downstream-tokens/{id}",
            web::delete().to(revoke_downstream_legacy),
        )
        .service(
            Files::new("/admin", "/app/static")
                .index_file("index.html")
                .guard(guard::fn_guard(|context| {
                    let mut hosts = context.head().headers.get_all(header::HOST);
                    let Some(host) = hosts.next().and_then(|value| value.to_str().ok()) else {
                        return false;
                    };
                    hosts.next().is_none() && admin_host_allowed(host)
                })),
        );
}

async fn build_state() -> Result<AppState> {
    let master_key = read_master_key()?;
    let admin_token = read_required_secret(
        "NVIDIA_BUILD_LB_ADMIN_TOKEN",
        "/run/nvidia-build-lb/secrets/admin_token",
    )?;
    let upstream_url = env::var("NBLB_UPSTREAM_URL")
        .unwrap_or_else(|_| "https://integrate.api.nvidia.com/v1/chat/completions".to_owned());
    validate_admin_token(&admin_token, upstream_url.starts_with("mock://"))?;
    let path = env::var("NBLB_VAULT_PATH")
        .unwrap_or_else(|_| "/var/lib/nvidia-build-lb/vault.json".to_owned());
    if let Some(parent) = std::path::Path::new(&path).parent() {
        std::fs::create_dir_all(parent).context("create vault directory")?;
    }
    let require_downstream_token =
        env::var("NBLB_REQUIRE_DOWNSTREAM_TOKEN").ok().as_deref() != Some("0");
    let database = if let Ok(url) = env::var("NBLB_DATABASE_URL") {
        let url = database_url_with_password(url)?;
        let pool = PgPoolOptions::new()
            .max_connections(5)
            .connect(&url)
            .await
            .context("connect PostgreSQL")?;
        seed_existing_routing_profiles(&pool).await?;
        sqlx::migrate!("../../migrations/sqlx")
            .run(&pool)
            .await
            .context("run SQLx migrations")?;
        Some(pool)
    } else {
        None
    };
    let vault = VaultStore::open(path, master_key, database.clone()).await?;
    let routers = PROFILES
        .iter()
        .map(|profile| {
            (
                (*profile).to_owned(),
                Router::with_next_slot(
                    vault
                        .vault
                        .lock()
                        .map(|inner| inner.router_cursor_for(profile))
                        .unwrap_or_default(),
                ),
            )
        })
        .collect::<HashMap<_, _>>();
    // The shared client must not have a total timeout: a valid SSE completion
    // may run longer than the ordinary request budget. Individual
    // non-stream requests and probe/poll calls apply bounded timeouts below.
    let mut client_builder = reqwest::Client::builder()
        .connect_timeout(UPSTREAM_CONNECT_TIMEOUT)
        // A provider must never receive a bearer token on an implicit
        // cross-origin redirect.  Endpoint aliases are explicit in
        // `upstream_endpoint_for`, so redirects are not part of the contract.
        .redirect(reqwest::redirect::Policy::none());
    if let Ok(ca_path) = env::var("NBLB_UPSTREAM_CA_FILE") {
        let certificate = reqwest::Certificate::from_pem(
            &std::fs::read(&ca_path).with_context(|| format!("read upstream CA: {ca_path}"))?,
        )
        .context("parse upstream CA")?;
        client_builder = client_builder.add_root_certificate(certificate);
    }
    Ok(AppState {
        vault,
        router: Mutex::new(routers),
        selection_lock: tokio::sync::Mutex::new(()),
        client: client_builder.build().context("http client")?,
        admin_token,
        upstream_url,
        require_downstream_token,
        public_port: env::var("NVIDIA_BUILD_LB_PUBLIC_PORT")
            .ok()
            .and_then(|value| value.parse().ok())
            .unwrap_or(2456),
        csp_hashes: static_script_hashes("/app/static"),
    })
}

async fn seed_existing_routing_profiles(pool: &PgPool) -> Result<()> {
    let table_exists =
        sqlx::query_scalar::<_, bool>("SELECT to_regclass('nblb.routing_state') IS NOT NULL")
            .fetch_one(pool)
            .await
            .context("inspect routing state before migrations")?;
    if !table_exists {
        return Ok(());
    }
    // The 0001 CHECK predates Parakeet; 0004 widens it and 0009 seeds that
    // eighth profile after migrations have run.
    sqlx::query(
        "INSERT INTO nblb.routing_state (profile_id, next_slot, generation) VALUES ('z-ai/glm-5.2',1,0), ('microsoft/phi-4-multimodal-instruct',1,0), ('nvidia/vila',1,0), ('nvidia/nvclip',1,0), ('black-forest-labs/flux.1-kontext-dev',1,0), ('stabilityai/stable-video-diffusion',1,0), ('nvidia/magpie-tts-multilingual',1,0) ON CONFLICT (profile_id) DO NOTHING",
    )
    .execute(pool)
    .await
    .context("seed existing routing profiles before migrations")?;
    Ok(())
}

fn static_script_hashes(root: &str) -> Vec<String> {
    ["index.html"]
        .into_iter()
        .filter_map(|name| std::fs::read_to_string(std::path::Path::new(root).join(name)).ok())
        .flat_map(|html| inline_script_bodies(&html))
        .map(|body| {
            use base64::{Engine as _, engine::general_purpose::STANDARD};
            use sha2::{Digest, Sha256};
            STANDARD.encode(Sha256::digest(body.as_bytes()))
        })
        .collect()
}

fn inline_script_bodies(html: &str) -> Vec<String> {
    let mut bodies = Vec::new();
    let mut cursor = html;
    while let Some(start) = cursor.find("<script>") {
        let body_start = start + "<script>".len();
        let Some(end) = cursor[body_start..].find("</script>") else {
            break;
        };
        bodies.push(cursor[body_start..body_start + end].to_owned());
        cursor = &cursor[body_start + end + "</script>".len()..];
    }
    bodies
}

fn database_url_with_password(url: String) -> Result<String> {
    let mut url = url;
    let needs_password = {
        let userinfo = url
            .split_once("://")
            .and_then(|(_, rest)| rest.split_once('@').map(|(user, _)| user));
        let has_password = userinfo
            .and_then(|user| user.rsplit_once(':'))
            .is_some_and(|(_, password)| !password.is_empty());
        userinfo.is_some() && !has_password
    };
    if needs_password
        && let Ok(password) = read_required_secret(
            "NBLB_DATABASE_PASSWORD",
            "/run/nvidia-build-lb/secrets/db_password",
        )
    {
        url = url.replacen("@", &format!(":{}@", percent_encode_userinfo(&password)), 1);
    }
    if !url
        .split_once('?')
        .is_some_and(|(_, query)| query.split('&').any(|part| part.starts_with("sslmode=")))
    {
        url.push(if url.contains('?') { '&' } else { '?' });
        url.push_str("sslmode=disable");
    }
    Ok(url)
}

fn percent_encode_userinfo(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len());
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            encoded.push(char::from(byte));
        } else {
            encoded.push('%');
            encoded.push_str(&format!("{byte:02X}"));
        }
    }
    encoded
}

fn validate_admin_token(value: &str, allow_test_token: bool) -> Result<()> {
    let valid_production = value.len() == 75
        && value.starts_with("nblb_admin_")
        && value[11..].bytes().all(|byte| byte.is_ascii_hexdigit());
    if valid_production || (allow_test_token && !value.is_empty()) {
        Ok(())
    } else {
        bail!("admin token must be nblb_admin_ followed by 64 hexadecimal characters")
    }
}

fn read_master_key() -> Result<[u8; 32]> {
    let raw = if let Ok(value) = env::var("NBLB_VAULT_MASTER_KEY") {
        value.into_bytes()
    } else {
        std::fs::read("/run/nvidia-build-lb/secrets/vault_master_key")
            .context("required secret is missing: NBLB_VAULT_MASTER_KEY")?
    };
    if raw.len() == 32 {
        return raw
            .try_into()
            .map_err(|_| anyhow!("invalid vault master key"));
    }
    let text = std::str::from_utf8(&raw)
        .context("vault master key encoding")?
        .trim();
    if text.len() == 64 {
        return hex::decode(text)
            .context("decode vault master key")?
            .try_into()
            .map_err(|_| anyhow!("invalid vault master key"));
    }
    base64::Engine::decode(&base64::engine::general_purpose::URL_SAFE_NO_PAD, text)
        .context("decode vault master key")?
        .try_into()
        .map_err(|_| anyhow!("vault master key must be exactly 32 bytes"))
}

fn read_required_secret(env_name: &str, path: &str) -> Result<String> {
    let fallback = std::path::Path::new(path).file_name().and_then(|name| {
        std::fs::read_to_string(path).ok().or_else(|| {
            std::fs::read_to_string(std::path::Path::new("/run/canonical-secrets").join(name)).ok()
        })
    });
    let value = env::var(env_name)
        .ok()
        .or_else(|| fallback.map(|item| item.trim_end_matches(['\r', '\n']).to_owned()))
        .ok_or_else(|| anyhow!("required secret is missing: {env_name}"))?;
    if value.is_empty() {
        bail!("required secret is empty: {env_name}")
    }
    Ok(value)
}

fn public_guard_response(req: &HttpRequest) -> Option<HttpResponse> {
    let mut hosts = req.headers().get_all(header::HOST);
    let raw_host = hosts.next().and_then(|value| value.to_str().ok());
    if hosts.next().is_some() {
        return Some(HttpResponse::Forbidden().json(json!({
            "error": {"message": "The request host is not allowed.", "type": "permission_error", "code": "host_forbidden"}
        })));
    }
    let Some(raw_host) = raw_host else {
        return Some(HttpResponse::Forbidden().json(json!({
            "error": {"message": "The request host is not allowed.", "type": "permission_error", "code": "host_forbidden"}
        })));
    };
    if raw_host.contains(',') || raw_host.is_empty() || !host_authority_well_formed(raw_host) {
        return Some(HttpResponse::Forbidden().json(json!({
            "error": {"message": "The request host is not allowed.", "type": "permission_error", "code": "host_forbidden"}
        })));
    }
    let (host, port) = parse_host_authority(raw_host);
    let local = matches!(host.as_str(), "127.0.0.1" | "localhost" | "::1");
    let public = host == "nvidia-lb.dongwontuna.net";
    let local_port = env::var("NVIDIA_BUILD_LB_PUBLIC_PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(2456);
    let authority_ok =
        (local && port == Some(local_port)) || (public && port.is_none_or(|value| value == 443));
    if !authority_ok {
        return Some(HttpResponse::Forbidden().json(json!({
            "error": {"message": "The request host is not allowed.", "type": "permission_error", "code": "host_forbidden"}
        })));
    }
    if req.headers().contains_key(header::ORIGIN) {
        return Some(HttpResponse::Forbidden().json(json!({
            "error": {"message": "Cross-origin requests are not allowed.", "type": "permission_error", "code": "origin_forbidden"}
        })));
    }
    None
}

async fn health(req: HttpRequest, state: web::Data<AppState>) -> impl Responder {
    if let Some(response) = public_guard_response(&req) {
        return response;
    }
    let database_ready = match &state.vault.database {
        Some(pool) => sqlx::query_scalar::<_, i32>("SELECT 1")
            .fetch_one(pool)
            .await
            .is_ok(),
        None => true,
    };
    let keys = state.vault.list();
    let eligible_keys = eligible_key_count(&keys);
    let ready = database_ready && keys.len() == nvidia_build_lb_core::MAX_UPSTREAM_KEYS;
    // One healthy upstream can still serve traffic; the second slot is the
    // failover/distribution objective, not a hard availability requirement.
    let traffic_ready = database_ready && eligible_keys > 0;
    let mut response = if traffic_ready {
        HttpResponse::Ok()
    } else {
        HttpResponse::ServiceUnavailable()
    };
    response
        .insert_header(("cache-control", "no-store"))
        .json(Health {
            status: if traffic_ready { "ok" } else { "degraded" },
            ready,
            traffic_ready,
            eligible_keys,
        })
}

async fn operator_readiness(req: HttpRequest, state: web::Data<AppState>) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized();
    }
    let database_ready = match &state.vault.database {
        Some(pool) => sqlx::query_scalar::<_, i32>("SELECT 1")
            .fetch_one(pool)
            .await
            .is_ok(),
        None => true,
    };
    if !database_ready {
        return HttpResponse::ServiceUnavailable().json(json!({
            "runtime_state": "unavailable",
            "readiness_cause": "runtime_unavailable",
            "ledger_status": "capacity_blocked",
            "capacity_blocker": "orphaned_pending"
        }));
    }
    let eligible = eligible_key_count(&state.vault.list());
    HttpResponse::Ok()
        .insert_header(("cache-control", "no-store"))
        .json(json!({
            "runtime_state": "operational",
            "readiness_cause": if eligible > 0 { "ready" } else { "no_eligible_upstream" },
            "ledger_status": "ok",
            "capacity_blocker": "none"
        }))
}

/// Returns the durable profile/key provider receipts.  A successful key-level
/// probe alone must never make every modality appear ready.
async fn profile_proof_keys(state: &AppState) -> Result<HashMap<String, HashSet<Uuid>>> {
    let Some(pool) = &state.vault.database else {
        return Ok(HashMap::new());
    };
    let rows = sqlx::query_as::<_, (String, Uuid)>(
        "SELECT profile_id, key_id FROM nblb.profile_probe_receipts",
    )
    .fetch_all(pool)
    .await
    .context("load profile provider receipts")?;
    let mut proofs = HashMap::new();
    for (profile, key_id) in rows {
        proofs
            .entry(profile)
            .or_insert_with(HashSet::new)
            .insert(key_id);
    }
    Ok(proofs)
}

async fn models(req: HttpRequest, state: web::Data<AppState>) -> impl Responder {
    if let Some(response) = public_guard_response(&req) {
        return response;
    }
    if let Err(response) = authorize_scope(&req, &state, "models:read").await {
        return response;
    }
    let keys = state.vault.list();
    let eligible = keys
        .iter()
        .filter(|key| {
            key.enabled
                && key.verified
                && key.cooldown_until.is_none_or(|until| until <= Utc::now())
        })
        .map(|key| key.id)
        .collect::<HashSet<_>>();
    let proofs = match profile_proof_keys(&state).await {
        Ok(proofs) => proofs,
        Err(_) => {
            return HttpResponse::ServiceUnavailable()
                .json(json!({"error":{"code":"capability_state_unavailable"}}));
        }
    };
    let data: Vec<Value> = PROFILES
        .iter()
        .filter(|id| {
            proofs
                .get(**id)
                .is_some_and(|key_ids| key_ids.iter().any(|key_id| eligible.contains(key_id)))
        })
        .map(|id| json!({"id": id, "object": "model", "owned_by": "nvidia", "created": 1784332800}))
        .collect();
    HttpResponse::Ok().json(json!({"object":"list","data":data}))
}

async fn list_keys(req: HttpRequest, state: web::Data<AppState>) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized();
    }
    let items = state.vault.list();
    HttpResponse::Ok().json(json!({"items": items}))
}

async fn overview(req: HttpRequest, state: web::Data<AppState>) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized();
    }
    let keys = state.vault.list();
    let downstream = state.vault.list_downstream();
    let eligible_keys = eligible_key_count(&keys);
    let database_ready = match &state.vault.database {
        Some(pool) => sqlx::query_scalar::<_, i32>("SELECT 1")
            .fetch_one(pool)
            .await
            .is_ok(),
        None => true,
    };
    let evidence = state
        .vault
        .evidence()
        .await
        .unwrap_or_else(|_| json!({"source_of_truth": "unavailable"}));
    let recommendation = if keys.is_empty() {
        json!({"action":"add_upstream_key","label":"첫 번째 키 구성","route":"routing","reason":"두 개의 NVIDIA 키를 저장해야 요청을 받을 수 있습니다."})
    } else if keys.iter().any(|key| !key.verified) {
        json!({"action":"probe_upstream_key","label":"제공자 검증","route":"routing","reason":"저장된 키는 제공자 검증을 통과해야 라우팅에 포함할 수 있습니다."})
    } else if eligible_keys == 0 {
        json!({"action":"inspect_routing","label":"라우팅 상태 확인","route":"routing","reason":"현재 요청 가능한 키가 없어 cooldown·중지 원인을 확인해야 합니다."})
    } else if keys.len() < nvidia_build_lb_core::MAX_UPSTREAM_KEYS {
        json!({"action":"add_upstream_key","label":"두 번째 키 구성","route":"routing","reason":"두 번째 키를 추가하면 rate-aware 분산과 장애 전환을 확인할 수 있습니다."})
    } else {
        json!({"action":"monitor","label":"현재 상태 확인","route":"evidence","reason":"두 슬롯이 구성되어 요청을 받을 수 있습니다."})
    };
    let attentions = keys
        .iter()
        .filter(|key| !key.enabled || !key.verified || key.cooldown_until.is_some())
        .map(|key| {
            json!({
                "code": if key.cooldown_until.is_some() { "upstream_cooldown" } else if !key.verified { "probe_required" } else { "upstream_disabled" },
                "resource": {"kind":"upstream_key","id":key.id},
                "label": key.label,
                "next_action": if !key.verified { "probe" } else { "inspect_routing" },
                "expires_at": key.cooldown_until,
            })
        })
        .collect::<Vec<_>>();
    let checks = keys
        .iter()
        .map(|key| {
            json!({
                "kind":"upstream_key",
                "id":key.id,
                "label":key.label,
                "status": if key.enabled && key.verified && key.cooldown_until.is_none() { "eligible" } else if key.cooldown_until.is_some() { "cooldown" } else if key.verified { "disabled" } else { "probe_required" },
                "failure_count":key.failure_count,
                "request_count":key.request_count,
            })
        })
        .collect::<Vec<_>>();
    HttpResponse::Ok().insert_header(("cache-control", "no-store")).json(json!({
        "runtime": {"status": if database_ready && eligible_keys > 0 { "ok" } else { "degraded" }, "ready": database_ready && keys.len() == nvidia_build_lb_core::MAX_UPSTREAM_KEYS, "traffic_ready": database_ready && eligible_keys > 0, "eligible_keys": if database_ready { eligible_keys } else { 0 }},
        "upstream_keys": {"items": keys},
        "downstream_credentials": {"items": downstream},
        "models": PROFILES,
        "evidence": evidence,
        "server_recommendation": recommendation,
        "attentions": attentions,
        "checks": checks,
        "public_health": {"hostname":"nvidia-lb.dongwontuna.net","status":"not_verified","next_action":"verify_public_route"},
        "snapshot_observed_at": Utc::now(),
    }))
}

async fn evidence(req: HttpRequest, state: web::Data<AppState>) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized();
    }
    match state.vault.evidence().await {
        Ok(value) => HttpResponse::Ok()
            .insert_header(("cache-control", "no-store"))
            .json(value),
        Err(_) => HttpResponse::ServiceUnavailable()
            .json(json!({"error":{"code":"evidence_unavailable"}})),
    }
}

#[derive(Debug, Deserialize)]
struct PageQuery {
    before: Option<String>,
    limit: Option<u16>,
}

#[derive(Debug, Deserialize, Serialize)]
struct PageCursor {
    created_at: chrono::DateTime<Utc>,
    id: Uuid,
}

fn encode_page_cursor(cursor: &PageCursor) -> Result<String> {
    Ok(URL_SAFE_NO_PAD.encode(serde_json::to_vec(cursor)?))
}

fn page_before(query: &PageQuery) -> Result<Option<PageCursor>, HttpResponse> {
    query.before.as_deref().map(|value| {
        let decoded = URL_SAFE_NO_PAD.decode(value).map_err(|_| {
            HttpResponse::BadRequest().json(json!({
                "error": {"code": "invalid_page_cursor", "message": "before is not a valid page cursor"}
            }))
        })?;
        serde_json::from_slice(&decoded).map_err(|_| {
            HttpResponse::BadRequest().json(json!({
                "error": {"code": "invalid_page_cursor", "message": "before is not a valid page cursor"}
            }))
        })
    }).transpose()
}

fn admin_snapshot() -> Value {
    let observed_at = Utc::now();
    json!({"observed_at": observed_at, "generation": observed_at.to_rfc3339()})
}

async fn upstream_slots(req: HttpRequest, state: web::Data<AppState>) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized();
    }
    let keys = state.vault.list();
    let proofs = match profile_proof_keys(&state).await {
        Ok(proofs) => proofs,
        Err(_) => {
            return HttpResponse::ServiceUnavailable()
                .json(json!({"error":{"code":"capability_state_unavailable"}}));
        }
    };
    let slots = keys
        .iter()
        .enumerate()
        .map(|(index, key)| {
            let eligible = key.enabled && key.verified && key.cooldown_until.is_none_or(|until| until <= Utc::now());
            json!({
                "slot_no": index + 1,
                "key_id": key.id,
                "label": key.label,
                "status": if eligible { "eligible" } else if key.cooldown_until.is_some() { "cooldown" } else if !key.verified { "probe_required" } else { "disabled" },
                "request_count": key.request_count,
                "failure_count": key.failure_count,
                "cooldown_until": key.cooldown_until,
                "profiles": PROFILES.iter().map(|profile| {
                    let proof = proofs.get(*profile).is_some_and(|key_ids| key_ids.contains(&key.id));
                    json!({
                        "profile_id": profile,
                        "proof_revision": if proof { "durable" } else { "missing" },
                        "eligible_now": eligible && proof,
                        "reason": if !eligible { if !key.verified { json!("missing_key_probe") } else if key.cooldown_until.is_some() { json!("cooldown") } else { json!("manual_disabled") } } else if !proof { json!("missing_profile_proof") } else { Value::Null }
                    })
                }).collect::<Vec<_>>()
            })
        })
        .collect::<Vec<_>>();
    HttpResponse::Ok()
        .insert_header(("cache-control", "no-store"))
        .json(json!({"snapshot":admin_snapshot(),"slots":slots}))
}

async fn model_capabilities(req: HttpRequest, state: web::Data<AppState>) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized();
    }
    let keys = state.vault.list();
    let eligible = keys
        .iter()
        .filter(|key| {
            key.enabled
                && key.verified
                && key.cooldown_until.is_none_or(|until| until <= Utc::now())
        })
        .map(|key| key.id)
        .collect::<HashSet<_>>();
    let proofs = match profile_proof_keys(&state).await {
        Ok(proofs) => proofs,
        Err(_) => {
            return HttpResponse::ServiceUnavailable()
                .json(json!({"error":{"code":"capability_state_unavailable"}}));
        }
    };
    let models = PROFILES
        .iter()
        .map(|profile| {
            let route = match *profile {
                "z-ai/glm-5.2" | "microsoft/phi-4-multimodal-instruct" | "nvidia/vila" => {
                    "/v1/chat/completions"
                }
                "nvidia/nvclip" => "/v1/embeddings",
                "black-forest-labs/flux.1-kontext-dev" => "/v1/images/generations",
                "stabilityai/stable-video-diffusion" => "/v1/videos/generations",
                "nvidia/magpie-tts-multilingual" => "/v1/audio/speech",
                "nvidia/parakeet-ctc-1.1b" => "/v1/audio/transcriptions",
                _ => "/v1/nvidia/inference",
            };
            let available_now = proofs
                .get(*profile)
                .is_some_and(|key_ids| key_ids.iter().any(|key_id| eligible.contains(key_id)));
            let proof_status = if available_now {
                "provider_proof_verified"
            } else if eligible.is_empty() {
                "pair_not_ready"
            } else {
                "provider_proof_required"
            };
            json!({
                "id":profile,
                "route":route,
                "advertised":true,
                "available_now":available_now,
                "proof_status":proof_status,
                "modalities":model_modalities(profile)
            })
        })
        .collect::<Vec<_>>();
    HttpResponse::Ok()
        .insert_header(("cache-control", "no-store"))
        .json(json!({"snapshot":admin_snapshot(),"models":models}))
}

fn model_modalities(profile: &str) -> &'static [&'static str] {
    match profile {
        "z-ai/glm-5.2" => &["text"],
        "microsoft/phi-4-multimodal-instruct" => &["text", "image", "audio"],
        "nvidia/vila" => &["text", "image", "video"],
        "nvidia/nvclip" => &["image", "embedding"],
        "black-forest-labs/flux.1-kontext-dev" => &["image_generation"],
        "stabilityai/stable-video-diffusion" => &["video_generation"],
        "nvidia/magpie-tts-multilingual" => &["audio_generation"],
        "nvidia/parakeet-ctc-1.1b" => &["audio_transcription"],
        _ => &[],
    }
}

async fn generation_readiness(req: HttpRequest, state: web::Data<AppState>) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized();
    }
    let keys = state.vault.list();
    let configured = keys.len();
    let verified = keys.iter().filter(|key| key.verified).count();
    let eligible = eligible_key_count(&keys);
    let proofs = match profile_proof_keys(&state).await {
        Ok(proofs) => proofs,
        Err(_) => {
            return HttpResponse::ServiceUnavailable()
                .json(json!({"error":{"code":"capability_state_unavailable"}}));
        }
    };
    let eligible_ids = keys
        .iter()
        .filter(|key| {
            key.enabled
                && key.verified
                && key.cooldown_until.is_none_or(|until| until <= Utc::now())
        })
        .map(|key| key.id)
        .collect::<HashSet<_>>();
    let available_profiles = PROFILES
        .iter()
        .filter(|profile| {
            proofs
                .get(**profile)
                .is_some_and(|key_ids| key_ids.iter().any(|key_id| eligible_ids.contains(key_id)))
        })
        .count();
    let ready = configured == nvidia_build_lb_core::MAX_UPSTREAM_KEYS
        && verified == nvidia_build_lb_core::MAX_UPSTREAM_KEYS
        && eligible == nvidia_build_lb_core::MAX_UPSTREAM_KEYS
        && available_profiles == PROFILES.len();
    let reasons = if ready {
        Vec::new()
    } else {
        let mut reasons = Vec::new();
        if configured < nvidia_build_lb_core::MAX_UPSTREAM_KEYS {
            reasons.push("missing_upstream_slot");
        }
        if verified < nvidia_build_lb_core::MAX_UPSTREAM_KEYS {
            reasons.push("probe_required");
        }
        if eligible < nvidia_build_lb_core::MAX_UPSTREAM_KEYS {
            reasons.push("slot_unavailable");
        }
        if available_profiles < PROFILES.len() {
            reasons.push("provider_proof_required");
        }
        reasons
    };
    HttpResponse::Ok().insert_header(("cache-control", "no-store")).json(json!({
        "snapshot":admin_snapshot(),"ready":ready,"configured_slots":configured,"verified_slots":verified,"eligible_slots":eligible,"advertised_profiles":PROFILES.len(),"available_profiles":available_profiles,"reasons":reasons
    }))
}

async fn operations(
    req: HttpRequest,
    state: web::Data<AppState>,
    query: web::Query<PageQuery>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized();
    }
    let before = match page_before(&query) {
        Ok(before) => before,
        Err(response) => return response,
    };
    let limit = query.limit.unwrap_or(50).clamp(1, 100) as i64;
    let Some(pool) = &state.vault.database else {
        return HttpResponse::Ok()
            .insert_header(("cache-control", "no-store"))
            .json(json!({"snapshot":admin_snapshot(),"operations":[],"next_before":Value::Null}));
    };
    let attempts = match before {
        Some(before) => sqlx::query_as::<_, (Uuid, Uuid, String, Uuid, String, chrono::DateTime<Utc>, Option<chrono::DateTime<Utc>>)>(
            "SELECT id, request_id, profile_id, key_id, outcome, created_at, finished_at FROM nblb.request_attempts WHERE (created_at, id) < ($1, $2) ORDER BY created_at DESC, id DESC LIMIT $3"
        ).bind(before.created_at).bind(before.id).bind(limit).fetch_all(pool).await,
        None => sqlx::query_as::<_, (Uuid, Uuid, String, Uuid, String, chrono::DateTime<Utc>, Option<chrono::DateTime<Utc>>)>(
            "SELECT id, request_id, profile_id, key_id, outcome, created_at, finished_at FROM nblb.request_attempts ORDER BY created_at DESC, id DESC LIMIT $1"
        ).bind(limit).fetch_all(pool).await,
    };
    let attempts = match attempts {
        Ok(attempts) => attempts,
        Err(error) => {
            eprintln!("admin operations query failed: {error:#}");
            return HttpResponse::ServiceUnavailable()
                .json(json!({"error":{"code":"operations_unavailable"}}));
        }
    };
    let next_before = if attempts.len() as i64 == limit {
        attempts.last().and_then(|attempt| {
            encode_page_cursor(&PageCursor {
                created_at: attempt.5,
                id: attempt.0,
            })
            .ok()
        })
    } else {
        None
    };
    let rows = attempts
        .into_iter()
        .map(|(id, request_id, profile_id, key_id, outcome, created_at, finished_at)| {
            json!({"id":id,"request_id":request_id,"profile_id":profile_id,"key_id":key_id,"status":outcome,"created_at":created_at,"finished_at":finished_at})
        })
        .collect::<Vec<_>>();
    HttpResponse::Ok()
        .insert_header(("cache-control", "no-store"))
        .json(json!({"snapshot":admin_snapshot(),"operations":rows,"next_before":next_before}))
}

async fn operation_detail(
    req: HttpRequest,
    state: web::Data<AppState>,
    path: web::Path<Uuid>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized();
    }
    let id = path.into_inner();
    let Some(pool) = &state.vault.database else {
        return HttpResponse::NotFound().json(json!({"error":{"code":"resource_not_found"}}));
    };
    let row = sqlx::query_as::<_, (Uuid, Uuid, String, Uuid, String, Option<chrono::DateTime<Utc>>)>("SELECT id, request_id, profile_id, key_id, outcome, finished_at FROM nblb.request_attempts WHERE id=$1").bind(id).fetch_optional(pool).await;
    match row {
        Ok(Some((id, request_id, profile_id, key_id, outcome, finished_at))) => HttpResponse::Ok().json(json!({"snapshot":admin_snapshot(),"operation":{"id":id,"request_id":request_id,"profile_id":profile_id,"key_id":key_id,"status":outcome,"finished_at":finished_at}})),
        _ => HttpResponse::NotFound().json(json!({"error":{"code":"resource_not_found"}})),
    }
}

async fn attentions(
    req: HttpRequest,
    state: web::Data<AppState>,
    _query: web::Query<PageQuery>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized();
    }
    let _before = _query.before.as_deref();
    let items = state.vault.list().into_iter().filter(|key| !key.enabled || !key.verified || key.cooldown_until.is_some()).map(|key| json!({"id":key.id,"code":if key.cooldown_until.is_some(){"upstream_cooldown"}else if !key.verified{"probe_required"}else{"upstream_disabled"},"resource":{"kind":"upstream_key","id":key.id},"label":key.label,"next_action":if !key.verified{"probe"}else{"inspect_routing"},"expires_at":key.cooldown_until})).collect::<Vec<_>>();
    HttpResponse::Ok()
        .insert_header(("cache-control", "no-store"))
        .json(json!({"snapshot":admin_snapshot(),"attentions":items,"next_before":Value::Null}))
}

async fn events(
    req: HttpRequest,
    state: web::Data<AppState>,
    query: web::Query<PageQuery>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized();
    }
    let before = match page_before(&query) {
        Ok(before) => before,
        Err(response) => return response,
    };
    let limit = query.limit.unwrap_or(50).clamp(1, 100) as i64;
    let Some(pool) = &state.vault.database else {
        return HttpResponse::Ok()
            .insert_header(("cache-control", "no-store"))
            .json(json!({"snapshot":admin_snapshot(),"events":[],"next_before":Value::Null}));
    };
    let attempts = match before {
        Some(before) => sqlx::query_as::<_, (Uuid, Uuid, String, Uuid, String, chrono::DateTime<Utc>)>(
            "SELECT id, request_id, profile_id, key_id, outcome, created_at FROM nblb.request_attempts WHERE (created_at, id) < ($1, $2) ORDER BY created_at DESC, id DESC LIMIT $3"
        ).bind(before.created_at).bind(before.id).bind(limit).fetch_all(pool).await,
        None => sqlx::query_as::<_, (Uuid, Uuid, String, Uuid, String, chrono::DateTime<Utc>)>(
            "SELECT id, request_id, profile_id, key_id, outcome, created_at FROM nblb.request_attempts ORDER BY created_at DESC, id DESC LIMIT $1"
        ).bind(limit).fetch_all(pool).await,
    };
    let attempts = match attempts {
        Ok(attempts) => attempts,
        Err(error) => {
            eprintln!("admin events query failed: {error:#}");
            return HttpResponse::ServiceUnavailable()
                .json(json!({"error":{"code":"events_unavailable"}}));
        }
    };
    let next_before = if attempts.len() as i64 == limit {
        attempts.last().and_then(|attempt| {
            encode_page_cursor(&PageCursor {
                created_at: attempt.5,
                id: attempt.0,
            })
            .ok()
        })
    } else {
        None
    };
    let rows = attempts
        .into_iter()
        .map(|(id, request_id, profile_id, key_id, outcome, created_at)| {
            json!({"id":id,"kind":"request_attempt","request_id":request_id,"profile_id":profile_id,"key_id":key_id,"outcome":outcome,"created_at":created_at})
        })
        .collect::<Vec<_>>();
    HttpResponse::Ok()
        .insert_header(("cache-control", "no-store"))
        .json(json!({"snapshot":admin_snapshot(),"events":rows,"next_before":next_before}))
}

async fn event_detail(
    req: HttpRequest,
    state: web::Data<AppState>,
    path: web::Path<Uuid>,
) -> impl Responder {
    operation_detail(req, state, path).await
}

async fn evidence_detail(
    req: HttpRequest,
    state: web::Data<AppState>,
    path: web::Path<Uuid>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized();
    }
    let id = path.into_inner();
    let Some(pool) = &state.vault.database else {
        return HttpResponse::NotFound().json(json!({"error":{"code":"resource_not_found"}}));
    };
    let row = sqlx::query_as::<_, (Uuid, Uuid, String, String)>(
        "SELECT id, request_id, profile_id, outcome FROM nblb.request_attempts WHERE id=$1",
    )
    .bind(id)
    .fetch_optional(pool)
    .await;
    match row {
        Ok(Some((id, request_id, profile_id, outcome))) => HttpResponse::Ok().json(json!({"snapshot":admin_snapshot(),"evidence":{"id":id,"request_id":request_id,"profile_id":profile_id,"outcome":outcome}})),
        _ => HttpResponse::NotFound().json(json!({"error":{"code":"resource_not_found"}})),
    }
}

async fn add_key(
    req: HttpRequest,
    state: web::Data<AppState>,
    payload: web::Json<KeyInput>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized();
    }
    match state
        .vault
        .mutate(|vault| vault.add(&payload.label, &payload.credential))
        .await
    {
        Ok(summary) => HttpResponse::Created().json(summary),
        Err(error) if is_unique_conflict(&error) => HttpResponse::Conflict().json(
            json!({"error":{"code":"resource_conflict","message":"An active upstream slot with this label or credential already exists."}}),
        ),
        Err(error) => HttpResponse::UnprocessableEntity()
            .json(json!({"error":{"code":"invalid_request","message":error.to_string()}})),
    }
}

async fn toggle_key(
    req: HttpRequest,
    state: web::Data<AppState>,
    path: web::Path<Uuid>,
    payload: web::Json<ToggleInput>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized();
    }
    match state
        .vault
        .mutate(|vault| vault.set_enabled(path.into_inner(), payload.enabled))
        .await
    {
        Ok(summary) => HttpResponse::Ok().json(summary),
        Err(error) if error.to_string().contains("probe") => HttpResponse::Conflict().json(
            json!({"error":{"code":"probe_required","message":"provider probe is required before enabling this key"}}),
        ),
        Err(_) => HttpResponse::NotFound().json(json!({"error":{"code":"resource_not_found"}})),
    }
}

async fn enable_key_alias(
    req: HttpRequest,
    state: web::Data<AppState>,
    path: web::Path<Uuid>,
) -> impl Responder {
    toggle_key(req, state, path, web::Json(ToggleInput { enabled: true })).await
}

async fn disable_key_alias(
    req: HttpRequest,
    state: web::Data<AppState>,
    path: web::Path<Uuid>,
) -> impl Responder {
    toggle_key(req, state, path, web::Json(ToggleInput { enabled: false })).await
}

/// Verifies one stored credential without changing its routing state.  The
/// response is deliberately reduced to a status class; provider bodies and
/// credential material never cross the admin boundary.
async fn record_probe_receipt(
    state: &web::Data<AppState>,
    key_id: Uuid,
) -> Result<(), HttpResponse> {
    state
        .vault
        .record_profile_proof("z-ai/glm-5.2", key_id)
        .await
        .map_err(|_| {
            HttpResponse::ServiceUnavailable().json(json!({
                "probe_status": "unavailable",
                "error": {"code": "probe_persistence_failed"}
            }))
        })
}

async fn probe_key(
    req: HttpRequest,
    state: web::Data<AppState>,
    path: web::Path<Uuid>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized();
    }
    let id = path.into_inner();
    let credential = match state.vault.credential_for_probe(id) {
        Ok(value) => value,
        Err(_) => {
            return HttpResponse::NotFound().json(json!({"error":{"code":"resource_not_found"}}));
        }
    };
    if state.upstream_url.starts_with("mock://") {
        if credential.contains("fail") {
            return HttpResponse::UnprocessableEntity().json(json!({
                "probe_status": "invalid_credential",
                "error": {"code": "invalid_upstream_credential"}
            }));
        }
        return match state.vault.mutate(|vault| vault.mark_verified(id)).await {
            Ok(_) => match record_probe_receipt(&state, id).await {
                Ok(()) => HttpResponse::Ok().json(json!({"probe_status":"valid","status":200})),
                Err(response) => response,
            },
            Err(_) => HttpResponse::ServiceUnavailable().json(
                json!({"probe_status":"unavailable","error":{"code":"probe_persistence_failed"}}),
            ),
        };
    }
    let endpoint =
        upstream_endpoint_for(&state.upstream_url, "/v1/chat/completions", "z-ai/glm-5.2");
    let response = state
        .client
        .post(endpoint)
        .bearer_auth(&credential)
        .header(reqwest::header::ACCEPT, "application/json")
        .json(&json!({
            "model": "z-ai/glm-5.2",
            "messages": [{"role":"user","content":"health probe"}],
            "max_tokens": 1,
            "stream": false
        }))
        .timeout(UPSTREAM_REQUEST_TIMEOUT)
        .send()
        .await;
    let response = match response {
        Ok(response) => response,
        Err(_) => {
            return HttpResponse::ServiceUnavailable().json(json!({
                "probe_status": "unavailable",
                "error": {"code": "upstream_unavailable"}
            }));
        }
    };
    let status = response.status();
    if status == reqwest::StatusCode::UNAUTHORIZED || status == reqwest::StatusCode::FORBIDDEN {
        if let Err(response) = quarantine_key(&state, id).await {
            return response;
        }
        return HttpResponse::UnprocessableEntity().json(json!({
            "probe_status": "invalid_credential",
            "status": status.as_u16(),
            "error": {"code": "invalid_upstream_credential"}
        }));
    }
    if status == reqwest::StatusCode::TOO_MANY_REQUESTS {
        return HttpResponse::TooManyRequests().json(json!({
            "probe_status": "rate_limited",
            "status": status.as_u16(),
            "error": {"code": "upstream_rate_limited"}
        }));
    }
    if !status.is_success() {
        return HttpResponse::ServiceUnavailable().json(json!({
            "probe_status": "unavailable",
            "status": status.as_u16(),
            "error": {"code": "upstream_unavailable"}
        }));
    }
    let bytes = match response.bytes().await {
        Ok(bytes) => bytes,
        Err(_) => {
            return HttpResponse::BadGateway().json(json!({
                "probe_status": "invalid_response",
                "error": {"code": "upstream_protocol_error"}
            }));
        }
    };
    if validate_chat_response(&bytes, false).is_err() {
        return HttpResponse::BadGateway().json(json!({
            "probe_status": "invalid_response",
            "error": {"code": "upstream_protocol_error"}
        }));
    }
    match state.vault.mutate(|vault| vault.mark_verified(id)).await {
        Ok(_) => match record_probe_receipt(&state, id).await {
            Ok(()) => {
                HttpResponse::Ok().json(json!({"probe_status":"valid","status":status.as_u16()}))
            }
            Err(response) => response,
        },
        Err(_) => HttpResponse::ServiceUnavailable().json(
            json!({"probe_status":"unavailable","error":{"code":"probe_persistence_failed"}}),
        ),
    }
}

async fn delete_key(
    req: HttpRequest,
    state: web::Data<AppState>,
    path: web::Path<Uuid>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized();
    }
    match state
        .vault
        // Request attempts retain a foreign key to the encrypted key row.
        // “Delete” is therefore a durable retirement, which preserves audit
        // evidence while removing the encrypted credential from all new
        // routing and admin read surfaces.
        .mutate(|vault| vault.delete(path.into_inner()))
        .await
    {
        Ok(()) => HttpResponse::NoContent().finish(),
        Err(_) => HttpResponse::NotFound().json(json!({"error":{"code":"resource_not_found"}})),
    }
}

async fn list_downstream(req: HttpRequest, state: web::Data<AppState>) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized();
    }
    let items = state.vault.list_downstream();
    HttpResponse::Ok().json(json!({"items": items}))
}

async fn add_downstream(
    req: HttpRequest,
    state: web::Data<AppState>,
    payload: web::Json<DownstreamInput>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized();
    }
    let keys = state.vault.list();
    let ready = keys.len() == nvidia_build_lb_core::MAX_UPSTREAM_KEYS
        && keys.iter().all(|key| {
            key.enabled
                && key.verified
                && key.cooldown_until.is_none_or(|until| until <= Utc::now())
        });
    if !ready {
        return HttpResponse::Conflict().json(json!({
            "error": {
                "code": "generation_not_ready",
                "message": "Both NVIDIA upstream slots must be probed and available before issuing a downstream credential.",
                "next_action": "probe_and_enable_all_upstream_slots"
            }
        }));
    }
    match state
        .vault
        .mutate(|vault| vault.issue_downstream(&payload.label, &payload.scopes))
        .await
    {
        Ok(issued) => {
            let mut value = serde_json::to_value(issued.summary).unwrap_or_else(|_| json!({}));
            if let Value::Object(object) = &mut value {
                object.insert("token".into(), Value::String(issued.token));
            }
            HttpResponse::Created().json(value)
        }
        Err(error)
            if error.to_string().contains("already exists") || is_unique_conflict(&error) =>
        {
            HttpResponse::Conflict()
                .json(json!({"error":{"code":"resource_conflict","message":error.to_string()}}))
        }
        Err(error) => HttpResponse::UnprocessableEntity()
            .json(json!({"error":{"code":"invalid_request","message":error.to_string()}})),
    }
}

fn is_unique_conflict(error: &anyhow::Error) -> bool {
    error.chain().any(|cause| {
        let text = cause.to_string();
        text.contains("duplicate key value violates unique constraint")
            || text.contains("upstream_keys_active_")
            || text.contains("downstream_credentials_active_label_idx")
            || cause
                .downcast_ref::<sqlx::Error>()
                .and_then(|error| error.as_database_error())
                .and_then(|database| database.code())
                .as_deref()
                == Some("23505")
    })
}

async fn revoke_downstream(
    req: HttpRequest,
    state: web::Data<AppState>,
    path: web::Path<Uuid>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized();
    }
    match state
        .vault
        .mutate(|vault| vault.revoke_downstream(path.into_inner()))
        .await
    {
        Ok(summary) => HttpResponse::Ok().json(summary),
        Err(_) => HttpResponse::NotFound().json(json!({"error":{"code":"resource_not_found"}})),
    }
}

async fn revoke_downstream_legacy(
    req: HttpRequest,
    state: web::Data<AppState>,
    path: web::Path<Uuid>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized();
    }
    match state
        .vault
        .mutate(|vault| vault.revoke_downstream(path.into_inner()))
        .await
    {
        Ok(_) => HttpResponse::NoContent().finish(),
        Err(_) => HttpResponse::NotFound().json(json!({"error":{"code":"resource_not_found"}})),
    }
}

async fn chat_completions(
    req: HttpRequest,
    state: web::Data<AppState>,
    mut payload: web::Payload,
) -> impl Responder {
    if let Some(response) = public_guard_response(&req) {
        return response;
    }
    if let Err(response) = authorize_scope(&req, &state, "chat:write").await {
        return response;
    }
    let body = match read_request_body(&mut payload, 64 * 1024 * 1024).await {
        Ok(body) => body,
        Err(response) => return response,
    };
    let request: Value = match serde_json::from_slice(&body) {
        Ok(request) => request,
        Err(_) => return invalid_request("request body must be valid JSON"),
    };
    if let Err(response) = validate_chat_request(&request) {
        return response;
    }
    let request_id = Uuid::new_v4();
    let profile = request
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or(PROFILES[0]);
    let stream = request
        .get("stream")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let key_attempts = state.vault.list().len().max(1);
    let mut attempted = Vec::new();
    let mut rate_limited = false;
    let mut all_attempts_rate_limited = true;
    for _ in 0..key_attempts {
        let id = select_key(&state, profile).await;
        let Some(id) = id else { break };
        if attempted.contains(&id) {
            break;
        }
        attempted.push(id);
        if let Err(response) = attempt_started(&state, request_id, profile, id).await {
            return response;
        }
        let credential = match state.vault.credential(id) {
            Ok(value) => value,
            Err(_) => {
                all_attempts_rate_limited = false;
                if let Err(response) = attempt_finished(&state, request_id, id, "failed").await {
                    return response;
                }
                continue;
            }
        };
        if state.upstream_url.starts_with("mock://") {
            all_attempts_rate_limited = false;
            let force_first_failure = request
                .get("metadata")
                .and_then(Value::as_object)
                .and_then(|metadata| metadata.get("force_first_upstream_failure"))
                .and_then(Value::as_bool)
                .unwrap_or(false)
                && attempted.len() == 1;
            if credential.contains("fail") || force_first_failure {
                if let Err(response) = record_failure(&state, id, Some(Duration::seconds(2))).await
                {
                    return response;
                }
                if let Err(response) = attempt_finished(&state, request_id, id, "failed").await {
                    return response;
                }
                continue;
            }
            if stream {
                return mock_stream_response(&request, state.clone(), request_id, id);
            }
            if let Err(response) = record_request(&state, id).await {
                return response;
            }
            if let Err(response) = attempt_finished(&state, request_id, id, "succeeded").await {
                return response;
            }
            return mock_response(&request, stream);
        }
        let mut upstream_request = state
            .client
            .post(upstream_endpoint_for(
                &state.upstream_url,
                "/v1/chat/completions",
                profile,
            ))
            .bearer_auth(&credential)
            .json(&request);
        if !stream {
            upstream_request = upstream_request.timeout(UPSTREAM_REQUEST_TIMEOUT);
        }
        let result = upstream_request.send().await;
        match result.as_ref() {
            Ok(response) if response.status().as_u16() == 429 => rate_limited = true,
            _ => all_attempts_rate_limited = false,
        }
        match result {
            Ok(response)
                if response.status().as_u16() == 202
                    && !stream
                    && matches!(
                        profile,
                        "microsoft/phi-4-multimodal-instruct" | "nvidia/vila"
                    ) =>
            {
                let chat_endpoint =
                    upstream_endpoint_for(&state.upstream_url, "/v1/chat/completions", profile);
                let response = match poll_nvcf(&state.client, response, &chat_endpoint, &credential)
                    .await
                {
                    Ok(response) => response,
                    Err(_) => {
                        if let Err(response) = record_failure(&state, id, None).await {
                            return response;
                        }
                        if let Err(response) =
                            attempt_finished(&state, request_id, id, "failed").await
                        {
                            return response;
                        }
                        return HttpResponse::BadGateway().json(json!({
                                "error": {"message": "NVIDIA accepted the request but polling did not complete", "type": "upstream_poll_error"}
                            }));
                    }
                };
                let status = actix_web::http::StatusCode::from_u16(response.status().as_u16())
                    .unwrap_or(actix_web::http::StatusCode::BAD_GATEWAY);
                let bytes = match response.bytes().await {
                    Ok(bytes) => bytes,
                    Err(_) => {
                        if let Err(response) =
                            attempt_finished(&state, request_id, id, "failed").await
                        {
                            return response;
                        }
                        continue;
                    }
                };
                if validate_chat_response(&bytes, false).is_err() {
                    if let Err(response) = quarantine_key(&state, id).await {
                        return response;
                    }
                    if let Err(response) = attempt_finished(&state, request_id, id, "failed").await
                    {
                        return response;
                    }
                    return HttpResponse::BadGateway().json(json!({
                        "error": {"message": "provider returned an invalid chat response", "type": "upstream_protocol_error"}
                    }));
                }
                if let Err(response) = record_request(&state, id).await {
                    return response;
                }
                if let Err(response) = attempt_finished(&state, request_id, id, "succeeded").await {
                    return response;
                }
                return HttpResponse::build(status)
                    .insert_header(("content-type", "application/json"))
                    .body(bytes);
            }
            Ok(response) if response.status().as_u16() == 202 => {
                if let Err(response) = attempt_finished(&state, request_id, id, "failed").await {
                    return response;
                }
                return HttpResponse::BadGateway().json(json!({"error":{"message":"NVIDIA returned an unsupported asynchronous response","type":"upstream_protocol_error"}}));
            }
            Ok(response) if response.status().is_success() => {
                let status = actix_web::http::StatusCode::from_u16(response.status().as_u16())
                    .unwrap_or(actix_web::http::StatusCode::BAD_GATEWAY);
                let content_type = response
                    .headers()
                    .get("content-type")
                    .and_then(|value| value.to_str().ok())
                    .unwrap_or("application/json")
                    .to_owned();
                if stream {
                    if !content_type
                        .split(';')
                        .next()
                        .is_some_and(|value| value.trim().eq_ignore_ascii_case("text/event-stream"))
                    {
                        if let Err(response) = record_failure(&state, id, None).await {
                            return response;
                        }
                        if let Err(response) =
                            attempt_finished(&state, request_id, id, "failed").await
                        {
                            return response;
                        }
                        continue;
                    }
                    let upstream = Box::pin(response.bytes_stream());
                    // Own the started attempt before awaiting the first SSE
                    // frame. If the client disconnects while the provider is
                    // silent, dropping this guard still closes the ledger row.
                    let stream_guard = StreamAttemptGuard::new(state.clone(), request_id, id);
                    let (upstream, validator, prefix) =
                        match tokio::time::timeout(STREAM_PRIME_TIMEOUT, prime_stream(upstream))
                            .await
                        {
                            Ok(Ok(value)) => value,
                            _ => {
                                stream_guard.terminal.store(true, Ordering::Release);
                                if let Err(response) = record_failure(&state, id, None).await {
                                    return response;
                                }
                                if let Err(response) =
                                    attempt_finished(&state, request_id, id, "failed").await
                                {
                                    return response;
                                }
                                continue;
                            }
                        };
                    let downstream = chat_response_stream(
                        upstream,
                        validator,
                        prefix,
                        state.clone(),
                        request_id,
                        id,
                        stream_guard,
                    );
                    return HttpResponse::build(status)
                        .insert_header(("content-type", content_type))
                        .insert_header(("cache-control", "no-cache"))
                        .streaming(downstream);
                }
                let bytes = match response.bytes().await {
                    Ok(bytes) => bytes,
                    Err(_) => {
                        if let Err(response) = record_failure(&state, id, None).await {
                            return response;
                        }
                        if let Err(response) =
                            attempt_finished(&state, request_id, id, "failed").await
                        {
                            return response;
                        }
                        continue;
                    }
                };
                if validate_chat_response(&bytes, stream).is_err() {
                    if let Err(response) = quarantine_key(&state, id).await {
                        return response;
                    }
                    if let Err(response) = attempt_finished(&state, request_id, id, "failed").await
                    {
                        return response;
                    }
                    return HttpResponse::BadGateway().json(json!({
                        "error": {"message": "provider returned an invalid chat response", "type": "upstream_protocol_error"}
                    }));
                }
                if let Err(response) = record_request(&state, id).await {
                    return response;
                }
                if let Err(response) = attempt_finished(&state, request_id, id, "succeeded").await {
                    return response;
                }
                return HttpResponse::build(status)
                    .insert_header(("content-type", content_type))
                    .body(bytes);
            }
            Ok(response)
                if response.status().as_u16() == 408
                    || response.status().as_u16() == 401
                    || response.status().as_u16() == 402
                    || response.status().as_u16() == 403
                    || response.status().as_u16() == 429
                    || response.status().is_server_error() =>
            {
                // 402 means quota/credit exhaustion, not invalid custody. It
                // therefore receives the same bounded cooldown/failover path
                // as 429 instead of permanently quarantining the credential.
                let auth_failure = matches!(response.status().as_u16(), 401 | 403);
                if auth_failure {
                    if let Err(response) = quarantine_key(&state, id).await {
                        return response;
                    }
                } else {
                    let retry = retry_after_duration(&response);
                    if let Err(response) = record_failure(&state, id, retry).await {
                        return response;
                    }
                }
                if let Err(response) = attempt_finished(&state, request_id, id, "failed").await {
                    return response;
                }
            }
            Ok(response) => {
                let status = actix_web::http::StatusCode::from_u16(response.status().as_u16())
                    .unwrap_or(actix_web::http::StatusCode::BAD_GATEWAY);
                if let Err(response) = attempt_finished(&state, request_id, id, "failed").await {
                    return response;
                }
                return HttpResponse::build(status).json(json!({"error":{"message":"NVIDIA rejected the request","type":"upstream_request_rejected"}}));
            }
            Err(_) => {
                if let Err(response) = record_failure(&state, id, None).await {
                    return response;
                }
                if let Err(response) = attempt_finished(&state, request_id, id, "failed").await {
                    return response;
                }
            }
        }
    }
    if rate_limited
        && all_attempts_rate_limited
        && !attempted.is_empty()
        && let Some(seconds) = retry_after_for_keys(&state.vault.list())
    {
        return HttpResponse::TooManyRequests()
            .insert_header(("retry-after", seconds.to_string()))
            .json(json!({"error":{"message":"All NVIDIA upstream keys are rate limited","type":"upstream_rate_limited"}}));
    }
    HttpResponse::ServiceUnavailable().json(json!({"error":{"message":"No eligible NVIDIA upstream key","type":"upstream_unavailable"}}))
}

/// Handles the non-chat OpenAI/NVIDIA representations through the same
/// authenticated router. The mock path deliberately returns shape-valid
/// fixtures for every advertised modality, while production forwards the
/// original JSON body to the configured NVIDIA endpoint.
async fn multimodal(
    req: HttpRequest,
    state: web::Data<AppState>,
    mut payload: web::Payload,
) -> impl Responder {
    if let Some(response) = public_guard_response(&req) {
        return response;
    }
    let scope = match req.path() {
        "/v1/embeddings" => "embeddings:write",
        "/v1/images/generations" => "images:write",
        "/v1/audio/speech" | "/v1/audio/transcriptions" => "audio:write",
        _ => "media:write",
    };
    if let Err(response) = authorize_scope(&req, &state, scope).await {
        return response;
    }
    let body = match read_request_body(&mut payload, 64 * 1024 * 1024).await {
        Ok(body) => body,
        Err(response) => return response,
    };
    let content_type = req
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("application/json");
    if body.len() > modality_body_limit(req.path()) {
        return HttpResponse::PayloadTooLarge().json(json!({
            "error": {"message": "request body exceeds the endpoint limit", "type": "request_too_large"}
        }));
    }
    let request = match parse_multimodal_request(req.path(), &body, content_type) {
        Ok(request) => request,
        Err(response) => return response,
    };
    let upstream_request = match prepare_modality_request(req.path(), &request) {
        Ok(request) => request,
        Err(response) => return response,
    };
    let request_id = Uuid::new_v4();
    let profile = request
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or(PROFILES[0]);
    let key_attempts = state.vault.list().len().max(1);
    let mut attempted = Vec::new();
    let mut rate_limited = false;
    let mut all_attempts_rate_limited = true;
    for _ in 0..key_attempts {
        let Some(id) = select_key(&state, profile).await else {
            break;
        };
        if attempted.contains(&id) {
            break;
        }
        attempted.push(id);
        if let Err(response) = attempt_started(&state, request_id, profile, id).await {
            return response;
        }
        let credential = match state.vault.credential(id).ok() {
            Some(value) => value,
            None => {
                all_attempts_rate_limited = false;
                if let Err(response) = attempt_finished(&state, request_id, id, "failed").await {
                    return response;
                }
                continue;
            }
        };
        if state.upstream_url.starts_with("mock://") {
            if let Err(response) = record_request(&state, id).await {
                return response;
            }
            if let Err(response) = attempt_finished(&state, request_id, id, "succeeded").await {
                return response;
            }
            let (body, content_type) = mock_modality(req.path(), &request);
            let (body, content_type) = match normalize_modality_response(
                req.path(),
                &body,
                content_type,
            ) {
                Ok(value) => value,
                Err(_) => {
                    return HttpResponse::BadGateway().json(json!({
                        "error": {"message": "mock provider fixture failed the modality contract", "type": "mock_contract_invalid"}
                    }));
                }
            };
            return HttpResponse::Ok()
                .insert_header(("content-type", content_type))
                .body(body);
        }
        let endpoint = upstream_endpoint_for(&state.upstream_url, req.path(), profile);
        let multipart_transcription = req.path() == "/v1/audio/transcriptions"
            && content_type.starts_with("multipart/form-data")
            && request.get("__nblb_multipart").and_then(Value::as_bool) == Some(true);
        let request_result = if multipart_transcription {
            state
                .client
                .post(&endpoint)
                .bearer_auth(&credential)
                .header(reqwest::header::CONTENT_TYPE, content_type)
                .body(body.clone())
                .timeout(UPSTREAM_REQUEST_TIMEOUT)
                .send()
                .await
        } else if req.path() == "/v1/audio/speech" {
            let input = request
                .get("input")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let voice = request
                .get("voice")
                .and_then(Value::as_str)
                .unwrap_or("English-US.Female-1");
            let language = if voice.to_ascii_lowercase().starts_with("ko") {
                "ko-KR"
            } else {
                "en-US"
            };
            let form = reqwest::multipart::Form::new()
                .text("text", input.to_owned())
                .text("language", language.to_owned())
                .text("voice", voice.to_owned())
                .text("encoding", "LINEAR_PCM".to_owned())
                .text("sample_rate_hz", "44100".to_owned());
            state
                .client
                .post(&endpoint)
                .bearer_auth(&credential)
                .multipart(form)
                .timeout(UPSTREAM_REQUEST_TIMEOUT)
                .send()
                .await
        } else {
            state
                .client
                .post(&endpoint)
                .bearer_auth(&credential)
                .header(reqwest::header::CONTENT_TYPE, "application/json")
                .json(&upstream_request)
                .timeout(UPSTREAM_REQUEST_TIMEOUT)
                .send()
                .await
        };
        match request_result.as_ref() {
            Ok(response) if response.status().as_u16() == 429 => rate_limited = true,
            _ => all_attempts_rate_limited = false,
        }
        match request_result {
            Ok(response) if response.status().is_success() => {
                // A 202 means NVIDIA accepted an asynchronous job. It is
                // already an upstream side effect, so poll the same request
                // id and never retry the POST on another key.
                let response = if response.status().as_u16() == 202 {
                    match poll_nvcf(&state.client, response, &endpoint, &credential).await {
                        Ok(response) => response,
                        Err(_) => {
                            if let Err(response) = record_failure(&state, id, None).await {
                                return response;
                            }
                            if let Err(response) =
                                attempt_finished(&state, request_id, id, "failed").await
                            {
                                return response;
                            }
                            return HttpResponse::BadGateway().json(json!({
                                "error": {"message": "NVIDIA accepted the media request but polling did not complete", "type": "upstream_poll_error"}
                            }));
                        }
                    }
                } else {
                    response
                };
                let status = actix_web::http::StatusCode::from_u16(response.status().as_u16())
                    .unwrap_or(actix_web::http::StatusCode::BAD_GATEWAY);
                let content_type = response
                    .headers()
                    .get("content-type")
                    .and_then(|value| value.to_str().ok())
                    .unwrap_or("application/json")
                    .to_owned();
                let bytes = match response.bytes().await {
                    Ok(bytes) => bytes,
                    Err(_) => {
                        if let Err(response) = record_failure(&state, id, None).await {
                            return response;
                        }
                        if let Err(response) =
                            attempt_finished(&state, request_id, id, "failed").await
                        {
                            return response;
                        }
                        continue;
                    }
                };
                let (bytes, content_type) = match normalize_modality_response(
                    req.path(),
                    &bytes,
                    &content_type,
                ) {
                    Ok(value) => value,
                    Err(_) => {
                        if let Err(response) = quarantine_key(&state, id).await {
                            return response;
                        }
                        if let Err(response) =
                            attempt_finished(&state, request_id, id, "failed").await
                        {
                            return response;
                        }
                        return HttpResponse::BadGateway().json(json!({
                                "error": {"message": "provider returned an invalid modality response", "type": "upstream_protocol_error"}
                            }));
                    }
                };
                if let Err(response) = record_request(&state, id).await {
                    return response;
                }
                if let Err(response) = attempt_finished(&state, request_id, id, "succeeded").await {
                    return response;
                }
                return HttpResponse::build(status)
                    .insert_header(("content-type", content_type))
                    .body(bytes);
            }
            Ok(response)
                if response.status().as_u16() == 408
                    || response.status().as_u16() == 401
                    || response.status().as_u16() == 402
                    || response.status().as_u16() == 403
                    || response.status().as_u16() == 429
                    || response.status().is_server_error() =>
            {
                // 402 is a provider entitlement/credit signal, so cooldown
                // and failover remain possible; only 401/403 quarantine.
                let auth_failure = matches!(response.status().as_u16(), 401 | 403);
                if auth_failure {
                    if let Err(response) = quarantine_key(&state, id).await {
                        return response;
                    }
                } else {
                    let retry = retry_after_duration(&response);
                    if let Err(response) = record_failure(&state, id, retry).await {
                        return response;
                    }
                }
                if let Err(response) = attempt_finished(&state, request_id, id, "failed").await {
                    return response;
                }
            }
            Ok(response) => {
                if let Err(response) = attempt_finished(&state, request_id, id, "failed").await {
                    return response;
                }
                return HttpResponse::build(actix_web::http::StatusCode::from_u16(response.status().as_u16()).unwrap_or(actix_web::http::StatusCode::BAD_GATEWAY))
                    .json(json!({"error":{"message":"NVIDIA rejected the request","type":"upstream_request_rejected"}}));
            }
            Err(_) => {
                if let Err(response) = record_failure(&state, id, None).await {
                    return response;
                }
                if let Err(response) = attempt_finished(&state, request_id, id, "failed").await {
                    return response;
                }
            }
        }
    }
    if rate_limited
        && all_attempts_rate_limited
        && !attempted.is_empty()
        && let Some(seconds) = retry_after_for_keys(&state.vault.list())
    {
        return HttpResponse::TooManyRequests()
            .insert_header(("retry-after", seconds.to_string()))
            .json(json!({"error":{"message":"All NVIDIA upstream keys are rate limited","type":"upstream_rate_limited"}}));
    }
    HttpResponse::ServiceUnavailable().json(json!({"error":{"message":"No eligible NVIDIA upstream key","type":"upstream_unavailable"}}))
}

fn parse_multimodal_request(
    path: &str,
    body: &[u8],
    content_type: &str,
) -> Result<Value, HttpResponse> {
    if path == "/v1/audio/transcriptions" && content_type.starts_with("multipart/form-data") {
        let boundary = content_type
            .split(';')
            .map(str::trim)
            .find_map(|part| part.strip_prefix("boundary="))
            .map(|value| value.trim_matches('"'))
            .filter(|value| !value.is_empty())
            .ok_or_else(|| invalid_request("multipart boundary is required"))?;
        let marker = format!("--{boundary}").into_bytes();
        let has_marker = body.windows(marker.len()).any(|window| window == marker);
        let has_model = body
            .windows(b"name=\"model\"".len())
            .any(|window| window == b"name=\"model\"");
        let has_file = body
            .windows(b"name=\"file\"".len())
            .any(|window| window == b"name=\"file\"");
        if !has_marker || !has_model || !has_file {
            return Err(invalid_request(
                "multipart transcription requires model and file fields",
            ));
        }
        if body.len() > 64 * 1024 * 1024 {
            return Err(invalid_request("multipart body exceeds 64 MiB"));
        }
        let has_audio_part = body
            .windows(b"Content-Type: audio/".len())
            .any(|window| window.eq_ignore_ascii_case(b"Content-Type: audio/"));
        if !has_audio_part {
            return Err(invalid_request(
                "multipart transcription file must declare an audio MIME type",
            ));
        }
        let text = String::from_utf8_lossy(body);
        let model = text
            .split("name=\"model\"")
            .nth(1)
            .and_then(|part| part.split("\r\n\r\n").nth(1))
            .and_then(|value| value.split("\r\n--").next())
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| invalid_request("multipart model field is empty"))?;
        if !PROFILES.contains(&model) {
            return Err(model_not_found());
        }
        if !profile_supports_path(path, model) {
            return Err(HttpResponse::UnprocessableEntity().json(json!({
                "error": {"message": "model is not compatible with this endpoint", "type": "model_route_mismatch"}
            })));
        }
        return Ok(json!({"model": model, "__nblb_multipart": true}));
    }

    if !content_type.starts_with("application/json") {
        return Err(invalid_request(
            "JSON content-type is required for this modality",
        ));
    }
    let request = serde_json::from_slice::<Value>(body)
        .map_err(|_| invalid_request("request body must be valid JSON"))?;
    let object = request
        .as_object()
        .ok_or_else(|| invalid_request("request body must be a JSON object"))?;
    let model = object
        .get("model")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| invalid_request("model is required"))?;
    if !PROFILES.contains(&model) {
        return Err(model_not_found());
    }
    if !profile_supports_path(path, model) {
        return Err(HttpResponse::UnprocessableEntity().json(json!({
            "error": {"message": "model is not compatible with this endpoint", "type": "model_route_mismatch"}
        })));
    }
    validate_modality_fields(path, object)?;
    let required_field = match path {
        "/v1/images/generations" => Some("prompt"),
        "/v1/videos/generations" => Some("input_reference"),
        "/v1/embeddings" => Some("input"),
        "/v1/audio/speech" => Some("input"),
        _ => None,
    };
    if let Some(field) = required_field
        && !object.contains_key(field)
    {
        return Err(invalid_request(&format!("{field} is required")));
    }
    Ok(request)
}

/// Reads a request body only after the route has passed host and bearer
/// authorization.  Using the raw Actix payload here prevents an unauthenticated
/// caller from forcing JSON extraction and validation work before rejection.
async fn read_request_body(
    payload: &mut web::Payload,
    limit: usize,
) -> Result<Bytes, HttpResponse> {
    let mut body = BytesMut::new();
    while let Some(chunk) = payload.next().await {
        let chunk = chunk.map_err(|_| {
            HttpResponse::BadRequest().json(json!({
                "error": {"message": "request body could not be read", "type": "invalid_request"}
            }))
        })?;
        if body.len().saturating_add(chunk.len()) > limit {
            return Err(HttpResponse::PayloadTooLarge().json(json!({
                "error": {"message": "request body exceeds the endpoint limit", "type": "request_too_large"}
            })));
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body.freeze())
}

fn modality_body_limit(path: &str) -> usize {
    match path {
        "/v1/images/generations" => 256 * 1024,
        "/v1/audio/speech" => 64 * 1024,
        "/v1/nvidia/inference" => 1024 * 1024,
        "/v1/embeddings" | "/v1/audio/transcriptions" => 32 * 1024 * 1024,
        "/v1/videos/generations" => 32 * 1024 * 1024,
        _ => 64 * 1024 * 1024,
    }
}

fn profile_supports_path(path: &str, model: &str) -> bool {
    match path {
        "/v1/chat/completions" => matches!(
            model,
            "z-ai/glm-5.2" | "microsoft/phi-4-multimodal-instruct" | "nvidia/vila"
        ),
        "/v1/embeddings" => model == "nvidia/nvclip",
        "/v1/images/generations" => model == "black-forest-labs/flux.1-kontext-dev",
        "/v1/videos/generations" => model == "stabilityai/stable-video-diffusion",
        "/v1/audio/speech" => model == "nvidia/magpie-tts-multilingual",
        "/v1/audio/transcriptions" => model == "nvidia/parakeet-ctc-1.1b",
        "/v1/nvidia/inference" => model == "stabilityai/stable-video-diffusion",
        _ => false,
    }
}

fn invalid_request(message: &str) -> HttpResponse {
    HttpResponse::BadRequest().json(json!({"error":{"message":message,"type":"invalid_request"}}))
}

fn retry_after_duration(response: &reqwest::Response) -> Option<Duration> {
    let value = response.headers().get("retry-after")?.to_str().ok()?.trim();
    if let Ok(seconds) = value.parse::<i64>() {
        return (1..=300)
            .contains(&seconds)
            .then(|| Duration::seconds(seconds));
    }
    let deadline = httpdate::parse_http_date(value).ok()?;
    let remaining = deadline.duration_since(std::time::SystemTime::now()).ok()?;
    let seconds = i64::try_from(remaining.as_secs()).ok()?.clamp(1, 300);
    Some(Duration::seconds(seconds))
}

fn retry_after_for_keys(keys: &[nvidia_build_lb_core::KeySummary]) -> Option<u64> {
    let now = Utc::now();
    if keys.is_empty()
        || keys.iter().any(|key| {
            key.enabled && key.verified && key.cooldown_until.is_none_or(|until| until <= now)
        })
    {
        return None;
    }
    keys.iter()
        .filter_map(|key| key.cooldown_until)
        .filter_map(|until| (until - now).num_seconds().try_into().ok())
        .min()
        .map(|seconds: u64| seconds.clamp(1, 300))
}

fn model_not_found() -> HttpResponse {
    HttpResponse::NotFound().json(json!({
        "error": {"message": "model is not advertised", "type": "model_not_found"}
    }))
}

fn validate_modality_fields(
    path: &str,
    object: &serde_json::Map<String, Value>,
) -> Result<(), HttpResponse> {
    let (required, allowed): (&[&str], &[&str]) = match path {
        "/v1/embeddings" => (
            &["input"],
            &["model", "input", "encoding_format", "dimensions", "user"],
        ),
        "/v1/images/generations" => (
            &["prompt"],
            &["model", "prompt", "n", "size", "response_format", "user"],
        ),
        "/v1/audio/speech" => (
            &["input"],
            &["model", "input", "voice", "response_format", "speed"],
        ),
        "/v1/videos/generations" => (
            &["input_reference"],
            &[
                "model",
                "input_reference",
                "seed",
                "cfg_scale",
                "motion_bucket_id",
            ],
        ),
        "/v1/nvidia/inference" => (&["input"], &["model", "input"]),
        _ => (&[], &["model"]),
    };
    for field in required {
        if !object.contains_key(*field) {
            return Err(invalid_request(&format!("{field} is required")));
        }
    }
    if let Some(unknown) = object
        .keys()
        .find(|field| !allowed.contains(&field.as_str()))
    {
        return Err(invalid_request(&format!("unsupported field: {unknown}")));
    }
    match path {
        "/v1/embeddings" => {
            let valid = object["input"]
                .as_str()
                .is_some_and(|value| !value.is_empty())
                || object["input"].as_array().is_some_and(|items| {
                    !items.is_empty()
                        && items.len() <= 64
                        && items
                            .iter()
                            .all(|item| item.as_str().is_some_and(|value| !value.is_empty()))
                });
            if valid {
                Ok(())
            } else {
                Err(invalid_request(
                    "input must be a non-empty string or an array of at most 64 strings",
                ))
            }
        }
        "/v1/images/generations" => {
            if !object["prompt"].is_string() {
                return Err(invalid_request("prompt must be a string"));
            }
            if let Some(size) = object.get("size")
                && !matches!(
                    size.as_str(),
                    Some("1024x1024" | "1792x1024" | "1536x864" | "1024x1792" | "864x1536")
                )
            {
                return Err(invalid_request(
                    "size must be one of 1024x1024, 1792x1024, 1536x864, 1024x1792, 864x1536",
                ));
            }
            if object.get("n").and_then(Value::as_u64).unwrap_or(1) != 1 {
                return Err(invalid_request("n must be 1"));
            }
            if let Some(format) = object.get("response_format")
                && format.as_str() != Some("b64_json")
            {
                return Err(invalid_request("response_format must be b64_json"));
            }
            Ok(())
        }
        "/v1/videos/generations" => {
            if object
                .get("input_reference")
                .and_then(Value::as_str)
                .is_none_or(|value| !valid_data_url(value, true))
            {
                return Err(invalid_request(
                    "input_reference must be a valid PNG or JPEG data URL",
                ));
            }
            validate_video_options(object)
        }
        "/v1/audio/speech" => {
            if !object["input"].is_string() {
                return Err(invalid_request("input must be a string"));
            }
            if let Some(format) = object.get("response_format")
                && format.as_str() != Some("wav")
            {
                return Err(invalid_request("response_format must be wav"));
            }
            if let Some(speed) = object.get("speed")
                && speed.as_f64() != Some(1.0)
            {
                return Err(invalid_request("speed must be 1.0"));
            }
            if let Some(voice) = object.get("voice")
                && voice.as_str().is_none_or(|value| {
                    value.trim().is_empty() || value.chars().any(char::is_control)
                })
            {
                return Err(invalid_request("voice must be a non-empty safe string"));
            }
            Ok(())
        }
        "/v1/nvidia/inference" => {
            let input = object
                .get("input")
                .ok_or_else(|| invalid_request("input is required"))?;
            let Some(input) = input.as_object() else {
                return Err(invalid_request("input must be an object"));
            };
            if let Some(unknown) = input.keys().find(|field| {
                !["image", "seed", "cfg_scale", "motion_bucket_id"].contains(&field.as_str())
            }) {
                return Err(invalid_request(&format!(
                    "unsupported input field: {unknown}"
                )));
            }
            if input
                .get("image")
                .and_then(Value::as_str)
                .is_none_or(|value| !valid_data_url(value, true))
            {
                return Err(invalid_request(
                    "input.image must be a valid PNG or JPEG data URL",
                ));
            }
            if let Some(seed) = input.get("seed")
                && seed
                    .as_i64()
                    .is_none_or(|value| !(0..=u32::MAX as i64).contains(&value))
            {
                return Err(invalid_request(
                    "seed must be an integer between 0 and 4294967295",
                ));
            }
            if let Some(cfg) = input.get("cfg_scale")
                && cfg.as_f64().is_none_or(|value| {
                    value.is_nan() || !(1.0..=9.0).contains(&value) || value == 1.0
                })
            {
                return Err(invalid_request(
                    "cfg_scale must be greater than 1 and at most 9",
                ));
            }
            if let Some(bucket) = input.get("motion_bucket_id")
                && bucket.as_i64() != Some(127)
            {
                return Err(invalid_request("motion_bucket_id must equal 127"));
            }
            Ok(())
        }
        _ => Ok(()),
    }
}

fn validate_video_options(object: &serde_json::Map<String, Value>) -> Result<(), HttpResponse> {
    if let Some(seed) = object.get("seed")
        && seed
            .as_i64()
            .is_none_or(|value| !(0..=u32::MAX as i64).contains(&value))
    {
        return Err(invalid_request(
            "seed must be an integer between 0 and 4294967295",
        ));
    }
    if let Some(cfg) = object.get("cfg_scale")
        && cfg
            .as_f64()
            .is_none_or(|value| value.is_nan() || !(1.0..=9.0).contains(&value) || value == 1.0)
    {
        return Err(invalid_request(
            "cfg_scale must be greater than 1 and at most 9",
        ));
    }
    if let Some(bucket) = object.get("motion_bucket_id")
        && bucket.as_i64() != Some(127)
    {
        return Err(invalid_request("motion_bucket_id must equal 127"));
    }
    Ok(())
}

fn validate_chat_request(request: &Value) -> Result<(), HttpResponse> {
    let object = request
        .as_object()
        .ok_or_else(|| invalid_request("request body must be a JSON object"))?;
    let model = object
        .get("model")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| invalid_request("model is required"))?;
    if !PROFILES.contains(&model) {
        return Err(model_not_found());
    }
    if !profile_supports_path("/v1/chat/completions", model) {
        return Err(HttpResponse::UnprocessableEntity().json(json!({
            "error": {"message": "model is not compatible with this endpoint", "type": "model_route_mismatch"}
        })));
    }
    const GLM_ALLOWED: &[&str] = &[
        "model",
        "messages",
        "stream",
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "stop",
        "tools",
        "tool_choice",
        "response_format",
        "user",
        "n",
        "seed",
        "frequency_penalty",
        "presence_penalty",
        "metadata",
        "chat_template_kwargs",
    ];
    let allowed = if model == "z-ai/glm-5.2" {
        GLM_ALLOWED
    } else {
        &[
            "model",
            "messages",
            "stream",
            "temperature",
            "top_p",
            "max_tokens",
            "seed",
        ]
    };
    if let Some(unknown) = object
        .keys()
        .find(|field| !allowed.contains(&field.as_str()))
    {
        return Err(invalid_request(&format!("unsupported field: {unknown}")));
    }
    let messages = object
        .get("messages")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid_request("messages must be an array"))?;
    if messages.is_empty() {
        return Err(invalid_request(
            "messages must contain valid role/content entries",
        ));
    }
    let allowed_content = match model {
        "microsoft/phi-4-multimodal-instruct" => &["text", "image_url", "audio_url"][..],
        "nvidia/vila" => &["text", "image_url", "video_url"][..],
        _ => &["text"][..],
    };
    for message in messages {
        let Some(message) = message.as_object() else {
            return Err(invalid_request(
                "messages must contain valid role/content entries",
            ));
        };
        if !matches!(
            message.get("role").and_then(Value::as_str),
            Some("system" | "user" | "assistant" | "tool")
        ) {
            return Err(invalid_request("message role is not supported"));
        }
        let Some(content) = message.get("content") else {
            return Err(invalid_request("message content is required"));
        };
        if content.is_null()
            && message.get("role").and_then(Value::as_str) == Some("assistant")
            && (message.get("tool_calls").is_some_and(Value::is_array)
                || message.get("function_call").is_some_and(Value::is_object))
        {
            continue;
        }
        if !validate_chat_content(content, allowed_content) {
            return Err(invalid_request(
                "message content does not match the selected model",
            ));
        }
    }
    Ok(())
}

fn validate_chat_content(content: &Value, allowed_types: &[&str]) -> bool {
    if let Some(text) = content.as_str() {
        return !text.is_empty();
    }
    let Some(items) = content.as_array() else {
        return false;
    };
    !items.is_empty()
        && items.iter().all(|item| {
            let Some(item) = item.as_object() else {
                return false;
            };
            let Some(kind) = item.get("type").and_then(Value::as_str) else {
                return false;
            };
            if !allowed_types.contains(&kind) {
                return false;
            }
            match kind {
                "text" => {
                    item.get("text")
                        .and_then(Value::as_str)
                        .is_some_and(|value| !value.is_empty())
                        && item
                            .keys()
                            .all(|key| matches!(key.as_str(), "type" | "text"))
                }
                "image_url" | "audio_url" | "video_url" => {
                    item.keys().all(|key| {
                        matches!(
                            key.as_str(),
                            "type" | "image_url" | "audio_url" | "video_url"
                        )
                    }) && item
                        .get(kind)
                        .is_some_and(|value| value.is_string() || value.is_object())
                }
                _ => false,
            }
        })
}

/// Resolve a modality endpoint from the configured chat endpoint without
/// duplicating the `/v1` prefix. Incoming bytes and their content type are
/// forwarded unchanged, so multipart uploads and binary responses remain
/// compatible with OpenAI-style clients.
fn upstream_endpoint(configured: &str, path: &str) -> String {
    let configured = configured.trim_end_matches('/');
    let versionless_path = path.strip_prefix("/v1").unwrap_or(path);
    if let Some(prefix) = configured.strip_suffix("/chat/completions") {
        return format!("{prefix}{versionless_path}");
    }
    if configured.ends_with("/v1") {
        return format!("{configured}{versionless_path}");
    }
    format!("{configured}{path}")
}

fn upstream_endpoint_for(configured: &str, path: &str, model: &str) -> String {
    if configured.starts_with("mock://") {
        return configured.to_owned();
    }
    let configured = configured.trim_end_matches('/');
    let is_default_nvidia = matches!(
        configured,
        "https://integrate.api.nvidia.com/v1/chat/completions"
            | "https://integrate.api.nvidia.com/v1"
    );
    if is_default_nvidia {
        return match model {
            "nvidia/vila" => "https://ai.api.nvidia.com/v1/vlm/nvidia/vila".to_owned(),
            "black-forest-labs/flux.1-kontext-dev" => {
                "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-kontext-dev".to_owned()
            }
            "stabilityai/stable-video-diffusion" => {
                "https://ai.api.nvidia.com/v1/genai/stabilityai/stable-video-diffusion".to_owned()
            }
            "microsoft/phi-4-multimodal-instruct" => {
                "https://integrate.api.nvidia.com/v1/chat/completions".to_owned()
            }
            "nvidia/nvclip" => "https://integrate.api.nvidia.com/v1/embeddings".to_owned(),
            "nvidia/magpie-tts-multilingual" => magpie_tts_endpoint(),
            "nvidia/parakeet-ctc-1.1b" => upstream_endpoint(configured, "/v1/audio/transcriptions"),
            _ => upstream_endpoint(configured, path),
        };
    }
    upstream_endpoint(configured, path)
}

fn magpie_tts_endpoint() -> String {
    env::var("NBLB_MAGPIE_TTS_ENDPOINT").unwrap_or_else(|_| {
        "https://877104f7-e885-42b9-8de8-f6e4c6303969.invocation.api.nvcf.nvidia.com/v1/audio/synthesize".to_owned()
    })
}

fn prepare_modality_request(path: &str, request: &Value) -> Result<Value, HttpResponse> {
    let object = request
        .as_object()
        .ok_or_else(|| invalid_request("request body must be a JSON object"))?;
    match path {
        "/v1/images/generations" => {
            let prompt = object
                .get("prompt")
                .and_then(Value::as_str)
                .ok_or_else(|| invalid_request("prompt must be a string"))?;
            let size = object
                .get("size")
                .and_then(Value::as_str)
                .unwrap_or("1024x1024");
            let aspect_ratio = match size {
                "1024x1024" => "1:1",
                "1792x1024" | "1536x864" => "16:9",
                "1024x1792" | "864x1536" => "9:16",
                _ => return Err(invalid_request("size is not supported by FLUX")),
            };
            if object.get("n").and_then(Value::as_u64).unwrap_or(1) != 1 {
                return Err(invalid_request("FLUX supports exactly one image"));
            }
            Ok(
                json!({"prompt": prompt, "image": Value::Null, "aspect_ratio": aspect_ratio, "samples": 1}),
            )
        }
        "/v1/videos/generations" => {
            let input = object
                .get("input_reference")
                .and_then(Value::as_str)
                .filter(|value| {
                    value.starts_with("data:image/png;base64,")
                        || value.starts_with("data:image/jpeg;base64,")
                })
                .ok_or_else(|| invalid_request("input_reference must be a PNG or JPEG data URL"))?;
            let mut result = serde_json::Map::new();
            result.insert("image".to_owned(), Value::String(input.to_owned()));
            result.insert(
                "seed".to_owned(),
                object.get("seed").cloned().unwrap_or_else(|| json!(0)),
            );
            result.insert(
                "cfg_scale".to_owned(),
                object
                    .get("cfg_scale")
                    .cloned()
                    .unwrap_or_else(|| json!(1.8)),
            );
            result.insert(
                "motion_bucket_id".to_owned(),
                object
                    .get("motion_bucket_id")
                    .cloned()
                    .unwrap_or_else(|| json!(127)),
            );
            Ok(Value::Object(result))
        }
        "/v1/nvidia/inference" => {
            let input = object
                .get("input")
                .and_then(Value::as_object)
                .ok_or_else(|| invalid_request("input must be an object"))?;
            let image = input
                .get("image")
                .and_then(Value::as_str)
                .ok_or_else(|| invalid_request("input.image is required"))?;
            let mut result = serde_json::Map::new();
            result.insert("image".to_owned(), Value::String(image.to_owned()));
            result.insert(
                "seed".to_owned(),
                input.get("seed").cloned().unwrap_or_else(|| json!(0)),
            );
            result.insert(
                "cfg_scale".to_owned(),
                input
                    .get("cfg_scale")
                    .cloned()
                    .unwrap_or_else(|| json!(1.8)),
            );
            result.insert(
                "motion_bucket_id".to_owned(),
                input
                    .get("motion_bucket_id")
                    .cloned()
                    .unwrap_or_else(|| json!(127)),
            );
            Ok(Value::Object(result))
        }
        _ => Ok(request.clone()),
    }
}

async fn poll_nvcf(
    client: &reqwest::Client,
    response: reqwest::Response,
    _endpoint: &str,
    credential: &str,
) -> Result<reqwest::Response> {
    let request_id = response
        .headers()
        .get("nvcf-reqid")
        .and_then(|value| value.to_str().ok())
        .filter(|value| {
            value.len() == 36
                && value.bytes().enumerate().all(|(index, byte)| {
                    matches!(index, 8 | 13 | 18 | 23) && byte == b'-'
                        || !matches!(index, 8 | 13 | 18 | 23)
                            && (byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
                })
        })
        .context("NVCF response is missing canonical request id")?;
    let parsed = Uuid::parse_str(request_id).context("invalid NVCF request id")?;
    if parsed.to_string() != request_id {
        bail!("NVCF request id is not canonical lowercase UUID")
    }
    if response.status().as_u16() != 202 {
        bail!("NVCF polling requires origin 202")
    }
    let poll_endpoint = format!("https://api.nvcf.nvidia.com/v2/nvcf/pexec/status/{request_id}");
    for header_name in ["location", "nvcf-status-url"] {
        if let Some(value) = response
            .headers()
            .get(header_name)
            .and_then(|value| value.to_str().ok())
            && value != poll_endpoint
        {
            bail!("NVCF returned a non-canonical polling endpoint")
        }
    }
    let mut delay = StdDuration::from_millis(250);
    let deadline = Instant::now() + StdDuration::from_secs(30);
    loop {
        if Instant::now() >= deadline {
            bail!("NVCF request polling timed out")
        }
        tokio::time::sleep(delay).await;
        if Instant::now() >= deadline {
            bail!("NVCF request polling timed out")
        }
        let response = client
            .get(&poll_endpoint)
            .bearer_auth(credential)
            .header(reqwest::header::ACCEPT, "application/json")
            .timeout(NVCF_POLL_TIMEOUT)
            .send()
            .await
            .context("poll NVCF request")?;
        if response.status().is_success() {
            let content_type = response
                .headers()
                .get(reqwest::header::CONTENT_TYPE)
                .and_then(|value| value.to_str().ok())
                .unwrap_or_default();
            if !content_type
                .split(';')
                .next()
                .is_some_and(|value| value.trim().eq_ignore_ascii_case("application/json"))
            {
                bail!("NVCF poll returned a non-JSON success")
            }
            return Ok(response);
        }
        if response.status().as_u16() != 202 {
            bail!("NVCF poll returned {}", response.status());
        }
        delay = (delay * 2).min(StdDuration::from_secs(2));
    }
}

fn mock_modality(path: &str, request: &Value) -> (Vec<u8>, &'static str) {
    let model = request
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or("nvidia/multimodal");
    match path {
        "/v1/embeddings" => {
            let embedding = vec![0.0_f64; 1024];
            (
                serde_json::to_vec(&json!({
                    "object":"list",
                    "data":[{"object":"embedding","index":0,"embedding":embedding}],
                    "model":model,
                    "usage":{"prompt_tokens":1,"total_tokens":1}
                }))
                .expect("mock embedding fixture is serializable"),
                "application/json",
            )
        }
        "/v1/images/generations" => {
            let encoded = base64::engine::general_purpose::STANDARD.encode(mock_jpeg());
            (
                serde_json::to_vec(&json!({
                    "artifacts":[{"base64":encoded,"finishReason":"SUCCESS","seed":0}],
                    "model":model
                }))
                .expect("mock image fixture is serializable"),
                "application/json",
            )
        }
        "/v1/audio/speech" => (mock_wav(), "audio/wav"),
        "/v1/audio/transcriptions" => (
            serde_json::to_vec(&json!({"text":"NVIDIA Build LB","model":model}))
                .expect("mock transcription fixture is serializable"),
            "application/json",
        ),
        _ => {
            let encoded = base64::engine::general_purpose::STANDARD.encode(mock_mp4());
            (
                serde_json::to_vec(&json!({
                    "video":encoded,
                    "finish_reason":"SUCCESS",
                    "seed":0,
                    "model":model
                }))
                .expect("mock video fixture is serializable"),
                "application/json",
            )
        }
    }
}

fn mock_jpeg() -> Vec<u8> {
    base64::engine::general_purpose::STANDARD
        .decode("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAH/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAEFAqf/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAEDAQE/AYf/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAECAQE/AYf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAY/Av/Z")
        .expect("built-in JPEG fixture is valid base64")
}

fn mock_wav() -> Vec<u8> {
    let mut bytes = vec![0_u8; 48];
    bytes[0..4].copy_from_slice(b"RIFF");
    bytes[4..8].copy_from_slice(&40_u32.to_le_bytes());
    bytes[8..12].copy_from_slice(b"WAVE");
    bytes[12..16].copy_from_slice(b"fmt ");
    bytes[16..20].copy_from_slice(&16_u32.to_le_bytes());
    bytes[20..22].copy_from_slice(&1_u16.to_le_bytes());
    bytes[22..24].copy_from_slice(&1_u16.to_le_bytes());
    bytes[24..28].copy_from_slice(&44_100_u32.to_le_bytes());
    bytes[34..36].copy_from_slice(&16_u16.to_le_bytes());
    bytes[36..40].copy_from_slice(b"data");
    bytes[40..44].copy_from_slice(&4_u32.to_le_bytes());
    bytes[44..48].copy_from_slice(&[0, 0, 0, 0]);
    bytes
}

fn mock_mp4() -> Vec<u8> {
    vec![
        0, 0, 0, 24, b'f', b't', b'y', b'p', b'i', b's', b'o', b'm', 0, 0, 0, 0, b'i', b's', b'o',
        b'm', b'm', b'p', b'4', 0, 0, 0, 8, b'm', b'o', b'o', b'v',
    ]
}

fn mock_response(request: &Value, stream: bool) -> HttpResponse {
    let model = request
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or("z-ai/glm-5.2");
    let id = format!("chatcmpl-{}", Uuid::new_v4());
    if stream {
        let first = Bytes::from(format!(
            "data: {}\n\n",
            json!({"id":id,"object":"chat.completion.chunk","model":model,"choices":[{"index":0,"delta":{"role":"assistant","content":"NVIDIA Build LB"},"finish_reason":null}]})
        ));
        let terminal = Bytes::from(format!(
            "data: {}\n\ndata: [DONE]\n\n",
            json!({"id":id,"object":"chat.completion.chunk","model":model,"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]})
        ));
        let chunks = vec![first, terminal];
        let delay = env::var("NBLB_MOCK_STREAM_DELAY_MS")
            .ok()
            .and_then(|value| value.parse::<u64>().ok())
            .map(StdDuration::from_millis);
        let body = stream::unfold((chunks, 0_usize), move |(chunks, index)| async move {
            let chunk = chunks.get(index).cloned()?;
            if let Some(delay) = delay {
                tokio::time::sleep(delay).await;
            }
            Some((Ok::<Bytes, actix_web::Error>(chunk), (chunks, index + 1)))
        });
        HttpResponse::Ok()
            .insert_header(("content-type", "text/event-stream"))
            .insert_header(("cache-control", "no-cache"))
            .streaming(body)
    } else {
        HttpResponse::Ok().json(json!({"id":id,"object":"chat.completion","model":model,"choices":[{"index":0,"message":{"role":"assistant","content":"NVIDIA Build LB"},"finish_reason":"stop"}],"usage":{"prompt_tokens":0,"completion_tokens":3,"total_tokens":3}}))
    }
}

/// Mock streaming uses the same durable stream guard as a real provider. The
/// first frame is intentionally yielded by the body stream (rather than
/// before the response is returned), so a client timeout exercises the actual
/// disconnect/cancellation path in PostgreSQL smoke.
fn mock_stream_response(
    request: &Value,
    state: web::Data<AppState>,
    request_id: Uuid,
    key_id: Uuid,
) -> HttpResponse {
    let model = request
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or("z-ai/glm-5.2")
        .to_owned();
    let id = format!("chatcmpl-{}", Uuid::new_v4());
    let chunks = vec![
        Bytes::from(format!(
            "data: {}\n\n",
            json!({"id":id,"object":"chat.completion.chunk","model":model,"choices":[{"index":0,"delta":{"role":"assistant","content":"NVIDIA Build LB"},"finish_reason":null}]})
        )),
        Bytes::from(format!(
            "data: {}\n\ndata: [DONE]\n\n",
            json!({"id":id,"object":"chat.completion.chunk","model":model,"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]})
        )),
    ];
    let delay = env::var("NBLB_MOCK_STREAM_DELAY_MS")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .map(StdDuration::from_millis);
    let upstream: UpstreamByteStream = Box::pin(stream::unfold(
        (chunks, 0_usize),
        move |(chunks, index)| async move {
            let chunk = chunks.get(index).cloned()?;
            if let Some(delay) = delay {
                tokio::time::sleep(delay).await;
            }
            Some((Ok::<Bytes, reqwest::Error>(chunk), (chunks, index + 1)))
        },
    ));
    let downstream = chat_response_stream(
        upstream,
        SseValidator::default(),
        VecDeque::new(),
        state.clone(),
        request_id,
        key_id,
        StreamAttemptGuard::new(state, request_id, key_id),
    );
    HttpResponse::Ok()
        .insert_header(("content-type", "text/event-stream"))
        .insert_header(("cache-control", "no-cache"))
        .streaming(downstream)
}

fn validate_chat_response(body: &[u8], stream: bool) -> Result<(), ()> {
    if stream {
        let mut validator = SseValidator::default();
        validator.feed(body)?;
        return validator.finish();
    }
    let value: Value = serde_json::from_slice(body).map_err(|_| ())?;
    let object = value.as_object().ok_or(())?;
    if object.keys().any(|key| {
        !matches!(
            key.as_str(),
            "id" | "object"
                | "created"
                | "model"
                | "choices"
                | "usage"
                | "system_fingerprint"
                | "service_tier"
                | "prompt_filter_results"
                | "reasoning"
                | "reasoning_content"
        )
    }) || object.get("object").and_then(Value::as_str) != Some("chat.completion")
        || object
            .get("id")
            .and_then(Value::as_str)
            .is_none_or(str::is_empty)
        || object
            .get("model")
            .and_then(Value::as_str)
            .is_none_or(str::is_empty)
    {
        return Err(());
    }
    if let Some(created) = object.get("created")
        && created.as_i64().is_none_or(|value| value < 0)
    {
        return Err(());
    }
    let choices = object.get("choices").and_then(Value::as_array).ok_or(())?;
    if choices.is_empty() {
        return Err(());
    }
    for choice in choices {
        let choice = choice.as_object().ok_or(())?;
        if choice.keys().any(|key| {
            !matches!(
                key.as_str(),
                "index" | "message" | "finish_reason" | "logprobs" | "content_filter_results"
            )
        }) || choice.get("index").and_then(Value::as_u64).is_none()
            || choice
                .get("finish_reason")
                .is_none_or(|value| !value.is_null() && !value.is_string())
        {
            return Err(());
        }
        let message = choice.get("message").and_then(Value::as_object).ok_or(())?;
        if message.keys().any(|key| {
            !matches!(
                key.as_str(),
                "role"
                    | "content"
                    | "tool_calls"
                    | "function_call"
                    | "refusal"
                    | "audio"
                    | "annotations"
            )
        }) || message
            .get("role")
            .and_then(Value::as_str)
            .is_none_or(str::is_empty)
            || message
                .get("content")
                .is_some_and(|value| !value.is_string() && !value.is_null())
            || message
                .get("refusal")
                .is_some_and(|value| !value.is_string() && !value.is_null())
            || message
                .get("tool_calls")
                .is_some_and(|value| !value.is_array())
            || message
                .get("annotations")
                .is_some_and(|value| !value.is_array())
        {
            return Err(());
        }
    }
    if let Some(usage) = object.get("usage") {
        let usage = usage.as_object().ok_or(())?;
        if usage.keys().any(|key| {
            !matches!(
                key.as_str(),
                "prompt_tokens"
                    | "completion_tokens"
                    | "total_tokens"
                    | "prompt_tokens_details"
                    | "completion_tokens_details"
            )
        }) || usage.iter().any(|(key, value)| {
            if matches!(
                key.as_str(),
                "prompt_tokens" | "completion_tokens" | "total_tokens"
            ) {
                value.as_u64().is_none()
            } else {
                !value.is_object() && !value.is_null()
            }
        }) {
            return Err(());
        }
    }
    Ok(())
}

fn sse_error_frame(message: &str) -> Bytes {
    // Once an SSE response has started, an HTTP status cannot be changed. A
    // bounded error event followed by the normal terminator prevents clients
    // from waiting forever without reflecting upstream headers or secrets.
    Bytes::from(format!(
        "event: error\ndata: {}\n\ndata: [DONE]\n\n",
        json!({"error":{"message":message,"type":"upstream_stream_error"}})
    ))
}

type UpstreamByteStream = Pin<Box<dyn Stream<Item = Result<Bytes, reqwest::Error>> + Send>>;

async fn prime_stream(
    mut upstream: UpstreamByteStream,
) -> Result<(UpstreamByteStream, SseValidator, VecDeque<Bytes>), ()> {
    let mut validator = SseValidator::default();
    let mut prefix = VecDeque::new();
    loop {
        match upstream.next().await {
            Some(Ok(chunk)) => {
                validator.feed(&chunk)?;
                prefix.extend(validator.take_emitted());
                // A bare [DONE] is not a usable completion.  Do not hand the
                // response to Actix until at least one validated data chunk
                // exists, otherwise failover is lost after a premature
                // provider terminator.
                if validator.data_frame_count > 0 {
                    return Ok((upstream, validator, prefix));
                }
                if prefix.len() > 256 {
                    return Err(());
                }
            }
            Some(Err(_)) | None => return Err(()),
        }
    }
}

struct StreamAttemptGuard {
    state: web::Data<AppState>,
    request_id: Uuid,
    key_id: Uuid,
    terminal: Arc<AtomicBool>,
}

impl StreamAttemptGuard {
    fn new(state: web::Data<AppState>, request_id: Uuid, key_id: Uuid) -> Self {
        Self {
            state,
            request_id,
            key_id,
            terminal: Arc::new(AtomicBool::new(false)),
        }
    }
}

impl Drop for StreamAttemptGuard {
    fn drop(&mut self) {
        if self.terminal.swap(true, Ordering::AcqRel) {
            return;
        }
        let state = self.state.clone();
        let request_id = self.request_id;
        let key_id = self.key_id;
        // Actix drops the body stream on a client disconnect. The durable
        // terminal update is therefore scheduled from Drop so a started row
        // cannot survive an abandoned downstream connection.
        tokio::spawn(async move {
            if let Err(error) = state
                .vault
                .attempt_finished(request_id, key_id, "cancelled")
                .await
            {
                eprintln!("stream cancellation ledger update failed: {error:#}");
            }
        });
    }
}

async fn finish_stream_failure(state: &web::Data<AppState>, request_id: Uuid, key_id: Uuid) {
    if record_failure(state, key_id, None).await.is_err() {
        eprintln!("stream failure health update failed");
    }
    if let Err(error) = state
        .vault
        .attempt_finished(request_id, key_id, "failed")
        .await
    {
        eprintln!("stream failure ledger update failed: {error:#}");
    }
}

#[derive(Default)]
struct SseValidator {
    buffer: Vec<u8>,
    emitted: VecDeque<Bytes>,
    done: bool,
    frame_count: usize,
    data_frame_count: usize,
    message_id: Option<String>,
    model: Option<String>,
}

impl SseValidator {
    fn take_emitted(&mut self) -> VecDeque<Bytes> {
        std::mem::take(&mut self.emitted)
    }

    fn feed(&mut self, bytes: &[u8]) -> Result<(), ()> {
        if self.buffer.len().saturating_add(bytes.len()) > 256 * 1024 * 1024 {
            return Err(());
        }
        self.buffer.extend_from_slice(bytes);
        loop {
            let lf = self.buffer.windows(2).position(|pair| pair == b"\n\n");
            let crlf = self.buffer.windows(4).position(|pair| pair == b"\r\n\r\n");
            let (index, delimiter_len) = match (lf, crlf) {
                (Some(lf), Some(crlf)) if crlf < lf => (crlf, 4),
                (Some(lf), _) => (lf, 2),
                (None, Some(crlf)) => (crlf, 4),
                (None, None) => break,
            };
            let frame = self
                .buffer
                .drain(..index + delimiter_len)
                .collect::<Vec<_>>();
            self.validate_frame(&frame[..frame.len() - delimiter_len])?;
        }
        Ok(())
    }

    fn finish(&mut self) -> Result<(), ()> {
        if !self.buffer.iter().all(u8::is_ascii_whitespace) {
            return Err(());
        }
        if self.done && self.data_frame_count >= 1 {
            Ok(())
        } else {
            Err(())
        }
    }

    fn validate_frame(&mut self, frame: &[u8]) -> Result<(), ()> {
        if frame.len() > 1024 * 1024 {
            return Err(());
        }
        let frame = std::str::from_utf8(frame).map_err(|_| ())?;
        if frame.is_empty() {
            return Ok(());
        }
        if self.done {
            return Err(());
        }
        let mut data_lines = Vec::new();
        for line in frame.lines() {
            let line = line.strip_suffix('\r').unwrap_or(line);
            if line.is_empty() || line.starts_with(':') {
                continue;
            }
            if let Some(value) = line.strip_prefix("data:") {
                data_lines.push(value.strip_prefix(' ').unwrap_or(value));
                continue;
            }
            if let Some(value) = line.strip_prefix("event:") {
                if value.trim().len() > 64
                    || !value
                        .trim()
                        .bytes()
                        .all(|byte| (0x20..=0x7e).contains(&byte))
                {
                    return Err(());
                }
                continue;
            }
            if let Some(value) = line.strip_prefix("id:") {
                if value.trim().len() > 128 || value.contains('\0') {
                    return Err(());
                }
                continue;
            }
            if let Some(value) = line.strip_prefix("retry:") {
                if value.trim().is_empty()
                    || value.trim().len() > 6
                    || !value.trim().bytes().all(|byte| byte.is_ascii_digit())
                {
                    return Err(());
                }
                continue;
            }
            return Err(());
        }
        if data_lines.is_empty() {
            return Ok(());
        }
        let data = data_lines.join("\n");
        self.frame_count = self.frame_count.saturating_add(1);
        if data.trim() == "[DONE]" {
            self.done = true;
            return Ok(());
        }
        self.data_frame_count = self.data_frame_count.saturating_add(1);
        let value: Value = serde_json::from_str(&data).map_err(|_| ())?;
        let object = value.as_object().ok_or(())?;
        if object.keys().any(|key| {
            !matches!(
                key.as_str(),
                "id" | "object"
                    | "model"
                    | "choices"
                    | "created"
                    | "system_fingerprint"
                    | "service_tier"
                    | "usage"
                    | "reasoning"
                    | "reasoning_content"
            )
        }) {
            return Err(());
        }
        let id = value.get("id").and_then(Value::as_str).ok_or(())?;
        let model = value.get("model").and_then(Value::as_str).ok_or(())?;
        if value.get("object").and_then(Value::as_str) != Some("chat.completion.chunk")
            || id.is_empty()
            || model.is_empty()
            || value.get("choices").and_then(Value::as_array).is_none()
        {
            return Err(());
        }
        if self
            .message_id
            .as_deref()
            .is_some_and(|previous| previous != id)
            || self
                .model
                .as_deref()
                .is_some_and(|previous| previous != model)
        {
            return Err(());
        }
        self.message_id = Some(id.to_owned());
        self.model = Some(model.to_owned());
        let choices = value.get("choices").and_then(Value::as_array).ok_or(())?;
        if choices.is_empty() {
            return Err(());
        }
        if choices.iter().any(|choice| {
            !choice.is_object()
                || choice.as_object().is_some_and(|choice| {
                    choice.keys().any(|key| {
                        !matches!(
                            key.as_str(),
                            "index"
                                | "delta"
                                | "finish_reason"
                                | "logprobs"
                                | "content_filter_results"
                        )
                    })
                })
                || choice.get("index").and_then(Value::as_u64).is_none()
                || choice
                    .get("delta")
                    .and_then(Value::as_object)
                    .is_none_or(|delta| {
                        delta.keys().any(|key| {
                            !matches!(
                                key.as_str(),
                                "role"
                                    | "content"
                                    | "tool_calls"
                                    | "function_call"
                                    | "refusal"
                                    | "audio"
                            )
                        })
                    })
                || !choice
                    .get("finish_reason")
                    .is_some_and(|reason| reason.is_null() || reason.is_string())
        }) {
            return Err(());
        }
        self.emitted
            .push_back(Bytes::from(format!("data: {data}\n\n")));
        Ok(())
    }
}

fn chat_response_stream(
    upstream: UpstreamByteStream,
    validator: SseValidator,
    prefix: VecDeque<Bytes>,
    state: web::Data<AppState>,
    request_id: Uuid,
    key_id: Uuid,
    guard: StreamAttemptGuard,
) -> impl Stream<Item = Result<Bytes, actix_web::Error>> {
    stream::unfold(
        (
            upstream, validator, prefix, state, request_id, key_id, guard, false,
        ),
        |(
            mut upstream,
            mut validator,
            mut prefix,
            state,
            request_id,
            key_id,
            guard,
            mut terminal,
        )| async move {
            if terminal {
                return None;
            }
            loop {
                if let Some(chunk) = prefix.pop_front() {
                    return Some((
                        Ok(chunk),
                        (
                            upstream, validator, prefix, state, request_id, key_id, guard, terminal,
                        ),
                    ));
                }
                match upstream.next().await {
                    Some(Ok(chunk)) => {
                        if validator.feed(&chunk).is_err() {
                            finish_stream_failure(&state, request_id, key_id).await;
                            terminal = true;
                            guard.terminal.store(true, Ordering::Release);
                            return Some((
                                Ok(sse_error_frame("invalid upstream stream")),
                                (
                                    upstream, validator, prefix, state, request_id, key_id, guard,
                                    terminal,
                                ),
                            ));
                        }
                        prefix.extend(validator.take_emitted());
                        continue;
                    }
                    Some(Err(_)) => {
                        finish_stream_failure(&state, request_id, key_id).await;
                        terminal = true;
                        guard.terminal.store(true, Ordering::Release);
                        return Some((
                            Ok(sse_error_frame("upstream stream failed")),
                            (
                                upstream, validator, prefix, state, request_id, key_id, guard,
                                terminal,
                            ),
                        ));
                    }
                    None => {
                        if validator.finish().is_err() {
                            finish_stream_failure(&state, request_id, key_id).await;
                            guard.terminal.store(true, Ordering::Release);
                            return Some((
                                Ok(sse_error_frame("incomplete upstream stream")),
                                (
                                    upstream, validator, prefix, state, request_id, key_id, guard,
                                    true,
                                ),
                            ));
                        }
                        if record_request(&state, key_id).await.is_err() {
                            if let Err(error) = state
                                .vault
                                .attempt_finished(request_id, key_id, "failed")
                                .await
                            {
                                eprintln!("stream accounting ledger update failed: {error:#}");
                            }
                            guard.terminal.store(true, Ordering::Release);
                            return Some((
                                Ok(sse_error_frame("request accounting unavailable")),
                                (
                                    upstream, validator, prefix, state, request_id, key_id, guard,
                                    true,
                                ),
                            ));
                        }
                        if let Err(error) = state
                            .vault
                            .attempt_finished(request_id, key_id, "succeeded")
                            .await
                        {
                            eprintln!("stream success ledger update failed: {error:#}");
                            guard.terminal.store(true, Ordering::Release);
                            return Some((
                                Ok(sse_error_frame("request ledger unavailable")),
                                (
                                    upstream, validator, prefix, state, request_id, key_id, guard,
                                    true,
                                ),
                            ));
                        }
                        guard.terminal.store(true, Ordering::Release);
                        // Do not expose the provider's success terminator until
                        // the durable request and attempt ledger commits have
                        // succeeded.  A client must never observe `[DONE]` for
                        // a request the gateway recorded as failed.
                        return Some((
                            Ok(Bytes::from_static(b"data: [DONE]\n\n")),
                            (
                                upstream, validator, prefix, state, request_id, key_id, guard, true,
                            ),
                        ));
                    }
                }
            }
        },
    )
}

fn decode_base64(value: &str) -> Option<Vec<u8>> {
    if value.is_empty() || value.bytes().any(|byte| byte.is_ascii_whitespace()) {
        return None;
    }
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(value.as_bytes())
        .ok()?;
    (base64::engine::general_purpose::STANDARD.encode(&bytes) == value).then_some(bytes)
}

fn valid_jpeg(bytes: &[u8]) -> bool {
    if bytes.len() < 16 || !bytes.starts_with(&[0xff, 0xd8]) || !bytes.ends_with(&[0xff, 0xd9]) {
        return false;
    }
    let mut index = 2;
    let mut has_frame = false;
    while index + 3 < bytes.len().saturating_sub(2) {
        if bytes[index] != 0xff {
            index += 1;
            continue;
        }
        while index < bytes.len() && bytes[index] == 0xff {
            index += 1;
        }
        if index >= bytes.len() {
            break;
        }
        let marker = bytes[index];
        index += 1;
        if marker == 0xd9 || marker == 0xda {
            break;
        }
        if marker == 0xd8 || marker == 0x01 || (0xd0..=0xd7).contains(&marker) {
            continue;
        }
        if index + 2 > bytes.len() {
            return false;
        }
        let segment_len = u16::from_be_bytes([bytes[index], bytes[index + 1]]) as usize;
        if segment_len < 2 || index + segment_len > bytes.len() {
            return false;
        }
        if (0xc0..=0xc3).contains(&marker) {
            if segment_len < 7 {
                return false;
            }
            let height = u16::from_be_bytes([bytes[index + 3], bytes[index + 4]]);
            let width = u16::from_be_bytes([bytes[index + 5], bytes[index + 6]]);
            has_frame = width > 0 && height > 0;
        }
        index += segment_len;
    }
    has_frame
}

fn valid_image(bytes: &[u8]) -> bool {
    valid_jpeg(bytes) || valid_png(bytes)
}

fn valid_png(bytes: &[u8]) -> bool {
    bytes.len() >= 24
        && bytes.starts_with(b"\x89PNG\r\n\x1a\n")
        && bytes.windows(4).any(|chunk| chunk == b"IEND")
}

fn valid_mp4(bytes: &[u8]) -> bool {
    if bytes.len() < 16 || &bytes[4..8] != b"ftyp" {
        return false;
    }
    let size = u32::from_be_bytes(bytes[0..4].try_into().unwrap_or_default()) as usize;
    size >= 16
        && size <= bytes.len()
        && bytes[8..size].windows(4).any(|brand| brand == b"isom")
        && bytes.windows(4).any(|atom| atom == b"moov")
}

fn valid_wav(bytes: &[u8]) -> bool {
    if bytes.len() < 44 || &bytes[0..4] != b"RIFF" || &bytes[8..12] != b"WAVE" {
        return false;
    }
    let fmt_size = u32::from_le_bytes(bytes[16..20].try_into().unwrap_or_default()) as usize;
    let channels = u16::from_le_bytes(bytes[22..24].try_into().unwrap_or_default());
    let sample_rate = u32::from_le_bytes(bytes[24..28].try_into().unwrap_or_default());
    let bits = u16::from_le_bytes(bytes[34..36].try_into().unwrap_or_default());
    let riff_size = u32::from_le_bytes(bytes[4..8].try_into().unwrap_or_default()) as usize;
    let Some(data_offset) = bytes.windows(4).position(|chunk| chunk == b"data") else {
        return false;
    };
    if data_offset + 8 > bytes.len() {
        return false;
    }
    let data_size = u32::from_le_bytes(
        bytes[data_offset + 4..data_offset + 8]
            .try_into()
            .unwrap_or_default(),
    ) as usize;
    fmt_size >= 16
        && channels == 1
        && sample_rate == 44_100
        && bits == 16
        && riff_size + 8 == bytes.len()
        && data_size > 0
        && data_offset + 8 + data_size == bytes.len()
}

fn valid_data_url(value: &str, image_only: bool) -> bool {
    let Some((prefix, encoded)) = value.split_once(",") else {
        return false;
    };
    let Some(bytes) = decode_base64(encoded) else {
        return false;
    };
    match (prefix, image_only) {
        ("data:image/png;base64", true) => valid_png(&bytes),
        ("data:image/jpeg;base64", true) => valid_jpeg(&bytes),
        _ => false,
    }
}

fn validate_modality_response(path: &str, body: &[u8], content_type: &str) -> Result<(), ()> {
    let media_type = content_type.split(';').next().unwrap_or_default().trim();
    if path == "/v1/audio/speech" && matches!(media_type, "audio/wav" | "audio/x-wav") {
        return valid_wav(body).then_some(()).ok_or(());
    }
    let value: Value = serde_json::from_slice(body).map_err(|_| ())?;
    let valid = match path {
        "/v1/embeddings" => {
            value.as_object().is_some_and(|object| {
                object
                    .keys()
                    .all(|key| matches!(key.as_str(), "object" | "data" | "model" | "usage"))
            }) && value.get("object").and_then(Value::as_str) == Some("list")
                && value.get("model").and_then(Value::as_str) == Some("nvidia/nvclip")
                && value
                    .get("data")
                    .and_then(Value::as_array)
                    .is_some_and(|items| {
                        !items.is_empty()
                            && items.iter().enumerate().all(|(index, item)| {
                                item.get("object").and_then(Value::as_str) == Some("embedding")
                                    && item.get("index").and_then(Value::as_u64)
                                        == Some(index as u64)
                                    && item.get("embedding").and_then(Value::as_array).is_some_and(
                                        |vector| {
                                            vector.len() == 1024
                                                && vector.iter().all(|value| {
                                                    value.as_f64().is_some_and(f64::is_finite)
                                                })
                                        },
                                    )
                            })
                    })
                && value
                    .get("usage")
                    .and_then(Value::as_object)
                    .is_some_and(|usage| {
                        usage.contains_key("prompt_tokens")
                            && usage.contains_key("total_tokens")
                            && usage
                                .keys()
                                .all(|key| matches!(key.as_str(), "prompt_tokens" | "total_tokens"))
                            && usage.values().all(|value| value.as_u64().is_some())
                    })
        }
        "/v1/images/generations" => {
            value
                .get("data")
                .and_then(Value::as_array)
                .is_some_and(|items| {
                    !items.is_empty()
                        && items.iter().all(|item| {
                            item.get("b64_json")
                                .and_then(Value::as_str)
                                .and_then(decode_base64)
                                .is_some_and(|bytes| valid_image(&bytes))
                        })
                })
                || value
                    .get("artifacts")
                    .and_then(Value::as_array)
                    .is_some_and(|items| {
                        !items.is_empty()
                            && items.iter().all(|item| {
                                item.get("finishReason").and_then(Value::as_str) == Some("SUCCESS")
                                    && item.get("seed").and_then(Value::as_i64).is_some()
                                    && item
                                        .get("base64")
                                        .and_then(Value::as_str)
                                        .and_then(decode_base64)
                                        .is_some_and(|bytes| valid_image(&bytes))
                            })
                    })
        }
        "/v1/audio/speech" => value
            .get("audio")
            .or_else(|| value.get("data"))
            .and_then(Value::as_str)
            .and_then(decode_base64)
            .is_some_and(|bytes| valid_wav(&bytes)),
        "/v1/audio/transcriptions" => value
            .get("text")
            .and_then(Value::as_str)
            .is_some_and(|text| !text.trim().is_empty()),
        "/v1/videos/generations" => {
            (value
                .get("video")
                .and_then(Value::as_str)
                .and_then(decode_base64)
                .is_some_and(|bytes| valid_mp4(&bytes))
                && value.get("finish_reason").and_then(Value::as_str) == Some("SUCCESS")
                && value.get("seed").and_then(Value::as_i64).is_some())
                || value
                    .get("data")
                    .and_then(Value::as_array)
                    .is_some_and(|items| {
                        !items.is_empty()
                            && items.iter().all(|item| {
                                item.get("b64_json")
                                    .and_then(Value::as_str)
                                    .and_then(decode_base64)
                                    .is_some_and(|bytes| valid_mp4(&bytes))
                            })
                    })
        }
        "/v1/nvidia/inference" => {
            value
                .get("video")
                .and_then(Value::as_str)
                .and_then(decode_base64)
                .is_some_and(|bytes| valid_mp4(&bytes))
                && value.get("finish_reason").and_then(Value::as_str) == Some("SUCCESS")
                && value.get("seed").and_then(Value::as_i64).is_some()
        }
        _ => value.is_object(),
    };
    valid.then_some(()).ok_or(())
}

fn normalize_modality_response(
    path: &str,
    body: &[u8],
    content_type: &str,
) -> Result<(Vec<u8>, String), ()> {
    validate_modality_response(path, body, content_type)?;
    if path == "/v1/images/generations" {
        let value: Value = serde_json::from_slice(body).map_err(|_| ())?;
        if value.get("data").is_some() {
            return Ok((body.to_vec(), "application/json".to_owned()));
        }
        let artifacts = value.get("artifacts").and_then(Value::as_array).ok_or(())?;
        let data = artifacts
            .iter()
            .map(|item| {
                let encoded = item.get("base64").and_then(Value::as_str).ok_or(())?;
                if encoded.is_empty() {
                    return Err(());
                }
                Ok(json!({"b64_json": encoded}))
            })
            .collect::<Result<Vec<_>, ()>>()?;
        let normalized = json!({"created": Utc::now().timestamp(), "data": data});
        return serde_json::to_vec(&normalized)
            .map(|bytes| (bytes, "application/json".to_owned()))
            .map_err(|_| ());
    }
    if path == "/v1/videos/generations" {
        let value: Value = serde_json::from_slice(body).map_err(|_| ())?;
        if value.get("data").is_some() {
            return Ok((body.to_vec(), "application/json".to_owned()));
        }
        if let Some(video) = value.get("video").and_then(Value::as_str) {
            let normalized = json!({"data":[{"b64_json":video}],"model":value.get("model")});
            return serde_json::to_vec(&normalized)
                .map(|bytes| (bytes, "application/json".to_owned()))
                .map_err(|_| ());
        }
    }
    Ok((body.to_vec(), content_type.to_owned()))
}

async fn authorize_scope(
    req: &HttpRequest,
    state: &web::Data<AppState>,
    scope: &str,
) -> Result<(), HttpResponse> {
    let Some(token) = bearer(req) else {
        if !state.require_downstream_token {
            return Ok(());
        }
        return Err(downstream_unauthorized());
    };
    match state.vault.authenticate(token, scope).await {
        Ok(_) => Ok(()),
        Err(error) if error.to_string() == "insufficient scope" => Err(downstream_forbidden(scope)),
        Err(error) if error.to_string() == "invalid downstream credential" => {
            Err(downstream_unauthorized())
        }
        Err(_) => Err(HttpResponse::ServiceUnavailable().json(json!({
            "error": {"code": "credential_store_unavailable", "message": "Credential store is temporarily unavailable."}
        }))),
    }
}

fn authorized(req: &HttpRequest, state: &AppState) -> bool {
    admin_surface_allowed(req, state)
        && bearer(req).is_some_and(|value| {
            constant_time_equal(value.as_bytes(), state.admin_token.as_bytes())
        })
}

fn admin_surface_allowed(req: &HttpRequest, state: &AppState) -> bool {
    let mut hosts = req.headers().get_all(header::HOST);
    let Some(raw_host) = hosts.next().and_then(|value| value.to_str().ok()) else {
        return false;
    };
    if hosts.next().is_some() {
        return false;
    }
    if raw_host.contains(',') || !host_authority_well_formed(raw_host) {
        return false;
    }
    let (host, port) = parse_host_authority(raw_host);
    if !admin_host_allowed(&host)
        || port.is_some_and(|port| port != state.public_port)
        || port.is_none() && state.public_port != 80 && state.public_port != 443
    {
        return false;
    }
    let Some(origin) = req.headers().get(header::ORIGIN) else {
        return true;
    };
    let Ok(origin) = origin.to_str() else {
        return false;
    };
    let origin_host = format_origin_host(&host);
    origin == format!("https://{origin_host}:{}", state.public_port)
        || origin == format!("http://{origin_host}:{}", state.public_port)
        || (state.public_port == 80 && origin == format!("http://{origin_host}"))
        || (state.public_port == 443 && origin == format!("https://{origin_host}"))
}

fn parse_host_authority(raw: &str) -> (String, Option<u16>) {
    if let Some(value) = raw.strip_prefix('[')
        && let Some((host, remainder)) = value.split_once(']')
    {
        let port = remainder
            .strip_prefix(':')
            .and_then(|value| value.parse::<u16>().ok());
        return (host.to_ascii_lowercase(), port);
    }
    if let Some((host, port)) = raw.rsplit_once(':')
        && let Ok(port) = port.parse::<u16>()
    {
        return (host.to_ascii_lowercase(), Some(port));
    }
    (raw.to_ascii_lowercase(), None)
}

fn host_authority_well_formed(raw: &str) -> bool {
    if let Some(value) = raw.strip_prefix('[') {
        let Some((_, remainder)) = value.split_once(']') else {
            return false;
        };
        return remainder.is_empty()
            || (remainder.starts_with(':') && remainder[1..].parse::<u16>().is_ok());
    }
    if raw.contains('[') || raw.contains(']') || raw.is_empty() {
        return false;
    }
    match raw.matches(':').count() {
        0 => true,
        1 => raw
            .split_once(':')
            .is_some_and(|(host, port)| !host.is_empty() && port.parse::<u16>().is_ok()),
        _ => raw.parse::<std::net::Ipv6Addr>().is_ok(),
    }
}

fn format_origin_host(host: &str) -> String {
    if host.contains(':') {
        format!("[{host}]")
    } else {
        host.to_owned()
    }
}

fn admin_host_allowed(raw_host: &str) -> bool {
    if raw_host.is_empty() || raw_host.contains(',') || !host_authority_well_formed(raw_host) {
        return false;
    }
    if raw_host.eq_ignore_ascii_case("::1") {
        return true;
    }
    if let Some(value) = raw_host.strip_prefix('[') {
        let Some((host, remainder)) = value.split_once(']') else {
            return false;
        };
        if remainder.is_empty() {
            return host.eq_ignore_ascii_case("::1");
        }
        if !remainder.starts_with(':')
            || remainder[1..].parse::<u16>().is_err()
            || !host.eq_ignore_ascii_case("::1")
        {
            return false;
        }
        return true;
    }
    let host = raw_host
        .strip_prefix('[')
        .and_then(|value| value.split_once(']').map(|(host, _)| host))
        .unwrap_or_else(|| raw_host.split(':').next().unwrap_or_default())
        .to_ascii_lowercase();
    // The Cloudflare/public listener is deliberately API-only. Keeping the
    // owner surface loopback-only prevents an ingress hostname from becoming
    // an accidental admin bearer-token endpoint.
    matches!(host.as_str(), "localhost" | "127.0.0.1" | "::1")
}
fn bearer(req: &HttpRequest) -> Option<&str> {
    let mut values = req.headers().get_all(header::AUTHORIZATION);
    let value = values.next()?;
    if values.next().is_some() {
        return None;
    }
    value.to_str().ok().and_then(|v| v.strip_prefix("Bearer "))
}
fn admin_unauthorized() -> HttpResponse {
    HttpResponse::Unauthorized()
        .insert_header(("www-authenticate", "Bearer realm=\"nvidia-build-lb-admin\""))
        .json(json!({"error":{"code":"invalid_admin_token","message":"Invalid admin token."}}))
}
fn downstream_unauthorized() -> HttpResponse {
    HttpResponse::Unauthorized().insert_header(("www-authenticate", "Bearer realm=\"nvidia-build-lb\"")).json(json!({"error":{"code":"invalid_downstream_token","message":"Invalid downstream credential or scope."}}))
}
fn downstream_forbidden(scope: &str) -> HttpResponse {
    HttpResponse::Forbidden()
        .insert_header(("www-authenticate", format!("Bearer realm=\"nvidia-build-lb\", error=\"insufficient_scope\", scope=\"{scope}\"")))
        .json(json!({"error":{"code":"insufficient_scope","message":"The downstream credential lacks the required scope."}}))
}

fn constant_time_equal(left: &[u8], right: &[u8]) -> bool {
    let mut difference = left.len() ^ right.len();
    for index in 0..left.len().max(right.len()) {
        difference |= usize::from(
            left.get(index).copied().unwrap_or_default()
                ^ right.get(index).copied().unwrap_or_default(),
        );
    }
    difference == 0
}

fn eligible_key_count(keys: &[nvidia_build_lb_core::KeySummary]) -> usize {
    let now = Utc::now();
    keys.iter()
        .filter(|key| {
            key.enabled && key.verified && key.cooldown_until.is_none_or(|until| until <= now)
        })
        .count()
}

/// Serialize selection with cursor persistence. Without this boundary two
/// concurrent requests could persist their cursors in reverse completion order
/// and make restart resume from an older slot.
async fn select_key(state: &web::Data<AppState>, profile: &str) -> Option<Uuid> {
    let _selection = state.selection_lock.lock().await;
    let keys = state.vault.list();
    let (id, previous_cursor, next_cursor) = {
        let mut routers = state.router.lock().ok()?;
        let router = routers.entry(profile.to_owned()).or_default();
        let previous_cursor = router.next_slot();
        let id = router.select(&keys)?;
        (id, previous_cursor, router.next_slot())
    };
    if state.vault.set_cursor(profile, next_cursor).await.is_err() {
        if let Ok(mut routers) = state.router.lock()
            && let Some(router) = routers.get_mut(profile)
        {
            router.set_next_slot(previous_cursor);
        }
        return None;
    }
    Some(id)
}

async fn record_request(state: &web::Data<AppState>, id: Uuid) -> Result<(), HttpResponse> {
    state
        .vault
        .mutate(|vault| vault.record_request(id))
        .await
        .map_err(|_| {
            HttpResponse::InternalServerError().json(json!({
                "error": {"message": "request accounting unavailable", "type": "ledger_unavailable"}
            }))
        })
}
async fn record_failure(
    state: &web::Data<AppState>,
    id: Uuid,
    retry_after: Option<Duration>,
) -> Result<(), HttpResponse> {
    state
        .vault
        .mutate(|vault| vault.record_failure(id, retry_after))
        .await
        .map_err(|_| {
            HttpResponse::ServiceUnavailable().json(json!({
                "error": {"message": "upstream health state unavailable", "type": "ledger_unavailable"}
            }))
        })
}

async fn quarantine_key(state: &web::Data<AppState>, id: Uuid) -> Result<(), HttpResponse> {
    state
        .vault
        .mutate(|vault| vault.quarantine(id))
        .await
        .map(|_| ())
        .map_err(|_| {
            HttpResponse::ServiceUnavailable().json(json!({
                "error": {"message": "upstream health state unavailable", "type": "ledger_unavailable"}
            }))
        })
}

async fn attempt_started(
    state: &web::Data<AppState>,
    request_id: Uuid,
    profile: &str,
    key_id: Uuid,
) -> Result<(), HttpResponse> {
    state
        .vault
        .attempt_started(request_id, profile, key_id)
        .await
        .map_err(|_| {
            HttpResponse::InternalServerError().json(json!({
                "error": {"message": "request attempt ledger unavailable", "type": "ledger_unavailable"}
            }))
        })
}

async fn attempt_finished(
    state: &web::Data<AppState>,
    request_id: Uuid,
    key_id: Uuid,
    outcome: &str,
) -> Result<(), HttpResponse> {
    state
        .vault
        .attempt_finished(request_id, key_id, outcome)
        .await
        .map_err(|_| {
            HttpResponse::InternalServerError().json(json!({
                "error": {"message": "request attempt ledger unavailable", "type": "ledger_unavailable"}
            }))
        })
}

#[cfg(test)]
mod tests {
    use super::{
        PageCursor, PageQuery, SseValidator, admin_host_allowed, bearer, eligible_key_count,
        encode_page_cursor, format_origin_host, host_authority_well_formed, inline_script_bodies,
        page_before, parse_multimodal_request, percent_encode_userinfo, should_migrate_file_vault,
        upstream_endpoint, upstream_endpoint_for, validate_admin_token, validate_chat_request,
        validate_chat_response,
    };
    use actix_web::http::StatusCode;
    use actix_web::http::header;
    use actix_web::test::TestRequest;
    use chrono::{Duration, Utc};
    use nvidia_build_lb_core::KeySummary;
    use uuid::Uuid;

    #[test]
    fn page_cursor_is_explicit_and_fail_closed() {
        let id = Uuid::new_v4();
        let query = PageQuery {
            before: Some(
                encode_page_cursor(&PageCursor {
                    created_at: "2026-07-19T12:34:56Z".parse().expect("timestamp"),
                    id,
                })
                .expect("encode cursor"),
            ),
            limit: Some(10),
        };
        let parsed = page_before(&query)
            .expect("valid cursor")
            .expect("cursor value");
        assert_eq!(parsed.created_at.to_rfc3339(), "2026-07-19T12:34:56+00:00");
        assert_eq!(parsed.id, id);
        let invalid = PageQuery {
            before: Some("not-a-timestamp".to_owned()),
            limit: None,
        };
        assert_eq!(
            page_before(&invalid).unwrap_err().status(),
            StatusCode::BAD_REQUEST
        );
    }

    #[test]
    fn modality_paths_replace_only_the_endpoint_suffix() {
        let base = "https://integrate.api.nvidia.com/v1/chat/completions";
        assert_eq!(
            upstream_endpoint(base, "/v1/embeddings"),
            "https://integrate.api.nvidia.com/v1/embeddings"
        );
        assert_eq!(
            upstream_endpoint(base, "/v1/audio/transcriptions"),
            "https://integrate.api.nvidia.com/v1/audio/transcriptions"
        );
        assert_eq!(
            upstream_endpoint("https://provider.example/v1", "/v1/images/generations"),
            "https://provider.example/v1/images/generations"
        );
        assert_eq!(
            upstream_endpoint_for(base, "/v1/nvidia/inference", "nvidia/vila"),
            "https://ai.api.nvidia.com/v1/vlm/nvidia/vila"
        );
        assert_eq!(
            upstream_endpoint_for(
                base,
                "/v1/images/generations",
                "black-forest-labs/flux.1-kontext-dev"
            ),
            "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-kontext-dev"
        );
        assert_eq!(
            upstream_endpoint_for(
                "https://provider.example/v1/chat/completions",
                "/v1/images/generations",
                "black-forest-labs/flux.1-kontext-dev",
            ),
            "https://provider.example/v1/images/generations"
        );
    }

    #[test]
    fn modality_and_chat_boundaries_reject_malformed_requests() {
        assert!(
            parse_multimodal_request("/v1/embeddings", b"{not-json", "application/json").is_err()
        );
        assert!(
            parse_multimodal_request(
                "/v1/embeddings",
                br#"{"model":"nvidia/nvclip","input":"hello"}"#,
                "application/json",
            )
            .is_ok()
        );
        assert!(
            validate_chat_request(&serde_json::json!({
                "model": "z-ai/glm-5.2",
                "messages": [{"role":"user","content":"hello"}]
            }))
            .is_ok()
        );
        assert!(validate_chat_request(&serde_json::json!({"model":"z-ai/glm-5.2"})).is_err());
        let png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
        assert!(
            parse_multimodal_request(
                "/v1/videos/generations",
                &serde_json::to_vec(&serde_json::json!({
                    "model":"stabilityai/stable-video-diffusion",
                    "input_reference": png
                }))
                .expect("video request"),
                "application/json",
            )
            .is_ok()
        );
        assert!(parse_multimodal_request(
            "/v1/videos/generations",
            br#"{"model":"stabilityai/stable-video-diffusion","input_reference":"data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=","motion_bucket_id":126}"#,
            "application/json",
        )
        .is_err());
        assert!(parse_multimodal_request(
            "/v1/nvidia/inference",
            br#"{"model":"nvidia/vila","input":{"image":"data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="}}"#,
            "application/json",
        )
        .is_err());
    }

    #[test]
    fn multipart_transcription_requires_model_and_file() {
        let body = b"--test\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nnvidia/parakeet-ctc-1.1b\r\n--test\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\nContent-Type: audio/wav\r\n\r\naudio\r\n--test--\r\n";
        let parsed = parse_multimodal_request(
            "/v1/audio/transcriptions",
            body,
            "multipart/form-data; boundary=test",
        )
        .expect("valid transcription multipart");
        assert_eq!(parsed["model"], "nvidia/parakeet-ctc-1.1b");
        assert_eq!(parsed["__nblb_multipart"], true);
        assert!(
            parse_multimodal_request(
                "/v1/audio/transcriptions",
                b"--test\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nnvidia/parakeet-ctc-1.1b\r\n--test--\r\n",
                "multipart/form-data; boundary=test",
            )
            .is_err()
        );
        assert!(
            parse_multimodal_request(
                "/v1/audio/transcriptions",
                b"--test--",
                "multipart/form-data; boundary=test",
            )
            .is_err()
        );
    }

    #[test]
    fn chat_response_contract_rejects_provider_extras() {
        let valid = br#"{"id":"chat-1","object":"chat.completion","model":"z-ai/glm-5.2","choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}"#;
        assert!(validate_chat_response(valid, false).is_ok());
        let tool_call = br#"{"id":"chat-1","object":"chat.completion","model":"z-ai/glm-5.2","system_fingerprint":"fp","service_tier":"default","choices":[{"index":0,"message":{"role":"assistant","content":null,"tool_calls":[{"id":"call-1","type":"function","function":{"name":"lookup","arguments":"{}"}}],"refusal":null},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2,"completion_tokens_details":{"reasoning_tokens":0}}}"#;
        assert!(validate_chat_response(tool_call, false).is_ok());
        let leaked = br#"{"id":"chat-1","object":"chat.completion","model":"z-ai/glm-5.2","choices":[],"provider_secret":"do-not-forward"}"#;
        assert!(validate_chat_response(leaked, false).is_err());
    }

    #[test]
    fn admin_host_boundary_handles_ipv6_and_header_confusion() {
        assert!(admin_host_allowed("[::1]:2456"));
        assert!(admin_host_allowed("::1"));
        assert_eq!(format_origin_host("::1"), "[::1]");
        assert_eq!(format_origin_host("127.0.0.1"), "127.0.0.1");
        assert!(!admin_host_allowed("127.0.0.1:2456,evil"));
        assert!(!admin_host_allowed("[::1]evil"));
        assert!(!admin_host_allowed("[::1]:not-a-port"));
        assert!(!host_authority_well_formed("[::1]evil"));
        assert!(!host_authority_well_formed(
            "[nvidia-lb.dongwontuna.net]evil"
        ));
        assert!(!host_authority_well_formed("localhost:not-a-port"));
        assert!(!admin_host_allowed("localhost:not-a-port"));
        assert!(host_authority_well_formed("localhost:2456"));
        assert!(host_authority_well_formed("::1"));
        assert!(!admin_host_allowed(""));
    }

    #[test]
    fn bearer_rejects_duplicate_authorization_headers() {
        let request = TestRequest::default()
            .append_header((header::AUTHORIZATION, "Bearer one"))
            .append_header((header::AUTHORIZATION, "Bearer two"))
            .to_http_request();
        assert!(bearer(&request).is_none());
    }

    #[test]
    fn chat_request_contract_is_profile_aware() {
        let phi = serde_json::json!({
            "model": "microsoft/phi-4-multimodal-instruct",
            "messages": [{"role":"user","content":[{"type":"image_url","image_url":{"url":"data:image/png;base64,AA=="}}]}]
        });
        assert!(validate_chat_request(&phi).is_ok());
        let phi_tools = serde_json::json!({
            "model": "microsoft/phi-4-multimodal-instruct",
            "tools": [],
            "messages": [{"role":"user","content":"hello"}]
        });
        assert!(validate_chat_request(&phi_tools).is_err());
        let glm_object = serde_json::json!({
            "model": "z-ai/glm-5.2",
            "messages": [{"role":"user","content":{"type":"image_url","image_url":{"url":"data:image/png;base64,AA=="}}}]
        });
        assert!(validate_chat_request(&glm_object).is_err());
        let glm_tool_follow_up = serde_json::json!({
            "model": "z-ai/glm-5.2",
            "chat_template_kwargs": {"enable_thinking": false},
            "messages": [
                {"role":"assistant","content":null,"tool_calls":[{"id":"call-1","type":"function","function":{"name":"lookup","arguments":"{}"}}]},
                {"role":"tool","content":"result"}
            ]
        });
        assert!(validate_chat_request(&glm_tool_follow_up).is_ok());
    }

    #[test]
    fn health_eligibility_requires_verified_upstream_keys() {
        let keys = vec![
            KeySummary {
                id: Uuid::from_u128(1),
                label: "unverified".into(),
                fingerprint: "a".into(),
                enabled: true,
                verified: false,
                cooldown_until: None,
                request_count: 0,
                failure_count: 0,
            },
            KeySummary {
                id: Uuid::from_u128(2),
                label: "verified".into(),
                fingerprint: "b".into(),
                enabled: true,
                verified: true,
                cooldown_until: Some(Utc::now() + Duration::minutes(1)),
                request_count: 0,
                failure_count: 0,
            },
        ];
        assert_eq!(eligible_key_count(&keys), 0);
    }

    #[test]
    fn csp_hash_source_parser_only_accepts_inline_bootstrap() {
        let html = r#"<script src=\"/admin/app.js\"></script><script> boot(); </script>"#;
        assert_eq!(inline_script_bodies(html), vec![" boot(); ".to_owned()]);
    }

    #[test]
    fn provider_proof_and_boundary_helpers_fail_closed() {
        assert_eq!(percent_encode_userinfo("a@b:c/%"), "a%40b%3Ac%2F%25");
        assert!(
            validate_admin_token(
                "nblb_admin_0000000000000000000000000000000000000000000000000000000000000001",
                false,
            )
            .is_ok()
        );
        assert!(validate_admin_token("short", false).is_err());
        assert!(validate_admin_token("test-token", true).is_ok());
    }

    #[test]
    fn seeded_routing_rows_do_not_block_file_vault_migration() {
        assert!(should_migrate_file_vault(0, 0, 2, 0));
        assert!(should_migrate_file_vault(0, 0, 0, 1));
        assert!(!should_migrate_file_vault(0, 0, 0, 0));
        assert!(!should_migrate_file_vault(1, 0, 2, 0));
    }

    #[test]
    fn sse_validation_preserves_frames_split_across_chunks() {
        let mut validator = SseValidator::default();
        validator
            .feed(br#"data: {"id":"chat-1","object":"chat.completion.chunk","model":"z-ai/glm-5.2","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}"#)
            .expect("partial frame is buffered");
        assert!(validator.take_emitted().is_empty());
        validator
            .feed(b"\n\ndata: [DONE]\n\n")
            .expect("complete frames");
        let emitted: Vec<_> = validator.take_emitted().into_iter().collect();
        assert_eq!(emitted.len(), 1);
        assert!(std::str::from_utf8(&emitted[0]).unwrap().contains("chat-1"));
        validator.finish().expect("complete stream");
    }

    #[test]
    fn sse_done_without_data_is_not_a_completion() {
        let mut validator = SseValidator::default();
        validator
            .feed(b"data: [DONE]\n\n")
            .expect("valid terminator");
        assert_eq!(validator.frame_count, 1);
        assert_eq!(validator.data_frame_count, 0);
        assert!(validator.finish().is_err());
    }
}
