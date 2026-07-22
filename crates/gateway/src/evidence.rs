//! Privacy-safe request evidence and bounded operations maintenance.

use actix_web::{HttpResponse, http::StatusCode, web};
use anyhow::{Context, Result, anyhow, bail};
use std::time::{Duration, Instant};
use uuid::Uuid;

use super::{
    AppState, AttemptTerminal, PermitError, RequestTerminal, VaultStore, attempt_started,
    openai_error, select_initial_key,
};

const ROLLUP_BATCH_SIZE: i64 = 500;
const OPERATIONS_MAINTENANCE_LOCK_KEY: i64 = 0x4e42_4c42_4f50_534d;
const AUDIT_AND_QA_RETENTION_DAYS: i32 = 180;
const PERMIT_RELEASE_RETRY_INITIAL: Duration = Duration::from_millis(25);
const PERMIT_RELEASE_RETRY_MAX: Duration = Duration::from_secs(5);

struct PermitReleaseGuard {
    state: web::Data<AppState>,
    request_id: Uuid,
    armed: bool,
}

impl PermitReleaseGuard {
    fn new(state: web::Data<AppState>, request_id: Uuid, armed: bool) -> Self {
        Self {
            state,
            request_id,
            armed,
        }
    }

    fn transfer(&mut self) -> bool {
        let armed = self.armed;
        self.armed = false;
        armed
    }
}

impl Drop for PermitReleaseGuard {
    fn drop(&mut self) {
        if self.armed {
            spawn_permit_release_retry(self.state.clone(), self.request_id);
        }
    }
}

fn spawn_permit_release_retry(state: web::Data<AppState>, request_id: Uuid) {
    tokio::spawn(async move {
        let mut delay = PERMIT_RELEASE_RETRY_INITIAL;
        let mut failures = 0_u64;
        loop {
            match state.vault.release_downstream_permit(request_id).await {
                Ok(()) => {
                    if failures > 0 {
                        eprintln!(
                            "request permit release recovered after {failures} failed attempt(s)"
                        );
                    }
                    return;
                }
                Err(error) => {
                    failures = failures.saturating_add(1);
                    if failures == 1 {
                        eprintln!("request permit release failed; retrying: {error:#}");
                    }
                    tokio::time::sleep(delay).await;
                    delay = delay.saturating_mul(2).min(PERMIT_RELEASE_RETRY_MAX);
                }
            }
        }
    });
}

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
    permit_acquired: bool,
    drop_recovery: DropRecovery,
}

impl RequestEvidence {
    pub(crate) async fn start_selected(
        state: web::Data<AppState>,
        request_id: Uuid,
        downstream_credential_id: Option<Uuid>,
        endpoint: &str,
        profile_id: &str,
        stream: bool,
        modality: &str,
    ) -> Result<(Self, Uuid), HttpResponse> {
        if state.vault.database.is_none() {
            let mut evidence = Self::start(
                state.clone(),
                request_id,
                downstream_credential_id,
                endpoint,
                profile_id,
                stream,
                modality,
            )
            .await?;
            let Some(key_id) = select_initial_key(&state, profile_id).await else {
                evidence
                    .fail(StatusCode::SERVICE_UNAVAILABLE, "no_eligible_upstream")
                    .await?;
                return Err(openai_error(
                    StatusCode::SERVICE_UNAVAILABLE,
                    "No verified NVIDIA upstream is currently available.",
                    "service_unavailable_error",
                    "no_eligible_upstream",
                ));
            };
            evidence.arm_evidence_failure(None, 0);
            attempt_started(&state, request_id, profile_id, key_id).await?;
            evidence.reset_drop_recovery();
            return Ok((evidence, key_id));
        }

        let permit_acquired = state
            .vault
            .acquire_downstream_permit(request_id, downstream_credential_id, profile_id)
            .await
            .map_err(permit_error_response)?;
        let mut permit_guard = PermitReleaseGuard::new(state.clone(), request_id, permit_acquired);
        let selected = state
            .vault
            .select_and_start_request(
                request_id,
                downstream_credential_id,
                endpoint,
                profile_id,
                stream,
                modality,
            )
            .await;
        match selected {
            Ok(Some(key_id)) => Ok((
                Self {
                    state,
                    request_id,
                    started: Instant::now(),
                    terminal: false,
                    permit_acquired: permit_guard.transfer(),
                    drop_recovery: DropRecovery::default(),
                },
                key_id,
            )),
            Ok(None) => Err(openai_error(
                StatusCode::SERVICE_UNAVAILABLE,
                "No verified NVIDIA upstream is currently available.",
                "service_unavailable_error",
                "no_eligible_upstream",
            )),
            Err(_) => Err(evidence_unavailable()),
        }
    }

    pub(crate) async fn start(
        state: web::Data<AppState>,
        request_id: Uuid,
        downstream_credential_id: Option<Uuid>,
        endpoint: &str,
        profile_id: &str,
        stream: bool,
        modality: &str,
    ) -> Result<Self, HttpResponse> {
        let permit_acquired = state
            .vault
            .acquire_downstream_permit(request_id, downstream_credential_id, profile_id)
            .await
            .map_err(permit_error_response)?;
        let mut permit_guard = PermitReleaseGuard::new(state.clone(), request_id, permit_acquired);
        if let Err(error) = state
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
        {
            let _ = error;
            return Err(evidence_unavailable());
        }
        Ok(Self {
            state,
            request_id,
            started: Instant::now(),
            terminal: false,
            permit_acquired: permit_guard.transfer(),
            drop_recovery: DropRecovery::default(),
        })
    }

