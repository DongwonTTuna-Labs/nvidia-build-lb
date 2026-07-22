//! Asynchronous, evidence-backed QA run evaluation.

use actix_web::web;
use anyhow::{Context, Result, anyhow, bail};
use base64::Engine;
use chrono::{DateTime, Utc};
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::{PgConnection, PgPool};
use std::{collections::HashSet, time::Duration};
use uuid::Uuid;

use crate::{AppState, PROFILES, VaultAuditMutation};

use super::audio_probe::{
    transcription_probe_matches, transcription_probe_wav, transcription_text_matches,
};

const HERMES_QA_TIMEOUT: Duration = Duration::from_secs(30 * 60);
const PERSISTENCE_QA_TIMEOUT: Duration = Duration::from_secs(15 * 60);
const LOCAL_QA_BASE_URL: &str = "http://127.0.0.1:2456";
const PUBLIC_QA_BASE_URL: &str = "https://nvidia-lb.dongwontuna.net";
const QA_H264_MP4_DATA_URL: &str = "data:video/mp4;base64,AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAAIZnJlZQAABRRtZGF0AAACrgYF//+q3EXpvebZSLeWLNgg2SPu73gyNjQgLSBjb3JlIDE2NCByMzEwOCAzMWUxOWY5IC0gSC4yNjQvTVBFRy00IEFWQyBjb2RlYyAtIENvcHlsZWZ0IDIwMDMtMjAyMyAtIGh0dHA6Ly93d3cudmlkZW9sYW4ub3JnL3gyNjQuaHRtbCAtIG9wdGlvbnM6IGNhYmFjPTEgcmVmPTMgZGVibG9jaz0xOjA6MCBhbmFseXNlPTB4MzoweDExMyBtZT1oZXggc3VibWU9NyBwc3k9MSBwc3lfcmQ9MS4wMDowLjAwIG1peGVkX3JlZj0xIG1lX3JhbmdlPTE2IGNocm9tYV9tZT0xIHRyZWxsaXM9MSA4eDhkY3Q9MSBjcW09MCBkZWFkem9uZT0yMSwxMSBmYXN0X3Bza2lwPTEgY2hyb21hX3FwX29mZnNldD0tMiB0aHJlYWRzPTcgbG9va2FoZWFkX3RocmVhZHM9MSBzbGljZWRfdGhyZWFkcz0wIG5yPTAgZGVjaW1hdGU9MSBpbnRlcmxhY2VkPTAgYmx1cmF5X2NvbXBhdD0wIGNvbnN0cmFpbmVkX2ludHJhPTAgYmZyYW1lcz0zIGJfcHlyYW1pZD0yIGJfYWRhcHQ9MSBiX2JpYXM9MCBkaXJlY3Q9MSB3ZWlnaHRiPTEgb3Blbl9nb3A9MCB3ZWlnaHRwPTIga2V5aW50PTI1MCBrZXlpbnRfbWluPTI1IHNjZW5lY3V0PTQwIGludHJhX3JlZnJlc2g9MCByY19sb29rYWhlYWQ9NDAgcmM9Y3JmIG1idHJlZT0xIGNyZj0yMy4wIHFjb21wPTAuNjAgcXBtaW49MCBxcG1heD02OSBxcHN0ZXA9NCBpcF9yYXRpbz0xLjQwIGFxPTE6MS4wMACAAAAAQWWIhAA3//7hA/gUz3v5jGrdUWWmFcuRtxKXTNnzkBiPXQVxBGAAJZTkDZ2geVuIPgEqAAASgNaNNhqpyTz6+ldRAAAADUGaJGxDf/6nhAAA3oAAAAAKQZ5CeIV/AAC2gQAAAAoBnmF0Qn8AAOmAAAAACgGeY2pCfwAA6YEAAAASQZpoSahBaJlMCGf//p4QAANnAAAADEGehkURLCv/AAC2gQAAAAoBnqV0Qn8AAOmBAAAACgGep2pCfwAA6YAAAAASQZqsSahBbJlMCF///oywAANqAAAADEGeykUVLCv/AAC2gQAAAAoBnul0Qn8AAOmAAAAACgGe62pCfwAA6YAAAAATQZruSahBbJlMFEwn//3xAAAf4QAAAAoBnw1qQn8AAOmBAAAAQkGIw8BD//7mwPmWWiHjgFCHa4ZOtIuUl4pMMreD6cQDLkNDy58AAnqU69xcRCLFcLYCLAAAHRDYgK2UyWbxYSTmcQAAABJBmzNJ4Q6JlMCG//6nhAAA3oAAAAAMQZ9RRRE8K/8AALaAAAAACgGfcHRCfwAA6YEAAAAKAZ9yakJ/AADpgAAAABJBm3dJqEFomUwIZ//+nhAAA2YAAAAMQZ+VRREsK/8AALaBAAAACgGftHRCfwAA6YAAAAAKAZ+2akJ/AADpgQAAABJBm7tJqEFsmUwIX//+jLAAA2sAAAAMQZ/ZRRUsK/8AALaAAAAACgGf+HRCfwAA6YEAAAAKAZ/6akJ/AADpgAAAABNBm/1JqEFsmUwUTCf//fEAAB/hAAAACgGeHGpCfwAA6YEAAASibW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAA+gAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAA810cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAA+gAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAUAAAADwAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAAAPoAAAEAAABAAAAAANFbWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAAA8AAAAPABVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAAC8G1pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAArBzdGJsAAAAwHN0c2QAAAAAAAAAAQAAALBhdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAUAA8ABIAAAASAAAAAAAAAABFUxhdmM2MS4xOS4xMDEgbGlieDI2NAAAAAAAAAAAAAAAGP//AAAANmF2Y0MBZAAN/+EAGWdkAA2s2UFB+wEQAAADABAAAAMDwPFCmWABAAZo6+PLIsD9+PgAAAAAEHBhc3AAAAABAAAAAQAAABRidHJ0AAAAAAAAKGAAAAAAAAAAGHN0dHMAAAAAAAAAAQAAAB4AAAIAAAAAFHN0c3MAAAAAAAAAAQAAAAEAAAEAY3R0cwAAAAAAAAAeAAAAAQAABAAAAAABAAAKAAAAAAEAAAQAAAAAAQAAAAAAAAABAAACAAAAAAEAAAoAAAAAAQAABAAAAAABAAAAAAAAAAEAAAIAAAAAAQAACgAAAAABAAAEAAAAAAEAAAAAAAAAAQAAAgAAAAABAAAGAAAAAAEAAAIAAAAAAQAAAAAAAAABAAAAAAAAAAEAAAAAAAAAAQAAAAAAAAABAAAAAAAAAAEAAAAAAAAAAQAAAAAAAAABAAAAAAAAAAEAAAAAAAAAAQAAAAAAAAABAAAAAAAAAAEAAAAAAAAAAQAAAAAAAAABAAAAAAAAAAEAAAAAAAAAHHN0c2MAAAAAAAAAAQAAAAEAAAAeAAAAAQAAAIxzdHN6AAAAAAAAAAAAAAAeAAAC9wAAABEAAAAOAAAADgAAAA4AAAAWAAAAEAAAAA4AAAAOAAAAFgAAABAAAAAOAAAADgAAABcAAAAOAAAARgAAABYAAAAQAAAADgAAAA4AAAAWAAAAEAAAAA4AAAAOAAAAFgAAABAAAAAOAAAADgAAABcAAAAOAAAAFHN0Y28AAAAAAAAAAQAAADAAAABhdWR0YQAAAFltZXRhAAAAAAAAACFoZGxyAAAAAAAAAABtZGlyYXBwbAAAAAAAAAAAAAAAACxpbHN0AAAAJKl0b28AAAAcZGF0YQAAAAEAAAAATGF2ZjYxLjcuMTAw";
pub(crate) const REQUIRED_SUITES: [&str; 6] = [
    "smoke",
    "distribution",
    "failover",
    "persistence",
    "multimodal",
    "hermes-e2e",
];

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct HermesCompletion {
    status: String,
    app_commit: Option<String>,
    generation: Option<Uuid>,
    client_id: Option<Uuid>,
    #[serde(default)]
    doctor: bool,
    #[serde(default)]
    exact_marker: bool,
    #[serde(default)]
    tool_task: bool,
    #[serde(default)]
    request_correlation: bool,
    secret_scan: Option<SecretScanEvidence>,
    #[serde(default)]
    rollback_rehearsal: bool,
    #[serde(default)]
    reconcile_committed: bool,
    duration_ms: Option<u64>,
    #[serde(default)]
    lb_requests: Vec<HermesRequestEvidence>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct SecretScanEvidence {
    database_matches: u64,
    app_log_matches: u64,
    hermes_log_matches: u64,
    cloudflared_log_matches: u64,
    image_metadata_matches: u64,
    helper_artifact_matches: u64,
    hermes_tree_matches: u64,
    public_bundle_matches: u64,
    admin_response_matches: u64,
    issued_token_matches: u64,
    authorization_value_matches: u64,
}

impl SecretScanEvidence {
    fn total_matches(&self) -> u64 {
        self.database_matches
            .saturating_add(self.app_log_matches)
            .saturating_add(self.hermes_log_matches)
            .saturating_add(self.cloudflared_log_matches)
            .saturating_add(self.image_metadata_matches)
            .saturating_add(self.helper_artifact_matches)
            .saturating_add(self.hermes_tree_matches)
            .saturating_add(self.public_bundle_matches)
            .saturating_add(self.admin_response_matches)
            .saturating_add(self.issued_token_matches)
            .saturating_add(self.authorization_value_matches)
    }
}

pub(crate) async fn database_secret_matches(pool: &PgPool) -> Result<u64> {
    let count = sqlx::query_scalar::<_, i64>(
        "SELECT count(*)::bigint FROM (\
         SELECT to_jsonb(record)::text AS value FROM nblb.audit_events record UNION ALL \
         SELECT to_jsonb(record)::text FROM nblb.downstream_credentials record UNION ALL \
         SELECT to_jsonb(record)::text FROM nblb.downstream_request_permits record UNION ALL \
         SELECT to_jsonb(record)::text FROM nblb.gateway_instances record UNION ALL \
         SELECT to_jsonb(record)::text FROM nblb.incident_updates record UNION ALL \
         SELECT to_jsonb(record)::text FROM nblb.incidents record UNION ALL \
         SELECT to_jsonb(record)::text FROM nblb.metric_buckets_minute record UNION ALL \
         SELECT to_jsonb(record)::text FROM nblb.model_catalog record UNION ALL \
         SELECT to_jsonb(record)::text FROM nblb.operations_settings record UNION ALL \
         SELECT to_jsonb(record)::text FROM nblb.probe_runs record UNION ALL \
         SELECT to_jsonb(record)::text FROM nblb.profile_probe_receipts record UNION ALL \
         SELECT to_jsonb(record)::text FROM nblb.proxy_requests record UNION ALL \
         SELECT to_jsonb(record)::text FROM nblb.qa_cases record UNION ALL \
         SELECT to_jsonb(record)::text FROM nblb.qa_failure_fixtures record UNION ALL \
         SELECT to_jsonb(record)::text FROM nblb.qa_runs record UNION ALL \
         SELECT to_jsonb(record)::text FROM nblb.request_attempts record UNION ALL \
         SELECT to_jsonb(record)::text FROM nblb.routing_policies record UNION ALL \
         SELECT to_jsonb(record)::text FROM nblb.routing_state record UNION ALL \
         SELECT to_jsonb(record)::text FROM nblb.upstream_keys record\
         ) persisted WHERE strpos(value,'nvapi-')>0 OR strpos(value,'nblb_ds_')>0 OR strpos(value,'nblb_admin_')>0",
    )
    .fetch_one(pool)
    .await
    .context("scan persisted operations data for raw credentials")?;
    u64::try_from(count).context("secret match count is negative")
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct HermesRequestEvidence {
    request_id: Uuid,
    attempt_count: usize,
    outcome: String,
    stage: String,
}

#[derive(Debug, sqlx::FromRow)]
struct CorrelatedRequest {
    request_id: Uuid,
    profile_id: String,
    outcome: String,
    attempt_count: i64,
    started_at: DateTime<Utc>,
    finished_at: Option<DateTime<Utc>>,
}

#[derive(Debug, Default)]
struct TrafficEvidence {
    http_contract: Option<HttpContractEvidence>,
    distribution_generation_before: Option<i64>,
    distribution_generation_after: Option<i64>,
}

#[derive(Debug)]
struct HttpContractEvidence {
    local_liveness_status: u16,
    local_readiness_status: u16,
    public_root_status: Option<u16>,
    public_liveness_status: Option<u16>,
    models_unauthorized_status: u16,
    models_authorized_status: u16,
    models_request_id: Option<Uuid>,
    model_count: usize,
    public_required: bool,
}

#[derive(Debug, sqlx::FromRow)]
struct DistributionRow {
    request_id: Uuid,
    slot_no: i16,
    request_outcome: String,
    attempt_count: i64,
    succeeded_attempts: i64,
}

pub(crate) fn spawn(state: web::Data<AppState>, run_id: Uuid) {
    tokio::spawn(async move {
        if let Err(error) = execute(&state, run_id).await {
            eprintln!("QA run {run_id} failed to execute: {error:#}");
            if let Some(pool) = &state.vault.database {
                let _ = sqlx::query(
                    "UPDATE nblb.qa_cases SET status='failed',evidence=jsonb_build_object('error_code','qa_runner_failed'),started_at=COALESCE(started_at,now()),finished_at=now() WHERE run_id=$1 AND status IN ('pending','running')",
                )
                .bind(run_id)
                .execute(pool)
                .await;
                let _ = sqlx::query(
                    "UPDATE nblb.qa_runs SET status='failed',started_at=COALESCE(started_at,now()),finished_at=now() WHERE id=$1 AND status IN ('queued','running')",
                )
                .bind(run_id)
                .execute(pool)
                .await;
            }
        }
    });
}

#[derive(Debug, serde::Deserialize, serde::Serialize)]
struct PersistenceSnapshot {
    owner_id: Uuid,
    key_count: i64,
    key_hash: String,
    active_client_count: i64,
    client_hash: String,
    receipt_count: i64,
    receipt_key_count: i64,
    receipt_hash: String,
    routing_count: i64,
    routing_generation_count: i64,
    routing_hash: String,
}

pub(crate) async fn recover_interrupted_runs(state: &AppState) -> Result<()> {
    let Some(pool) = &state.vault.database else {
        return Ok(());
    };
    close_interrupted_runs(pool).await?;
    let runs = sqlx::query_scalar::<_, Uuid>(
        "SELECT id FROM nblb.qa_runs WHERE status='running' AND suite='persistence' ORDER BY created_at",
    )
    .fetch_all(pool)
    .await
    .context("load interrupted persistence QA runs")?;
    for run_id in runs {
        if let Err(error) = resume_persistence(pool, state.vault.owner_id, run_id).await {
            eprintln!("persistence QA {run_id} resume failed: {error:#}");
            fail_running_run(pool, run_id, "persistence_resume_failed")
                .await
                .with_context(|| format!("close persistence QA {run_id} after resume failure"))?;
        }
    }
    Ok(())
}

async fn close_interrupted_runs(pool: &PgPool) -> Result<()> {
    let mut tx = pool
        .begin()
        .await
        .context("begin interrupted QA recovery")?;
    sqlx::query(
            "UPDATE nblb.qa_cases SET status='failed',evidence=jsonb_build_object('error_code','gateway_restarted_during_qa'),started_at=COALESCE(started_at,now()),finished_at=now() WHERE run_id IN (SELECT id FROM nblb.qa_runs WHERE status='queued' OR (status='running' AND suite<>'persistence')) AND status IN ('pending','running')",
        )
        .execute(&mut *tx)
        .await
        .context("close interrupted QA cases")?;
    sqlx::query(
            "UPDATE nblb.qa_runs SET status='failed',started_at=COALESCE(started_at,now()),finished_at=now() WHERE status='queued' OR (status='running' AND suite<>'persistence')",
        )
        .execute(&mut *tx)
        .await
        .context("close interrupted QA runs")?;
    tx.commit()
        .await
        .context("commit interrupted QA recovery")?;
    Ok(())
}

async fn execute(state: &AppState, run_id: Uuid) -> Result<()> {
    let pool = state
        .vault
        .database
        .as_ref()
        .context("QA runner requires PostgreSQL")?;
    let (suite, live, provider_identity) = sqlx::query_as::<_, (String, bool, String)>(
        "SELECT suite,live,provider_identity FROM nblb.qa_runs WHERE id=$1 AND status='queued'",
    )
    .bind(run_id)
    .fetch_one(pool)
    .await
    .context("load QA run mode")?;
    if live
        && (provider_identity != "nvidia_hosted"
            || !crate::provider::live_qa_provider_is_canonical(&state.upstream_url))
    {
        bail!("live QA requires canonical NVIDIA hosted provider identity")
    }
    if !live && provider_identity != "fake" {
        bail!("fake QA requires fake provider identity")
    }
    if suite == "persistence" {
        prepare_persistence(pool, state.vault.owner_id, run_id).await?;
        schedule_persistence_timeout(pool.clone(), run_id);
        return Ok(());
    }
    if suite == "hermes-e2e" {
        if !live
            || provider_identity != "nvidia_hosted"
            || !crate::provider::live_qa_provider_is_canonical(&state.upstream_url)
        {
            bail!("Hermes E2E QA requires canonical NVIDIA hosted live mode")
        }
        arm_hermes(pool, run_id).await?;
        schedule_hermes_timeout(pool.clone(), run_id);
        return Ok(());
    }
    start_queued_run(pool, run_id).await?;
    let traffic_suite = matches!(
        suite.as_str(),
        "smoke" | "distribution" | "failover" | "multimodal"
    );
    let mock_provider = state.upstream_url.starts_with("mock://");
    let mut traffic_evidence = None;
    let traffic_error = if traffic_suite && (live || mock_provider) {
        match exercise_traffic(state, run_id, &suite, live).await {
            Ok(evidence) => {
                traffic_evidence = Some(evidence);
                None
            }
            Err(error) => Some(bounded_reason(&error)),
        }
    } else if traffic_suite {
        Some("non_live_traffic_requires_mock_provider".to_owned())
    } else {
        None
    };
    let cases = sqlx::query_as::<_, (Uuid, String)>(
        "SELECT id,name FROM nblb.qa_cases WHERE run_id=$1 ORDER BY id",
    )
    .bind(run_id)
    .fetch_all(pool)
    .await
    .context("load QA cases")?;
    let mut failed = traffic_error.is_some();
    for (case_id, name) in cases {
        sqlx::query("UPDATE nblb.qa_cases SET status='running',started_at=now() WHERE id=$1")
            .bind(case_id)
            .execute(pool)
            .await
            .context("start QA case")?;
        let result = evaluate_case(pool, run_id, &name, traffic_evidence.as_ref()).await;
        let (status, mut evidence) = match result {
            Ok((true, evidence)) => ("passed", evidence),
            Ok((false, evidence)) => {
                failed = true;
                ("failed", evidence)
            }
            Err(error) => {
                failed = true;
                (
                    "failed",
                    json!({"error_code":"qa_case_query_failed","reason":bounded_reason(&error)}),
                )
            }
        };
        if let (Some(reason), Some(object)) = (traffic_error.as_deref(), evidence.as_object_mut()) {
            object.insert("traffic_error".to_owned(), Value::String(reason.to_owned()));
        }
        sqlx::query(
            "UPDATE nblb.qa_cases SET status=$2,evidence=$3,finished_at=now() WHERE id=$1 AND status='running'",
        )
        .bind(case_id)
        .bind(status)
        .bind(evidence)
        .execute(pool)
        .await
        .context("finish QA case")?;
    }
    sqlx::query(
        "UPDATE nblb.qa_runs SET status=$2,finished_at=now() WHERE id=$1 AND status='running'",
    )
    .bind(run_id)
    .bind(if failed { "failed" } else { "passed" })
    .execute(pool)
    .await
    .context("finish QA run")?;
    Ok(())
}

async fn arm_hermes(pool: &PgPool, run_id: Uuid) -> Result<()> {
    let mut tx = pool.begin().await.context("begin Hermes QA arm")?;
    let (suite, live, status) = sqlx::query_as::<_, (String, bool, String)>(
        "SELECT suite,live,status FROM nblb.qa_runs WHERE id=$1 FOR UPDATE",
    )
    .bind(run_id)
    .fetch_one(&mut *tx)
    .await
    .context("lock Hermes QA run for arm")?;
    if suite != "hermes-e2e" || !live {
        bail!("Hermes QA arm requires a live Hermes E2E run")
    }
    if status != "queued" {
        bail!("Hermes QA run is not queued")
    }
    let result = sqlx::query(
        "UPDATE nblb.qa_cases SET status='running',evidence=jsonb_build_object('contract','external_rust_helper','timeout_seconds',$2),started_at=now() WHERE run_id=$1 AND status='pending'",
    )
    .bind(run_id)
    .bind(i64::try_from(HERMES_QA_TIMEOUT.as_secs()).unwrap_or(i64::MAX))
    .execute(&mut *tx)
    .await
    .context("arm Hermes QA cases")?;
    if result.rows_affected() != 6 {
        bail!("Hermes QA run does not contain the complete six-case contract")
    }
    sqlx::query("UPDATE nblb.qa_runs SET status='running',started_at=now() WHERE id=$1")
        .bind(run_id)
        .execute(&mut *tx)
        .await
        .context("publish armed Hermes QA run")?;
    tx.commit().await.context("commit Hermes QA arm")?;
    Ok(())
}

async fn start_queued_run(pool: &PgPool, run_id: Uuid) -> Result<()> {
    let started = sqlx::query(
        "UPDATE nblb.qa_runs SET status='running',started_at=now() WHERE id=$1 AND status='queued'",
    )
    .bind(run_id)
    .execute(pool)
    .await
    .context("start QA run")?;
    if started.rows_affected() != 1 {
        bail!("QA run is not queued")
    }
    Ok(())
}

fn schedule_hermes_timeout(pool: PgPool, run_id: Uuid) {
    tokio::spawn(async move {
        tokio::time::sleep(HERMES_QA_TIMEOUT).await;
        if let Err(error) = fail_running_run(&pool, run_id, "hermes_helper_timeout").await {
            eprintln!("Hermes QA {run_id} timeout close failed: {error:#}");
        }
    });
}

fn schedule_persistence_timeout(pool: PgPool, run_id: Uuid) {
    tokio::spawn(async move {
        tokio::time::sleep(PERSISTENCE_QA_TIMEOUT).await;
        if let Err(error) = fail_running_run(&pool, run_id, "persistence_restart_timeout").await {
            eprintln!("Persistence QA {run_id} timeout close failed: {error:#}");
        }
    });
}

async fn fail_running_run(pool: &PgPool, run_id: Uuid, error_code: &str) -> Result<()> {
    let mut tx = pool.begin().await.context("begin QA failure close")?;
    let status =
        sqlx::query_scalar::<_, String>("SELECT status FROM nblb.qa_runs WHERE id=$1 FOR UPDATE")
            .bind(run_id)
            .fetch_optional(&mut *tx)
            .await
            .context("lock QA run for failure close")?;
    if !matches!(status.as_deref(), Some("queued" | "running")) {
        tx.commit().await.context("commit no-op QA failure close")?;
        return Ok(());
    }
    sqlx::query(
        "UPDATE nblb.qa_cases SET status='failed',evidence=jsonb_build_object('error_code',$2),started_at=COALESCE(started_at,now()),finished_at=now() WHERE run_id=$1 AND status IN ('pending','running')",
    )
    .bind(run_id)
    .bind(error_code)
    .execute(&mut *tx)
    .await
    .context("close QA cases as failed")?;
    sqlx::query(
        "UPDATE nblb.qa_runs SET status='failed',started_at=COALESCE(started_at,now()),finished_at=now() WHERE id=$1 AND status IN ('queued','running')",
    )
    .bind(run_id)
    .execute(&mut *tx)
    .await
    .context("close QA run as failed")?;
    tx.commit().await.context("commit QA failure close")?;
    Ok(())
}

pub(crate) async fn complete_hermes(
    pool: &PgPool,
    run_id: Uuid,
    input: &HermesCompletion,
    request_id: Uuid,
) -> Result<(super::dto::QaRun, Uuid)> {
    let mut tx = pool.begin().await.context("begin Hermes QA completion")?;
    let run = sqlx::query_as::<_, (String, bool, String, String, Option<DateTime<Utc>>, String)>(
        "SELECT suite,live,provider_identity,status,started_at,deployment_commit FROM nblb.qa_runs WHERE id=$1 FOR UPDATE",
    )
    .bind(run_id)
    .fetch_optional(&mut *tx)
    .await
    .context("lock Hermes QA run")?
    .context("Hermes QA run not found")?;
    if run.0 != "hermes-e2e" {
        bail!("QA run is not a Hermes E2E run")
    }
    if !run.1 || run.2 != "nvidia_hosted" {
        bail!("Hermes QA completion requires NVIDIA hosted live evidence")
    }
    if run.3 == "passed" && input.status == "passed" {
        let generation = input
            .generation
            .context("passed Hermes QA requires generation")?;
        let app_commit = input
            .app_commit
            .as_deref()
            .context("passed Hermes QA requires app_commit")?;
        let audit = sqlx::query_scalar::<_, Uuid>(
            "SELECT id FROM nblb.audit_events WHERE action='qa.hermes.complete' AND resource_id=$1 AND outcome='succeeded' AND detail->>'generation'=$2 AND detail->>'app_commit'=$3 ORDER BY created_at DESC LIMIT 1",
        )
        .bind(run_id)
        .bind(generation.to_string())
        .bind(app_commit)
        .fetch_optional(&mut *tx)
        .await
        .context("load idempotent Hermes completion")?;
        if let Some(audit) = audit {
            tx.commit()
                .await
                .context("commit idempotent Hermes completion")?;
            let item = super::admin_repository::qa_run(pool, run_id)
                .await?
                .context("completed Hermes QA run missing")?;
            return Ok((item, audit));
        }
    }
    let recovering_committed =
        run.3 == "failed" && input.status == "passed" && input.reconcile_committed;
    if run.3 != "running" && !recovering_committed {
        bail!("Hermes QA run is not running")
    }
    let started_at = run.4.context("Hermes QA run has no start time")?;

    if input.status == "failed" {
        sqlx::query(
            "UPDATE nblb.qa_cases SET status='failed',evidence=jsonb_build_object('error_code','hermes_helper_failed'),finished_at=now() WHERE run_id=$1 AND status='running'",
        )
        .bind(run_id)
        .execute(&mut *tx)
        .await
        .context("store Hermes helper failure")?;
        sqlx::query(
            "UPDATE nblb.qa_runs SET status='failed',finished_at=now() WHERE id=$1 AND status='running'",
        )
        .bind(run_id)
        .execute(&mut *tx)
        .await
        .context("finish failed Hermes QA run")?;
        let audit = super::admin_repository::insert_audit(
            &mut tx,
            "qa.hermes.complete",
            "qa_run",
            Some(run_id),
            Some(request_id),
            json!({"status":"failed","error_code":"hermes_helper_failed"}),
        )
        .await?;
        tx.commit().await.context("commit failed Hermes QA run")?;
        let item = super::admin_repository::qa_run(pool, run_id)
            .await?
            .context("completed Hermes QA run missing")?;
        return Ok((item, audit));
    }
    if input.status != "passed" {
        bail!("Hermes completion status must be passed or failed")
    }
    let generation = input
        .generation
        .context("passed Hermes QA requires generation")?;
    let client_id = input
        .client_id
        .context("passed Hermes QA requires client_id")?;
    let duration_ms = input
        .duration_ms
        .context("passed Hermes QA requires duration_ms")?;
    if duration_ms == 0 || duration_ms > 3_600_000 {
        bail!("Hermes QA duration is outside the accepted range")
    }
    let app_commit = input
        .app_commit
        .as_deref()
        .context("passed Hermes QA requires app_commit")?;
    if app_commit != run.5 || app_commit != crate::BUILD_COMMIT {
        bail!("Hermes QA app commit does not match the active deployment")
    }
    let client = sqlx::query_as::<_, (String, bool, DateTime<Utc>, Option<DateTime<Utc>>)>(
        "SELECT label,active,created_at,revoked_at FROM nblb.downstream_credentials WHERE id=$1 FOR SHARE",
    )
    .bind(client_id)
    .fetch_optional(&mut *tx)
    .await
    .context("load Hermes downstream client")?
    .context("Hermes downstream client not found")?;
    if client.0 != format!("hermes-cutover-{generation}")
        || !client.1
        || client.2 < started_at
        || client.3.is_some()
    {
        bail!("Hermes downstream client identity is not valid for this run")
    }
    if !input.doctor
        || !input.exact_marker
        || !input.tool_task
        || !input.request_correlation
        || input.secret_scan.is_none()
        || !input.rollback_rehearsal
    {
        bail!("passed Hermes QA requires every verification boolean")
    }
    let secret_scan = input
        .secret_scan
        .as_ref()
        .context("passed Hermes QA requires structured secret scan evidence")?;
    let database_matches = database_secret_matches(pool).await?;
    if secret_scan.database_matches != database_matches || secret_scan.total_matches() != 0 {
        bail!("Hermes secret scan evidence contains or disagrees on raw secret matches")
    }
    if !(3..=5).contains(&input.lb_requests.len()) {
        bail!("passed Hermes QA requires three to five correlated requests")
    }
    let marker_count = input
        .lb_requests
        .iter()
        .filter(|request| request.stage == "marker")
        .count();
    let tool_count = input
        .lb_requests
        .iter()
        .filter(|request| request.stage == "tool")
        .count();
    if marker_count != 1
        || !(2..=4).contains(&tool_count)
        || marker_count + tool_count != input.lb_requests.len()
    {
        bail!("Hermes QA requires exactly one marker request and two to four tool requests")
    }
    let request_ids = input
        .lb_requests
        .iter()
        .map(|item| item.request_id)
        .collect::<Vec<_>>();
    if request_ids.iter().copied().collect::<HashSet<_>>().len() != request_ids.len() {
        bail!("Hermes QA request evidence contains duplicate IDs")
    }
    let correlated = sqlx::query_as::<_, CorrelatedRequest>(
        "SELECT request.request_id,request.profile_id,request.outcome,count(attempt.id)::bigint AS attempt_count,request.started_at,request.finished_at FROM nblb.proxy_requests request LEFT JOIN nblb.request_attempts attempt ON attempt.proxy_request_id=request.id WHERE request.downstream_credential_id=$1 AND request.started_at>=$2 AND request.request_id=ANY($3) GROUP BY request.id,request.request_id,request.profile_id,request.outcome,request.started_at,request.finished_at ORDER BY request.started_at,request.request_id",
    )
    .bind(client_id)
    .bind(started_at)
    .bind(&request_ids)
    .fetch_all(&mut *tx)
    .await
    .context("validate correlated Hermes requests")?;
    if correlated.len() != input.lb_requests.len() {
        bail!("Hermes QA request evidence does not match persisted requests")
    }
    for submitted in &input.lb_requests {
        let persisted = correlated
            .iter()
            .find(|item| item.request_id == submitted.request_id)
            .context("Hermes QA request evidence is missing a persisted request")?;
        if submitted.outcome != "succeeded"
            || persisted.outcome != submitted.outcome
            || usize::try_from(persisted.attempt_count).ok() != Some(submitted.attempt_count)
            || persisted.finished_at.is_none()
        {
            bail!("Hermes QA request evidence disagrees with terminal persistence")
        }
    }
    let marker_request = input
        .lb_requests
        .iter()
        .find(|request| request.stage == "marker")
        .context("Hermes QA marker request is missing")?;
    let marker_started_at = correlated
        .iter()
        .find(|request| request.request_id == marker_request.request_id)
        .map(|request| request.started_at)
        .context("Hermes QA marker request is not persisted")?;
    if input
        .lb_requests
        .iter()
        .filter(|request| request.stage == "tool")
        .filter_map(|request| {
            correlated
                .iter()
                .find(|persisted| persisted.request_id == request.request_id)
        })
        .any(|request| request.started_at < marker_started_at)
    {
        bail!("Hermes QA tool requests precede the marker request")
    }
    let window_start = correlated
        .first()
        .map(|item| item.started_at)
        .context("Hermes QA request window is empty")?;
    let window_end = correlated
        .iter()
        .filter_map(|item| item.finished_at)
        .max()
        .context("Hermes QA request window is not terminal")?;
    let window_count = sqlx::query_scalar::<_, i64>(
        "SELECT count(*) FROM nblb.proxy_requests WHERE downstream_credential_id=$1 AND started_at>=$2 AND started_at<=$3",
    )
    .bind(client_id)
    .bind(window_start)
    .bind(window_end)
    .fetch_one(&mut *tx)
    .await
    .context("validate complete Hermes request window")?;
    if usize::try_from(window_count).ok() != Some(correlated.len()) {
        bail!("Hermes QA request window contains unreported requests")
    }

    for (name, passed, evidence) in [
        ("doctor", input.doctor, json!({"passed":input.doctor})),
        (
            "exact_marker",
            input.exact_marker,
            json!({"passed":input.exact_marker}),
        ),
        (
            "tool_task",
            input.tool_task,
            json!({"passed":input.tool_task}),
        ),
        (
            "request_correlation",
            input.request_correlation,
            json!({
                "passed":input.request_correlation,
                "client_id":client_id.to_string(),
                "request_count":correlated.len(),
                "request_ids":correlated.iter().map(|item| item.request_id.to_string()).collect::<Vec<_>>().join(", "),
                "profiles":correlated.iter().map(|item| item.profile_id.as_str()).collect::<HashSet<_>>().into_iter().collect::<Vec<_>>().join(", "),
                "attempt_counts":correlated.iter().map(|item| format!("{}:{}",item.request_id,item.attempt_count)).collect::<Vec<_>>().join(", ")
                ,"marker_request_count":marker_count
                ,"tool_request_count":tool_count
            }),
        ),
        (
            "secret_scan",
            secret_scan.total_matches() == 0,
            json!({
                "database_matches":secret_scan.database_matches,
                "app_log_matches":secret_scan.app_log_matches,
                "hermes_log_matches":secret_scan.hermes_log_matches,
                "cloudflared_log_matches":secret_scan.cloudflared_log_matches,
                "image_metadata_matches":secret_scan.image_metadata_matches,
                "helper_artifact_matches":secret_scan.helper_artifact_matches,
                "hermes_tree_matches":secret_scan.hermes_tree_matches,
                "public_bundle_matches":secret_scan.public_bundle_matches,
                "admin_response_matches":secret_scan.admin_response_matches,
                "issued_token_matches":secret_scan.issued_token_matches,
                "authorization_value_matches":secret_scan.authorization_value_matches,
                "total_matches":secret_scan.total_matches()
            }),
        ),
        (
            "rollback_rehearsal",
            input.rollback_rehearsal,
            json!({"passed":input.rollback_rehearsal}),
        ),
    ] {
        let updated = sqlx::query(
            "UPDATE nblb.qa_cases SET status=$3,evidence=$4,finished_at=now() WHERE run_id=$1 AND name=$2 AND (($5=false AND status='running') OR ($5=true AND status='failed'))",
        )
        .bind(run_id)
        .bind(name)
        .bind(if passed { "passed" } else { "failed" })
        .bind(evidence)
        .bind(recovering_committed)
        .execute(&mut *tx)
        .await
        .context("finish Hermes QA case")?;
        if updated.rows_affected() != 1 {
            bail!("Hermes QA case is not in the expected reconciliation state")
        }
    }
    let completed = sqlx::query(
        "UPDATE nblb.qa_runs SET status='passed',finished_at=now() WHERE id=$1 AND status=$2",
    )
    .bind(run_id)
    .bind(if recovering_committed {
        "failed"
    } else {
        "running"
    })
    .execute(&mut *tx)
    .await
    .context("finish passed Hermes QA run")?;
    if completed.rows_affected() != 1 {
        bail!("Hermes QA run changed while completing reconciliation")
    }
    let audit = super::admin_repository::insert_audit(
        &mut tx,
        "qa.hermes.complete",
        "qa_run",
        Some(run_id),
        Some(request_id),
        json!({
            "status":"passed",
            "generation":generation,
            "app_commit":app_commit,
            "client_id":client_id,
            "duration_ms":duration_ms,
            "request_count":correlated.len(),
            "rollback_rehearsal":true,
            "reconciled_from_failed":recovering_committed
        }),
    )
    .await?;
    tx.commit().await.context("commit passed Hermes QA run")?;
    let item = super::admin_repository::qa_run(pool, run_id)
        .await?
        .context("completed Hermes QA run missing")?;
    Ok((item, audit))
}

async fn prepare_persistence(pool: &PgPool, owner_id: Option<Uuid>, run_id: Uuid) -> Result<()> {
    let owner_id = owner_id.context("persistence QA requires a gateway owner")?;
    let mut tx = pool.begin().await.context("begin persistence QA arm")?;
    let snapshot = persistence_snapshot(&mut tx, owner_id).await?;
    let status =
        sqlx::query_scalar::<_, String>("SELECT status FROM nblb.qa_runs WHERE id=$1 FOR UPDATE")
            .bind(run_id)
            .fetch_one(&mut *tx)
            .await
            .context("lock persistence QA run for arm")?;
    if status != "queued" {
        bail!("persistence QA run is not queued")
    }
    let case_count =
        sqlx::query_scalar::<_, i64>("SELECT count(*) FROM nblb.qa_cases WHERE run_id=$1")
            .bind(run_id)
            .fetch_one(&mut *tx)
            .await
            .context("count persistence QA cases")?;
    if case_count != 6 {
        bail!("persistence QA run does not contain the complete six-case contract")
    }
    let armed = sqlx::query(
        "UPDATE nblb.qa_cases SET status='running',evidence=$2,started_at=now() WHERE run_id=$1 AND name='restart_observed' AND status='pending'",
    )
    .bind(run_id)
    .bind(serde_json::to_value(snapshot)?)
    .execute(&mut *tx)
    .await
    .context("arm persistence QA restart evidence")?;
    if armed.rows_affected() != 1 {
        bail!("persistence QA restart case could not be armed")
    }
    sqlx::query("UPDATE nblb.qa_runs SET status='running',started_at=now() WHERE id=$1")
        .bind(run_id)
        .execute(&mut *tx)
        .await
        .context("publish armed persistence QA run")?;
    tx.commit().await.context("commit persistence QA arm")?;
    Ok(())
}

async fn resume_persistence(pool: &PgPool, owner_id: Option<Uuid>, run_id: Uuid) -> Result<()> {
    let owner_id = owner_id.context("resumed persistence QA requires a gateway owner")?;
    let mut tx = pool.begin().await.context("begin persistence QA resume")?;
    let run_status =
        sqlx::query_scalar::<_, String>("SELECT status FROM nblb.qa_runs WHERE id=$1 FOR UPDATE")
            .bind(run_id)
            .fetch_one(&mut *tx)
            .await
            .context("lock persistence QA run for resume")?;
    if run_status != "running" {
        bail!("persistence QA run is not running")
    }
    let evidence = sqlx::query_scalar::<_, Value>(
        "SELECT evidence FROM nblb.qa_cases WHERE run_id=$1 AND name='restart_observed' AND status='running'",
    )
    .bind(run_id)
    .fetch_one(&mut *tx)
    .await
    .context("load persistence QA baseline")?;
    let baseline: PersistenceSnapshot =
        serde_json::from_value(evidence).context("decode persistence QA baseline")?;
    if baseline.owner_id == owner_id {
        bail!("persistence QA has not observed a different gateway owner")
    }
    let current = persistence_snapshot(&mut tx, owner_id).await?;
    let owner_changed = baseline.owner_id != current.owner_id;
    sqlx::query(
        "UPDATE nblb.qa_cases SET status=$2,evidence=jsonb_build_object('owner_changed',$3,'before_owner',$4::text,'after_owner',$5::text),finished_at=now() WHERE run_id=$1 AND name='restart_observed' AND status='running'",
    )
    .bind(run_id)
    .bind(if owner_changed { "passed" } else { "failed" })
    .bind(owner_changed)
    .bind(baseline.owner_id)
    .bind(current.owner_id)
    .execute(&mut *tx)
    .await
    .context("finish persistence restart case")?;

    let cases = sqlx::query_as::<_, (Uuid, String)>(
        "SELECT id,name FROM nblb.qa_cases WHERE run_id=$1 AND status='pending' ORDER BY id",
    )
    .bind(run_id)
    .fetch_all(&mut *tx)
    .await
    .context("load persistence QA cases")?;
    let mut failed = !owner_changed;
    for (case_id, name) in cases {
        let (passed, evidence) = persistence_case(&baseline, &current, &name);
        failed |= !passed;
        sqlx::query(
            "UPDATE nblb.qa_cases SET status=$2,evidence=$3,started_at=now(),finished_at=now() WHERE id=$1 AND status='pending'",
        )
        .bind(case_id)
        .bind(if passed { "passed" } else { "failed" })
        .bind(evidence)
        .execute(&mut *tx)
        .await
        .context("finish persistence QA case")?;
    }
    sqlx::query(
        "UPDATE nblb.qa_runs SET status=$2,finished_at=now() WHERE id=$1 AND status='running'",
    )
    .bind(run_id)
    .bind(if failed { "failed" } else { "passed" })
    .execute(&mut *tx)
    .await
    .context("finish persistence QA run")?;
    tx.commit().await.context("commit persistence QA resume")?;
    Ok(())
}

fn persistence_case(
    before: &PersistenceSnapshot,
    after: &PersistenceSnapshot,
    name: &str,
) -> (bool, Value) {
    match name {
        "encrypted_keys" => {
            let passed = before.key_count >= 2
                && before.key_count == after.key_count
                && before.key_hash == after.key_hash;
            (
                passed,
                json!({"before":before.key_count,"after":after.key_count,"ciphertext_hash_preserved":before.key_hash==after.key_hash}),
            )
        }
        "downstream_token" => {
            let passed = before.active_client_count >= 1
                && before.active_client_count == after.active_client_count
                && before.client_hash == after.client_hash;
            (
                passed,
                json!({"before":before.active_client_count,"after":after.active_client_count,"digest_hash_preserved":before.client_hash==after.client_hash}),
            )
        }
        "profile_receipts" => {
            let passed = before.receipt_key_count >= 2
                && before.receipt_count == after.receipt_count
                && before.receipt_hash == after.receipt_hash;
            (
                passed,
                json!({"before":before.receipt_count,"after":after.receipt_count,"key_count":after.receipt_key_count,"receipt_hash_preserved":before.receipt_hash==after.receipt_hash}),
            )
        }
        "routing_cursor" => {
            let passed = before.routing_generation_count >= 1
                && before.routing_count == after.routing_count
                && before.routing_hash == after.routing_hash;
            (
                passed,
                json!({"profiles":after.routing_count,"advanced_profiles":after.routing_generation_count,"routing_hash_preserved":before.routing_hash==after.routing_hash}),
            )
        }
        "owner_lease" => {
            let passed = before.owner_id != after.owner_id;
            (
                passed,
                json!({"owner_changed":passed,"active_owner":after.owner_id}),
            )
        }
        _ => (false, json!({"error_code":"unknown_persistence_case"})),
    }
}

async fn persistence_snapshot(
    connection: &mut PgConnection,
    owner_id: Uuid,
) -> Result<PersistenceSnapshot> {
    let (key_count, key_hash) = sqlx::query_as::<_, (i64, String)>(
        "SELECT count(*),md5(COALESCE(string_agg(encode(ciphertext,'hex')||':'||encode(nonce,'hex'),'|' ORDER BY id),'')) FROM nblb.upstream_keys WHERE retired=false",
    )
    .fetch_one(&mut *connection)
    .await
    .context("snapshot encrypted keys")?;
    let (active_client_count, client_hash) = sqlx::query_as::<_, (i64, String)>(
        "SELECT count(*),md5(COALESCE(string_agg(encode(digest,'hex'),'|' ORDER BY id),'')) FROM nblb.downstream_credentials WHERE active=true",
    )
    .fetch_one(&mut *connection)
    .await
    .context("snapshot downstream clients")?;
    let (receipt_count, receipt_key_count, receipt_hash) =
        sqlx::query_as::<_, (i64, i64, String)>(
            "SELECT count(*),count(DISTINCT key_id),md5(COALESCE(string_agg(profile_id||':'||key_id::text||':'||verified_at::text,'|' ORDER BY profile_id,key_id),'')) FROM nblb.profile_probe_receipts WHERE invalidated_at IS NULL",
        )
        .fetch_one(&mut *connection)
        .await
        .context("snapshot profile receipts")?;
    let (routing_count, routing_generation_count, routing_hash) =
        sqlx::query_as::<_, (i64, i64, String)>(
            "SELECT count(*),count(*) FILTER (WHERE generation>0),md5(COALESCE(string_agg(profile_id||':'||next_slot::text||':'||generation::text,'|' ORDER BY profile_id),'')) FROM nblb.routing_state",
        )
        .fetch_one(&mut *connection)
        .await
        .context("snapshot routing cursors")?;
    Ok(PersistenceSnapshot {
        owner_id,
        key_count,
        key_hash,
        active_client_count,
        client_hash,
        receipt_count,
        receipt_key_count,
        receipt_hash,
        routing_count,
        routing_generation_count,
        routing_hash,
    })
}

async fn exercise_traffic(
    state: &AppState,
    run_id: Uuid,
    suite: &str,
    live: bool,
) -> Result<TrafficEvidence> {
    let label = format!("qa-run-{run_id}");
    let (issued, _) = state
        .vault
        .mutate_with_audit(
            |vault| {
                vault.issue_downstream(
                    &label,
                    &[
                        "models:read",
                        "chat:write",
                        "embeddings:write",
                        "images:write",
                        "media:write",
                        "audio:write",
                    ]
                    .map(str::to_owned),
                )
            },
            |issued| VaultAuditMutation {
                action: "qa.client.create",
                resource_kind: "client",
                resource_id: Some(issued.summary.id),
                request_id: run_id,
                detail: json!({"ephemeral":true}),
                client_policy: None,
                probe_result: None,
            },
        )
        .await
        .context("issue ephemeral QA client")?;
    if suite == "failover" {
        let pool = state
            .vault
            .database
            .as_ref()
            .context("failover QA requires PostgreSQL")?;
        let live = sqlx::query_scalar::<_, bool>("SELECT live FROM nblb.qa_runs WHERE id=$1")
            .bind(run_id)
            .fetch_one(pool)
            .await
            .context("load failover QA mode")?;
        if live && !state.upstream_url.starts_with("mock://") {
            for kind in ["before_first_frame", "after_first_frame"] {
                sqlx::query(
                    "INSERT INTO nblb.qa_failure_fixtures(run_id,kind,downstream_credential_id) VALUES($1,$2,$3)",
                )
                .bind(run_id)
                .bind(kind)
                .bind(issued.summary.id)
                .execute(pool)
                .await
                .context("arm one-shot live failover fixture")?;
            }
        }
    }
    let mut traffic_evidence = TrafficEvidence::default();
    if suite == "smoke" {
        traffic_evidence.http_contract =
            Some(probe_http_contract(&issued.token, live, state.public_port).await?);
    }
    if suite == "distribution" {
        let pool = state
            .vault
            .database
            .as_ref()
            .context("distribution QA requires PostgreSQL")?;
        traffic_evidence.distribution_generation_before =
            Some(prepare_distribution_cursor(pool).await?);
    }
    let traffic = send_suite_requests(suite, &issued.token, state.public_port).await;
    if traffic.is_ok() && suite == "distribution" {
        let pool = state
            .vault
            .database
            .as_ref()
            .context("distribution QA requires PostgreSQL")?;
        traffic_evidence.distribution_generation_after = Some(
            sqlx::query_scalar::<_, i64>(
                "SELECT generation FROM nblb.routing_state WHERE profile_id='z-ai/glm-5.2'",
            )
            .fetch_one(pool)
            .await
            .context("load distribution final generation")?,
        );
    }
    let client_id = issued.summary.id;
    let cleanup = state
        .vault
        .mutate_with_audit(
            |vault| vault.revoke_downstream(client_id),
            |_| VaultAuditMutation {
                action: "qa.client.revoke",
                resource_kind: "client",
                resource_id: Some(client_id),
                request_id: run_id,
                detail: json!({"ephemeral":true}),
                client_policy: None,
                probe_result: None,
            },
        )
        .await
        .context("revoke ephemeral QA client")
        .map(|_| ());
    combine_traffic_cleanup(traffic.map(|()| traffic_evidence), cleanup)
}

fn combine_traffic_cleanup<T>(traffic: Result<T>, cleanup: Result<()>) -> Result<T> {
    match (traffic, cleanup) {
        (Ok(value), Ok(())) => Ok(value),
        (Err(traffic), Ok(())) => Err(traffic.context("QA traffic failed; client cleanup passed")),
        (Ok(_), Err(cleanup)) => Err(cleanup.context("QA traffic passed; client cleanup failed")),
        (Err(traffic), Err(cleanup)) => Err(anyhow!(
            "QA traffic and client cleanup both failed: traffic={}; cleanup={}",
            bounded_reason(&traffic),
            bounded_reason(&cleanup)
        )),
    }
}

async fn prepare_distribution_cursor(pool: &PgPool) -> Result<i64> {
    let mut tx = pool
        .begin()
        .await
        .context("begin distribution cursor preparation")?;
    let generation = sqlx::query_scalar::<_, i64>(
        "SELECT generation FROM nblb.routing_state WHERE profile_id='z-ai/glm-5.2' FOR UPDATE",
    )
    .fetch_one(&mut *tx)
    .await
    .context("lock distribution routing cursor")?;
    sqlx::query("UPDATE nblb.routing_state SET next_slot=1 WHERE profile_id='z-ai/glm-5.2'")
        .execute(&mut *tx)
        .await
        .context("reset distribution routing cursor")?;
    tx.commit()
        .await
        .context("commit distribution cursor preparation")?;
    Ok(generation)
}

fn local_qa_request(
    client: &reqwest::Client,
    method: reqwest::Method,
    url: String,
    public_port: u16,
) -> reqwest::RequestBuilder {
    client
        .request(method, url)
        .header(reqwest::header::HOST, format!("127.0.0.1:{public_port}"))
}

async fn probe_http_contract(
    token: &str,
    live: bool,
    public_port: u16,
) -> Result<HttpContractEvidence> {
    let client = reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(30))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .context("build HTTP contract QA client")?;
    let local_liveness_status = local_qa_request(
        &client,
        reqwest::Method::GET,
        format!("{LOCAL_QA_BASE_URL}/health/live"),
        public_port,
    )
    .send()
    .await
    .context("request local liveness")?
    .status()
    .as_u16();
    let local_readiness_status = local_qa_request(
        &client,
        reqwest::Method::GET,
        format!("{LOCAL_QA_BASE_URL}/health/ready"),
        public_port,
    )
    .send()
    .await
    .context("request local readiness")?
    .status()
    .as_u16();
    let (public_root_status, public_liveness_status) = if live {
        let root = client
            .get(format!("{PUBLIC_QA_BASE_URL}/"))
            .send()
            .await
            .context("request public tunnel root")?
            .status()
            .as_u16();
        let liveness = client
            .get(format!("{PUBLIC_QA_BASE_URL}/health/live"))
            .send()
            .await
            .context("request public tunnel liveness")?
            .status()
            .as_u16();
        (Some(root), Some(liveness))
    } else {
        (None, None)
    };
    let models_unauthorized_status = local_qa_request(
        &client,
        reqwest::Method::GET,
        format!("{LOCAL_QA_BASE_URL}/v1/models"),
        public_port,
    )
    .send()
    .await
    .context("request models without authentication")?
    .status()
    .as_u16();
    let models_response = local_qa_request(
        &client,
        reqwest::Method::GET,
        format!("{LOCAL_QA_BASE_URL}/v1/models"),
        public_port,
    )
    .bearer_auth(token)
    .send()
    .await
    .context("request authenticated models")?;
    let models_authorized_status = models_response.status().as_u16();
    let models_request_id = models_response
        .headers()
        .get("x-request-id")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| Uuid::parse_str(value).ok());
    let model_count = if models_response.status().is_success() {
        models_response
            .json::<Value>()
            .await
            .context("decode authenticated models response")?
            .get("data")
            .and_then(Value::as_array)
            .map(Vec::len)
            .unwrap_or_default()
    } else {
        0
    };
    Ok(HttpContractEvidence {
        local_liveness_status,
        local_readiness_status,
        public_root_status,
        public_liveness_status,
        models_unauthorized_status,
        models_authorized_status,
        models_request_id,
        model_count,
        public_required: live,
    })
}

