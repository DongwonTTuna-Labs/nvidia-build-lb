#![forbid(unsafe_code)]
//! Actix gateway for the NVIDIA hosted API load balancer.

mod admin;
mod errors;
mod provider;
mod proxy;
mod streaming;
#[cfg(test)]
mod tests;
pub(crate) use errors::invalid_request;
pub(crate) use provider::{
    prepare_modality_request, upstream_endpoint_for, validate_chat_response,
};
pub(crate) use streaming::{
    SseValidator, StreamAttemptGuard, UpstreamByteStream, chat_response_stream,
    normalize_modality_response, prime_stream, valid_data_url,
};

use actix_files::Files;
use actix_web::{
    App, HttpRequest, HttpResponse, HttpServer, guard, http::header, middleware::DefaultHeaders,
    web,
};
use anyhow::{Context, Result, anyhow, bail};
use base64::Engine;
use bytes::Bytes;
use chrono::{Duration, Utc};
use futures_util::stream;
use nvidia_build_lb_core::{
    DownstreamSummary, PROFILES, Router, Vault, VaultDownstreamRecord, VaultKeyRecord,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::{FromRow, PgPool, postgres::PgPoolOptions};
use std::{
    collections::{BTreeMap, HashMap, VecDeque},
    env,
    sync::Mutex,
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
        // Provider receipts are invalid once a key loses verification or is
        // durably retired. Delete them in this transaction so capability
        // projections cannot observe proof from an older credential state.
        if (!current.verified && previous.verified) || (current.retired && !previous.retired) {
            sqlx::query("DELETE FROM nblb.profile_probe_receipts WHERE key_id=$1")
                .bind(current.id)
                .execute(&mut *tx)
                .await
                .context("invalidate stale profile provider receipts")?;
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
    cfg.route("/health", web::get().to(admin::health))
        .route(
            "/admin/api/v1/operator-readiness",
            web::get().to(admin::operator_readiness),
        )
        .route("/v1/models", web::get().to(admin::models))
        .route(
            "/v1/chat/completions",
            web::post().to(proxy::chat_completions),
        )
        .route("/v1/embeddings", web::post().to(proxy::multimodal))
        .route("/v1/images/generations", web::post().to(proxy::multimodal))
        .route("/v1/videos/generations", web::post().to(proxy::multimodal))
        .route("/v1/audio/speech", web::post().to(proxy::multimodal))
        .route(
            "/v1/audio/transcriptions",
            web::post().to(proxy::multimodal),
        )
        .route("/v1/nvidia/inference", web::post().to(proxy::multimodal))
        .route(
            "/admin/api/v1/upstream-keys",
            web::get().to(admin::list_keys),
        )
        .route("/admin/api/v1/overview", web::get().to(admin::overview))
        .route("/admin/api/v1/evidence", web::get().to(admin::evidence))
        .route(
            "/admin/api/v1/upstream-slots",
            web::get().to(admin::upstream_slots),
        )
        .route(
            "/admin/api/v1/model-capabilities",
            web::get().to(admin::model_capabilities),
        )
        .route(
            "/admin/api/v1/generation-readiness",
            web::get().to(admin::generation_readiness),
        )
        .route("/admin/api/v1/operations", web::get().to(admin::operations))
        .route(
            "/admin/api/v1/operations/{id}",
            web::get().to(admin::operation_detail),
        )
        .route("/admin/api/v1/attentions", web::get().to(admin::attentions))
        .route("/admin/api/v1/events", web::get().to(admin::events))
        .route(
            "/admin/api/v1/events/{id}",
            web::get().to(admin::event_detail),
        )
        .route(
            "/admin/api/v1/evidence/{id}",
            web::get().to(admin::evidence_detail),
        )
        .route(
            "/admin/api/v1/upstream-keys",
            web::post().to(admin::add_key),
        )
        .route(
            "/admin/api/v1/upstream-keys/{id}",
            web::delete().to(admin::delete_key),
        )
        .route(
            "/admin/api/v1/upstream-keys/{id}/state",
            web::post().to(admin::toggle_key),
        )
        .route(
            "/admin/api/v1/upstream-keys/{id}/enable",
            web::post().to(admin::enable_key_alias),
        )
        .route(
            "/admin/api/v1/upstream-keys/{id}/disable",
            web::post().to(admin::disable_key_alias),
        )
        .route(
            "/admin/api/v1/upstream-keys/{id}/probe",
            web::post().to(admin::probe_key),
        )
        .route(
            "/admin/api/v1/downstream-credentials",
            web::get().to(admin::list_downstream),
        )
        .route(
            "/admin/api/v1/downstream-credentials",
            web::post().to(admin::add_downstream),
        )
        .route(
            "/admin/api/v1/downstream-credentials/{id}/revoke",
            web::post().to(admin::revoke_downstream),
        )
        // Compatibility aliases retained for existing operators while the
        // canonical resource name is downstream-credentials.
        .route(
            "/admin/api/v1/downstream-tokens",
            web::get().to(admin::list_downstream),
        )
        .route(
            "/admin/api/v1/downstream-tokens",
            web::post().to(admin::add_downstream),
        )
        .route(
            "/admin/api/v1/downstream-tokens/{id}",
            web::delete().to(admin::revoke_downstream_legacy),
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
    // Keep the public status artifact separate from the loopback-only admin
    // files so the root dashboard cannot expose operator data by fallback.
    cfg.service(Files::new("/", "/app/public").index_file("index.html"));
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
        csp_hashes: static_script_hashes(&["/app/static", "/app/public"]),
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

fn static_script_hashes(roots: &[&str]) -> Vec<String> {
    roots
        .iter()
        .flat_map(|root| {
            ["index.html"].into_iter().filter_map(|name| {
                std::fs::read_to_string(std::path::Path::new(root).join(name)).ok()
            })
        })
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
