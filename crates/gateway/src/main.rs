#![forbid(unsafe_code)]
//! Actix gateway for the NVIDIA hosted API load balancer.

use actix_files::Files;
use actix_web::{App, HttpRequest, HttpResponse, HttpServer, Responder, guard, http::header, web};
use anyhow::{Context, Result, anyhow, bail};
use base64::Engine;
use bytes::Bytes;
use chrono::{Duration, Utc};
use futures_util::{Stream, StreamExt, stream};
use nvidia_build_lb_core::{
    DownstreamSummary, PROFILES, Router, Vault, VaultDownstreamRecord, VaultKeyRecord,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::{FromRow, PgPool, postgres::PgPoolOptions};
use std::{
    collections::{BTreeMap, HashMap},
    env,
    pin::Pin,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration as StdDuration, Instant},
};
use uuid::Uuid;

struct AppState {
    vault: VaultStore,
    router: Mutex<HashMap<String, Router>>,
    selection_lock: tokio::sync::Mutex<()>,
    client: reqwest::Client,
    admin_token: String,
    upstream_url: String,
    require_downstream_token: bool,
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
}

impl VaultStore {
    async fn open(
        path: impl AsRef<std::path::Path>,
        master_key: [u8; 32],
        database: Option<PgPool>,
    ) -> Result<Self> {
        let path = path.as_ref().to_path_buf();
        let file_vault = Vault::open(&path, master_key)?;
        let vault = if let Some(pool) = &database {
            sqlx::query("UPDATE nblb.request_attempts SET outcome='abandoned_after_restart', finished_at=now() WHERE finished_at IS NULL")
                .execute(pool)
                .await
                .context("close attempts left by previous process")?;
            let keys = sqlx::query_as::<_, DbKeyRow>(
                "SELECT id, label, fingerprint, ciphertext, nonce, enabled, verified, cooldown_until, request_count, failure_count FROM nblb.upstream_keys ORDER BY created_at, id",
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
            let routing_state_empty = rows.is_empty();
            let cursors = rows
                .into_iter()
                .map(|(profile, slot)| (profile, slot.saturating_sub(1) as usize))
                .collect::<BTreeMap<_, _>>();
            if keys.is_empty() && downstream.is_empty() && routing_state_empty {
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
            file_vault
        };
        Ok(Self {
            vault: Mutex::new(vault),
            database,
            sync_lock: tokio::sync::Mutex::new(()),
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
        if let Err(error) = self.sync_unlocked().await {
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
        if let Err(error) = self.sync_unlocked().await {
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

    async fn sync_unlocked(&self) -> Result<()> {
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
        sync_database_rows(pool, &keys, &downstream, &cursors).await
    }

    async fn evidence(&self) -> Result<Value> {
        if let Some(pool) = &self.database {
            let persisted_keys =
                sqlx::query_scalar::<_, i64>("SELECT count(*) FROM nblb.upstream_keys")
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
            "INSERT INTO nblb.request_attempts (request_id, profile_id, key_id, outcome) VALUES ($1,$2,$3,'started')",
        )
        .bind(request_id)
        .bind(profile)
        .bind(key_id)
        .execute(pool)
        .await
        .context("record request attempt")?;
        Ok(())
    }

    async fn attempt_finished(&self, request_id: Uuid, key_id: Uuid, outcome: &str) -> Result<()> {
        let Some(pool) = &self.database else {
            return Ok(());
        };
        let result = sqlx::query(
            "UPDATE nblb.request_attempts SET outcome=$3, finished_at=now() WHERE id = (SELECT id FROM nblb.request_attempts WHERE request_id=$1 AND key_id=$2 AND finished_at IS NULL ORDER BY created_at DESC LIMIT 1)",
        )
        .bind(request_id)
        .bind(key_id)
        .bind(outcome)
        .execute(pool)
        .await
        .context("finish request attempt")?;
        if result.rows_affected() != 1 {
            bail!("request attempt terminal row is missing")
        }
        Ok(())
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
    let key_ids: Vec<Uuid> = keys.iter().map(|key| key.id).collect();
    sqlx::query("DELETE FROM nblb.upstream_keys WHERE id <> ALL($1::uuid[])")
        .bind(&key_ids)
        .execute(&mut *tx)
        .await
        .context("remove deleted upstream keys")?;
    let downstream_ids: Vec<Uuid> = downstream.iter().map(|item| item.id).collect();
    sqlx::query("DELETE FROM nblb.downstream_credentials WHERE id <> ALL($1::uuid[])")
        .bind(&downstream_ids)
        .execute(&mut *tx)
        .await
        .context("remove deleted downstream credentials")?;
    for key in keys {
        sqlx::query(
            "INSERT INTO nblb.upstream_keys (id, label, fingerprint, ciphertext, nonce, enabled, verified, cooldown_until, request_count, failure_count) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT (id) DO UPDATE SET label=EXCLUDED.label, fingerprint=EXCLUDED.fingerprint, ciphertext=EXCLUDED.ciphertext, nonce=EXCLUDED.nonce, enabled=EXCLUDED.enabled, verified=EXCLUDED.verified, cooldown_until=EXCLUDED.cooldown_until, request_count=EXCLUDED.request_count, failure_count=EXCLUDED.failure_count",
        )
        .bind(key.id)
        .bind(&key.label)
        .bind(&key.fingerprint)
        .bind(&key.ciphertext)
        .bind(&key.nonce)
        .bind(key.enabled)
        .bind(key.verified)
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
    let port = env::var("NVIDIA_BUILD_LB_PUBLIC_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(2456);
    let state = web::Data::new(build_state().await.expect("gateway configuration"));
    HttpServer::new(move || {
        App::new()
            .app_data(state.clone())
            .app_data(web::PayloadConfig::new(64 * 1024 * 1024))
            .configure(routes)
    })
    .bind(("0.0.0.0", port))?
    .run()
    .await
}

fn routes(cfg: &mut web::ServiceConfig) {
    cfg.route("/health", web::get().to(health))
        .route("/v1/models", web::get().to(models))
        .route("/v1/chat/completions", web::post().to(chat_completions))
        .route("/v1/embeddings", web::post().to(multimodal))
        .route("/v1/images/generations", web::post().to(multimodal))
        .route("/v1/audio/speech", web::post().to(multimodal))
        .route("/v1/audio/transcriptions", web::post().to(multimodal))
        .route("/v1/nvidia/inference", web::post().to(multimodal))
        .route("/admin/api/v1/upstream-keys", web::get().to(list_keys))
        .route("/admin/api/v1/overview", web::get().to(overview))
        .route("/admin/api/v1/evidence", web::get().to(evidence))
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
            web::delete().to(revoke_downstream),
        )
        .service(
            Files::new("/admin", "/app/static")
                .index_file("index.html")
                .guard(guard::fn_guard(|context| {
                    let host = context
                        .head()
                        .headers
                        .get(header::HOST)
                        .and_then(|value| value.to_str().ok())
                        .unwrap_or_default();
                    admin_host_allowed(host)
                })),
        );
}

async fn build_state() -> Result<AppState> {
    let master_key = read_master_key()?;
    let admin_token = read_required_secret(
        "NVIDIA_BUILD_LB_ADMIN_TOKEN",
        "/run/nvidia-build-lb/secrets/admin_token",
    )?;
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
    let mut client_builder = reqwest::Client::builder().timeout(StdDuration::from_secs(60));
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
        upstream_url: env::var("NBLB_UPSTREAM_URL")
            .unwrap_or_else(|_| "https://integrate.api.nvidia.com/v1/chat/completions".to_owned()),
        require_downstream_token,
    })
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
        url = url.replacen("@", &format!(":{password}@"), 1);
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

async fn health(state: web::Data<AppState>) -> impl Responder {
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
    let traffic_ready = ready && eligible_keys > 0;
    let mut response = if ready {
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

async fn models(req: HttpRequest, state: web::Data<AppState>) -> impl Responder {
    if let Err(response) = authorize_scope(&req, &state, "models:read").await {
        return response;
    }
    let data: Vec<Value> = PROFILES
        .iter()
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
    HttpResponse::Ok().insert_header(("cache-control", "no-store")).json(json!({
        "runtime": {"status": if database_ready && eligible_keys > 0 { "ok" } else { "degraded" }, "ready": database_ready && keys.len() == nvidia_build_lb_core::MAX_UPSTREAM_KEYS, "traffic_ready": database_ready && eligible_keys > 0, "eligible_keys": if database_ready { eligible_keys } else { 0 }},
        "upstream_keys": {"items": keys},
        "downstream_credentials": {"items": downstream},
        "models": PROFILES,
        "evidence": evidence,
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
async fn probe_key(
    req: HttpRequest,
    state: web::Data<AppState>,
    path: web::Path<Uuid>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized();
    }
    let id = path.into_inner();
    let credential = match state.vault.credential(id) {
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
            Ok(_) => HttpResponse::Ok().json(json!({"probe_status":"valid","status":200})),
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
        Ok(_) => HttpResponse::Ok().json(json!({"probe_status":"valid","status":status.as_u16()})),
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
        // “Delete” is therefore a durable revocation (disabled slot), which
        // preserves audit evidence and keeps restart state authoritative.
        .mutate(|vault| vault.set_enabled(path.into_inner(), false).map(|_| ()))
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
        Err(error) if error.to_string().contains("already exists") => HttpResponse::Conflict()
            .json(json!({"error":{"code":"resource_conflict","message":error.to_string()}})),
        Err(error) => HttpResponse::UnprocessableEntity()
            .json(json!({"error":{"code":"invalid_request","message":error.to_string()}})),
    }
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

async fn chat_completions(
    req: HttpRequest,
    state: web::Data<AppState>,
    body: web::Json<Value>,
) -> impl Responder {
    if let Err(response) = authorize_scope(&req, &state, "chat:write").await {
        return response;
    }
    let request = body.into_inner();
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
                if let Err(response) = attempt_finished(&state, request_id, id, "failed").await {
                    return response;
                }
                continue;
            }
        };
        if state.upstream_url.starts_with("mock://") {
            if credential.contains("fail") {
                if let Err(response) = record_failure(&state, id, Some(Duration::seconds(2))).await
                {
                    return response;
                }
                if let Err(response) = attempt_finished(&state, request_id, id, "failed").await {
                    return response;
                }
                continue;
            }
            if let Err(response) = record_request(&state, id).await {
                return response;
            }
            if let Err(response) = attempt_finished(&state, request_id, id, "succeeded").await {
                return response;
            }
            return mock_response(&request, stream);
        }
        let result = state
            .client
            .post(upstream_endpoint_for(
                &state.upstream_url,
                "/v1/chat/completions",
                profile,
            ))
            .bearer_auth(&credential)
            .json(&request)
            .send()
            .await;
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
                let response =
                    match poll_nvcf(&state.client, response, &chat_endpoint, &credential).await {
                        Ok(response) => response,
                        Err(_) => {
                            if let Err(response) =
                                attempt_finished(&state, request_id, id, "failed").await
                            {
                                return response;
                            }
                            continue;
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
                    if let Err(response) = attempt_finished(&state, request_id, id, "failed").await
                    {
                        return response;
                    }
                    continue;
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
                    let downstream = chat_response_stream(upstream, state.clone(), request_id, id);
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
                    if let Err(response) = record_failure(&state, id, None).await {
                        return response;
                    }
                    if let Err(response) = attempt_finished(&state, request_id, id, "failed").await
                    {
                        return response;
                    }
                    continue;
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
                    || response.status().as_u16() == 403
                    || response.status().as_u16() == 429
                    || response.status().is_server_error() =>
            {
                if matches!(response.status().as_u16(), 401 | 403)
                    && let Err(response) = quarantine_key(&state, id).await
                {
                    return response;
                }
                let retry = retry_after_duration(&response);
                if let Err(response) = record_failure(&state, id, retry).await {
                    return response;
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
    HttpResponse::ServiceUnavailable().json(json!({"error":{"message":"No eligible NVIDIA upstream key","type":"upstream_unavailable"}}))
}

/// Handles the non-chat OpenAI/NVIDIA representations through the same
/// authenticated router. The mock path deliberately returns shape-valid
/// fixtures for every advertised modality, while production forwards the
/// original JSON body to the configured NVIDIA endpoint.
async fn multimodal(
    req: HttpRequest,
    state: web::Data<AppState>,
    body: web::Bytes,
) -> impl Responder {
    let scope = match req.path() {
        "/v1/embeddings" => "embeddings:write",
        "/v1/images/generations" => "images:write",
        "/v1/audio/speech" | "/v1/audio/transcriptions" => "audio:write",
        _ => "media:write",
    };
    if let Err(response) = authorize_scope(&req, &state, scope).await {
        return response;
    }
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
            return mock_modality(req.path(), &request);
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
                .text("encoding", "pcm16".to_owned())
                .text("sample_rate", "44100".to_owned());
            state
                .client
                .post(&endpoint)
                .bearer_auth(&credential)
                .multipart(form)
                .send()
                .await
        } else {
            state
                .client
                .post(&endpoint)
                .bearer_auth(&credential)
                .header(reqwest::header::CONTENT_TYPE, "application/json")
                .json(&upstream_request)
                .send()
                .await
        };
        match request_result {
            Ok(response) if response.status().is_success() => {
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
                            continue;
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
                let (bytes, content_type) =
                    match normalize_modality_response(req.path(), &bytes, &content_type) {
                        Ok(value) => value,
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
                    || response.status().as_u16() == 403
                    || response.status().as_u16() == 429
                    || response.status().is_server_error() =>
            {
                if matches!(response.status().as_u16(), 401 | 403)
                    && let Err(response) = quarantine_key(&state, id).await
                {
                    return response;
                }
                let retry = retry_after_duration(&response);
                if let Err(response) = record_failure(&state, id, retry).await {
                    return response;
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
        "/v1/nvidia/inference" => {
            matches!(model, "nvidia/vila" | "stabilityai/stable-video-diffusion")
        }
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
        "/v1/nvidia/inference" => (&[], &["model", "input", "messages", "stream", "parameters"]),
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
        "/v1/embeddings" if !object["input"].is_string() && !object["input"].is_array() => {
            Err(invalid_request("input must be a string or array"))
        }
        "/v1/images/generations" if !object["prompt"].is_string() => {
            Err(invalid_request("prompt must be a string"))
        }
        "/v1/videos/generations"
            if object
                .get("input_reference")
                .and_then(Value::as_str)
                .is_none_or(|value| !valid_data_url(value, true)) =>
        {
            Err(invalid_request(
                "input_reference must be a valid PNG or JPEG data URL",
            ))
        }
        "/v1/audio/speech" if !object["input"].is_string() => {
            Err(invalid_request("input must be a string"))
        }
        "/v1/nvidia/inference" => {
            let input = object
                .get("input")
                .ok_or_else(|| invalid_request("input is required"))?;
            if !input.is_string() && !input.is_object() {
                return Err(invalid_request("input must be a string or object"));
            }
            if let Some(input) = input.as_object() {
                if let Some(unknown) = input.keys().find(|field| {
                    ![
                        "image",
                        "image_url",
                        "prompt",
                        "seed",
                        "cfg_scale",
                        "motion_bucket_id",
                    ]
                    .contains(&field.as_str())
                }) {
                    return Err(invalid_request(&format!(
                        "unsupported input field: {unknown}"
                    )));
                }
                if !input.contains_key("image")
                    && !input.contains_key("image_url")
                    && !input.contains_key("prompt")
                {
                    return Err(invalid_request(
                        "input requires image, image_url, or prompt",
                    ));
                }
                if let Some(image) = input.get("image").or_else(|| input.get("image_url"))
                    && image
                        .as_str()
                        .is_none_or(|value| !valid_data_url(value, true))
                {
                    return Err(invalid_request(
                        "input image must be a valid PNG or JPEG data URL",
                    ));
                }
                if let Some(seed) = input.get("seed")
                    && !seed.is_i64()
                {
                    return Err(invalid_request("seed must be an integer"));
                }
                if let Some(cfg) = input.get("cfg_scale")
                    && cfg
                        .as_f64()
                        .is_none_or(|value| !(0.0..=30.0).contains(&value))
                {
                    return Err(invalid_request("cfg_scale must be between 0 and 30"));
                }
                if let Some(bucket) = input.get("motion_bucket_id")
                    && bucket.as_u64().is_none_or(|value| value > 255)
                {
                    return Err(invalid_request(
                        "motion_bucket_id must be between 0 and 255",
                    ));
                }
            }
            Ok(())
        }
        _ => Ok(()),
    }
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
    const ALLOWED: &[&str] = &[
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
    ];
    if let Some(unknown) = object
        .keys()
        .find(|field| !ALLOWED.contains(&field.as_str()))
    {
        return Err(invalid_request(&format!("unsupported field: {unknown}")));
    }
    let messages = object
        .get("messages")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid_request("messages must be an array"))?;
    if messages.is_empty()
        || messages.iter().any(|message| {
            !message.is_object()
                || !matches!(
                    message.get("role").and_then(Value::as_str),
                    Some("system" | "user" | "assistant" | "tool")
                )
                || message.get("content").is_some_and(Value::is_null)
        })
    {
        return Err(invalid_request(
            "messages must contain valid role/content entries",
        ));
    }
    Ok(())
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
            "nvidia/magpie-tts-multilingual" => "https://877104f7-e885-42b9-8de8-f6e4c6303969.invocation.api.nvcf.nvidia.com/v1/audio/synthesize".to_owned(),
            "nvidia/parakeet-ctc-1.1b" => upstream_endpoint(configured, "/v1/audio/transcriptions"),
            _ => upstream_endpoint(configured, path),
        };
    }
    upstream_endpoint(configured, path)
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
            for field in ["seed", "cfg_scale", "motion_bucket_id"] {
                if let Some(value) = object.get(field) {
                    result.insert(field.to_owned(), value.clone());
                }
            }
            Ok(Value::Object(result))
        }
        "/v1/nvidia/inference" => {
            let model = object
                .get("model")
                .and_then(Value::as_str)
                .unwrap_or_default();
            if model == "stabilityai/stable-video-diffusion"
                && let Some(input) = object.get("input").and_then(Value::as_object)
            {
                return Ok(input.clone().into());
            }
            Ok(request.clone())
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

fn mock_modality(path: &str, request: &Value) -> HttpResponse {
    let model = request
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or("nvidia/multimodal");
    let body = match path {
        "/v1/embeddings" => {
            json!({"object":"list","data":[{"object":"embedding","index":0,"embedding":[0.0,1.0]}],"model":model,"usage":{"prompt_tokens":1,"total_tokens":1}})
        }
        "/v1/images/generations" => {
            json!({"artifacts":[{"base64":"","finishReason":"SUCCESS","seed":0}],"model":model})
        }
        "/v1/audio/speech" => {
            return HttpResponse::Ok()
                .content_type("audio/wav")
                .body(b"RIFF\x24\x00\x00\x00WAVEfmt ".to_vec());
        }
        "/v1/audio/transcriptions" => json!({"text":"NVIDIA Build LB","model":model}),
        _ => {
            json!({"video":"ZGF0YQ==","finish_reason":"SUCCESS","seed":0,"model":model})
        }
    };
    HttpResponse::Ok().json(body)
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
        HttpResponse::Ok()
            .insert_header(("content-type", "text/event-stream"))
            .insert_header(("cache-control", "no-cache"))
            .streaming(stream::iter(vec![
                Ok::<Bytes, actix_web::Error>(first),
                Ok(terminal),
            ]))
    } else {
        HttpResponse::Ok().json(json!({"id":id,"object":"chat.completion","model":model,"choices":[{"index":0,"message":{"role":"assistant","content":"NVIDIA Build LB"},"finish_reason":"stop"}],"usage":{"prompt_tokens":0,"completion_tokens":3,"total_tokens":3}}))
    }
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
            "id" | "object" | "created" | "model" | "choices" | "usage"
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
                "index" | "message" | "finish_reason" | "logprobs"
            )
        }) || choice.get("index").and_then(Value::as_u64).is_none()
            || choice
                .get("finish_reason")
                .is_none_or(|value| !value.is_null() && !value.is_string())
        {
            return Err(());
        }
        if let Some(message) = choice.get("message") {
            let message = message.as_object().ok_or(())?;
            if message
                .keys()
                .any(|key| !matches!(key.as_str(), "role" | "content" | "tool_calls" | "refusal"))
                || message
                    .get("role")
                    .and_then(Value::as_str)
                    .is_none_or(str::is_empty)
            {
                return Err(());
            }
        }
    }
    if let Some(usage) = object.get("usage") {
        let usage = usage.as_object().ok_or(())?;
        if usage.keys().any(|key| {
            !matches!(
                key.as_str(),
                "prompt_tokens" | "completion_tokens" | "total_tokens"
            )
        }) || usage.values().any(|value| value.as_u64().is_none())
        {
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

struct StreamAttemptGuard {
    state: web::Data<AppState>,
    request_id: Uuid,
    key_id: Uuid,
    terminal: Arc<AtomicBool>,
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
            let _ = state
                .vault
                .attempt_finished(request_id, key_id, "cancelled")
                .await;
        });
    }
}

#[derive(Default)]
struct SseValidator {
    buffer: Vec<u8>,
    done: bool,
    frame_count: usize,
    message_id: Option<String>,
    model: Option<String>,
}

impl SseValidator {
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
        if self.done && self.frame_count >= 2 {
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
        let value: Value = serde_json::from_str(&data).map_err(|_| ())?;
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
                || choice.get("index").and_then(Value::as_u64).is_none()
                || choice.get("delta").and_then(Value::as_object).is_none()
                || !choice
                    .get("finish_reason")
                    .is_some_and(|reason| reason.is_null() || reason.is_string())
        }) {
            return Err(());
        }
        Ok(())
    }
}

fn chat_response_stream(
    upstream: UpstreamByteStream,
    state: web::Data<AppState>,
    request_id: Uuid,
    key_id: Uuid,
) -> impl Stream<Item = Result<Bytes, actix_web::Error>> {
    let guard_state = state.clone();
    stream::unfold(
        (
            upstream,
            SseValidator::default(),
            state,
            request_id,
            key_id,
            StreamAttemptGuard {
                state: guard_state,
                request_id,
                key_id,
                terminal: Arc::new(AtomicBool::new(false)),
            },
            false,
        ),
        |(mut upstream, mut validator, state, request_id, key_id, guard, mut terminal)| async move {
            if terminal {
                return None;
            }
            match upstream.next().await {
                Some(Ok(chunk)) => {
                    if validator.feed(&chunk).is_err() {
                        let _ = record_failure(&state, key_id, None).await;
                        let _ = state
                            .vault
                            .attempt_finished(request_id, key_id, "failed")
                            .await;
                        terminal = true;
                        guard.terminal.store(true, Ordering::Release);
                        return Some((
                            Ok(sse_error_frame("invalid upstream stream")),
                            (
                                upstream, validator, state, request_id, key_id, guard, terminal,
                            ),
                        ));
                    }
                    Some((
                        Ok(chunk),
                        (
                            upstream, validator, state, request_id, key_id, guard, terminal,
                        ),
                    ))
                }
                Some(Err(_)) => {
                    let _ = record_failure(&state, key_id, None).await;
                    let _ = state
                        .vault
                        .attempt_finished(request_id, key_id, "failed")
                        .await;
                    terminal = true;
                    guard.terminal.store(true, Ordering::Release);
                    Some((
                        Ok(sse_error_frame("upstream stream failed")),
                        (
                            upstream, validator, state, request_id, key_id, guard, terminal,
                        ),
                    ))
                }
                None => {
                    if validator.finish().is_err() {
                        let _ = record_failure(&state, key_id, None).await;
                        let _ = state
                            .vault
                            .attempt_finished(request_id, key_id, "failed")
                            .await;
                        guard.terminal.store(true, Ordering::Release);
                        return Some((
                            Ok(sse_error_frame("incomplete upstream stream")),
                            (upstream, validator, state, request_id, key_id, guard, true),
                        ));
                    }
                    if record_request(&state, key_id).await.is_err() {
                        let _ = state
                            .vault
                            .attempt_finished(request_id, key_id, "failed")
                            .await;
                        guard.terminal.store(true, Ordering::Release);
                        return Some((
                            Ok(sse_error_frame("request accounting unavailable")),
                            (upstream, validator, state, request_id, key_id, guard, true),
                        ));
                    }
                    if state
                        .vault
                        .attempt_finished(request_id, key_id, "succeeded")
                        .await
                        .is_err()
                    {
                        guard.terminal.store(true, Ordering::Release);
                        return Some((
                            Ok(sse_error_frame("request ledger unavailable")),
                            (upstream, validator, state, request_id, key_id, guard, true),
                        ));
                    }
                    guard.terminal.store(true, Ordering::Release);
                    None
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
    bytes.len() >= 4 && bytes.starts_with(&[0xff, 0xd8]) && bytes.ends_with(&[0xff, 0xd9])
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
    fmt_size >= 16
        && channels == 1
        && sample_rate == 44_100
        && bits == 16
        && bytes.windows(4).any(|chunk| chunk == b"data")
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
            value.get("object").and_then(Value::as_str) == Some("list")
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
                        usage
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
                                .is_some_and(|bytes| valid_jpeg(&bytes))
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
                                        .is_some_and(|bytes| valid_jpeg(&bytes))
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
        "/v1/videos/generations" | "/v1/nvidia/inference" => {
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
        Err(_) => Err(downstream_unauthorized()),
    }
}

fn authorized(req: &HttpRequest, state: &AppState) -> bool {
    admin_surface_allowed(req)
        && bearer(req).is_some_and(|value| {
            constant_time_equal(value.as_bytes(), state.admin_token.as_bytes())
        })
}

fn admin_surface_allowed(req: &HttpRequest) -> bool {
    let raw_host = req
        .headers()
        .get(header::HOST)
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default();
    let host = raw_host
        .strip_prefix('[')
        .and_then(|value| value.split_once(']').map(|(host, _)| host))
        .unwrap_or_else(|| raw_host.split(':').next().unwrap_or_default())
        .to_ascii_lowercase();
    let host_allowed = admin_host_allowed(&host);
    if !host_allowed {
        return false;
    }
    let Some(origin) = req.headers().get(header::ORIGIN) else {
        return true;
    };
    let Ok(origin) = origin.to_str() else {
        return false;
    };
    origin == format!("https://{host}")
        || origin == format!("http://{host}")
        || origin == "http://localhost:2456"
        || origin == "http://127.0.0.1:2456"
}

fn admin_host_allowed(raw_host: &str) -> bool {
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
    req.headers()
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
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
        .filter(|key| key.enabled && key.cooldown_until.is_none_or(|until| until <= now))
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
        parse_multimodal_request, upstream_endpoint, upstream_endpoint_for, validate_chat_request,
        validate_chat_response,
    };

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
    }

    #[test]
    fn multipart_transcription_requires_model_and_file() {
        let body = b"--test\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nnvidia/parakeet-ctc-1.1b\r\n--test\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n\r\naudio\r\n--test--\r\n";
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
        let leaked = br#"{"id":"chat-1","object":"chat.completion","model":"z-ai/glm-5.2","choices":[],"provider_secret":"do-not-forward"}"#;
        assert!(validate_chat_response(leaked, false).is_err());
    }
}