async fn send_suite_requests(suite: &str, token: &str, public_port: u16) -> Result<()> {
    // The container listener is fixed at 2456. `public_port` is the external
    // Host authority and may be remapped by compose.
    let base = LOCAL_QA_BASE_URL;
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(90))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .context("build QA HTTP client")?;
    match suite {
        "smoke" => {
            send_json(
                &client,
                token,
                &format!("{base}/v1/chat/completions"),
                json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}],"max_tokens":2,"stream":false}),
                public_port,
            )
            .await?;
            send_json(
                &client,
                token,
                &format!("{base}/v1/chat/completions"),
                json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}],"metadata":{"mock_stream_scenario":"fragmented_sse"},"max_tokens":2,"stream":true}),
                public_port,
            )
            .await?;
        }
        "distribution" => {
            for _ in 0..6 {
                send_json(
                    &client,
                    token,
                    &format!("{base}/v1/chat/completions"),
                    json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}],"max_tokens":2,"stream":false}),
                    public_port,
                )
                .await?;
            }
        }
        "failover" => {
            send_json(
                &client,
                token,
                &format!("{base}/v1/chat/completions"),
                json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}],"metadata":{"mock_stream_scenario":"disconnect_before_first_frame"},"max_tokens":2,"stream":true}),
                public_port,
            )
            .await?;
            let bytes = send_json(
                &client,
                token,
                &format!("{base}/v1/chat/completions"),
                json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}],"metadata":{"mock_stream_scenario":"disconnect_after_first_frame"},"max_tokens":2,"stream":true}),
                public_port,
            )
            .await?;
            if !bytes
                .windows(b"event: error".len())
                .any(|window| window == b"event: error")
            {
                bail!("post-first-frame fixture did not emit a terminal SSE error")
            }
        }
        "multimodal" => {
            let png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
            let wav = transcription_probe_wav().context("decode speech QA fixture")?;
            let wav_data = format!(
                "data:audio/wav;base64,{}",
                base64::engine::general_purpose::STANDARD.encode(&wav)
            );
            // One-second, 320x240 H.264/MP4 fixture. Unlike an ftyp-only
            // placeholder this contains a complete moov table and media samples.
            let mp4_data = QA_H264_MP4_DATA_URL;
            for (path, body) in [
                (
                    "/v1/chat/completions",
                    json!({"model":"microsoft/phi-4-multimodal-instruct","messages":[{"role":"user","content":"Reply QA"}],"max_tokens":2}),
                ),
                (
                    "/v1/chat/completions",
                    json!({"model":"microsoft/phi-4-multimodal-instruct","messages":[{"role":"user","content":[{"type":"text","text":"Describe"},{"type":"image_url","image_url":{"url":png}}]}],"max_tokens":2}),
                ),
                (
                    "/v1/nvidia/inference",
                    json!({"model":"nvidia/vila","messages":[{"role":"user","content":"Reply QA"}],"max_tokens":2}),
                ),
                (
                    "/v1/nvidia/inference",
                    json!({"model":"nvidia/vila","messages":[{"role":"user","content":[{"type":"text","text":"Describe"},{"type":"image_url","image_url":{"url":png}}]}],"max_tokens":2}),
                ),
                (
                    "/v1/nvidia/inference",
                    json!({"model":"nvidia/vila","messages":[{"role":"user","content":[{"type":"text","text":"Describe"},{"type":"video_url","video_url":{"url":mp4_data}}]}],"max_tokens":2}),
                ),
                (
                    "/v1/embeddings",
                    json!({"model":"nvidia/nvclip","input":["QA"],"encoding_format":"float"}),
                ),
                (
                    "/v1/embeddings",
                    json!({"model":"nvidia/nvclip","input":[png],"encoding_format":"float"}),
                ),
                (
                    "/v1/images/generations",
                    json!({"model":"black-forest-labs/flux.1-kontext-dev","prompt":"green square","n":1,"size":"1024x1024","response_format":"b64_json"}),
                ),
                (
                    "/v1/images/generations",
                    json!({"model":"black-forest-labs/flux.1-kontext-dev","prompt":"make it greener","image":png,"n":1,"size":"1024x1024","response_format":"b64_json"}),
                ),
                (
                    "/v1/videos/generations",
                    json!({"model":"stabilityai/stable-video-diffusion","input_reference":png}),
                ),
                (
                    "/v1/audio/speech",
                    json!({"model":"nvidia/magpie-tts-multilingual","input":"QA","voice":"Magpie-Multilingual.EN-US.Aria","response_format":"wav"}),
                ),
            ] {
                send_json(&client, token, &format!("{base}{path}"), body, public_port).await?;
            }
            let audio_chat = send_json(
                &client,
                token,
                &format!("{base}/v1/chat/completions"),
                json!({"model":"microsoft/phi-4-multimodal-instruct","messages":[{"role":"user","content":[{"type":"text","text":"Transcribe the audio and reply with the spoken word."},{"type":"audio_url","audio_url":{"url":wav_data}}]}],"max_tokens":8}),
                public_port,
            )
            .await?;
            validate_audio_chat_qa_response(&audio_chat)?;
            let form = reqwest::multipart::Form::new()
                .text("model", "nvidia/parakeet-ctc-1.1b")
                .part(
                    "file",
                    reqwest::multipart::Part::bytes(wav)
                        .file_name("qa.wav")
                        .mime_str("audio/wav")
                        .context("build QA WAV part")?,
                );
            let response = local_qa_request(
                &client,
                reqwest::Method::POST,
                format!("{base}/v1/audio/transcriptions"),
                public_port,
            )
            .bearer_auth(token)
            .multipart(form)
            .send()
            .await
            .context("send transcription QA request")?;
            let body = require_success(response, "transcription").await?;
            validate_transcription_qa_response(&body)?;
        }
        _ => bail!("unsupported traffic QA suite"),
    }
    Ok(())
}

