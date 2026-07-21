//! Privacy-safe request evidence and bounded operations maintenance.

use actix_web::{HttpResponse, http::StatusCode, web};
use anyhow::{Context, Result, anyhow, bail};
use std::time::{Duration, Instant};
use uuid::Uuid;

use super::{AppState, AttemptTerminal, RequestTerminal, VaultStore, openai_error};

const ROLLUP_BATCH_SIZE: i64 = 500;
const OPERATIONS_MAINTENANCE_LOCK_KEY: i64 = 0x4e42_4c42_4f50_534d;

#[derive(Clone, Copy)]
struct DropRecovery {
    outcome: &'static str,
    status_code: Option<u16>,
    error_class: &'static str,
    ttfb_ms: Option<i64>,
    bytes_out: usize,
}

impl Default for DropRecovery {
    fn default() -> Self {
        Self {
            outcome: "failed",
            status_code: None,
            error_class: "handler_abandoned",
            ttfb_ms: None,
            bytes_out: 0,
        }
    }
}

/// Owns the downstream request row until a non-stream response is committed or
/// a streaming body reaches a terminal state. No request content enters this
/// type; only identifiers, route metadata, status classes, and timings do.
pub(crate) struct RequestEvidence {
    state: web::Data<AppState>,
    request_id: Uuid,
    started: Instant,
    terminal: bool,
    drop_recovery: DropRecovery,
}

impl RequestEvidence {
    pub(crate) async fn start(
        state: web::Data<AppState>,
        request_id: Uuid,
        downstream_credential_id: Option<Uuid>,
        endpoint: &str,
        profile_id: &str,
        stream: bool,
        modality: &str,
    ) -> Result<Self, HttpResponse> {
        state
            .vault
            .proxy_request_started(
                request_id,
                downstream_credential_id,
                endpoint,
                profile_id,
                stream,
                modality,
            )
            .await
            .map_err(|_| evidence_unavailable())?;
        Ok(Self {
            state,
            request_id,
            started: Instant::now(),
            terminal: false,
            drop_recovery: DropRecovery::default(),
        })
    }

    pub(crate) fn elapsed_ms(&self) -> i64 {
        i64::try_from(self.started.elapsed().as_millis()).unwrap_or(i64::MAX)
    }

    pub(crate) async fn fail(
        &mut self,
        status: StatusCode,
        error_class: &'static str,
    ) -> Result<(), HttpResponse> {
        self.finish("failed", Some(status.as_u16()), Some(error_class), None)
            .await
    }

    /// Commits the final upstream attempt and its parent request as one
    /// terminal transition. Intermediate attempts that return to the failover
    /// loop deliberately continue to use the attempt-only ledger update.
    pub(crate) async fn finish_with_attempt(
        &mut self,
        key_id: Uuid,
        attempt: AttemptTerminal,
        request: RequestTerminal,
    ) -> Result<(), HttpResponse> {
        self.arm_drop_recovery(
            "failed",
            Some(StatusCode::INTERNAL_SERVER_ERROR.as_u16()),
            "evidence_unavailable",
            request.ttfb_ms(),
            terminal_bytes_out(attempt),
        );
        self.state
            .vault
            .request_and_attempt_finished(self.request_id, key_id, attempt, request)
            .await
            .map_err(|_| evidence_unavailable())?;
        self.terminal = true;
        Ok(())
    }

    pub(crate) async fn finish_with_attempt_silently(
        &mut self,
        key_id: Uuid,
        attempt: AttemptTerminal,
        request: RequestTerminal,
    ) -> bool {
        self.arm_drop_recovery(
            "failed",
            request.status_code(),
            "evidence_unavailable",
            request.ttfb_ms(),
            terminal_bytes_out(attempt),
        );
        if let Err(error) = self
            .state
            .vault
            .request_and_attempt_finished(self.request_id, key_id, attempt, request)
            .await
        {
            eprintln!("request/final-attempt terminal update failed: {error:#}");
            return false;
        }
        self.terminal = true;
        true
    }

    pub(crate) async fn finish_with_open_attempts_silently(
        &mut self,
        outcome: &'static str,
        status_code: Option<u16>,
        error_class: &'static str,
        ttfb_ms: Option<i64>,
        bytes_out: usize,
    ) -> bool {
        self.arm_drop_recovery(outcome, status_code, error_class, ttfb_ms, bytes_out);
        if let Err(error) = self
            .state
            .vault
            .proxy_request_and_open_attempts_finished(
                self.request_id,
                outcome,
                status_code,
                error_class,
                ttfb_ms,
                bytes_out,
            )
            .await
        {
            eprintln!("request/attempt terminal update failed: {error:#}");
            return false;
        }
        self.terminal = true;
        true
    }

    async fn finish(
        &mut self,
        outcome: &'static str,
        status_code: Option<u16>,
        error_class: Option<&'static str>,
        ttfb_ms: Option<i64>,
    ) -> Result<(), HttpResponse> {
        self.arm_drop_recovery(
            "failed",
            Some(StatusCode::INTERNAL_SERVER_ERROR.as_u16()),
            "evidence_unavailable",
            ttfb_ms,
            0,
        );
        self.state
            .vault
            .proxy_request_finished(self.request_id, outcome, status_code, error_class, ttfb_ms)
            .await
            .map_err(|_| evidence_unavailable())?;
        self.terminal = true;
        Ok(())
    }

    fn arm_drop_recovery(
        &mut self,
        outcome: &'static str,
        status_code: Option<u16>,
        error_class: &'static str,
        ttfb_ms: Option<i64>,
        bytes_out: usize,
    ) {
        self.drop_recovery = DropRecovery {
            outcome,
            status_code,
            error_class,
            ttfb_ms,
            bytes_out,
        };
    }
}