    pub(crate) fn elapsed_ms(&self) -> i64 {
        i64::try_from(self.started.elapsed().as_millis()).unwrap_or(i64::MAX)
    }

    pub(crate) fn arm_evidence_failure(&mut self, ttfb_ms: Option<i64>, bytes_out: usize) {
        self.arm_drop_recovery(
            "failed",
            Some(StatusCode::INTERNAL_SERVER_ERROR.as_u16()),
            "evidence_unavailable",
            ttfb_ms,
            bytes_out,
        );
    }

    pub(crate) fn reset_drop_recovery(&mut self) {
        self.drop_recovery = DropRecovery::default();
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
        self.arm_evidence_failure(request.ttfb_ms(), terminal_bytes_out(attempt));
        self.state
            .vault
            .request_and_attempt_finished(self.request_id, key_id, attempt, request)
            .await
            .map_err(|_| evidence_unavailable())?;
        self.terminal = true;
        self.release_permit_now().await;
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
        self.release_permit_now().await;
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
        self.release_permit_now().await;
        true
    }

    async fn finish(
        &mut self,
        outcome: &'static str,
        status_code: Option<u16>,
        error_class: Option<&'static str>,
        ttfb_ms: Option<i64>,
    ) -> Result<(), HttpResponse> {
        self.arm_evidence_failure(ttfb_ms, 0);
        self.state
            .vault
            .proxy_request_finished(self.request_id, outcome, status_code, error_class, ttfb_ms)
            .await
            .map_err(|_| evidence_unavailable())?;
        self.terminal = true;
        self.release_permit_now().await;
        Ok(())
    }

    async fn release_permit_now(&mut self) {
        if !self.permit_acquired {
            return;
        }
        match self
            .state
            .vault
            .release_downstream_permit(self.request_id)
            .await
        {
            Ok(()) => self.permit_acquired = false,
            Err(error) => {
                eprintln!("request permit release failed; handing off to retry: {error:#}");
                spawn_permit_release_retry(self.state.clone(), self.request_id);
                self.permit_acquired = false;
            }
        }
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
        if self.terminal && !self.permit_acquired {
            return;
        }
        let state = self.state.clone();
        let request_id = self.request_id;
        let recovery = self.drop_recovery;
        let terminal = self.terminal;
        let permit_acquired = self.permit_acquired;
        tokio::spawn(async move {
            if !terminal
                && let Err(error) = state
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
            if permit_acquired {
                spawn_permit_release_retry(state, request_id);
            }
        });
    }
}

fn permit_error_response(error: PermitError) -> HttpResponse {
    match error {
        PermitError::Expired => openai_error(
            StatusCode::UNAUTHORIZED,
            "The downstream credential is expired or revoked.",
            "authentication_error",
            "invalid_downstream_token",
        ),
        PermitError::ModelForbidden => openai_error(
            StatusCode::FORBIDDEN,
            "The requested model is not allowed for this credential.",
            "permission_error",
            "model_not_allowed",
        ),
        PermitError::RateLimited(retry_after) => {
            let mut response = openai_error(
                StatusCode::TOO_MANY_REQUESTS,
                "The per-minute request limit has been reached.",
                "rate_limit_error",
                "rpm_limit_exceeded",
            );
            if let Ok(value) =
                actix_web::http::header::HeaderValue::from_str(&retry_after.to_string())
            {
                response
                    .headers_mut()
                    .insert(actix_web::http::header::RETRY_AFTER, value);
            }
            response
        }
        PermitError::DailyLimit => openai_error(
            StatusCode::TOO_MANY_REQUESTS,
            "The daily request limit has been reached.",
            "rate_limit_error",
            "daily_limit_exceeded",
        ),
        PermitError::ConcurrencyLimit => openai_error(
            StatusCode::TOO_MANY_REQUESTS,
            "The concurrent request limit has been reached.",
            "rate_limit_error",
            "concurrency_limit_exceeded",
        ),
        PermitError::StoreUnavailable => evidence_unavailable(),
    }
}