fn validate_transcription_qa_response(body: &[u8]) -> Result<()> {
    if transcription_probe_matches(body) {
        Ok(())
    } else {
        bail!("transcription QA response did not contain the expected spoken token")
    }
}

fn validate_audio_chat_qa_response(body: &[u8]) -> Result<()> {
    let content = serde_json::from_slice::<Value>(body)
        .ok()
        .and_then(|value| {
            value
                .pointer("/choices/0/message/content")?
                .as_str()
                .map(str::to_owned)
        });
    if content.is_some_and(|text| transcription_text_matches(&text)) {
        Ok(())
    } else {
        bail!("audio chat QA response did not contain the expected spoken token")
    }
}

async fn send_json(
    client: &reqwest::Client,
    token: &str,
    url: &str,
    body: Value,
    public_port: u16,
) -> Result<bytes::Bytes> {
    let response = local_qa_request(client, reqwest::Method::POST, url.to_owned(), public_port)
        .bearer_auth(token)
        .json(&body)
        .send()
        .await
        .with_context(|| {
            format!(
                "send QA request to {}",
                url.rsplit('/').next().unwrap_or("endpoint")
            )
        })?;
    require_success(response, "JSON endpoint").await
}

async fn require_success(response: reqwest::Response, endpoint: &str) -> Result<bytes::Bytes> {
    let status = response.status();
    const QA_RESPONSE_LIMIT: usize = 128 * 1024 * 1024;
    if response
        .content_length()
        .is_some_and(|length| length > u64::try_from(QA_RESPONSE_LIMIT).unwrap_or(u64::MAX))
    {
        bail!("{endpoint} QA response exceeded the size limit")
    }
    let mut body = bytes::BytesMut::new();
    let mut stream = response.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.with_context(|| format!("read {endpoint} QA response"))?;
        if body.len().saturating_add(chunk.len()) > QA_RESPONSE_LIMIT {
            bail!("{endpoint} QA response exceeded the size limit")
        }
        body.extend_from_slice(&chunk);
    }
    let bytes = body.freeze();
    if !status.is_success() {
        bail!("{endpoint} QA request returned HTTP {}", status.as_u16())
    }
    if bytes.is_empty() {
        bail!("{endpoint} QA response was empty")
    }
    Ok(bytes)
}