impl Drop for RequestEvidence {
    fn drop(&mut self) {
        if self.terminal {
            return;
        }
        let state = self.state.clone();
        let request_id = self.request_id;
        let recovery = self.drop_recovery;
        tokio::spawn(async move {
            if let Err(error) = state
                .vault
                .proxy_request_and_open_attempts_finished(
                    request_id,
                    recovery.outcome,
                    recovery.status_code,
                    recovery.error_class,
                    recovery.ttfb_ms,
                    recovery.bytes_out,
                )
                .await
            {
                eprintln!("request evidence drop update failed: {error:#}");
            }
        });
    }
}

impl VaultStore {
    async fn proxy_request_started(
        &self,
        request_id: Uuid,
        downstream_credential_id: Option<Uuid>,
        endpoint: &str,
        profile_id: &str,
        stream: bool,
        modality: &str,
    ) -> Result<()> {
        let Some(pool) = &self.database else {
            return Ok(());
        };
        let owner_id = self
            .owner_id
            .context("database evidence owner lease is missing")?;
        sqlx::query(
            "INSERT INTO nblb.proxy_requests (request_id, owner_id, downstream_credential_id, endpoint, profile_id, stream, modality) VALUES ($1,$2,$3,$4,$5,$6,$7)",
        )
        .bind(request_id)
        .bind(owner_id)
        .bind(downstream_credential_id)
        .bind(endpoint)
        .bind(profile_id)
        .bind(stream)
        .bind(modality)
        .execute(pool)
        .await
        .context("start proxy request evidence")?;
        Ok(())
    }

    async fn proxy_request_finished(
        &self,
        request_id: Uuid,
        outcome: &str,
        status_code: Option<u16>,
        error_class: Option<&str>,
        ttfb_ms: Option<i64>,
    ) -> Result<()> {
        let Some(pool) = &self.database else {
            return Ok(());
        };
        let result = sqlx::query(
            "UPDATE nblb.proxy_requests AS request SET outcome=$2, status_code=$3, error_class=$4, duration_ms=GREATEST(0, floor(extract(epoch FROM (now() - request.started_at)) * 1000)::bigint), ttfb_ms=$5, failover_count=GREATEST(0, (SELECT count(*) - 1 FROM nblb.request_attempts AS attempt WHERE attempt.proxy_request_id=request.id))::smallint, finished_at=now() WHERE request.request_id=$1 AND request.outcome='started'",
        )
        .bind(request_id)
        .bind(outcome)
        .bind(status_code.map(i32::from))
        .bind(error_class)
        .bind(ttfb_ms)
        .execute(pool)
        .await
        .context("finish proxy request evidence")?;
        if result.rows_affected() != 1 {
            bail!("proxy request terminal row is missing")
        }
        Ok(())
    }

    async fn proxy_request_and_open_attempts_finished(
        &self,
        request_id: Uuid,
        outcome: &str,
        status_code: Option<u16>,
        error_class: &str,
        ttfb_ms: Option<i64>,
        bytes_out: usize,
    ) -> Result<()> {
        let Some(pool) = &self.database else {
            return Ok(());
        };
        let owner_id = self
            .owner_id
            .context("database evidence owner lease is missing")?;
        let mut tx = pool
            .begin()
            .await
            .context("begin request terminal recovery")?;
        sqlx::query(
            "UPDATE nblb.request_attempts AS attempt SET outcome=$2, status_code=$3, error_class=$4, bytes_out=$5, latency_ms=GREATEST(0, floor(extract(epoch FROM (now() - attempt.created_at)) * 1000)::bigint), finished_at=now() WHERE attempt.request_id=$1 AND attempt.owner_id=$6 AND attempt.finished_at IS NULL",
        )
        .bind(request_id)
        .bind(outcome)
        .bind(status_code.map(i32::from))
        .bind(error_class)
        .bind(i64::try_from(bytes_out).unwrap_or(i64::MAX))
        .bind(owner_id)
        .execute(&mut *tx)
        .await
        .context("close open attempts with request")?;
        let result = sqlx::query(
            "UPDATE nblb.proxy_requests AS request SET outcome=$2, status_code=$3, error_class=$4, duration_ms=GREATEST(0, floor(extract(epoch FROM (now() - request.started_at)) * 1000)::bigint), ttfb_ms=$5, failover_count=GREATEST(0, (SELECT count(*) - 1 FROM nblb.request_attempts AS attempt WHERE attempt.proxy_request_id=request.id))::smallint, finished_at=now() WHERE request.request_id=$1 AND request.outcome='started'",
        )
        .bind(request_id)
        .bind(outcome)
        .bind(status_code.map(i32::from))
        .bind(error_class)
        .bind(ttfb_ms)
        .execute(&mut *tx)
        .await
        .context("close request with open attempts")?;
        if result.rows_affected() != 1 {
            bail!("proxy request terminal row is missing")
        }
        tx.commit()
            .await
            .context("commit request terminal recovery")?;
        Ok(())
    }

    pub(crate) async fn rollup_request_metrics(&self) -> Result<u64> {
        let Some(pool) = &self.database else {
            return Ok(0);
        };
        let mut tx = pool.begin().await.context("begin metric rollup")?;
        let locked = sqlx::query_scalar::<_, bool>("SELECT pg_try_advisory_xact_lock($1)")
            .bind(OPERATIONS_MAINTENANCE_LOCK_KEY)
            .fetch_one(&mut *tx)
            .await
            .context("lock metric rollup")?;
        if !locked {
            tx.rollback()
                .await
                .context("rollback skipped metric rollup")?;
            return Ok(0);
        }
        let rolled_up =
            sqlx::query_scalar::<_, i64>(include_str!("../sql/rollup_request_metrics.sql"))
                .bind(ROLLUP_BATCH_SIZE)
                .fetch_one(&mut *tx)
                .await
                .context("roll up request metrics")?;
        tx.commit().await.context("commit metric rollup")?;
        u64::try_from(rolled_up).map_err(|_| anyhow!("negative metric rollup count"))
    }