impl VaultStore {
    async fn select_and_start_request(
        &self,
        request_id: Uuid,
        downstream_credential_id: Option<Uuid>,
        endpoint: &str,
        profile_id: &str,
        stream: bool,
        modality: &str,
    ) -> Result<Option<Uuid>> {
        let pool = self
            .database
            .as_ref()
            .context("selection transaction requires PostgreSQL")?;
        let owner_id = self
            .owner_id
            .context("selection transaction owner lease is missing")?;
        let mut tx = pool.begin().await.context("begin first selection")?;
        let next_slot = sqlx::query_scalar::<_, i16>(
            "SELECT next_slot FROM nblb.routing_state WHERE profile_id=$1 FOR UPDATE",
        )
        .bind(profile_id)
        .fetch_optional(&mut *tx)
        .await
        .context("lock routing cursor")?
        .context("routing profile is missing")?;
        let candidates = sqlx::query_as::<_, (Uuid, i16)>(
            "SELECT key.id,key.slot_no FROM nblb.upstream_keys AS key JOIN nblb.profile_probe_receipts AS proof ON proof.key_id=key.id AND proof.profile_id=$1 AND proof.invalidated_at IS NULL WHERE key.retired=false AND key.enabled=true AND key.verified=true AND (key.cooldown_until IS NULL OR key.cooldown_until<=now()) AND proof.verified_at>=now()-make_interval(secs=>(SELECT proof_freshness_seconds FROM nblb.operations_settings WHERE singleton=true)) AND EXISTS(SELECT 1 FROM nblb.gateway_instances WHERE id=$2 AND last_seen_at>=now()-interval '30 seconds') ORDER BY key.slot_no",
        )
        .bind(profile_id)
        .bind(owner_id)
        .fetch_all(&mut *tx)
        .await
        .context("load hard-eligible upstream slots")?;
        let selected = candidates
            .iter()
            .find(|(_, slot)| *slot == next_slot)
            .or_else(|| candidates.first())
            .copied();

        let Some((key_id, slot_no)) = selected else {
            sqlx::query(
                "INSERT INTO nblb.proxy_requests(request_id,owner_id,downstream_credential_id,endpoint,profile_id,stream,modality,outcome,status_code,error_class,duration_ms,failover_count,finished_at) VALUES($1,$2,$3,$4,$5,$6,$7,'failed',503,'no_eligible_upstream',0,0,now())",
            )
            .bind(request_id)
            .bind(owner_id)
            .bind(downstream_credential_id)
            .bind(endpoint)
            .bind(profile_id)
            .bind(stream)
            .bind(modality)
            .execute(&mut *tx)
            .await
            .context("record no-eligible request")?;
            tx.commit().await.context("commit no-eligible request")?;
            return Ok(None);
        };

        let following_slot = if slot_no == 1 { 2_i16 } else { 1_i16 };
        sqlx::query(
            "UPDATE nblb.routing_state SET next_slot=$2,generation=generation+1 WHERE profile_id=$1",
        )
        .bind(profile_id)
        .bind(following_slot)
        .execute(&mut *tx)
        .await
        .context("advance routing cursor")?;
        let parent_id = sqlx::query_scalar::<_, Uuid>(
            "INSERT INTO nblb.proxy_requests(request_id,owner_id,downstream_credential_id,endpoint,profile_id,stream,modality) VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id",
        )
        .bind(request_id)
        .bind(owner_id)
        .bind(downstream_credential_id)
        .bind(endpoint)
        .bind(profile_id)
        .bind(stream)
        .bind(modality)
        .fetch_one(&mut *tx)
        .await
        .context("record selected proxy request")?;
        sqlx::query(
            "INSERT INTO nblb.request_attempts(request_id,profile_id,key_id,owner_id,outcome,proxy_request_id,attempt_no) VALUES($1,$2,$3,$4,'started',$5,1)",
        )
        .bind(request_id)
        .bind(profile_id)
        .bind(key_id)
        .bind(owner_id)
        .bind(parent_id)
        .execute(&mut *tx)
        .await
        .context("record first selected attempt")?;
        tx.commit().await.context("commit first selection")?;
        Ok(Some(key_id))
    }

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
            "UPDATE nblb.request_attempts AS attempt SET outcome=$2, status_code=$3, error_class=$4, bytes_out=$5, ttfb_ms=COALESCE(attempt.ttfb_ms,$6), response_started=attempt.response_started OR $6 IS NOT NULL, latency_ms=GREATEST(0, floor(extract(epoch FROM (now() - attempt.created_at)) * 1000)::bigint), finished_at=now() WHERE attempt.request_id=$1 AND attempt.owner_id=$7 AND attempt.finished_at IS NULL",
        )
        .bind(request_id)
        .bind(outcome)
        .bind(status_code.map(i32::from))
        .bind(error_class)
        .bind(i64::try_from(bytes_out).unwrap_or(i64::MAX))
        .bind(ttfb_ms)
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
        let (request_days, metric_days) = sqlx::query_as::<_, (i32, i32)>(
            "SELECT request_retention_days,metric_retention_days FROM nblb.operations_settings WHERE singleton=true",
        )
        .fetch_one(&mut *tx)
        .await
        .context("load operations retention settings")?;
        let mut deleted = 0_u64;
        deleted = deleted.saturating_add(
            sqlx::query(
                "DELETE FROM nblb.proxy_requests WHERE finished_at IS NOT NULL AND ((rolled_up_at IS NOT NULL AND started_at < now() - make_interval(days=>$1)) OR (rolled_up_at IS NULL AND started_at < now() - make_interval(days=>$2)))",
            )
            .bind(request_days)
            .bind(metric_days)
            .execute(&mut *tx)
            .await
            .context("expire terminal proxy requests at their evidence horizon")?
            .rows_affected(),
        );
        for (statement, days) in [
            (
                "DELETE FROM nblb.downstream_request_permits WHERE released_at IS NOT NULL AND acquired_at < now() - make_interval(days=>$1)",
                request_days,
            ),
            (
                "DELETE FROM nblb.request_attempts WHERE finished_at IS NOT NULL AND created_at < now() - make_interval(days=>$1)",
                request_days,
            ),
            (
                "DELETE FROM nblb.metric_buckets_minute WHERE bucket_start < now() - make_interval(days=>$1)",
                metric_days,
            ),
            (
                "DELETE FROM nblb.capacity_buckets_minute WHERE bucket_start < now() - make_interval(days=>$1)",
                metric_days,
            ),
            (
                "DELETE FROM nblb.probe_runs WHERE finished_at IS NOT NULL AND created_at < now() - make_interval(days=>$1)",
                metric_days,
            ),
            (
                "DELETE FROM nblb.audit_events WHERE created_at < now() - make_interval(days=>$1)",
                AUDIT_AND_QA_RETENTION_DAYS,
            ),
            (
                "DELETE FROM nblb.qa_runs WHERE finished_at IS NOT NULL AND created_at < now() - make_interval(days=>$1)",
                AUDIT_AND_QA_RETENTION_DAYS,
            ),
        ] {
            deleted = deleted.saturating_add(
                sqlx::query(statement)
                    .bind(days)
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

async fn sample_public_capacity(pool: &sqlx::PgPool) -> Result<u8> {
    let ids = crate::operations::repository::eligible_key_ids(pool, "z-ai/glm-5.2").await?;
    let eligible = u8::try_from(ids.len().min(2)).unwrap_or_default();
    sqlx::query(
        "INSERT INTO nblb.capacity_buckets_minute(bucket_start,eligible_provider_count) VALUES(date_trunc('minute',now()),$1) ON CONFLICT(bucket_start) DO UPDATE SET eligible_provider_count=EXCLUDED.eligible_provider_count",
    )
    .bind(i16::from(eligible))
    .execute(pool)
    .await
    .context("persist public hard-eligible capacity sample")?;
    Ok(eligible)
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
            if let Some(pool) = &rollup_state.vault.database
                && let Err(error) = sample_public_capacity(pool).await
            {
                eprintln!("capacity metric sample failed: {error:#}");
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
        AppState, AttemptTerminal, PermitError, RequestEvidence, RequestTerminal, VaultStore,
        modality_for_path, sample_public_capacity,
    };
    use crate::proxy::start_evidenced_attempt;
    use crate::streaming::{
        StreamAttemptGuard, StreamTerminalResult, UpstreamByteStream, chat_response_stream,
        prime_stream,
    };
    use crate::try_acquire_vault_owner_lock;
    use actix_web::web;
    use bytes::Bytes;
    use futures_util::{StreamExt, stream};
    use nvidia_build_lb_core::Vault;
    use serde_json::json;
    use sqlx::postgres::PgPoolOptions;
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
            _owner_guard: None,
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
        tokio::time::timeout(std::time::Duration::from_secs(30), async {
            loop {
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
        })
        .await
        .expect("request terminal pair did not close within the bounded recovery window")
    }

    async fn wait_for_request_terminal(
        pool: &sqlx::PgPool,
        request_id: Uuid,
    ) -> (String, Option<String>, Option<i16>) {
        tokio::time::timeout(std::time::Duration::from_secs(30), async {
            loop {
                let state = sqlx::query_as::<_, (String, Option<String>, Option<i16>)>(
                    "SELECT outcome,error_class,status_code FROM nblb.proxy_requests WHERE request_id=$1",
                )
                .bind(request_id)
                .fetch_one(pool)
                .await
                .expect("load request terminal state");
                if state.0 != "started" {
                    return state;
                }
                tokio::time::sleep(std::time::Duration::from_millis(10)).await;
            }
        })
        .await
        .expect("request terminal row did not close within the bounded recovery window")
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
            "INSERT INTO nblb.upstream_keys(id,label,fingerprint,ciphertext,nonce,enabled,verified,slot_no) VALUES ($1,'test-key',decode(repeat('11',32),'hex'),decode(repeat('22',24),'hex'),decode(repeat('33',12),'hex'),true,true,1)",
        )
        .bind(key_id)
        .execute(&pool)
        .await
        .expect("seed upstream");
        sqlx::query(
            "INSERT INTO nblb.downstream_credentials(id,label,digest,key_prefix,scopes) VALUES ($1,'test-client',decode(repeat('44',32),'hex'),'nblb_test_client',ARRAY['chat:write'])",
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
    async fn public_capacity_sample_requires_fresh_profile_proof(pool: sqlx::PgPool) {
        let key_id = Uuid::new_v4();
        sqlx::query(
            "INSERT INTO nblb.upstream_keys(id,label,fingerprint,ciphertext,nonce,enabled,verified,slot_no) VALUES ($1,'capacity-proof-key',decode(repeat('71',32),'hex'),decode(repeat('72',24),'hex'),decode(repeat('73',12),'hex'),true,true,1)",
        )
        .bind(key_id)
        .execute(&pool)
        .await
        .expect("seed verified capacity key");

        assert_eq!(
            sample_public_capacity(&pool)
                .await
                .expect("sample without proof"),
            0
        );
        sqlx::query(
            "INSERT INTO nblb.profile_probe_receipts(profile_id,key_id,verified_at) VALUES('z-ai/glm-5.2',$1,now()-interval '8 days')",
        )
        .bind(key_id)
        .execute(&pool)
        .await
        .expect("seed stale profile proof");
        assert_eq!(
            sample_public_capacity(&pool)
                .await
                .expect("sample stale proof"),
            0
        );

        sqlx::query(
            "UPDATE nblb.profile_probe_receipts SET verified_at=now() WHERE profile_id='z-ai/glm-5.2' AND key_id=$1",
        )
        .bind(key_id)
        .execute(&pool)
        .await
        .expect("refresh profile proof");
        assert_eq!(
            sample_public_capacity(&pool)
                .await
                .expect("sample fresh proof"),
            1
        );
        assert_eq!(
            sqlx::query_scalar::<_, i16>(
                "SELECT eligible_provider_count FROM nblb.capacity_buckets_minute ORDER BY bucket_start DESC LIMIT 1",
            )
            .fetch_one(&pool)
            .await
            .expect("load persisted capacity sample"),
            1,
        );
    }

    #[sqlx::test(migrations = "../../migrations/sqlx")]
    async fn pr2_restart_and_retention_preserve_terminal_contract(pool: sqlx::PgPool) {
        let dir = tempfile::tempdir().expect("test directory");
        let path = dir.path().join("vault.json");
        let key_id = {
            let mut vault = Vault::open(&path, [31; 32]).expect("open seed vault");
            let key = vault
                .add(
                    "restart-key",
                    concat!("nvapi", "-abcdefghijklmnopqrstuvwxyz123456"),
                )
                .expect("add encrypted key");
            let record = vault
                .key_records()
                .expect("read encrypted record")
                .remove(0);
            sqlx::query(
                "INSERT INTO nblb.upstream_keys(id,label,fingerprint,ciphertext,nonce,enabled,verified,retired,request_count,failure_count,slot_no) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,1)",
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
        let metric_horizon_unrolled_id = Uuid::new_v4();
        sqlx::query(
            "INSERT INTO nblb.proxy_requests(request_id,endpoint,profile_id,stream,modality,outcome,status_code,duration_ms,started_at,finished_at) VALUES ($1,'/v1/chat/completions','z-ai/glm-5.2',false,'text','succeeded',200,10,now()-interval '91 days',now()-interval '91 days')",
        )
        .bind(metric_horizon_unrolled_id)
        .execute(&pool)
        .await
        .expect("seed metric-horizon unrolled request");
        let recent_audit_id = sqlx::query_scalar::<_, Uuid>(
            "INSERT INTO nblb.audit_events(action,resource_kind,outcome,created_at) VALUES('retention.test','test','succeeded',now()-interval '91 days') RETURNING id",
        )
        .fetch_one(&pool)
        .await
        .expect("seed recent audit");
        let expired_audit_id = sqlx::query_scalar::<_, Uuid>(
            "INSERT INTO nblb.audit_events(action,resource_kind,outcome,created_at) VALUES('retention.test','test','succeeded',now()-interval '181 days') RETURNING id",
        )
        .fetch_one(&pool)
        .await
        .expect("seed expired audit");
        let recent_qa_id = sqlx::query_scalar::<_, Uuid>(
            "INSERT INTO nblb.qa_runs(suite,live,status,created_at,started_at,finished_at) VALUES('smoke',false,'failed',now()-interval '91 days',now()-interval '91 days',now()-interval '91 days') RETURNING id",
        )
        .fetch_one(&pool)
        .await
        .expect("seed recent terminal QA run");
        let expired_qa_id = sqlx::query_scalar::<_, Uuid>(
            "INSERT INTO nblb.qa_runs(suite,live,status,created_at,started_at,finished_at) VALUES('smoke',false,'failed',now()-interval '181 days',now()-interval '181 days',now()-interval '181 days') RETURNING id",
        )
        .fetch_one(&pool)
        .await
        .expect("seed expired terminal QA run");
        let recent_probe_id = sqlx::query_scalar::<_, Uuid>(
            "INSERT INTO nblb.probe_runs(kind,status,created_at,started_at,finished_at) VALUES('catalog','failed',now()-interval '89 days',now()-interval '89 days',now()-interval '89 days') RETURNING id",
        )
        .fetch_one(&pool)
        .await
        .expect("seed recent completed probe");
        let expired_probe_id = sqlx::query_scalar::<_, Uuid>(
            "INSERT INTO nblb.probe_runs(kind,status,created_at,started_at,finished_at) VALUES('catalog','failed',now()-interval '91 days',now()-interval '91 days',now()-interval '91 days') RETURNING id",
        )
        .fetch_one(&pool)
        .await
        .expect("seed expired completed probe");
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
        assert_eq!(
            sqlx::query_scalar::<_, i64>(
                "SELECT count(*) FROM nblb.proxy_requests WHERE request_id=$1"
            )
            .bind(metric_horizon_unrolled_id)
            .fetch_one(&pool)
            .await
            .expect("count metric-horizon request"),
            0
        );
        for (table, recent_id, expired_id) in [
            ("audit_events", recent_audit_id, expired_audit_id),
            ("qa_runs", recent_qa_id, expired_qa_id),
            ("probe_runs", recent_probe_id, expired_probe_id),
        ] {
            let counts = sqlx::query_as::<_, (i64, i64)>(&format!(
                "SELECT count(*) FILTER (WHERE id=$1),count(*) FILTER (WHERE id=$2) FROM nblb.{table}"
            ))
            .bind(recent_id)
            .bind(expired_id)
            .fetch_one(&pool)
            .await
            .expect("count retention boundary rows");
            assert_eq!(counts, (1, 0), "unexpected {table} retention boundary");
        }
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
            "INSERT INTO nblb.upstream_keys(id,label,fingerprint,ciphertext,nonce,enabled,verified,slot_no) VALUES ($1,'atomic-key',decode(repeat('51',32),'hex'),decode(repeat('52',24),'hex'),decode(repeat('53',12),'hex'),true,true,1)",
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
            _owner_guard: None,
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
        sqlx::query("CREATE TABLE nblb.test_attempt_start_failpoints(request_id uuid PRIMARY KEY)")
            .execute(&pool)
            .await
            .expect("create attempt-start failpoint table");
        sqlx::query(
            "CREATE FUNCTION nblb.test_attempt_start_failpoint() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF EXISTS (SELECT 1 FROM nblb.test_attempt_start_failpoints WHERE request_id=NEW.request_id) THEN RAISE EXCEPTION 'forced attempt-start failure'; END IF; RETURN NEW; END $$",
        )
        .execute(&pool)
        .await
        .expect("create attempt-start failpoint function");
        sqlx::query(
            "CREATE TRIGGER test_attempt_start_failpoint BEFORE INSERT ON nblb.request_attempts FOR EACH ROW EXECUTE FUNCTION nblb.test_attempt_start_failpoint()",
        )
        .execute(&pool)
        .await
        .expect("create attempt-start failpoint trigger");

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

        sqlx::query("ALTER SEQUENCE nblb.test_terminal_once RESTART WITH 2")
            .execute(&pool)
            .await
            .expect("disable terminal-update failpoint during attempt-start failure");
        let attempt_start_id = Uuid::new_v4();
        let mut attempt_start_evidence = match RequestEvidence::start(
            state.clone(),
            attempt_start_id,
            None,
            "/v1/chat/completions",
            "z-ai/glm-5.2",
            false,
            "text",
        )
        .await
        {
            Ok(evidence) => evidence,
            Err(_) => panic!("start attempt-start failure evidence"),
        };
        sqlx::query("INSERT INTO nblb.test_attempt_start_failpoints(request_id) VALUES ($1)")
            .bind(attempt_start_id)
            .execute(&pool)
            .await
            .expect("enable attempt-start failpoint");
        let response = start_evidenced_attempt(
            &state,
            &mut attempt_start_evidence,
            attempt_start_id,
            "z-ai/glm-5.2",
            key_id,
        )
        .await
        .expect_err("attempt-start INSERT must fail closed");
        assert_eq!(
            response.status(),
            actix_web::http::StatusCode::INTERNAL_SERVER_ERROR
        );
        drop(attempt_start_evidence);
        assert_eq!(
            wait_for_request_terminal(&pool, attempt_start_id).await,
            (
                "failed".into(),
                Some("evidence_unavailable".into()),
                Some(500)
            ),
            "attempt-start failure must not become handler abandonment"
        );
        assert_eq!(
            sqlx::query_scalar::<_, i64>(
                "SELECT count(*) FROM nblb.request_attempts WHERE request_id=$1"
            )
            .bind(attempt_start_id)
            .fetch_one(&pool)
            .await
            .expect("count failed attempt-start rows"),
            0,
            "failed attempt INSERT must not leave a child row"
        );

        sqlx::query("ALTER SEQUENCE nblb.test_terminal_once RESTART WITH 1")
            .execute(&pool)
            .await
            .expect("reset one-shot stream auxiliary failpoint");
        let stream_auxiliary_id = Uuid::new_v4();
        let mut stream_auxiliary_guard =
            start_test_stream(&state, stream_auxiliary_id, key_id).await;
        stream_auxiliary_guard.arm_evidence_failure(Some(35), 23);
        drop(stream_auxiliary_guard);
        assert_eq!(
            wait_for_terminal_pair(&pool, stream_auxiliary_id).await,
            (
                "failed".into(),
                "failed".into(),
                Some("evidence_unavailable".into()),
                Some("evidence_unavailable".into()),
            ),
            "pre-handoff auxiliary failure must survive a failed first Drop update"
        );
        assert_eq!(
            sqlx::query_as::<_, (Option<i64>, Option<i64>, bool, Option<i64>)>(
                "SELECT request.ttfb_ms,attempt.ttfb_ms,attempt.response_started,attempt.bytes_out FROM nblb.proxy_requests AS request JOIN nblb.request_attempts AS attempt ON attempt.proxy_request_id=request.id WHERE request.request_id=$1",
            )
            .bind(stream_auxiliary_id)
            .fetch_one(&pool)
            .await
            .expect("load stream auxiliary failure accounting"),
            (Some(35), Some(35), true, Some(23)),
        );

        sqlx::query("ALTER SEQUENCE nblb.test_terminal_once RESTART WITH 2")
            .execute(&pool)
            .await
            .expect("disable one-shot non-stream auxiliary failpoint");
        let request_auxiliary_id = Uuid::new_v4();
        let mut request_auxiliary_evidence = match RequestEvidence::start(
            state.clone(),
            request_auxiliary_id,
            None,
            "/v1/chat/completions",
            "z-ai/glm-5.2",
            false,
            "text",
        )
        .await
        {
            Ok(evidence) => evidence,
            Err(_) => panic!("start non-stream auxiliary evidence"),
        };
        state
            .vault
            .attempt_started(request_auxiliary_id, "z-ai/glm-5.2", key_id)
            .await
            .expect("start non-stream auxiliary attempt");
        request_auxiliary_evidence.arm_evidence_failure(None, 41);
        drop(request_auxiliary_evidence);
        assert_eq!(
            wait_for_terminal_pair(&pool, request_auxiliary_id).await,
            (
                "failed".into(),
                "failed".into(),
                Some("evidence_unavailable".into()),
                Some("evidence_unavailable".into()),
            ),
            "post-provider auxiliary failure must not become handler abandonment"
        );
        assert_eq!(
            sqlx::query_scalar::<_, Option<i64>>(
                "SELECT attempt.bytes_out FROM nblb.request_attempts AS attempt WHERE attempt.request_id=$1",
            )
            .bind(request_auxiliary_id)
            .fetch_one(&pool)
            .await
            .expect("load non-stream auxiliary byte accounting"),
            Some(41),
        );

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
            .mutate(|vault| {
                vault.add(
                    "stream-key",
                    concat!("nvapi", "-0123456789abcdefghijklmnopqrstuvwxyz"),
                )
            })
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

    #[sqlx::test(migrations = "../../migrations/sqlx")]
    async fn client_limits_are_atomic_under_concurrent_requests(pool: sqlx::PgPool) {
        let owner_id = Uuid::new_v4();
        sqlx::query("INSERT INTO nblb.gateway_instances(id) VALUES ($1)")
            .bind(owner_id)
            .execute(&pool)
            .await
            .expect("seed permit owner");
        let dir = tempfile::tempdir().expect("test directory");
        let store = VaultStore {
            vault: Mutex::new(
                Vault::open(dir.path().join("vault.json"), [31; 32]).expect("open permit vault"),
            ),
            database: Some(pool.clone()),
            sync_lock: tokio::sync::Mutex::new(()),
            owner_id: Some(owner_id),
            _owner_guard: None,
        };

        for (index, (column, rejected)) in [
            ("rpm_limit", "rpm"),
            ("max_concurrency", "concurrency"),
            ("request_limit_day", "daily"),
        ]
        .into_iter()
        .enumerate()
        {
            let client_id = Uuid::new_v4();
            let query = format!(
                "INSERT INTO nblb.downstream_credentials(id,label,digest,key_prefix,scopes,{column}) VALUES ($1,$2,$3,$4,ARRAY['chat:write'],1)"
            );
            sqlx::query(&query)
                .bind(client_id)
                .bind(format!("{rejected}-client"))
                .bind(vec![u8::try_from(index + 1).expect("small test index"); 32])
                .bind(format!("nblb_test_{index}"))
                .execute(&pool)
                .await
                .expect("seed limited downstream client");
            let first_id = Uuid::new_v4();
            let second_id = Uuid::new_v4();
            let (first, second) = tokio::join!(
                store.acquire_downstream_permit(first_id, Some(client_id), "z-ai/glm-5.2"),
                store.acquire_downstream_permit(second_id, Some(client_id), "z-ai/glm-5.2")
            );
            let successes = [&first, &second]
                .into_iter()
                .filter(|result| matches!(result, Ok(true)))
                .count();
            assert_eq!(successes, 1, "{column} must admit exactly one request");
            let rejected_as_expected = [&first, &second].into_iter().any(|result| {
                matches!(
                    (rejected, result),
                    ("rpm", Err(PermitError::RateLimited(_)))
                        | ("concurrency", Err(PermitError::ConcurrencyLimit))
                        | ("daily", Err(PermitError::DailyLimit))
                )
            });
            assert!(rejected_as_expected, "{column} rejected the wrong way");
        }
    }

    #[sqlx::test(migrations = "../../migrations/sqlx")]
    async fn permit_release_retries_until_live_owner_database_recovers(pool: sqlx::PgPool) {
        let owner_id = Uuid::new_v4();
        sqlx::query("INSERT INTO nblb.gateway_instances(id) VALUES ($1)")
            .bind(owner_id)
            .execute(&pool)
            .await
            .expect("seed active permit owner");
        let client_id = Uuid::new_v4();
        sqlx::query(
            "INSERT INTO nblb.downstream_credentials(id,label,digest,key_prefix,scopes,max_concurrency) VALUES ($1,'permit-retry-client',$2,'nblb_permit_retry',ARRAY['chat:write'],1)",
        )
        .bind(client_id)
        .bind(vec![8_u8; 32])
        .execute(&pool)
        .await
        .expect("seed concurrency-limited client");
        sqlx::query("CREATE TABLE nblb.test_permit_release_failure(enabled boolean NOT NULL)")
            .execute(&pool)
            .await
            .expect("create permit release failpoint state");
        sqlx::query("INSERT INTO nblb.test_permit_release_failure VALUES(true)")
            .execute(&pool)
            .await
            .expect("enable permit release failure");
        sqlx::query(
            "CREATE FUNCTION nblb.test_permit_release_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF (SELECT enabled FROM nblb.test_permit_release_failure LIMIT 1) AND NEW.released_at IS NOT NULL THEN RAISE EXCEPTION 'forced permit release failure'; END IF; RETURN NEW; END $$",
        )
        .execute(&pool)
        .await
        .expect("create permit release failpoint function");
        sqlx::query(
            "CREATE TRIGGER test_permit_release_failure BEFORE UPDATE ON nblb.downstream_request_permits FOR EACH ROW EXECUTE FUNCTION nblb.test_permit_release_failure()",
        )
        .execute(&pool)
        .await
        .expect("create permit release failpoint trigger");

        let directory = tempfile::tempdir().expect("permit retry test directory");
        let state = web::Data::new(AppState {
            vault: VaultStore {
                vault: Mutex::new(
                    Vault::open(directory.path().join("vault.json"), [31; 32])
                        .expect("open permit retry vault"),
                ),
                database: Some(pool.clone()),
                sync_lock: tokio::sync::Mutex::new(()),
                owner_id: Some(owner_id),
                _owner_guard: None,
            },
            router: Mutex::new(Default::default()),
            selection_lock: tokio::sync::Mutex::new(()),
            client: reqwest::Client::new(),
            admin_token: "test-admin".into(),
            upstream_url: "mock://provider".into(),
            require_downstream_token: true,
            public_port: 2456,
            csp_hashes: Vec::new(),
        });
        let request_id = Uuid::new_v4();
        let mut evidence = RequestEvidence::start(
            state.clone(),
            request_id,
            Some(client_id),
            "/v1/chat/completions",
            "z-ai/glm-5.2",
            false,
            "text",
        )
        .await
        .unwrap_or_else(|_| panic!("acquire request evidence and permit"));
        evidence
            .fail(
                actix_web::http::StatusCode::INTERNAL_SERVER_ERROR,
                "fixture_failure",
            )
            .await
            .unwrap_or_else(|_| panic!("terminal request must hand permit to retry worker"));
        assert_eq!(
            sqlx::query_scalar::<_, i64>(
                "SELECT count(*) FROM nblb.downstream_request_permits WHERE request_id=$1 AND released_at IS NULL",
            )
            .bind(request_id)
            .fetch_one(&pool)
            .await
            .expect("count permit while database failure persists"),
            1,
        );

        sqlx::query("UPDATE nblb.test_permit_release_failure SET enabled=false")
            .execute(&pool)
            .await
            .expect("recover permit release database path");
        for _ in 0..100 {
            let active = sqlx::query_scalar::<_, i64>(
                "SELECT count(*) FROM nblb.downstream_request_permits WHERE request_id=$1 AND released_at IS NULL",
            )
            .bind(request_id)
            .fetch_one(&pool)
            .await
            .expect("poll permit release recovery");
            if active == 0 {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
        assert_eq!(
            sqlx::query_scalar::<_, i64>(
                "SELECT count(*) FROM nblb.downstream_request_permits WHERE request_id=$1 AND released_at IS NULL",
            )
            .bind(request_id)
            .fetch_one(&pool)
            .await
            .expect("verify eventual permit release"),
            0,
        );
        assert!(matches!(
            state
                .vault
                .acquire_downstream_permit(Uuid::new_v4(), Some(client_id), "z-ai/glm-5.2",)
                .await,
            Ok(true)
        ));
    }

    #[sqlx::test(migrations = "../../migrations/sqlx")]
    async fn daily_limit_survives_gateway_owner_pruning(pool: sqlx::PgPool) {
        let expired_owner = Uuid::new_v4();
        let current_owner = Uuid::new_v4();
        for owner in [expired_owner, current_owner] {
            sqlx::query("INSERT INTO nblb.gateway_instances(id) VALUES ($1)")
                .bind(owner)
                .execute(&pool)
                .await
                .expect("seed permit owner");
        }
        let client_id = Uuid::new_v4();
        sqlx::query(
            "INSERT INTO nblb.downstream_credentials(id,label,digest,key_prefix,scopes,request_limit_day) VALUES ($1,'restart-daily-client',$2,'nblb_restart_daily',ARRAY['chat:write'],1)",
        )
        .bind(client_id)
        .bind(vec![7_u8; 32])
        .execute(&pool)
        .await
        .expect("seed daily-limited client");
        sqlx::query(
            "INSERT INTO nblb.downstream_request_permits(request_id,downstream_credential_id,owner_id,profile_id,released_at) VALUES ($1,$2,$3,'z-ai/glm-5.2',now())",
        )
        .bind(Uuid::new_v4())
        .bind(client_id)
        .bind(expired_owner)
        .execute(&pool)
        .await
        .expect("seed completed request permit");
        sqlx::query("DELETE FROM nblb.gateway_instances WHERE id=$1")
            .bind(expired_owner)
            .execute(&pool)
            .await
            .expect("prune expired owner");
        assert_eq!(
            sqlx::query_as::<_, (i64, i64)>(
                "SELECT count(*),count(owner_id) FROM nblb.downstream_request_permits WHERE downstream_credential_id=$1",
            )
            .bind(client_id)
            .fetch_one(&pool)
            .await
            .expect("load durable permit"),
            (1, 0),
            "completed usage must survive while its expired owner is detached",
        );

        let dir = tempfile::tempdir().expect("test directory");
        let store = VaultStore {
            vault: Mutex::new(
                Vault::open(dir.path().join("vault.json"), [31; 32])
                    .expect("open restart permit vault"),
            ),
            database: Some(pool),
            sync_lock: tokio::sync::Mutex::new(()),
            owner_id: Some(current_owner),
            _owner_guard: None,
        };
        assert!(matches!(
            store
                .acquire_downstream_permit(Uuid::new_v4(), Some(client_id), "z-ai/glm-5.2",)
                .await,
            Err(PermitError::DailyLimit)
        ));
    }

    #[sqlx::test(migrations = "../../migrations/sqlx")]
    async fn encrypted_vault_allows_only_one_gateway_owner(pool: sqlx::PgPool) {
        let first_dir = tempfile::tempdir().expect("first test directory");
        let second_dir = tempfile::tempdir().expect("second test directory");
        let base_options = pool.connect_options();
        let independent_pool = PgPoolOptions::new()
            .max_connections(1)
            .connect_with(base_options.as_ref().clone().database("postgres"))
            .await
            .expect("connect independent database");
        let mut current_connection = pool
            .acquire()
            .await
            .expect("acquire current database lock connection")
            .detach();
        let mut independent_connection = independent_pool
            .acquire()
            .await
            .expect("acquire independent database lock connection")
            .detach();
        assert!(
            try_acquire_vault_owner_lock(&mut current_connection)
                .await
                .expect("lock current database"),
        );
        assert!(
            try_acquire_vault_owner_lock(&mut independent_connection)
                .await
                .expect("lock independent database"),
            "separate databases in one PostgreSQL cluster must not share vault ownership",
        );
        drop(current_connection);
        drop(independent_connection);
        independent_pool.close().await;

        let first = VaultStore::open(
            first_dir.path().join("vault.json"),
            [41; 32],
            Some(pool.clone()),
        )
        .await
        .expect("first gateway owns vault");
        let second = VaultStore::open(
            second_dir.path().join("vault.json"),
            [41; 32],
            Some(pool.clone()),
        )
        .await;
        assert!(
            second
                .as_ref()
                .is_err_and(|error| error.to_string().contains("another gateway process")),
            "a second process must fail closed instead of serving stale state"
        );
        drop(first);
        VaultStore::open(second_dir.path().join("vault.json"), [41; 32], Some(pool))
            .await
            .expect("vault lock is released with the owning connection");
    }
}