fn bounded_reason(error: &anyhow::Error) -> String {
    let reason = error
        .chain()
        .last()
        .map(ToString::to_string)
        .unwrap_or_else(|| "unknown".to_owned());
    reason.chars().take(160).collect()
}

async fn evaluate_case(
    pool: &PgPool,
    run_id: Uuid,
    name: &str,
    traffic: Option<&TrafficEvidence>,
) -> Result<(bool, Value)> {
    let qa_label = format!("qa-run-{run_id}");
    let started_at = sqlx::query_scalar::<_, chrono::DateTime<chrono::Utc>>(
        "SELECT started_at FROM nblb.qa_runs WHERE id=$1",
    )
    .bind(run_id)
    .fetch_one(pool)
    .await
    .context("load QA start time")?;
    match name {
        "liveness" => {
            let Some(http) = traffic.and_then(|item| item.http_contract.as_ref()) else {
                return Ok((false, json!({"error_code":"http_contract_evidence_missing"})));
            };
            let public_passed = !http.public_required
                || (http.public_root_status == Some(200)
                    && http.public_liveness_status == Some(200));
            Ok((
                http.local_liveness_status == 200 && public_passed,
                json!({
                    "local_liveness_status":http.local_liveness_status,
                    "public_required":http.public_required,
                    "public_root_status":http.public_root_status,
                    "public_liveness_status":http.public_liveness_status
                }),
            ))
        }
        "readiness" | "two_eligible_slots" => {
            let count = eligible_count(pool).await?;
            let required = if name == "two_eligible_slots" { 2 } else { 1 };
            let http_status = traffic
                .and_then(|item| item.http_contract.as_ref())
                .map(|http| http.local_readiness_status);
            Ok((
                count >= required && (name != "readiness" || http_status == Some(200)),
                json!({"eligible_slots":count,"required":required,"local_readiness_status":http_status}),
            ))
        }
        "models" => {
            let Some(http) = traffic.and_then(|item| item.http_contract.as_ref()) else {
                return Ok((false, json!({"error_code":"models_http_evidence_missing"})));
            };
            Ok((
                http.models_unauthorized_status == 401
                    && http.models_authorized_status == 200
                    && http.models_request_id.is_some()
                    && http.model_count == PROFILES.len(),
                json!({
                    "unauthorized_status":http.models_unauthorized_status,
                    "authorized_status":http.models_authorized_status,
                    "request_id":http.models_request_id,
                    "model_count":http.model_count,
                    "required":PROFILES.len()
                }),
            ))
        }
        "chat_non_stream" => request_case(
            pool,
            started_at,
            &qa_label,
            "profile_id='z-ai/glm-5.2' AND stream=false",
        )
        .await,
        "chat_stream" => request_case(
            pool,
            started_at,
            &qa_label,
            "profile_id='z-ai/glm-5.2' AND stream=true",
        )
        .await,
        "privacy" | "secret_scan" => {
            let forbidden = sqlx::query_scalar::<_, i64>(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema='nblb' AND column_name IN ('prompt','messages','media','authorization','credential','provider_response_body')",
            )
            .fetch_one(pool)
            .await
            .context("inspect privacy schema")?;
            let raw_matches = database_secret_matches(pool).await?;
            Ok((
                forbidden == 0 && raw_matches == 0,
                json!({"forbidden_columns":forbidden,"database_raw_secret_matches":raw_matches}),
            ))
        }
        "six_requests" => {
            let rows = distribution_rows(pool, started_at, &qa_label).await?;
            let request_ids = rows
                .iter()
                .map(|row| row.request_id.to_string())
                .collect::<Vec<_>>();
            let attempts = rows.iter().map(|row| row.attempt_count).sum::<i64>();
            let all_single_attempt_success = rows.iter().all(|row| {
                row.request_outcome == "succeeded"
                    && row.attempt_count == 1
                    && row.succeeded_attempts == 1
            });
            Ok((
                rows.len() == 6 && attempts == 6 && all_single_attempt_success,
                json!({
                    "successful_requests":rows.len(),
                    "required":6,
                    "request_ids":request_ids.join(", "),
                    "attempt_count":attempts,
                    "one_attempt_each":all_single_attempt_success
                }),
            ))
        }
        "skew_at_most_one" => {
            let rows = distribution_rows(pool, started_at, &qa_label).await?;
            let slots = rows.iter().map(|row| row.slot_no).collect::<Vec<_>>();
            let expected = [1_i16, 2, 1, 2, 1, 2];
            let generation_before =
                traffic.and_then(|item| item.distribution_generation_before);
            let generation_after = traffic.and_then(|item| item.distribution_generation_after);
            let generation_delta = generation_before
                .zip(generation_after)
                .map(|(before, after)| after - before);
            let slot_1 = slots.iter().filter(|slot| **slot == 1).count();
            let slot_2 = slots.iter().filter(|slot| **slot == 2).count();
            let exact_single_attempts = rows.iter().all(|row| row.attempt_count == 1);
            Ok((
                slots == expected
                    && exact_single_attempts
                    && generation_delta == Some(6),
                json!({
                    "slot_sequence":slots.iter().map(ToString::to_string).collect::<Vec<_>>().join(","),
                    "expected_sequence":"1,2,1,2,1,2",
                    "slot_1":slot_1,
                    "slot_2":slot_2,
                    "one_attempt_each":exact_single_attempts,
                    "generation_before":generation_before,
                    "generation_after":generation_after,
                    "generation_delta":generation_delta,
                    "required_generation_delta":6
                }),
            ))
        }
        "before_first_frame" => {
            let count = sqlx::query_scalar::<_, i64>(
                "SELECT count(*) FROM nblb.proxy_requests request JOIN nblb.downstream_credentials client ON client.id=request.downstream_credential_id WHERE request.started_at>=$1 AND client.label=$2 AND request.outcome='succeeded' AND EXISTS(SELECT 1 FROM nblb.request_attempts attempt WHERE attempt.proxy_request_id=request.id AND attempt.attempt_no=1 AND attempt.outcome='failed' AND attempt.response_started=false) AND EXISTS(SELECT 1 FROM nblb.request_attempts attempt WHERE attempt.proxy_request_id=request.id AND attempt.attempt_no=2 AND attempt.outcome='succeeded')",
            )
            .bind(started_at)
            .bind(&qa_label)
            .fetch_one(pool)
            .await
            .context("load pre-frame failover evidence")?;
            Ok((count >= 1, json!({"successful_failovers":count})))
        }
        "after_first_frame_no_replay" => {
            let boundary_failures = sqlx::query_scalar::<_, i64>(
                "SELECT count(*) FROM nblb.request_attempts attempt JOIN nblb.proxy_requests request ON request.id=attempt.proxy_request_id JOIN nblb.downstream_credentials client ON client.id=request.downstream_credential_id WHERE request.started_at>=$1 AND client.label=$2 AND attempt.response_started=true AND attempt.outcome='failed' AND attempt.error_class IN ('upstream_stream_error','incomplete_upstream_stream','upstream_protocol_error','qa_injected_post_frame_failure')",
            )
            .bind(started_at)
            .bind(&qa_label)
            .fetch_one(pool)
            .await
            .context("load post-first-frame failure evidence")?;
            let replayed = sqlx::query_scalar::<_, i64>(
                "SELECT count(*) FROM nblb.request_attempts first_attempt JOIN nblb.request_attempts later ON later.proxy_request_id=first_attempt.proxy_request_id AND later.attempt_no>first_attempt.attempt_no JOIN nblb.proxy_requests request ON request.id=first_attempt.proxy_request_id JOIN nblb.downstream_credentials client ON client.id=request.downstream_credential_id WHERE request.started_at>=$1 AND client.label=$2 AND first_attempt.response_started=true",
            )
            .bind(started_at)
            .bind(&qa_label)
            .fetch_one(pool)
            .await
            .context("load replay boundary evidence")?;
            Ok((
                boundary_failures >= 1 && replayed == 0,
                json!({"post_first_frame_failures":boundary_failures,"replayed_after_first_frame":replayed}),
            ))
        }
        "terminal_evidence" => {
            let count = sqlx::query_scalar::<_, i64>(
                "SELECT count(*) FROM nblb.proxy_requests request JOIN nblb.downstream_credentials client ON client.id=request.downstream_credential_id WHERE request.started_at>=$1 AND client.label=$2 AND request.finished_at IS NULL",
            )
            .bind(started_at)
            .bind(&qa_label)
            .fetch_one(pool)
            .await
            .context("load open request evidence")?;
            Ok((count == 0, json!({"preexisting_open_requests":count})))
        }
        "encrypted_keys" => count_case(
            pool,
            "SELECT count(*) FROM nblb.upstream_keys WHERE retired=false AND octet_length(ciphertext)>0 AND octet_length(nonce)=12",
            2,
        )
        .await,
        "downstream_token" => count_case(
            pool,
            "SELECT count(*) FROM nblb.downstream_credentials WHERE active=true AND octet_length(digest)=32",
            1,
        )
        .await,
        "profile_receipts" => count_case(
            pool,
            "SELECT count(DISTINCT key_id) FROM nblb.profile_probe_receipts WHERE invalidated_at IS NULL",
            2,
        )
        .await,
        "routing_cursor" => count_case(
            pool,
            "SELECT count(*) FROM nblb.routing_state WHERE generation>0",
            1,
        )
        .await,
        "owner_lease" => count_case(
            pool,
            "SELECT count(*) FROM nblb.gateway_instances WHERE last_seen_at>=now()-interval '30 seconds'",
            1,
        )
        .await,
        "phi_text_image_audio" => {
            request_profile_count_case(pool, started_at, &qa_label, "microsoft/phi-4-multimodal-instruct", 3).await
        }
        "vila_text_image_video" => {
            request_profile_count_case(pool, started_at, &qa_label, "nvidia/vila", 3).await
        }
        "nvclip_text_image" => {
            request_profile_count_case(pool, started_at, &qa_label, "nvidia/nvclip", 2).await
        }
        "flux_text_image" => {
            request_profile_count_case(pool, started_at, &qa_label, "black-forest-labs/flux.1-kontext-dev", 2).await
        }
        "video_generation" => {
            request_profile_count_case(pool, started_at, &qa_label, "stabilityai/stable-video-diffusion", 1).await
        }
        "speech_and_transcription" => {
            let speech = successful_profile_count(pool, started_at, &qa_label, "nvidia/magpie-tts-multilingual").await?;
            let transcription = successful_profile_count(pool, started_at, &qa_label, "nvidia/parakeet-ctc-1.1b").await?;
            Ok((speech >= 1 && transcription >= 1, json!({"speech":speech,"transcription":transcription,"required_each":1})))
        }
        "doctor" | "exact_marker" | "tool_task" | "request_correlation" => {
            let count = successful_request_count(pool, started_at, Some("hermes-cutover-%")).await?;
            let required = if name == "tool_task" { 2 } else { 1 };
            Ok((
                count >= required,
                json!({"correlated_requests":count,"required":required}),
            ))
        }
        other => Ok((false, json!({"error_code":"unknown_qa_case","case":other}))),
    }
}