    pub(crate) async fn apply_operations_retention(&self) -> Result<u64> {
        let Some(pool) = &self.database else {
            return Ok(0);
        };
        let mut tx = pool.begin().await.context("begin operations retention")?;
        let locked = sqlx::query_scalar::<_, bool>("SELECT pg_try_advisory_xact_lock($1)")
            .bind(OPERATIONS_MAINTENANCE_LOCK_KEY)
            .fetch_one(&mut *tx)
            .await
            .context("lock operations retention")?;
        if !locked {
            tx.rollback()
                .await
                .context("rollback skipped operations retention")?;
            return Ok(0);
        }
        let mut deleted = 0_u64;
        for statement in [
            "DELETE FROM nblb.request_attempts WHERE finished_at IS NOT NULL AND created_at < now() - interval '30 days'",
            "DELETE FROM nblb.proxy_requests WHERE finished_at IS NOT NULL AND started_at < now() - interval '30 days' AND (rolled_up_at IS NOT NULL OR finished_at < now() - interval '90 days')",
            "DELETE FROM nblb.metric_buckets_minute WHERE bucket_start < now() - interval '90 days'",
            "DELETE FROM nblb.probe_runs WHERE finished_at IS NOT NULL AND created_at < now() - interval '90 days'",
            "DELETE FROM nblb.audit_events WHERE created_at < now() - interval '180 days'",
            "DELETE FROM nblb.qa_runs WHERE finished_at IS NOT NULL AND created_at < now() - interval '180 days'",
        ] {
            deleted = deleted.saturating_add(
                sqlx::query(statement)
                    .execute(&mut *tx)
                    .await
                    .context("apply operations retention statement")?
                    .rows_affected(),
            );
        }
        tx.commit().await.context("commit operations retention")?;
        Ok(deleted)
    }

    pub(crate) async fn cleanup_stale_proxy_requests(&self) -> Result<u64> {
        let Some(pool) = &self.database else {
            return Ok(0);
        };
        let result = sqlx::query(
            "UPDATE nblb.proxy_requests AS request SET outcome='abandoned_after_restart', error_class='owner_lease_expired', duration_ms=GREATEST(0, floor(extract(epoch FROM (now() - request.started_at)) * 1000)::bigint), failover_count=GREATEST(0, (SELECT count(*) - 1 FROM nblb.request_attempts AS attempt WHERE attempt.proxy_request_id=request.id))::smallint, finished_at=now() WHERE request.finished_at IS NULL AND (request.owner_id IS NULL OR NOT EXISTS (SELECT 1 FROM nblb.gateway_instances AS instance WHERE instance.id=request.owner_id AND instance.last_seen_at >= now() - interval '30 seconds'))",
        )
        .execute(pool)
        .await
        .context("close proxy requests from expired gateway owners")?;
        Ok(result.rows_affected())
    }
}

pub(crate) fn spawn_operations_workers(state: web::Data<AppState>) {
    if state.vault.database.is_none() {
        return;
    }
    let rollup_state = state.clone();
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(5));
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            interval.tick().await;
            if let Err(error) = rollup_state.vault.rollup_request_metrics().await {
                eprintln!("request metric rollup failed: {error:#}");
            }
        }
    });
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(60 * 60));
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            interval.tick().await;
            if let Err(error) = state.vault.apply_operations_retention().await {
                eprintln!("operations retention failed: {error:#}");
            }
        }
    });
}

pub(crate) fn modality_for_path(path: &str) -> &'static str {
    match path {
        "/v1/chat/completions" => "text",
        "/v1/embeddings" => "embedding",
        "/v1/images/generations" => "image",
        "/v1/videos/generations" | "/v1/nvidia/inference" => "video",
        "/v1/audio/speech" => "speech",
        "/v1/audio/transcriptions" => "transcription",
        _ => "unknown",
    }
}

fn terminal_bytes_out(terminal: AttemptTerminal) -> usize {
    terminal
        .bytes_out
        .and_then(|bytes| usize::try_from(bytes).ok())
        .unwrap_or(0)
}

fn evidence_unavailable() -> HttpResponse {
    openai_error(
        StatusCode::INTERNAL_SERVER_ERROR,
        "Request evidence is temporarily unavailable.",
        "service_unavailable_error",
        "evidence_unavailable",
    )
}

#[cfg(test)]
mod tests {
    use super::{
        AppState, AttemptTerminal, RequestEvidence, RequestTerminal, VaultStore, modality_for_path,
    };
    use crate::streaming::{
        StreamAttemptGuard, StreamTerminalResult, UpstreamByteStream, chat_response_stream,
        prime_stream,
    };
    use actix_web::web;
    use bytes::Bytes;
    use futures_util::{StreamExt, stream};
    use nvidia_build_lb_core::Vault;
    use serde_json::json;
    use std::sync::Mutex;
    use uuid::Uuid;

    // Rollup tests never create request evidence, so they intentionally do not
    // register a gateway owner lease. Evidence creation itself fails closed if
    // a database-backed store has no owner.
    fn rollup_test_store(pool: sqlx::PgPool, path: &std::path::Path) -> VaultStore {
        VaultStore {
            vault: Mutex::new(Vault::open(path, [31; 32]).expect("open test vault")),
            database: Some(pool),
            sync_lock: tokio::sync::Mutex::new(()),
            owner_id: None,
        }
    }

