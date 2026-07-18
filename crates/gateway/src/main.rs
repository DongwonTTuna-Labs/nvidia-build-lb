#![forbid(unsafe_code)]
//! Actix gateway for the NVIDIA hosted API load balancer.

use actix_files::Files;
use actix_web::{App, HttpRequest, HttpResponse, HttpServer, Responder, web};
use anyhow::{Context, Result, anyhow, bail};
use chrono::Duration;
use futures_util::StreamExt;
use nvidia_build_lb_core::{
    DownstreamSummary, PROFILES, Router, Vault, VaultDownstreamRecord, VaultKeyRecord,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::{FromRow, PgPool, postgres::PgPoolOptions};
use std::{env, sync::Mutex, time::Duration as StdDuration};
use uuid::Uuid;

struct AppState {
    vault: VaultStore,
    router: Mutex<Router>,
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
            let keys = sqlx::query_as::<_, DbKeyRow>(
                "SELECT id, label, fingerprint, ciphertext, nonce, enabled, cooldown_until, request_count, failure_count FROM nblb.upstream_keys ORDER BY created_at, id",
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
            let cursor = sqlx::query_scalar::<_, Option<i16>>(
                "SELECT next_slot FROM nblb.routing_state WHERE profile_id = $1",
            )
            .bind(PROFILES[0])
            .fetch_optional(pool)
            .await
            .context("load routing cursor")?
            .flatten()
            .unwrap_or(1)
            .saturating_sub(1) as usize;
            if keys.is_empty() && downstream.is_empty() {
                sync_database(pool, &file_vault).await?;
                file_vault
            } else {
                Vault::from_records(
                    &path,
                    master_key,
                    keys.into_iter().map(db_key_record).collect::<Result<_>>()?,
                    downstream
                        .into_iter()
                        .map(db_downstream_record)
                        .collect::<Result<_>>()?,
                    cursor,
                )?
            }
        } else {
            file_vault
        };
        Ok(Self {
            vault: Mutex::new(vault),
            database,
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

    fn cursor(&self) -> usize {
        self.vault
            .lock()
            .map(|vault| vault.router_cursor())
            .unwrap_or_default()
    }

    async fn set_cursor(&self, cursor: usize) -> Result<()> {
        {
            let mut vault = self.vault.lock().map_err(|_| anyhow!("vault lock"))?;
            vault.set_router_cursor(cursor)?;
        }
        self.sync().await
    }

    async fn mutate<F, T>(&self, operation: F) -> Result<T>
    where
        F: FnOnce(&mut Vault) -> Result<T>,
    {
        let result = {
            let mut vault = self.vault.lock().map_err(|_| anyhow!("vault lock"))?;
            operation(&mut vault)
        }?;
        self.sync().await?;
        Ok(result)
    }

    async fn authenticate(&self, token: &str, scope: &str) -> Result<DownstreamSummary> {
        self.mutate(|vault| vault.authenticate_downstream(token, scope))
            .await
    }

    async fn sync(&self) -> Result<()> {
        let Some(pool) = &self.database else {
            return Ok(());
        };
        let (keys, downstream, cursor) = {
            let vault = self.vault.lock().map_err(|_| anyhow!("vault lock"))?;
            (
                vault.key_records()?,
                vault.downstream_records()?,
                vault.router_cursor(),
            )
        };
        sync_database_rows(pool, &keys, &downstream, cursor).await
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
            return Ok(json!({
                "source_of_truth": "postgresql",
                "persisted_upstream_keys": persisted_keys,
                "persisted_downstream_credentials": persisted_downstream,
                "persisted_routing_profiles": routing_profiles,
            }));
        }
        Ok(json!({
            "source_of_truth": "encrypted-file-fallback",
            "persisted_upstream_keys": self.list().len(),
            "persisted_downstream_credentials": self.list_downstream().len(),
            "persisted_routing_profiles": 0,
        }))
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
    sync_database_rows(
        pool,
        &vault.key_records()?,
        &vault.downstream_records()?,
        vault.router_cursor(),
    )
    .await
}

async fn sync_database_rows(
    pool: &PgPool,
    keys: &[VaultKeyRecord],
    downstream: &[VaultDownstreamRecord],
    cursor: usize,
) -> Result<()> {
    let mut tx = pool.begin().await.context("begin vault sync")?;
    let key_ids: Vec<Uuid> = keys.iter().map(|key| key.id).collect();
    sqlx::query("DELETE FROM nblb.request_attempts WHERE key_id <> ALL($1::uuid[])")
        .bind(&key_ids)
        .execute(&mut *tx)
        .await
        .context("remove attempts for deleted keys")?;
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
            "INSERT INTO nblb.upstream_keys (id, label, fingerprint, ciphertext, nonce, enabled, cooldown_until, request_count, failure_count) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT (id) DO UPDATE SET label=EXCLUDED.label, fingerprint=EXCLUDED.fingerprint, ciphertext=EXCLUDED.ciphertext, nonce=EXCLUDED.nonce, enabled=EXCLUDED.enabled, cooldown_until=EXCLUDED.cooldown_until, request_count=EXCLUDED.request_count, failure_count=EXCLUDED.failure_count",
        )
        .bind(key.id)
        .bind(&key.label)
        .bind(&key.fingerprint)
        .bind(&key.ciphertext)
        .bind(&key.nonce)
        .bind(key.enabled)
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
        .bind(i16::try_from(cursor % 2 + 1).context("routing cursor overflow")?)
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
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    let port = env::var("NVIDIA_BUILD_LB_PUBLIC_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(2456);
    let state = web::Data::new(build_state().await.expect("gateway configuration"));
    HttpServer::new(move || App::new().app_data(state.clone()).configure(routes))
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
        .route("/v1/videos/generations", web::post().to(multimodal))
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
        .service(Files::new("/admin", "/app/static").index_file("index.html"));
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
    let cursor = vault.cursor();
    Ok(AppState {
        vault,
        router: Mutex::new(Router::with_next_slot(cursor)),
        client: reqwest::Client::builder()
            .timeout(StdDuration::from_secs(60))
            .build()
            .context("http client")?,
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
    if needs_password {
        if let Ok(password) = read_required_secret(
            "NBLB_DATABASE_PASSWORD",
            "/run/nvidia-build-lb/secrets/db_password",
        ) {
            url = url.replacen("@", &format!(":{password}@"), 1);
        }
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
    let ready = database_ready && !state.vault.list().is_empty();
    let mut response = if ready {
        HttpResponse::Ok()
    } else {
        HttpResponse::ServiceUnavailable()
    };
    response
        .insert_header(("cache-control", "no-store"))
        .json(Health {
            status: if ready { "ok" } else { "degraded" },
            ready,
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
    let evidence = state
        .vault
        .evidence()
        .await
        .unwrap_or_else(|_| json!({"source_of_truth": "unavailable"}));
    HttpResponse::Ok().insert_header(("cache-control", "no-store")).json(json!({
        "runtime": {"status": if keys.is_empty() { "degraded" } else { "ok" }, "ready": !keys.is_empty()},
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
        Err(_) => HttpResponse::NotFound().json(json!({"error":{"code":"resource_not_found"}})),
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
    let stream = request
        .get("stream")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let keys = state.vault.list();
    let mut attempted = Vec::new();
    for _ in 0..keys.len().max(1) {
        let id = state
            .router
            .lock()
            .ok()
            .and_then(|mut router| router.select(&keys));
        let Some(id) = id else { break };
        if attempted.contains(&id) {
            break;
        }
        attempted.push(id);
        let next_cursor = state.router.lock().ok().map(|router| router.next_slot());
        if let Some(next_cursor) = next_cursor {
            let _ = state.vault.set_cursor(next_cursor).await;
        }
        let credential = match state.vault.credential(id) {
            Ok(value) => value,
            Err(_) => continue,
        };
        if state.upstream_url.starts_with("mock://") {
            if credential.contains("fail") {
                record_failure(&state, id, Some(Duration::seconds(2))).await;
                continue;
            }
            record_request(&state, id).await;
            return mock_response(&request, stream);
        }
        let result = state
            .client
            .post(&state.upstream_url)
            .bearer_auth(credential)
            .json(&request)
            .send()
            .await;
        match result {
            Ok(response) if response.status().is_success() => {
                record_request(&state, id).await;
                let status = actix_web::http::StatusCode::from_u16(response.status().as_u16())
                    .unwrap_or(actix_web::http::StatusCode::BAD_GATEWAY);
                let content_type = response
                    .headers()
                    .get("content-type")
                    .and_then(|value| value.to_str().ok())
                    .unwrap_or("application/json")
                    .to_owned();
                let body_stream = response
                    .bytes_stream()
                    .map(|chunk| chunk.map_err(actix_web::error::ErrorBadGateway));
                return HttpResponse::build(status)
                    .insert_header(("content-type", content_type))
                    .streaming(body_stream);
            }
            Ok(response)
                if response.status().as_u16() == 408
                    || response.status().as_u16() == 429
                    || response.status().is_server_error() =>
            {
                let retry = response
                    .headers()
                    .get("retry-after")
                    .and_then(|v| v.to_str().ok())
                    .and_then(|v| v.parse::<i64>().ok())
                    .map(Duration::seconds);
                record_failure(&state, id, retry).await;
            }
            Ok(response) => {
                let status = actix_web::http::StatusCode::from_u16(response.status().as_u16())
                    .unwrap_or(actix_web::http::StatusCode::BAD_GATEWAY);
                return HttpResponse::build(status).json(json!({"error":{"message":"NVIDIA rejected the request","type":"upstream_request_rejected"}}));
            }
            Err(_) => record_failure(&state, id, None).await,
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
    body: web::Json<Value>,
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
    let request = body.into_inner();
    let keys = state.vault.list();
    for _ in 0..keys.len().max(1) {
        let Some(id) = state
            .router
            .lock()
            .ok()
            .and_then(|mut router| router.select(&keys))
        else {
            break;
        };
        let next_cursor = state.router.lock().ok().map(|router| router.next_slot());
        if let Some(next_cursor) = next_cursor {
            let _ = state.vault.set_cursor(next_cursor).await;
        }
        let credential = match state.vault.credential(id).ok() {
            Some(value) => value,
            None => continue,
        };
        if state.upstream_url.starts_with("mock://") {
            record_request(&state, id).await;
            return mock_modality(req.path(), &request);
        }
        let endpoint = state.upstream_url.replace("/chat/completions", req.path());
        match state
            .client
            .post(endpoint)
            .bearer_auth(credential)
            .json(&request)
            .send()
            .await
        {
            Ok(response) if response.status().is_success() => {
                record_request(&state, id).await;
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
                    Err(_) => continue,
                };
                return HttpResponse::build(status)
                    .insert_header(("content-type", content_type))
                    .body(bytes);
            }
            Ok(response)
                if response.status().as_u16() == 429 || response.status().is_server_error() =>
            {
                record_failure(&state, id, None).await;
            }
            Ok(response) => {
                return HttpResponse::build(actix_web::http::StatusCode::from_u16(response.status().as_u16()).unwrap_or(actix_web::http::StatusCode::BAD_GATEWAY))
                    .json(json!({"error":{"message":"NVIDIA rejected the request","type":"upstream_request_rejected"}}));
            }
            Err(_) => record_failure(&state, id, None).await,
        }
    }
    HttpResponse::ServiceUnavailable().json(json!({"error":{"message":"No eligible NVIDIA upstream key","type":"upstream_unavailable"}}))
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
            json!({"created":1784332800,"data":[{"b64_json":""}],"model":model})
        }
        "/v1/audio/speech" => {
            json!({"audio":"","format":request.get("response_format").and_then(Value::as_str).unwrap_or("mp3"),"model":model})
        }
        "/v1/audio/transcriptions" => json!({"text":"NVIDIA Build LB","model":model}),
        _ => {
            json!({"id":format!("media-{}",Uuid::new_v4()),"object":"video","status":"completed","model":model})
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
        let body = format!(
            "data: {}\n\ndata: [DONE]\n\n",
            json!({"id":id,"object":"chat.completion.chunk","model":model,"choices":[{"index":0,"delta":{"role":"assistant","content":"NVIDIA Build LB"},"finish_reason":null}]})
        );
        HttpResponse::Ok()
            .insert_header(("content-type", "text/event-stream"))
            .insert_header(("cache-control", "no-cache"))
            .body(body)
    } else {
        HttpResponse::Ok().json(json!({"id":id,"object":"chat.completion","model":model,"choices":[{"index":0,"message":{"role":"assistant","content":"NVIDIA Build LB"},"finish_reason":"stop"}],"usage":{"prompt_tokens":0,"completion_tokens":3,"total_tokens":3}}))
    }
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
    bearer(req)
        .is_some_and(|value| constant_time_equal(value.as_bytes(), state.admin_token.as_bytes()))
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

async fn record_request(state: &web::Data<AppState>, id: Uuid) {
    let _ = state.vault.mutate(|vault| vault.record_request(id)).await;
}
async fn record_failure(state: &web::Data<AppState>, id: Uuid, retry_after: Option<Duration>) {
    let _ = state
        .vault
        .mutate(|vault| vault.record_failure(id, retry_after))
        .await;
}