async fn count_case(pool: &PgPool, query: &str, required: i64) -> Result<(bool, Value)> {
    let count = sqlx::query_scalar::<_, i64>(query)
        .fetch_one(pool)
        .await
        .context("count QA evidence")?;
    Ok((
        count >= required,
        json!({"count":count,"required":required}),
    ))
}

async fn eligible_count(pool: &PgPool) -> Result<i64> {
    sqlx::query_scalar::<_, i64>(
        "SELECT count(DISTINCT key.id) FROM nblb.upstream_keys key JOIN nblb.profile_probe_receipts proof ON proof.key_id=key.id AND proof.profile_id='z-ai/glm-5.2' AND proof.invalidated_at IS NULL WHERE key.retired=false AND key.enabled=true AND key.verified=true AND (key.cooldown_until IS NULL OR key.cooldown_until<=now()) AND proof.verified_at>=now()-make_interval(secs=>(SELECT proof_freshness_seconds FROM nblb.operations_settings WHERE singleton=true))",
    )
    .fetch_one(pool)
    .await
    .context("count eligible QA slots")
}

async fn successful_request_count(
    pool: &PgPool,
    started_at: chrono::DateTime<chrono::Utc>,
    client_label: Option<&str>,
) -> Result<i64> {
    sqlx::query_scalar::<_, i64>(
        "SELECT count(*) FROM nblb.proxy_requests request LEFT JOIN nblb.downstream_credentials client ON client.id=request.downstream_credential_id WHERE request.started_at>=$1 AND request.outcome='succeeded' AND ($2::text IS NULL OR client.label LIKE $2)",
    )
    .bind(started_at)
    .bind(client_label)
    .fetch_one(pool)
    .await
    .context("count successful QA requests")
}