    async fn start_test_stream(
        state: &web::Data<AppState>,
        request_id: Uuid,
        key_id: Uuid,
    ) -> StreamAttemptGuard {
        let evidence = match RequestEvidence::start(
            state.clone(),
            request_id,
            None,
            "/v1/chat/completions",
            "z-ai/glm-5.2",
            true,
            "text",
        )
        .await
        {
            Ok(evidence) => evidence,
            Err(_) => panic!("start test request evidence"),
        };
        state
            .vault
            .attempt_started(request_id, "z-ai/glm-5.2", key_id)
            .await
            .expect("start test request attempt");
        StreamAttemptGuard::with_request(evidence)
    }

    async fn wait_for_terminal_pair(
        pool: &sqlx::PgPool,
        request_id: Uuid,
    ) -> (String, String, Option<String>, Option<String>) {
        for _ in 0..50 {
            let state = sqlx::query_as::<_, (String, String, Option<String>, Option<String>)>(
                "SELECT request.outcome,attempt.outcome,request.error_class,attempt.error_class FROM nblb.proxy_requests AS request JOIN nblb.request_attempts AS attempt ON attempt.proxy_request_id=request.id WHERE request.request_id=$1",
            )
            .bind(request_id)
            .fetch_one(pool)
            .await
            .expect("load request terminal pair");
            if state.0 != "started" && state.1 != "started" {
                return state;
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        panic!("request terminal pair did not close")
    }

    #[test]
    fn pr2_modality_mapping_contains_no_user_content() {
        assert_eq!(modality_for_path("/v1/chat/completions"), "text");
        assert_eq!(modality_for_path("/v1/embeddings"), "embedding");
        assert_eq!(
            modality_for_path("/v1/audio/transcriptions"),
            "transcription"
        );
        assert_eq!(modality_for_path("/unrecognised"), "unknown");
    }

    #[test]
    fn pr2_operations_schema_has_no_content_columns() {
        let sql = include_str!("../../../migrations/sqlx/0012_operations_evidence.sql");
        let schema = sql
            .lines()
            .filter(|line| !line.trim_start().starts_with("--"))
            .collect::<Vec<_>>()
            .join("\n")
            .to_ascii_lowercase();
        for forbidden in [
            "request_body",
            "response_body",
            "provider_body",
            "authorization_header",
            "prompt_text",
            "tool_arguments",
            "media_bytes",
            "plaintext_token",
            "plaintext_key",
        ] {
            assert!(
                !schema.contains(forbidden),
                "forbidden schema field: {forbidden}"
            );
        }
    }

    #[sqlx::test(migrations = "../../migrations/sqlx")]
    async fn pr2_database_evidence_requires_owner_lease(pool: sqlx::PgPool) {
        let dir = tempfile::tempdir().expect("test directory");
        let state = web::Data::new(AppState {
            vault: rollup_test_store(pool.clone(), &dir.path().join("vault.json")),
            router: Mutex::new(Default::default()),
            selection_lock: tokio::sync::Mutex::new(()),
            client: reqwest::Client::new(),
            admin_token: "test-admin".into(),
            upstream_url: "mock://provider".into(),
            require_downstream_token: false,
            public_port: 2456,
            csp_hashes: Vec::new(),
        });
        let request_id = Uuid::new_v4();

        assert!(
            RequestEvidence::start(
                state,
                request_id,
                None,
                "/v1/chat/completions",
                "z-ai/glm-5.2",
                false,
                "text",
            )
            .await
            .is_err(),
            "database-backed evidence must fail closed without an owner lease"
        );
        assert_eq!(
            sqlx::query_scalar::<_, i64>(
                "SELECT count(*) FROM nblb.proxy_requests WHERE request_id=$1"
            )
            .bind(request_id)
            .fetch_one(&pool)
            .await
            .expect("count owner-less evidence rows"),
            0,
        );
    }

    #[sqlx::test(migrations = "../../migrations/sqlx")]
    async fn pr2_rollup_is_idempotent_classified_and_percentile_ready(pool: sqlx::PgPool) {
        let key_id = Uuid::new_v4();
        let client_id = Uuid::new_v4();
        sqlx::query(
            "INSERT INTO nblb.upstream_keys(id,label,fingerprint,ciphertext,nonce,enabled,verified) VALUES ($1,'test-key',decode(repeat('11',32),'hex'),decode(repeat('22',24),'hex'),decode(repeat('33',12),'hex'),true,true)",
        )
        .bind(key_id)
        .execute(&pool)
        .await
        .expect("seed upstream");
        sqlx::query(
            "INSERT INTO nblb.downstream_credentials(id,label,digest,scopes) VALUES ($1,'test-client',decode(repeat('44',32),'hex'),ARRAY['chat:write'])",
        )
        .bind(client_id)
        .execute(&pool)
        .await
        .expect("seed downstream");

        let request_id = Uuid::new_v4();
        let proxy_id = sqlx::query_scalar::<_, Uuid>(
            "INSERT INTO nblb.proxy_requests(request_id,downstream_credential_id,endpoint,profile_id,stream,modality,outcome,status_code,duration_ms,ttfb_ms,failover_count,finished_at) VALUES ($1,$2,'/v1/chat/completions','z-ai/glm-5.2',true,'text','succeeded',200,600,300,1,now()) RETURNING id",
        )
        .bind(request_id)
        .bind(client_id)
        .fetch_one(&pool)
        .await
        .expect("seed parent request");
        for (attempt_no, outcome, status_code, error_class, bytes_out) in [
            (
                1_i16,
                "failed",
                Some(429_i16),
                Some("upstream_rate_limited"),
                None,
            ),
            (2_i16, "succeeded", Some(200_i16), None, Some(128_i64)),
        ] {
            sqlx::query(
                "INSERT INTO nblb.request_attempts(request_id,profile_id,key_id,outcome,proxy_request_id,attempt_no,status_code,error_class,latency_ms,response_started,bytes_out,finished_at) VALUES ($1,'z-ai/glm-5.2',$2,$3,$4,$5,$6,$7,100,true,$8,now())",
            )
            .bind(request_id)
            .bind(key_id)
            .bind(outcome)
            .bind(proxy_id)
            .bind(attempt_no)
            .bind(status_code)
            .bind(error_class)
            .bind(bytes_out)
            .execute(&pool)
            .await
            .expect("seed attempt");
        }

        let dir = tempfile::tempdir().expect("test directory");
        let store = rollup_test_store(pool.clone(), &dir.path().join("vault.json"));
        let (first, second) = tokio::join!(
            store.rollup_request_metrics(),
            store.rollup_request_metrics()
        );
        assert_eq!(
            first.expect("first rollup") + second.expect("second rollup"),
            1
        );
        assert_eq!(
            store.rollup_request_metrics().await.expect("repeat rollup"),
            0
        );

        let metric = sqlx::query_as::<_, (i64, i64, i64, i64, i64, i64)>(
            "SELECT request_count,failover_count,duration_sample_count,ttfb_sample_count,duration_le_1000,ttfb_le_500 FROM nblb.metric_buckets_minute WHERE profile_id='z-ai/glm-5.2' AND outcome_class='success'",
        )
        .fetch_one(&pool)
        .await
        .expect("load rollup");
        assert_eq!(metric, (1, 1, 1, 1, 1, 1));

        let rate_limited_id = Uuid::new_v4();
        sqlx::query(
            "INSERT INTO nblb.proxy_requests(request_id,endpoint,profile_id,stream,modality,outcome,status_code,error_class,duration_ms,finished_at) VALUES ($1,'/v1/chat/completions','z-ai/glm-5.2',false,'text','failed',429,'upstream_rate_limited',10,now())",
        )
        .bind(rate_limited_id)
        .execute(&pool)
        .await
        .expect("seed rate-limited request");
        assert_eq!(
            store
                .rollup_request_metrics()
                .await
                .expect("rate-limit rollup"),
            1
        );
        let upstream_errors = sqlx::query_scalar::<_, i64>(
            "SELECT sum(request_count)::bigint FROM nblb.metric_buckets_minute WHERE outcome_class='upstream_error'",
        )
        .fetch_one(&pool)
        .await
        .expect("load upstream errors");
        assert_eq!(upstream_errors, 1);

        assert!(
            sqlx::query("UPDATE nblb.downstream_credentials SET metadata='{\"nested\":{\"secret\":\"forbidden\"}}'::jsonb WHERE id=$1")
                .bind(client_id)
                .execute(&pool)
                .await
                .is_err()
        );
    }

    #[sqlx::test(migrations = "../../migrations/sqlx")]
    async fn pr2_restart_and_retention_preserve_terminal_contract(pool: sqlx::PgPool) {
        let dir = tempfile::tempdir().expect("test directory");
        let path = dir.path().join("vault.json");
        let key_id = {
            let mut vault = Vault::open(&path, [31; 32]).expect("open seed vault");
            let key = vault
                .add("restart-key", "nvapi-abcdefghijklmnopqrstuvwxyz123456")
                .expect("add encrypted key");
            let record = vault
                .key_records()
                .expect("read encrypted record")
                .remove(0);
            sqlx::query(
                "INSERT INTO nblb.upstream_keys(id,label,fingerprint,ciphertext,nonce,enabled,verified,retired,request_count,failure_count) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
            )
            .bind(record.id)
            .bind(record.label)
            .bind(record.fingerprint)
            .bind(record.ciphertext)
            .bind(record.nonce)
            .bind(record.enabled)
            .bind(record.verified)
            .bind(record.retired)
            .bind(i64::try_from(record.request_count).expect("request count"))
            .bind(i64::try_from(record.failure_count).expect("failure count"))
            .execute(&pool)
            .await
            .expect("seed encrypted upstream");
            key.id
        };
        let owner_id = Uuid::new_v4();
        sqlx::query(
            "INSERT INTO nblb.gateway_instances(id,started_at,last_seen_at) VALUES ($1,now()-interval '2 minutes',now()-interval '2 minutes')",
        )
        .bind(owner_id)
        .execute(&pool)
        .await
        .expect("seed stale owner");
        let request_id = Uuid::new_v4();
        let proxy_id = sqlx::query_scalar::<_, Uuid>(
            "INSERT INTO nblb.proxy_requests(request_id,owner_id,endpoint,profile_id,stream,modality) VALUES ($1,$2,'/v1/chat/completions','z-ai/glm-5.2',false,'text') RETURNING id",
        )
        .bind(request_id)
        .bind(owner_id)
        .fetch_one(&pool)
        .await
        .expect("seed stale request");
        sqlx::query(
            "INSERT INTO nblb.request_attempts(request_id,profile_id,key_id,outcome,owner_id,proxy_request_id,attempt_no) VALUES ($1,'z-ai/glm-5.2',$2,'started',$3,$4,1)",
        )
        .bind(request_id)
        .bind(key_id)
        .bind(owner_id)
        .bind(proxy_id)
        .execute(&pool)
        .await
        .expect("seed stale attempt");

        let store = VaultStore::open(&path, [31; 32], Some(pool.clone()))
            .await
            .expect("reopen gateway");
        let states = sqlx::query_as::<_, (String, String)>(
            "SELECT request.outcome,attempt.outcome FROM nblb.proxy_requests AS request JOIN nblb.request_attempts AS attempt ON attempt.proxy_request_id=request.id WHERE request.id=$1",
        )
        .bind(proxy_id)
        .fetch_one(&pool)
        .await
        .expect("load reconciled rows");
        assert_eq!(
            states,
            (
                "abandoned_after_restart".into(),
                "abandoned_after_restart".into()
            )
        );

        let unrolled_id = Uuid::new_v4();
        sqlx::query(
            "INSERT INTO nblb.proxy_requests(request_id,endpoint,profile_id,stream,modality,outcome,status_code,duration_ms,started_at,finished_at) VALUES ($1,'/v1/chat/completions','z-ai/glm-5.2',false,'text','succeeded',200,10,now()-interval '31 days',now()-interval '31 days')",
        )
        .bind(unrolled_id)
        .execute(&pool)
        .await
        .expect("seed unrolled request");
        store
            .apply_operations_retention()
            .await
            .expect("apply retention");
        assert_eq!(
            sqlx::query_scalar::<_, i64>(
                "SELECT count(*) FROM nblb.proxy_requests WHERE request_id=$1"
            )
            .bind(unrolled_id)
            .fetch_one(&pool)
            .await
            .expect("count preserved request"),
            1
        );
        sqlx::query("UPDATE nblb.proxy_requests SET rolled_up_at=now() WHERE request_id=$1")
            .bind(unrolled_id)
            .execute(&pool)
            .await
            .expect("mark request rolled up");
        store
            .apply_operations_retention()
            .await
            .expect("apply post-rollup retention");
        assert_eq!(
            sqlx::query_scalar::<_, i64>(
                "SELECT count(*) FROM nblb.proxy_requests WHERE request_id=$1"
            )
            .bind(unrolled_id)
            .fetch_one(&pool)
            .await
            .expect("count expired request"),
            0
        );
    }

    #[sqlx::test(migrations = "../../migrations/sqlx")]
    async fn pr2_final_attempt_and_parent_rollback_together(pool: sqlx::PgPool) {
        let key_id = Uuid::new_v4();
        let owner_id = Uuid::new_v4();
        sqlx::query(
            "INSERT INTO nblb.upstream_keys(id,label,fingerprint,ciphertext,nonce,enabled,verified) VALUES ($1,'atomic-key',decode(repeat('51',32),'hex'),decode(repeat('52',24),'hex'),decode(repeat('53',12),'hex'),true,true)",
        )
        .bind(key_id)
        .execute(&pool)
        .await
        .expect("seed atomic upstream");
        sqlx::query(
            "INSERT INTO nblb.gateway_instances(id,started_at,last_seen_at) VALUES ($1,now(),now())",
        )
        .bind(owner_id)
        .execute(&pool)
        .await
        .expect("seed active owner");
        sqlx::query(
            "CREATE TABLE nblb.test_terminal_failpoints(request_id uuid PRIMARY KEY,target text NOT NULL CHECK (target IN ('request_attempts','proxy_requests')))",
        )
        .execute(&pool)
        .await
        .expect("create failpoint table");
        sqlx::query(
            "CREATE FUNCTION nblb.test_terminal_failpoint() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF EXISTS (SELECT 1 FROM nblb.test_terminal_failpoints WHERE request_id=NEW.request_id AND target=TG_TABLE_NAME) THEN RAISE EXCEPTION 'forced terminal failure'; END IF; RETURN NEW; END $$",
        )
        .execute(&pool)
        .await
        .expect("create failpoint function");
        for table in ["request_attempts", "proxy_requests"] {
            sqlx::query(&format!(
                "CREATE TRIGGER test_terminal_failpoint BEFORE UPDATE ON nblb.{table} FOR EACH ROW EXECUTE FUNCTION nblb.test_terminal_failpoint()"
            ))
            .execute(&pool)
            .await
            .expect("create terminal failpoint trigger");
        }

        let dir = tempfile::tempdir().expect("test directory");
        let store = VaultStore {
            vault: Mutex::new(
                Vault::open(dir.path().join("vault.json"), [31; 32]).expect("open test vault"),
            ),
            database: Some(pool.clone()),
            sync_lock: tokio::sync::Mutex::new(()),
            owner_id: Some(owner_id),
        };

        for target in ["request_attempts", "proxy_requests"] {
            let request_id = Uuid::new_v4();
            let proxy_id = sqlx::query_scalar::<_, Uuid>(
                "INSERT INTO nblb.proxy_requests(request_id,owner_id,endpoint,profile_id,stream,modality) VALUES ($1,$2,'/v1/chat/completions','z-ai/glm-5.2',true,'text') RETURNING id",
            )
            .bind(request_id)
            .bind(owner_id)
            .fetch_one(&pool)
            .await
            .expect("seed atomic parent");
            sqlx::query(
                "INSERT INTO nblb.request_attempts(request_id,profile_id,key_id,owner_id,proxy_request_id,attempt_no,outcome) VALUES ($1,'z-ai/glm-5.2',$2,$3,$4,1,'started')",
            )
            .bind(request_id)
            .bind(key_id)
            .bind(owner_id)
            .bind(proxy_id)
            .execute(&pool)
            .await
            .expect("seed atomic child");
            sqlx::query(
                "INSERT INTO nblb.test_terminal_failpoints(request_id,target) VALUES ($1,$2)",
            )
            .bind(request_id)
            .bind(target)
            .execute(&pool)
            .await
            .expect("enable terminal failpoint");

            assert!(
                store
                    .request_and_attempt_finished(
                        request_id,
                        key_id,
                        AttemptTerminal::succeeded(200, Some(64)),
                        RequestTerminal::succeeded(200).with_ttfb(Some(25)),
                    )
                    .await
                    .is_err(),
                "{target} failpoint must abort the transaction"
            );
            let rolled_back = sqlx::query_as::<_, (String, String, Option<chrono::DateTime<chrono::Utc>>, Option<chrono::DateTime<chrono::Utc>>)>(
                "SELECT request.outcome,attempt.outcome,request.finished_at,attempt.finished_at FROM nblb.proxy_requests AS request JOIN nblb.request_attempts AS attempt ON attempt.proxy_request_id=request.id WHERE request.id=$1",
            )
            .bind(proxy_id)
            .fetch_one(&pool)
            .await
            .expect("load rolled-back terminal rows");
            assert_eq!(rolled_back.0, "started");
            assert_eq!(rolled_back.1, "started");
            assert!(rolled_back.2.is_none());
            assert!(rolled_back.3.is_none());
            assert_eq!(
                sqlx::query_scalar::<_, i64>(
                    "SELECT count(*) FROM nblb.profile_probe_receipts WHERE profile_id='z-ai/glm-5.2' AND key_id=$1"
                )
                .bind(key_id)
                .fetch_one(&pool)
                .await
                .expect("count rolled-back receipt"),
                0
            );

            sqlx::query("DELETE FROM nblb.test_terminal_failpoints WHERE request_id=$1")
                .bind(request_id)
                .execute(&pool)
                .await
                .expect("disable terminal failpoint");
            store
                .proxy_request_and_open_attempts_finished(
                    request_id,
                    "failed",
                    Some(500),
                    "ledger_recovery",
                    Some(25),
                    64,
                )
                .await
                .expect("recover request and attempt together");
            let recovered = sqlx::query_as::<_, (String, String)>(
                "SELECT request.outcome,attempt.outcome FROM nblb.proxy_requests AS request JOIN nblb.request_attempts AS attempt ON attempt.proxy_request_id=request.id WHERE request.id=$1",
            )
            .bind(proxy_id)
            .fetch_one(&pool)
            .await
            .expect("load recovered terminal rows");
            assert_eq!(recovered, ("failed".into(), "failed".into()));
        }

        sqlx::query("CREATE SEQUENCE nblb.test_terminal_once START 1")
            .execute(&pool)
            .await
            .expect("create one-shot failpoint sequence");
        sqlx::query(
            "CREATE FUNCTION nblb.test_terminal_fail_once() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF nextval('nblb.test_terminal_once')=1 THEN RAISE EXCEPTION 'forced one-shot terminal failure'; END IF; RETURN NEW; END $$",
        )
        .execute(&pool)
        .await
        .expect("create one-shot failpoint function");
        sqlx::query(
            "CREATE TRIGGER test_terminal_fail_once BEFORE UPDATE ON nblb.proxy_requests FOR EACH ROW EXECUTE FUNCTION nblb.test_terminal_fail_once()",
        )
        .execute(&pool)
        .await
        .expect("create one-shot failpoint trigger");

        let state = web::Data::new(AppState {
            vault: store,
            router: Mutex::new(Default::default()),
            selection_lock: tokio::sync::Mutex::new(()),
            client: reqwest::Client::new(),
            admin_token: "test-admin".into(),
            upstream_url: "mock://provider".into(),
            require_downstream_token: false,
            public_port: 2456,
            csp_hashes: Vec::new(),
        });

        sqlx::query("ALTER SEQUENCE nblb.test_terminal_once RESTART WITH 1")
            .execute(&pool)
            .await
            .expect("reset one-shot non-stream failpoint");
        let non_stream_id = Uuid::new_v4();
        let mut non_stream_evidence = match RequestEvidence::start(
            state.clone(),
            non_stream_id,
            None,
            "/v1/chat/completions",
            "z-ai/glm-5.2",
            false,
            "text",
        )
        .await
        {
            Ok(evidence) => evidence,
            Err(_) => panic!("start non-stream request evidence"),
        };
        state
            .vault
            .attempt_started(non_stream_id, "z-ai/glm-5.2", key_id)
            .await
            .expect("start non-stream request attempt");
        assert!(
            non_stream_evidence
                .finish_with_attempt(
                    key_id,
                    AttemptTerminal::succeeded(200, Some(64)),
                    RequestTerminal::succeeded(200),
                )
                .await
                .is_err(),
            "atomic finalizer failpoint must fail closed"
        );
        drop(non_stream_evidence);
        assert_eq!(
            wait_for_terminal_pair(&pool, non_stream_id).await,
            (
                "failed".into(),
                "failed".into(),
                Some("evidence_unavailable".into()),
                Some("evidence_unavailable".into()),
            ),
            "non-stream Drop recovery must not relabel a finalizer failure as handler abandonment"
        );

        sqlx::query("ALTER SEQUENCE nblb.test_terminal_once RESTART WITH 1")
            .execute(&pool)
            .await
            .expect("reset one-shot awaited recovery failpoint");
        let recovered_id = Uuid::new_v4();
        let mut recovered_guard = start_test_stream(&state, recovered_id, key_id).await;
        recovered_guard.mark_response_started(25);
        recovered_guard.mark_response_committed(200);
        recovered_guard.record_bytes_out(64);
        assert_eq!(
            recovered_guard
                .finish_request_and_attempt(
                    key_id,
                    AttemptTerminal::succeeded(200, Some(64)),
                    RequestTerminal::succeeded(200),
                    64,
                )
                .await,
            StreamTerminalResult::RecoveredAsEvidenceFailure,
            "one-shot atomic failure must be distinct from requested success"
        );
        recovered_guard.mark_terminal();
        drop(recovered_guard);
        let recovered = sqlx::query_as::<_, (String, String, Option<String>, Option<String>, Option<i64>, Option<i64>)>(
            "SELECT request.outcome,attempt.outcome,request.error_class,attempt.error_class,request.ttfb_ms,attempt.bytes_out FROM nblb.proxy_requests AS request JOIN nblb.request_attempts AS attempt ON attempt.proxy_request_id=request.id WHERE request.request_id=$1",
        )
        .bind(recovered_id)
        .fetch_one(&pool)
        .await
        .expect("load awaited stream recovery");
        assert_eq!(
            recovered,
            (
                "failed".into(),
                "failed".into(),
                Some("evidence_unavailable".into()),
                Some("evidence_unavailable".into()),
                Some(25),
                Some(64),
            )
        );

        let stream_key = state
            .vault
            .mutate(|vault| vault.add("stream-key", "nvapi-0123456789abcdefghijklmnopqrstuvwxyz"))
            .await
            .expect("seed stream key in vault and database");
        sqlx::query("ALTER SEQUENCE nblb.test_terminal_once RESTART WITH 1")
            .execute(&pool)
            .await
            .expect("reset one-shot stream failpoint");
        let stream_id = Uuid::new_v4();
        let mut stream_guard = start_test_stream(&state, stream_id, stream_key.id).await;
        state
            .vault
            .attempt_response_started(stream_id, stream_key.id, 40)
            .await
            .expect("mark stream response start");
        stream_guard.mark_response_started(40);
        stream_guard.mark_response_committed(200);
        let message_id = format!("chatcmpl-{}", Uuid::new_v4());
        let first = Bytes::from(format!(
            "data: {}\n\n",
            json!({"id":message_id,"object":"chat.completion.chunk","model":"z-ai/glm-5.2","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]})
        ));
        let upstream: UpstreamByteStream = Box::pin(stream::iter(vec![
            Ok::<Bytes, reqwest::Error>(first),
            Ok::<Bytes, reqwest::Error>(Bytes::from_static(b"data: [DONE]\n\n")),
        ]));
        let (upstream, validator, prefix) = prime_stream(upstream)
            .await
            .expect("prime valid test stream");
        let frames = chat_response_stream(
            upstream,
            validator,
            prefix,
            state.clone(),
            stream_id,
            stream_key.id,
            stream_guard,
        )
        .collect::<Vec<_>>()
        .await;
        let frames = frames
            .into_iter()
            .map(|frame| frame.expect("read downstream frame"))
            .collect::<Vec<_>>();
        let emitted_bytes = frames.iter().map(Bytes::len).sum::<usize>();
        let final_frame = std::str::from_utf8(frames.last().expect("terminal downstream frame"))
            .expect("terminal frame is UTF-8");
        assert!(
            final_frame.starts_with("event: error\n"),
            "fallback failure must emit an error frame, not a success terminator"
        );
        assert_eq!(
            wait_for_terminal_pair(&pool, stream_id).await,
            (
                "failed".into(),
                "failed".into(),
                Some("evidence_unavailable".into()),
                Some("evidence_unavailable".into()),
            )
        );
        let timings = sqlx::query_as::<_, (Option<i64>, Option<i64>)>(
            "SELECT request.ttfb_ms,attempt.bytes_out FROM nblb.proxy_requests AS request JOIN nblb.request_attempts AS attempt ON attempt.proxy_request_id=request.id WHERE request.request_id=$1",
        )
        .bind(stream_id)
        .fetch_one(&pool)
        .await
        .expect("load recovered stream byte accounting");
        assert_eq!(timings.0, Some(40));
        assert_eq!(
            timings.1,
            Some(i64::try_from(emitted_bytes).expect("emitted byte count"))
        );

        let precommit_id = Uuid::new_v4();
        let precommit_guard = start_test_stream(&state, precommit_id, key_id).await;
        drop(precommit_guard);
        assert_eq!(
            wait_for_terminal_pair(&pool, precommit_id).await,
            (
                "failed".into(),
                "failed".into(),
                Some("handler_abandoned".into()),
                Some("handler_abandoned".into()),
            )
        );

        sqlx::query("ALTER SEQUENCE nblb.test_terminal_once RESTART WITH 1")
            .execute(&pool)
            .await
            .expect("reset one-shot Drop recovery failpoint");
        let committed_id = Uuid::new_v4();
        let mut committed_guard = start_test_stream(&state, committed_id, key_id).await;
        committed_guard.mark_response_started(30);
        committed_guard.mark_response_committed(200);
        committed_guard.record_bytes_out(32);
        drop(committed_guard);
        assert_eq!(
            wait_for_terminal_pair(&pool, committed_id).await,
            (
                "cancelled".into(),
                "cancelled".into(),
                Some("downstream_cancelled".into()),
                Some("downstream_cancelled".into()),
            )
        );
        assert_eq!(
            sqlx::query_as::<_, (Option<i64>, Option<i64>)>(
                "SELECT request.ttfb_ms,attempt.bytes_out FROM nblb.proxy_requests AS request JOIN nblb.request_attempts AS attempt ON attempt.proxy_request_id=request.id WHERE request.request_id=$1",
            )
            .bind(committed_id)
            .fetch_one(&pool)
            .await
            .expect("load Drop-retried stream accounting"),
            (Some(30), Some(32)),
            "Drop retry must preserve the captured TTFB and byte count",
        );
    }
}