async fn distribution_rows(
    pool: &PgPool,
    started_at: chrono::DateTime<chrono::Utc>,
    client_label: &str,
) -> Result<Vec<DistributionRow>> {
    sqlx::query_as::<_, DistributionRow>(
        "SELECT request.request_id,min(key.slot_no) FILTER (WHERE attempt.attempt_no=1) AS slot_no,request.outcome AS request_outcome,count(attempt.id)::bigint AS attempt_count,count(attempt.id) FILTER (WHERE attempt.outcome='succeeded')::bigint AS succeeded_attempts FROM nblb.proxy_requests request JOIN nblb.downstream_credentials client ON client.id=request.downstream_credential_id JOIN nblb.request_attempts attempt ON attempt.proxy_request_id=request.id JOIN nblb.upstream_keys key ON key.id=attempt.key_id WHERE request.started_at>=$1 AND client.label=$2 AND request.profile_id='z-ai/glm-5.2' GROUP BY request.id,request.request_id,request.outcome,request.started_at ORDER BY request.started_at,request.request_id",
    )
    .bind(started_at)
    .bind(client_label)
    .fetch_all(pool)
    .await
    .context("load exact distribution request evidence")
}

async fn request_case(
    pool: &PgPool,
    started_at: chrono::DateTime<chrono::Utc>,
    client_label: &str,
    predicate: &str,
) -> Result<(bool, Value)> {
    let query = format!(
        "SELECT count(*) FROM nblb.proxy_requests request JOIN nblb.downstream_credentials client ON client.id=request.downstream_credential_id WHERE request.started_at>=$1 AND client.label=$2 AND request.outcome='succeeded' AND {predicate}"
    );
    let count = sqlx::query_scalar::<_, i64>(&query)
        .bind(started_at)
        .bind(client_label)
        .fetch_one(pool)
        .await
        .context("count request QA evidence")?;
    Ok((count >= 1, json!({"successful_requests":count})))
}

async fn successful_profile_count(
    pool: &PgPool,
    started_at: chrono::DateTime<chrono::Utc>,
    client_label: &str,
    profile: &str,
) -> Result<i64> {
    sqlx::query_scalar::<_, i64>(
        "SELECT count(*) FROM nblb.proxy_requests request JOIN nblb.downstream_credentials client ON client.id=request.downstream_credential_id WHERE request.started_at>=$1 AND client.label=$2 AND request.profile_id=$3 AND request.outcome='succeeded'",
    )
    .bind(started_at)
    .bind(client_label)
    .bind(profile)
    .fetch_one(pool)
    .await
    .context("count profile QA evidence")
}

async fn request_profile_count_case(
    pool: &PgPool,
    started_at: chrono::DateTime<chrono::Utc>,
    client_label: &str,
    profile: &str,
    required: i64,
) -> Result<(bool, Value)> {
    let count = successful_profile_count(pool, started_at, client_label, profile).await?;
    Ok((
        count >= required,
        json!({"profile_id":profile,"successful_requests":count,"required":required}),
    ))
}

#[cfg(test)]
mod tests {
    use super::{
        HermesCompletion, HermesRequestEvidence, PersistenceSnapshot, QA_H264_MP4_DATA_URL,
        SecretScanEvidence, close_interrupted_runs, combine_traffic_cleanup, complete_hermes,
        database_secret_matches, fail_running_run, local_qa_request, persistence_case,
        validate_audio_chat_qa_response, validate_transcription_qa_response,
    };
    use base64::Engine as _;
    use chrono::{Duration, Utc};
    use uuid::Uuid;

    #[test]
    fn local_qa_connects_to_the_fixed_listener_with_the_remapped_public_authority() {
        let client = reqwest::Client::new();
        let request = local_qa_request(
            &client,
            reqwest::Method::POST,
            "http://127.0.0.1:2456/v1/chat/completions".to_owned(),
            43_123,
        )
        .build()
        .expect("build remapped local QA request");

        assert_eq!(request.url().port(), Some(2456));
        assert_eq!(
            request
                .headers()
                .get(reqwest::header::HOST)
                .expect("explicit local QA Host")
                .to_str()
                .expect("ASCII local QA Host"),
            "127.0.0.1:43123"
        );
    }

    #[test]
    fn live_transcription_qa_requires_the_spoken_fixture_token() {
        assert!(validate_transcription_qa_response(br#"{"text":"hello"}"#).is_ok());
        assert!(validate_transcription_qa_response(br#"{"text":"Hello."}"#).is_ok());
        assert!(validate_transcription_qa_response(br#"{"text":""}"#).is_err());
        assert!(validate_transcription_qa_response(br#"{"text":"yellow"}"#).is_err());
    }

    #[test]
    fn live_audio_chat_qa_requires_the_spoken_fixture_token() {
        assert!(
            validate_audio_chat_qa_response(br#"{"choices":[{"message":{"content":"hello"}}]}"#)
                .is_ok()
        );
        assert!(
            validate_audio_chat_qa_response(
                br#"{"choices":[{"message":{"content":"The word is HELLO."}}]}"#
            )
            .is_ok()
        );
        assert!(
            validate_audio_chat_qa_response(br#"{"choices":[{"message":{"content":""}}]}"#)
                .is_err()
        );
        assert!(
            validate_audio_chat_qa_response(br#"{"choices":[{"message":{"content":"yellow"}}]}"#)
                .is_err()
        );
    }

    fn snapshot(owner_id: Uuid) -> PersistenceSnapshot {
        PersistenceSnapshot {
            owner_id,
            key_count: 2,
            key_hash: "keys".into(),
            active_client_count: 1,
            client_hash: "clients".into(),
            receipt_count: 4,
            receipt_key_count: 2,
            receipt_hash: "receipts".into(),
            routing_count: 3,
            routing_generation_count: 1,
            routing_hash: "routing".into(),
        }
    }

    #[test]
    fn hermes_secret_scan_contract_requires_every_expanded_scalar() {
        let value = serde_json::json!({
            "database_matches":0,
            "app_log_matches":0,
            "hermes_log_matches":0,
            "cloudflared_log_matches":0,
            "image_metadata_matches":0,
            "helper_artifact_matches":0,
            "hermes_tree_matches":0,
            "public_bundle_matches":0,
            "admin_response_matches":0,
            "issued_token_matches":0,
            "authorization_value_matches":0
        });
        let evidence: SecretScanEvidence =
            serde_json::from_value(value.clone()).expect("deserialize complete secret scan");
        assert_eq!(evidence.total_matches(), 0);

        let mut missing = value.clone();
        missing
            .as_object_mut()
            .expect("secret scan object")
            .remove("public_bundle_matches");
        assert!(serde_json::from_value::<SecretScanEvidence>(missing).is_err());

        let mut unknown = value;
        unknown
            .as_object_mut()
            .expect("secret scan object")
            .insert("unverified_surface".into(), serde_json::json!(0));
        assert!(serde_json::from_value::<SecretScanEvidence>(unknown).is_err());
    }

    #[test]
    fn live_multimodal_h264_fixture_is_a_complete_decodable_mp4_container() {
        fn boxes(bytes: &[u8], start: usize, end: usize) -> Vec<([u8; 4], usize, usize)> {
            let mut offset = start;
            let mut found = Vec::new();
            while offset < end {
                assert!(offset + 8 <= end, "truncated MP4 box at {offset}");
                let size = u32::from_be_bytes(
                    bytes[offset..offset + 4]
                        .try_into()
                        .expect("MP4 box size bytes"),
                ) as usize;
                assert!(
                    size >= 8 && offset + size <= end,
                    "invalid MP4 box size at offset {offset}: size={size}, boundary={end}"
                );
                found.push((
                    bytes[offset + 4..offset + 8]
                        .try_into()
                        .expect("MP4 box type bytes"),
                    offset,
                    offset + size,
                ));
                offset += size;
            }
            assert_eq!(offset, end, "MP4 boxes must exactly fill their parent");
            found
        }

        fn child(items: &[([u8; 4], usize, usize)], kind: &[u8; 4]) -> (usize, usize) {
            items
                .iter()
                .find(|item| &item.0 == kind)
                .map(|item| (item.1, item.2))
                .unwrap_or_else(|| panic!("missing MP4 box {}", String::from_utf8_lossy(kind)))
        }

        let encoded = QA_H264_MP4_DATA_URL
            .strip_prefix("data:video/mp4;base64,")
            .expect("H.264 fixture data URL prefix");
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(encoded)
            .expect("decode H.264 fixture");
        assert!(
            bytes.len() > 1_000,
            "fixture must contain real media samples"
        );
        assert!(crate::streaming::valid_mp4(&bytes));

        let top = boxes(&bytes, 0, bytes.len());
        child(&top, b"ftyp");
        child(&top, b"mdat");
        let moov = child(&top, b"moov");
        let moov_children = boxes(&bytes, moov.0 + 8, moov.1);
        child(&moov_children, b"mvhd");
        child(&moov_children, b"udta");
        let trak = child(&moov_children, b"trak");
        let trak_children = boxes(&bytes, trak.0 + 8, trak.1);
        child(&trak_children, b"tkhd");
        child(&trak_children, b"edts");
        let mdia = child(&trak_children, b"mdia");
        let mdia_children = boxes(&bytes, mdia.0 + 8, mdia.1);
        child(&mdia_children, b"mdhd");
        child(&mdia_children, b"hdlr");
        let minf = child(&mdia_children, b"minf");
        let minf_children = boxes(&bytes, minf.0 + 8, minf.1);
        child(&minf_children, b"vmhd");
        child(&minf_children, b"dinf");
        let stbl = child(&minf_children, b"stbl");
        let stbl_children = boxes(&bytes, stbl.0 + 8, stbl.1);
        for kind in [
            b"stsd", b"stts", b"stss", b"ctts", b"stsc", b"stsz", b"stco",
        ] {
            child(&stbl_children, kind);
        }
        assert!(bytes.windows(4).any(|window| window == b"avc1"));
        assert!(bytes.windows(4).any(|window| window == b"avcC"));
        assert!(bytes.windows(4).any(|window| window == b"stsz"));
        assert!(bytes.windows(4).any(|window| window == b"stco"));
    }

    #[sqlx::test(migrations = "../../migrations/sqlx")]
    async fn persisted_secret_scan_reports_raw_matches_not_boolean_claims(pool: sqlx::PgPool) {
        assert_eq!(
            database_secret_matches(&pool)
                .await
                .expect("scan clean database"),
            0,
        );
        let mut incident_ids = Vec::new();
        for title in [
            concat!("nvapi", "-test-fixture"),
            concat!("nblb_ds", "_test-fixture"),
            concat!("nblb_admin", "_test-fixture"),
        ] {
            let incident_id = Uuid::new_v4();
            sqlx::query(
                "INSERT INTO nblb.incidents(id,slug,title,status,severity,public) VALUES($1,$2,$3,'investigating','minor',false)",
            )
            .bind(incident_id)
            .bind(format!("scan-fixture-{incident_id}"))
            .bind(title)
            .execute(&pool)
            .await
            .expect("seed raw secret fixture");
            incident_ids.push(incident_id);
        }
        assert_eq!(
            database_secret_matches(&pool)
                .await
                .expect("scan contaminated database"),
            3,
        );
        sqlx::query("DELETE FROM nblb.incidents WHERE id=ANY($1)")
            .bind(&incident_ids)
            .execute(&pool)
            .await
            .expect("remove raw secret fixture");
        assert_eq!(
            database_secret_matches(&pool)
                .await
                .expect("rescan cleaned database"),
            0,
        );
    }

    #[test]
    fn persistence_cases_require_a_new_owner_and_identical_durable_state() {
        let before = snapshot(Uuid::new_v4());
        let after = snapshot(Uuid::new_v4());
        for name in [
            "encrypted_keys",
            "downstream_token",
            "profile_receipts",
            "routing_cursor",
            "owner_lease",
        ] {
            assert!(persistence_case(&before, &after, name).0, "{name}");
        }

        let same_owner = snapshot(before.owner_id);
        assert!(!persistence_case(&before, &same_owner, "owner_lease").0);
        let mut changed = after;
        changed.key_hash = "changed".into();
        changed.client_hash = "changed".into();
        changed.receipt_hash = "changed".into();
        changed.routing_hash = "changed".into();
        for name in [
            "encrypted_keys",
            "downstream_token",
            "profile_receipts",
            "routing_cursor",
        ] {
            assert!(!persistence_case(&before, &changed, name).0, "{name}");
        }
        assert!(!persistence_case(&before, &changed, "unknown").0);
    }

    #[test]
    fn qa_cleanup_reports_traffic_and_revocation_failures_together() {
        let result = combine_traffic_cleanup::<()>(
            Err(anyhow::anyhow!("traffic fixture failed")),
            Err(anyhow::anyhow!("client revoke failed")),
        )
        .expect_err("both failures must remain visible");
        let message = result.to_string();
        assert!(message.contains("traffic fixture failed"));
        assert!(message.contains("client revoke failed"));
    }

    #[sqlx::test(migrations = "../../migrations/sqlx")]
    async fn hermes_completion_revalidates_requests_and_closes_every_case(pool: sqlx::PgPool) {
        let request_id = Uuid::new_v4();
        let (run, _) = super::super::admin_repository::create_qa_run(
            &pool,
            "hermes-e2e",
            true,
            super::super::admin_repository::QaProviderIdentity::NvidiaHosted,
            request_id,
        )
        .await
        .expect("create Hermes QA run");
        let started_at = Utc::now();
        sqlx::query("UPDATE nblb.qa_runs SET status='running',started_at=$2 WHERE id=$1")
            .bind(run.id)
            .bind(started_at)
            .execute(&pool)
            .await
            .expect("start Hermes QA run");
        sqlx::query("UPDATE nblb.qa_cases SET status='running',started_at=$2 WHERE run_id=$1")
            .bind(run.id)
            .bind(started_at)
            .execute(&pool)
            .await
            .expect("arm Hermes QA cases");
        let generation = Uuid::new_v4();
        let client_id = sqlx::query_scalar::<_, Uuid>(
            "INSERT INTO nblb.downstream_credentials(label,digest,scopes,key_prefix) VALUES ($1,decode(repeat('11',32),'hex'),ARRAY['models:read','chat:write'],'hermes_test') RETURNING id",
        )
        .bind(format!("hermes-cutover-{generation}"))
        .fetch_one(&pool)
        .await
        .expect("seed Hermes client");
        let key_id = sqlx::query_scalar::<_, Uuid>(
            "INSERT INTO nblb.upstream_keys(label,fingerprint,ciphertext,nonce,enabled,verified,slot_no) VALUES ('slot-1',decode(repeat('22',32),'hex'),decode(repeat('33',17),'hex'),decode(repeat('44',12),'hex'),true,true,1) RETURNING id",
        )
        .fetch_one(&pool)
        .await
        .expect("seed upstream key");
        let mut requests = Vec::new();
        for offset in 1..=3 {
            let proxy_id = Uuid::new_v4();
            let public_id = Uuid::new_v4();
            let request_started = started_at + Duration::milliseconds(offset * 10);
            let request_finished = request_started + Duration::milliseconds(5);
            sqlx::query(
                "INSERT INTO nblb.proxy_requests(id,request_id,downstream_credential_id,endpoint,profile_id,stream,modality,outcome,status_code,duration_ms,started_at,finished_at) VALUES ($1,$2,$3,'/v1/chat/completions','z-ai/glm-5.2',false,'text','succeeded',200,5,$4,$5)",
            )
            .bind(proxy_id)
            .bind(public_id)
            .bind(client_id)
            .bind(request_started)
            .bind(request_finished)
            .execute(&pool)
            .await
            .expect("seed correlated proxy request");
            sqlx::query(
                "INSERT INTO nblb.request_attempts(request_id,profile_id,key_id,outcome,created_at,finished_at,proxy_request_id,attempt_no,status_code,response_started,bytes_out) VALUES ($1,'z-ai/glm-5.2',$2,'succeeded',$3,$4,$5,1,200,true,1)",
            )
            .bind(public_id)
            .bind(key_id)
            .bind(request_started)
            .bind(request_finished)
            .bind(proxy_id)
            .execute(&pool)
            .await
            .expect("seed correlated request attempt");
            requests.push(HermesRequestEvidence {
                request_id: public_id,
                attempt_count: 1,
                outcome: "succeeded".into(),
                stage: if offset == 1 { "marker" } else { "tool" }.into(),
            });
        }
        let rejected = HermesCompletion {
            status: "passed".into(),
            app_commit: Some(crate::BUILD_COMMIT.into()),
            generation: Some(generation),
            client_id: Some(client_id),
            doctor: true,
            exact_marker: true,
            tool_task: false,
            request_correlation: true,
            secret_scan: Some(SecretScanEvidence {
                database_matches: 0,
                app_log_matches: 0,
                hermes_log_matches: 0,
                cloudflared_log_matches: 0,
                image_metadata_matches: 0,
                helper_artifact_matches: 0,
                hermes_tree_matches: 0,
                public_bundle_matches: 0,
                admin_response_matches: 0,
                issued_token_matches: 0,
                authorization_value_matches: 0,
            }),
            rollback_rehearsal: true,
            reconcile_committed: false,
            duration_ms: Some(100),
            lb_requests: requests,
        };
        assert!(
            complete_hermes(&pool, run.id, &rejected, Uuid::new_v4())
                .await
                .is_err()
        );
        assert_eq!(
            sqlx::query_scalar::<_, String>("SELECT status FROM nblb.qa_runs WHERE id=$1")
                .bind(run.id)
                .fetch_one(&pool)
                .await
                .expect("load rejected run status"),
            "running"
        );
        let accepted = HermesCompletion {
            tool_task: true,
            ..rejected
        };
        let (completed, _) = complete_hermes(&pool, run.id, &accepted, Uuid::new_v4())
            .await
            .expect("complete Hermes QA run");
        assert_eq!(completed.status, "passed");
        assert_eq!(completed.cases.len(), 6);
        assert!(completed.cases.iter().all(|case| case.status == "passed"));

        let (failed_run, _) = super::super::admin_repository::create_qa_run(
            &pool,
            "hermes-e2e",
            true,
            super::super::admin_repository::QaProviderIdentity::NvidiaHosted,
            Uuid::new_v4(),
        )
        .await
        .expect("create interrupted committed Hermes run");
        sqlx::query(
            "UPDATE nblb.qa_cases SET status='failed',evidence=jsonb_build_object('error_code','gateway_restarted_during_qa'),started_at=$2,finished_at=now() WHERE run_id=$1",
        )
        .bind(failed_run.id)
        .bind(started_at)
        .execute(&pool)
        .await
        .expect("close interrupted Hermes cases");
        sqlx::query(
            "UPDATE nblb.qa_runs SET status='failed',started_at=$2,finished_at=now() WHERE id=$1",
        )
        .bind(failed_run.id)
        .bind(started_at)
        .execute(&pool)
        .await
        .expect("close interrupted Hermes run");

        let unproven_recovery = HermesCompletion {
            reconcile_committed: false,
            ..accepted.clone()
        };
        assert!(
            complete_hermes(&pool, failed_run.id, &unproven_recovery, Uuid::new_v4(),)
                .await
                .is_err(),
            "a general failed run must remain terminal",
        );
        let committed_recovery = HermesCompletion {
            reconcile_committed: true,
            ..accepted
        };
        let (reconciled, _) =
            complete_hermes(&pool, failed_run.id, &committed_recovery, Uuid::new_v4())
                .await
                .expect("reconcile exact committed Hermes receipt after restart failure");
        assert_eq!(reconciled.status, "passed");
        assert!(reconciled.cases.iter().all(|case| case.status == "passed"));
    }

    #[sqlx::test(migrations = "../../migrations/sqlx")]
    async fn restart_recovery_closes_orphaned_queued_run(pool: sqlx::PgPool) {
        let (run, _) = super::super::admin_repository::create_qa_run(
            &pool,
            "persistence",
            false,
            super::super::admin_repository::QaProviderIdentity::Fake,
            Uuid::new_v4(),
        )
        .await
        .expect("create queued persistence QA run");
        close_interrupted_runs(&pool)
            .await
            .expect("close orphaned queued QA run");
        let recovered = super::super::admin_repository::qa_run(&pool, run.id)
            .await
            .expect("load recovered QA run")
            .expect("recovered QA run exists");
        assert_eq!(recovered.status, "failed");
        assert!(recovered.cases.iter().all(|case| case.status == "failed"));
        assert!(
            recovered
                .cases
                .iter()
                .all(|case| { case.evidence["error_code"] == "gateway_restarted_during_qa" })
        );
    }

    #[sqlx::test(migrations = "../../migrations/sqlx")]
    async fn recovery_failure_close_never_leaves_a_running_run(pool: sqlx::PgPool) {
        let (run, _) = super::super::admin_repository::create_qa_run(
            &pool,
            "persistence",
            false,
            super::super::admin_repository::QaProviderIdentity::Fake,
            Uuid::new_v4(),
        )
        .await
        .expect("create persistence QA run");
        sqlx::query("UPDATE nblb.qa_runs SET status='running',started_at=now() WHERE id=$1")
            .bind(run.id)
            .execute(&pool)
            .await
            .expect("start persistence QA run");
        fail_running_run(&pool, run.id, "persistence_resume_failed")
            .await
            .expect("close failed persistence run");
        let completed = super::super::admin_repository::qa_run(&pool, run.id)
            .await
            .expect("load persistence QA run")
            .expect("persistence QA run exists");
        assert_eq!(completed.status, "failed");
        assert!(completed.cases.iter().all(|case| case.status == "failed"));
        assert!(
            completed
                .cases
                .iter()
                .all(|case| { case.evidence["error_code"] == "persistence_resume_failed" })
        );
    }

    #[sqlx::test(migrations = "../../migrations/sqlx")]
    async fn completion_uses_all_history_and_separates_latest_failure(pool: sqlx::PgPool) {
        for suite in super::REQUIRED_SUITES {
            sqlx::query(
                "INSERT INTO nblb.qa_runs(suite,live,provider_identity,deployment_commit,status,created_at,started_at,finished_at) VALUES($1,true,'nvidia_hosted',$2,'passed',now()-interval '2 hours',now()-interval '2 hours',now()-interval '2 hours')",
            )
            .bind(suite)
            .bind(crate::BUILD_COMMIT)
            .execute(&pool)
            .await
            .expect("seed current-deployment passed QA run");
        }
        for seconds in 1_i32..=55 {
            sqlx::query(
                "INSERT INTO nblb.qa_runs(suite,live,provider_identity,deployment_commit,status,created_at,started_at,finished_at) VALUES('smoke',true,'nvidia_hosted',$1,'failed',now()-make_interval(secs => $2),now()-make_interval(secs => $2),now()-make_interval(secs => $2))",
            )
            .bind(crate::BUILD_COMMIT)
            .bind(seconds)
            .execute(&pool)
            .await
            .expect("seed newer failed smoke run");
        }

        let completion = super::super::admin_repository::qa_completion(
            &pool,
            crate::BUILD_COMMIT,
            &super::REQUIRED_SUITES,
        )
        .await
        .expect("load authoritative QA completion");
        assert_eq!(completion.len(), 6);
        let smoke = completion
            .iter()
            .find(|item| item.suite == "smoke")
            .expect("smoke completion");
        assert!(smoke.passed);
        assert_eq!(
            smoke.passed_run.as_ref().map(|run| run.status.as_str()),
            Some("passed")
        );
        assert_eq!(
            smoke.latest_run.as_ref().map(|run| run.status.as_str()),
            Some("failed")
        );
        assert_ne!(
            smoke.passed_run.as_ref().map(|run| run.id),
            smoke.latest_run.as_ref().map(|run| run.id),
        );
    }
}
