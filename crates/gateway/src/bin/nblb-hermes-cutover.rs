#![forbid(unsafe_code)]
//! Root-only, crash-aware Hermes cutover and end-to-end verifier.

use anyhow::{Context, Result, bail};
use fs2::FileExt;
use futures_util::StreamExt;
use reqwest::{Client, StatusCode};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    env,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    process::{Command, Output, Stdio},
    thread,
    time::{Duration, Instant},
};
use uuid::Uuid;

const HERMES_DIR: &str = "/opt/agent-apps/data/hermes";
const ENV_PATH: &str = "/opt/agent-apps/data/hermes/.env";
const CONFIG_PATH: &str = "/opt/agent-apps/data/hermes/config.yaml";
const BACKUP_ROOT: &str = "/opt/nvidia-build-lb/hermes-cutover-backups";
const RECEIPT_ROOT: &str = "/opt/nvidia-build-lb/hermes-cutover-receipts";
const RETIREMENT_ROOT: &str = "/opt/nvidia-build-lb/hermes-backup-retirement-receipts";
const STATE_ROOT: &str = "/opt/nvidia-build-lb/hermes-cutover-state";
const ADMIN_TOKEN_PATH: &str = "/opt/nvidia-build-lb/secrets/admin_token";
const ADMIN_BASE: &str = "http://127.0.0.1:2456/admin/api/v2";
const PUBLIC_BASE: &str = "http://127.0.0.1:2456";
const CONTAINER: &str = "agent-hermes";
const APP_CONTAINER: &str = "nvidia-build-lb-app-1";
const CLOUDFLARED_CONTAINER: &str = "cloudflared-apps";
const OUTPUT_LIMIT: usize = 64 * 1024;
const TOOL_CONTENT: &[u8] = b"NBLB_TOOL_OK\n";
const EMBEDDED_COMMIT: &str = match option_env!("NBLB_GIT_COMMIT") {
    Some(value) => value,
    None => "unknown",
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Action {
    Preflight,
    Apply,
    Verify,
    RetireBackup,
    Version,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Options {
    action: Action,
    qa_run_id: Option<Uuid>,
    generation: Option<Uuid>,
    provider_revoked: bool,
}

#[derive(Debug)]
struct Paths {
    hermes_dir: PathBuf,
    env: PathBuf,
    config: PathBuf,
    backup_root: PathBuf,
    receipt_root: PathBuf,
    retirement_root: PathBuf,
    state_root: PathBuf,
    admin_token: PathBuf,
}

impl Paths {
    fn production() -> Self {
        Self {
            hermes_dir: HERMES_DIR.into(),
            env: ENV_PATH.into(),
            config: CONFIG_PATH.into(),
            backup_root: BACKUP_ROOT.into(),
            receipt_root: RECEIPT_ROOT.into(),
            retirement_root: RETIREMENT_ROOT.into(),
            state_root: STATE_ROOT.into(),
            admin_token: ADMIN_TOKEN_PATH.into(),
        }
    }
}

#[derive(Debug, Deserialize)]
struct MutationResponse {
    item: ClientItem,
    token: String,
}

#[derive(Debug, Deserialize)]
struct ClientItem {
    id: Uuid,
    label: String,
    active: bool,
}

#[derive(Debug, Deserialize)]
struct Page<T> {
    items: Vec<T>,
    next_before: Option<String>,
}

#[derive(Debug, Deserialize)]
struct RequestItem {
    request_id: Uuid,
    client_id: Option<Uuid>,
    outcome: String,
    started_at: chrono::DateTime<chrono::Utc>,
    finished_at: Option<chrono::DateTime<chrono::Utc>>,
    attempts: Option<Vec<Value>>,
}

#[derive(Clone, Debug, Deserialize)]
struct QaRunTarget {
    id: Uuid,
    suite: String,
    live: bool,
    provider_identity: String,
    deployment_commit: String,
    status: String,
}

#[derive(Debug, Default)]
struct RecoverySummary {
    reconciled_qa_runs: HashSet<Uuid>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum QaRunIntent {
    Start,
    RecoverOrConfirmCommitted,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum PostLockQaDecision {
    Start,
    Complete,
}

#[derive(Debug, Deserialize)]
struct SnapshotManifest {
    schema_version: String,
    generation: Uuid,
    env_sha256: String,
    config_sha256: String,
}

#[derive(Debug, Deserialize)]
struct RecoveryJournal {
    schema_version: String,
    state: String,
    operation_label: Option<String>,
    client_id: Option<Uuid>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum RecoveryAction {
    None,
    ReconcileCommitted,
    RollbackAndRevoke,
    RevokeAfterRollback,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum CutoverFailureDisposition {
    PreCommit,
    CommittedReconcilePending,
}

#[derive(Debug)]
struct CutoverFailure {
    disposition: CutoverFailureDisposition,
    source: anyhow::Error,
}

impl CutoverFailure {
    fn precommit(source: anyhow::Error) -> Self {
        Self {
            disposition: CutoverFailureDisposition::PreCommit,
            source,
        }
    }

    fn committed(source: anyhow::Error) -> Self {
        Self {
            disposition: CutoverFailureDisposition::CommittedReconcilePending,
            source,
        }
    }
}

impl std::fmt::Display for CutoverFailure {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{:?}: {:#}", self.disposition, self.source)
    }
}

impl std::error::Error for CutoverFailure {}

#[derive(Debug, Deserialize, Serialize)]
struct RetirementReceipt {
    schema_version: String,
    generation: Uuid,
    embedded_commit: String,
    cutover_client_id: Uuid,
    snapshot_env_sha256: String,
    snapshot_config_sha256: String,
    provider_revocation_confirmed: bool,
    retired_at: chrono::DateTime<chrono::Utc>,
}

#[derive(Debug, Deserialize, Serialize)]
struct SafeReceipt {
    schema_version: String,
    generation: Uuid,
    embedded_commit: String,
    client_id: Uuid,
    qa_run_id: Option<Uuid>,
    doctor: bool,
    marker: bool,
    tool: bool,
    rollback_rehearsal: bool,
    duration_ms: u64,
    tool_output_sha256: String,
    secret_scan: SecretScanReceipt,
    lb_requests: Vec<RequestReceipt>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct SecretScanReceipt {
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

impl SecretScanReceipt {
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

#[derive(Debug, Deserialize, Serialize)]
struct RequestReceipt {
    request_id: Uuid,
    attempt_count: usize,
    outcome: String,
    stage: String,
    started_at: chrono::DateTime<chrono::Utc>,
    finished_at: chrono::DateTime<chrono::Utc>,
}

#[derive(Debug)]
struct Verification {
    doctor: bool,
    marker: bool,
    tool: bool,
    tool_output_sha256: String,
    lb_requests: Vec<RequestReceipt>,
}

#[tokio::main]
async fn main() {
    let arguments = env::args().skip(1).collect::<Vec<_>>();
    if let Err(error) = run(arguments).await {
        eprintln!("nblb-hermes-cutover failed: {error:#}");
        std::process::exit(1);
    }
}

async fn run(arguments: Vec<String>) -> Result<()> {
    let options = parse_options(arguments)?;
    if options.action == Action::Version {
        println!("nblb-hermes-cutover {EMBEDDED_COMMIT}");
        return Ok(());
    }
    require_root()?;
    let expected_commit = validate_expected_commit()?;
    let paths = Paths::production();
    if options.action == Action::RetireBackup {
        let _cutover_lock = acquire_cutover_lock(&paths)?;
        retire_backup(
            &paths,
            options
                .generation
                .context("retire-backup requires --generation")?,
            options.provider_revoked,
        )?;
        return Ok(());
    }
    let admin_token = read_secret(&paths.admin_token)?;
    let client = http_client()?;
    let qa_target = match options.qa_run_id {
        Some(run_id) => {
            Some(validate_qa_run_target(&client, &admin_token, run_id, &expected_commit).await?)
        }
        None => None,
    };
    let qa_intent = match &qa_target {
        Some(target) => qa_run_intent(
            &target.status,
            has_matching_local_generation(
                &paths,
                target.id,
                &["committed", "reconcile_pending", "reconciled"],
            )?,
        )?,
        None => QaRunIntent::Start,
    };
    let _cutover_lock = acquire_cutover_lock(&paths)?;
    cleanup_atomic_temps(&paths.hermes_dir, &[".env", "config.yaml"])
        .context("clean interrupted Hermes configuration writes")?;
    let recovery = recover_incomplete_cutovers(&paths, &client, &admin_token).await?;
    if let Some(target) = &qa_target {
        let refreshed =
            validate_qa_run_target(&client, &admin_token, target.id, &expected_commit).await?;
        let local_reconciled = recovery.reconciled_qa_runs.contains(&target.id)
            || has_matching_local_generation(&paths, target.id, &["reconciled"])?;
        match post_lock_qa_decision(&refreshed.status, local_reconciled)? {
            PostLockQaDecision::Complete => {
                println!(
                    "cutover: confirmed reconciled generation for QA run {}",
                    target.id
                );
                return Ok(());
            }
            PostLockQaDecision::Start if qa_intent == QaRunIntent::Start => {}
            PostLockQaDecision::Start => {
                bail!("matching committed Hermes recovery was not reconciled")
            }
        }
    }
    if let Err(error) = preflight(&paths).await {
        if let Some(target) = &qa_target {
            let report = report_failed_hermes_run(&client, &admin_token, target.id).await;
            return Err(with_failure_report(error, report));
        }
        return Err(error);
    }
    if options.action == Action::Preflight {
        println!("preflight: ok");
        return Ok(());
    }
    if options.action == Action::Verify {
        let generation = Uuid::new_v4();
        let marker = marker_path(generation);
        let start = chrono::Utc::now();
        let client_id = active_hermes_client(&client, &admin_token).await?;
        let verification = verify_hermes(&marker, &client, &admin_token, client_id, start).await?;
        println!(
            "verify: doctor={} marker={} tool={}",
            verification.doctor, verification.marker, verification.tool
        );
        return Ok(());
    }
    apply(
        &paths,
        &client,
        &admin_token,
        qa_target.context("apply requires a prevalidated QA run")?,
    )
    .await
    .map_err(anyhow::Error::new)
}

fn parse_action(args: impl Iterator<Item = String>) -> Result<Action> {
    let values = args.collect::<Vec<_>>();
    match values.as_slice() {
        [] => bail!(
            "usage: nblb-hermes-cutover [preflight|apply --qa-run UUID|verify|version|retire-backup --generation UUID --confirm-provider-revoked]"
        ),
        [value] if value == "apply" => Ok(Action::Apply),
        [value] if value == "preflight" => Ok(Action::Preflight),
        [value] if value == "verify" => Ok(Action::Verify),
        [value] if value == "retire-backup" => Ok(Action::RetireBackup),
        [value] if value == "version" || value == "--version" => Ok(Action::Version),
        _ => bail!(
            "usage: nblb-hermes-cutover [preflight|apply --qa-run UUID|verify|version|retire-backup --generation UUID --confirm-provider-revoked]"
        ),
    }
}

fn parse_options(arguments: Vec<String>) -> Result<Options> {
    let mut positional = Vec::new();
    let mut qa_run_id = None;
    let mut generation = None;
    let mut provider_revoked = false;
    let mut index = 0;
    while index < arguments.len() {
        if arguments[index] == "--qa-run" {
            if qa_run_id.is_some() || index + 1 >= arguments.len() {
                bail!("--qa-run requires exactly one UUID")
            }
            qa_run_id =
                Some(Uuid::parse_str(&arguments[index + 1]).context("--qa-run must be a UUID")?);
            index += 2;
        } else if arguments[index] == "--generation" {
            if generation.is_some() || index + 1 >= arguments.len() {
                bail!("--generation requires exactly one UUID")
            }
            generation = Some(
                Uuid::parse_str(&arguments[index + 1]).context("--generation must be a UUID")?,
            );
            index += 2;
        } else if arguments[index] == "--confirm-provider-revoked" {
            if provider_revoked {
                bail!("--confirm-provider-revoked may be supplied only once")
            }
            provider_revoked = true;
            index += 1;
        } else {
            positional.push(arguments[index].clone());
            index += 1;
        }
    }
    let action = parse_action(positional.into_iter())?;
    if qa_run_id.is_some() && action != Action::Apply {
        bail!("--qa-run is accepted only with apply")
    }
    if action == Action::Apply && qa_run_id.is_none() {
        bail!("apply requires --qa-run UUID")
    }
    if action == Action::RetireBackup {
        if generation.is_none() || !provider_revoked {
            bail!("retire-backup requires --generation UUID and --confirm-provider-revoked")
        }
    } else if generation.is_some() || provider_revoked {
        bail!("retirement confirmation flags are accepted only with retire-backup")
    }
    Ok(Options {
        action,
        qa_run_id,
        generation,
        provider_revoked,
    })
}

async fn preflight(paths: &Paths) -> Result<()> {
    require_root()?;
    validate_secure_file(&paths.env, true)?;
    validate_secure_file(&paths.config, true)?;
    validate_secure_file(&paths.admin_token, false)?;
    reject_mountpoint(&paths.env)?;
    reject_mountpoint(&paths.config)?;
    validate_directory(&paths.hermes_dir)?;
    let client = http_client()?;
    let ready = client
        .get(format!("{PUBLIC_BASE}/health/ready"))
        .timeout(Duration::from_secs(3))
        .send()
        .await
        .context("request load balancer readiness")?;
    if !ready.status().is_success() {
        bail!("load balancer is not traffic-ready")
    }
    let token = read_secret(&paths.admin_token)?;
    let upstreams = admin_get(&client, &token, "/upstreams?limit=100").await?;
    let eligible = upstreams["items"]
        .as_array()
        .map(|items| {
            items
                .iter()
                .filter(|item| item["eligible_now"].as_bool() == Some(true))
                .count()
        })
        .unwrap_or_default();
    if eligible != 2 {
        bail!("exactly two eligible upstreams are required; observed {eligible}")
    }
    Ok(())
}

fn require_root() -> Result<()> {
    let status = fs::read_to_string("/proc/self/status").context("read process identity")?;
    let effective = status
        .lines()
        .find_map(|line| line.strip_prefix("Uid:"))
        .and_then(|value| value.split_whitespace().nth(1))
        .and_then(|value| value.parse::<u32>().ok());
    if effective != Some(0) {
        bail!("nblb-hermes-cutover must run as root")
    }
    Ok(())
}

fn acquire_cutover_lock(paths: &Paths) -> Result<File> {
    ensure_root_directory(&paths.state_root, "Hermes cutover state root")?;
    let lock_path = paths.state_root.join("cutover.lock");
    if !lock_path.exists() {
        OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&lock_path)
            .context("create Hermes cutover lock")?;
    }
    validate_secure_file(&lock_path, true)?;
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .open(&lock_path)
        .context("open Hermes cutover lock")?;
    lock.lock_exclusive()
        .context("acquire Hermes cutover lock")?;
    Ok(lock)
}

fn ensure_root_directory(path: &Path, label: &str) -> Result<()> {
    if !path.exists() {
        create_secure_directory(path).with_context(|| format!("create {label}"))?;
    }
    validate_existing_root_directory(path, label)
}

fn create_secure_directory(path: &Path) -> Result<()> {
    let mut builder = fs::DirBuilder::new();
    builder.mode(0o700);
    builder.create(path).context("create mode-0700 directory")
}

fn validate_existing_root_directory(path: &Path, label: &str) -> Result<()> {
    for ancestor in path
        .ancestors()
        .skip(1)
        .take_while(|value| *value != Path::new("/"))
    {
        let metadata = fs::symlink_metadata(ancestor)
            .with_context(|| format!("inspect {label} parent {}", ancestor.display()))?;
        if metadata.file_type().is_symlink() {
            bail!("{label} parent must not contain symlinks")
        }
    }
    let metadata = fs::symlink_metadata(path).with_context(|| format!("inspect {label}"))?;
    if !metadata.is_dir()
        || metadata.file_type().is_symlink()
        || metadata.uid() != 0
        || metadata.gid() != 0
        || metadata.mode() & 0o777 != 0o700
    {
        bail!("{label} must be a root:root non-symlink directory with mode 0700")
    }
    reject_mountpoint(path).with_context(|| format!("validate {label} mount boundary"))
}

fn validate_expected_commit() -> Result<String> {
    let expected = env::var("NBLB_EXPECTED_COMMIT").context("NBLB_EXPECTED_COMMIT is required")?;
    if expected.len() != 40 || !expected.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        bail!("NBLB_EXPECTED_COMMIT must be a full Git commit SHA")
    }
    if EMBEDDED_COMMIT != expected {
        bail!("helper commit does not match the pinned application commit")
    }
    Ok(expected)
}

fn validate_secure_file(path: &Path, require_single_link: bool) -> Result<()> {
    validate_secure_file_with_identity(path, require_single_link, 0, 0)
}

fn validate_secure_file_with_identity(
    path: &Path,
    require_single_link: bool,
    expected_uid: u32,
    expected_gid: u32,
) -> Result<()> {
    let metadata = fs::symlink_metadata(path)
        .with_context(|| format!("inspect secure file {}", path.display()))?;
    if !metadata.file_type().is_file() || metadata.file_type().is_symlink() {
        bail!("{} must be a regular non-symlink file", path.display())
    }
    if metadata.uid() != expected_uid
        || metadata.gid() != expected_gid
        || metadata.mode() & 0o777 != 0o600
    {
        bail!("{} must be root:root mode 0600", path.display())
    }
    if require_single_link && metadata.nlink() != 1 {
        bail!("{} must not be hard-linked", path.display())
    }
    Ok(())
}

fn validate_directory(path: &Path) -> Result<()> {
    let metadata = fs::symlink_metadata(path)
        .with_context(|| format!("inspect directory {}", path.display()))?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        bail!("{} must be a non-symlink directory", path.display())
    }
    Ok(())
}

fn reject_mountpoint(path: &Path) -> Result<()> {
    let canonical = fs::canonicalize(path).context("canonicalize secure file")?;
    let mountinfo = fs::read_to_string("/proc/self/mountinfo").context("read mount table")?;
    let mounted = mountinfo.lines().any(|line| {
        line.split_whitespace()
            .nth(4)
            .map(unescape_mount_path)
            .is_some_and(|mount| Path::new(&mount) == canonical)
    });
    if mounted {
        bail!("{} must not be a mountpoint", path.display())
    }
    Ok(())
}

fn unescape_mount_path(value: &str) -> String {
    value
        .replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
}

async fn apply(
    paths: &Paths,
    client: &Client,
    admin_token: &str,
    qa_target: QaRunTarget,
) -> std::result::Result<(), CutoverFailure> {
    let qa_run_id = qa_target.id;
    let started = Instant::now();
    let generation = Uuid::new_v4();
    let label = format!("hermes-cutover-{generation}");
    let backup = match snapshot(paths, generation, &label) {
        Ok(backup) => backup,
        Err(error) => {
            let report = report_failed_hermes_run(client, admin_token, qa_run_id).await;
            return Err(CutoverFailure::precommit(with_failure_report(
                error, report,
            )));
        }
    };
    let issued = match issue_or_rotate_client(client, admin_token, &label).await {
        Ok(issued) => issued,
        Err(error) => {
            let cleanup = rollback_after_failure(paths, &backup);
            let report = report_failed_hermes_run(client, admin_token, qa_run_id).await;
            return Err(CutoverFailure::precommit(with_precommit_cleanup(
                error, cleanup, report,
            )));
        }
    };
    let precommit = async {
        let candidate_env = update_dotenv(
            &fs::read_to_string(&paths.env)?,
            "NVIDIA_API_KEY",
            &issued.token,
        );
        let candidate_config = update_yaml(&fs::read_to_string(&paths.config)?)?;
        write_journal(
            &backup,
            "candidate_ready",
            Some(&label),
            Some(issued.item.id),
        )?;
        docker(&["stop", CONTAINER])?;
        atomic_write(&paths.env, candidate_env.as_bytes(), 0o600)?;
        atomic_write(&paths.config, candidate_config.as_bytes(), 0o600)?;
        scan_for_direct_nvidia_key(&paths.hermes_dir)?;
        write_journal(
            &backup,
            "candidate_applied",
            Some(&label),
            Some(issued.item.id),
        )?;
        docker(&["start", CONTAINER])?;
        wait_for_container_health()?;
        let marker = marker_path(generation);
        let verification_started = chrono::Utc::now();
        let first = verify_hermes(
            &marker,
            client,
            admin_token,
            Some(issued.item.id),
            verification_started,
        )
        .await?;

        docker(&["stop", CONTAINER])?;
        restore_snapshot(paths, &backup)?;
        docker(&["start", CONTAINER])?;
        wait_for_container_health()?;
        docker(&["stop", CONTAINER])?;
        atomic_write(&paths.env, candidate_env.as_bytes(), 0o600)?;
        atomic_write(&paths.config, candidate_config.as_bytes(), 0o600)?;
        docker(&["start", CONTAINER])?;
        wait_for_container_health()?;
        let second_started = chrono::Utc::now();
        let second = verify_hermes(
            &marker,
            client,
            admin_token,
            Some(issued.item.id),
            second_started,
        )
        .await?;
        let secret_scan = collect_secret_scan(paths, client, admin_token, &issued.token).await?;
        let receipt = SafeReceipt {
            schema_version: "nblb.hermes-cutover.v1".into(),
            generation,
            embedded_commit: EMBEDDED_COMMIT.into(),
            client_id: issued.item.id,
            qa_run_id: Some(qa_run_id),
            doctor: first.doctor && second.doctor,
            marker: first.marker && second.marker,
            tool: first.tool && second.tool,
            rollback_rehearsal: true,
            duration_ms: u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX),
            tool_output_sha256: second.tool_output_sha256,
            secret_scan,
            lb_requests: second.lb_requests,
        };
        write_receipt(paths, &receipt)?;
        write_journal(&backup, "committed", Some(&label), Some(issued.item.id))?;
        Ok::<SafeReceipt, anyhow::Error>(receipt)
    }
    .await;
    let receipt = match precommit {
        Ok(receipt) => receipt,
        Err(error) => {
            let rollback = rollback_after_failure(paths, &backup);
            let mut cleanup_errors = Vec::new();
            if let Err(rollback_error) = &rollback {
                cleanup_errors.push(format!("rollback failed: {rollback_error:#}"));
            } else {
                if let Err(journal_error) = write_journal(
                    &backup,
                    "rollback_cleanup_pending",
                    Some(&label),
                    Some(issued.item.id),
                ) {
                    cleanup_errors.push(format!("rollback journal failed: {journal_error:#}"));
                }
                match revoke_client(client, admin_token, issued.item.id).await {
                    Ok(()) => {
                        if let Err(journal_error) = write_journal(
                            &backup,
                            "rolled_back",
                            Some(&label),
                            Some(issued.item.id),
                        ) {
                            cleanup_errors.push(format!(
                                "rollback completion journal failed: {journal_error:#}"
                            ));
                        }
                    }
                    Err(revoke_error) => {
                        cleanup_errors.push(format!("candidate revoke failed: {revoke_error:#}"))
                    }
                }
            }
            let report = report_failed_hermes_run(client, admin_token, qa_run_id).await;
            if let Err(report_error) = report {
                cleanup_errors.push(format!("QA failure report failed: {report_error:#}"));
            }
            return if cleanup_errors.is_empty() {
                Err(CutoverFailure::precommit(error))
            } else {
                Err(CutoverFailure::precommit(
                    error.context(cleanup_errors.join("; ")),
                ))
            };
        }
    };

    let reconciliation = async {
        reconcile_remote_state(client, admin_token, &receipt, ADMIN_BASE).await?;
        write_journal(&backup, "reconciled", Some(&label), Some(issued.item.id))?;
        Ok::<(), anyhow::Error>(())
    }
    .await;
    if let Err(error) = reconciliation {
        let journal_error = write_journal(
            &backup,
            "reconcile_pending",
            Some(&label),
            Some(issued.item.id),
        )
        .err();
        eprintln!(
            "cutover committed; post-commit reconciliation will retry on the next helper run: {}",
            bounded_error(&error)
        );
        let context = journal_error.map_or_else(
            || "cutover is committed and reconciliation is pending".to_owned(),
            |journal| {
                format!(
                    "cutover is committed and reconciliation is pending; pending journal write also failed: {journal:#}"
                )
            },
        );
        return Err(CutoverFailure::committed(error.context(context)));
    }
    println!("cutover: committed generation={generation}");
    Ok(())
}

fn with_failure_report(error: anyhow::Error, report: Result<()>) -> anyhow::Error {
    match report {
        Ok(()) => error,
        Err(report_error) => error.context(format!("QA failure report failed: {report_error:#}")),
    }
}

fn with_precommit_cleanup(
    error: anyhow::Error,
    cleanup: Result<()>,
    report: Result<()>,
) -> anyhow::Error {
    let mut context = Vec::new();
    if let Err(cleanup_error) = cleanup {
        context.push(format!("pre-commit cleanup failed: {cleanup_error:#}"));
    }
    if let Err(report_error) = report {
        context.push(format!("QA failure report failed: {report_error:#}"));
    }
    if context.is_empty() {
        error
    } else {
        error.context(context.join("; "))
    }
}

fn snapshot(paths: &Paths, generation: Uuid, operation_label: &str) -> Result<PathBuf> {
    ensure_root_directory(&paths.backup_root, "Hermes cutover backup root")?;
    let directory = paths.backup_root.join(generation.to_string());
    let stage = paths.backup_root.join(format!(".staging-{generation}"));
    if stage.exists() || directory.exists() {
        bail!("Hermes snapshot generation already exists")
    }
    create_secure_directory(&stage).context("create staged generation backup")?;
    let result = (|| {
        copy_secure(&paths.env, &stage.join("hermes.env"))?;
        copy_secure(&paths.config, &stage.join("config.yaml"))?;
        let manifest = json!({
            "schema_version":"nblb.hermes-snapshot.v1",
            "generation":generation,
            "env_sha256":sha256_file(&stage.join("hermes.env"))?,
            "config_sha256":sha256_file(&stage.join("config.yaml"))?
        });
        atomic_write(
            &stage.join("manifest.json"),
            &serde_json::to_vec_pretty(&manifest)?,
            0o600,
        )?;
        write_journal(
            &stage,
            "client_operation_prepared",
            Some(operation_label),
            None,
        )?;
        sync_directory(&stage)?;
        fs::rename(&stage, &directory).context("publish staged generation backup")?;
        sync_directory(&paths.backup_root)?;
        Ok::<(), anyhow::Error>(())
    })();
    if let Err(error) = result {
        let _ = cleanup_snapshot_stage(&paths.backup_root, &stage);
        return Err(error);
    }
    Ok(directory)
}

fn copy_secure(source: &Path, destination: &Path) -> Result<()> {
    let bytes = fs::read(source).with_context(|| format!("read {}", source.display()))?;
    atomic_write(destination, &bytes, 0o600)
}

fn restore_snapshot(paths: &Paths, backup: &Path) -> Result<()> {
    verify_snapshot(backup)?;
    atomic_write(&paths.env, &fs::read(backup.join("hermes.env"))?, 0o600)?;
    atomic_write(&paths.config, &fs::read(backup.join("config.yaml"))?, 0o600)?;
    Ok(())
}

fn rollback_after_failure(paths: &Paths, backup: &Path) -> Result<()> {
    let _ = docker(&["stop", CONTAINER]);
    restore_snapshot(paths, backup)?;
    docker(&["start", CONTAINER])?;
    if let Err(error) = wait_for_container_health() {
        let _ = docker(&["stop", CONTAINER]);
        return Err(error.context("restored Hermes failed health check and was stopped"));
    }
    Ok(())
}

fn verify_snapshot(backup: &Path) -> Result<()> {
    let manifest_path = backup.join("manifest.json");
    validate_secure_file(&manifest_path, true)?;
    validate_secure_file(&backup.join("hermes.env"), true)?;
    validate_secure_file(&backup.join("config.yaml"), true)?;
    let manifest: SnapshotManifest = serde_json::from_slice(&fs::read(&manifest_path)?)
        .context("decode Hermes snapshot manifest")?;
    if !snapshot_manifest_matches(backup, &manifest)? {
        bail!("Hermes snapshot manifest verification failed")
    }
    Ok(())
}

fn snapshot_manifest_matches(backup: &Path, manifest: &SnapshotManifest) -> Result<bool> {
    let directory_generation = backup
        .file_name()
        .and_then(|value| value.to_str())
        .and_then(|value| Uuid::parse_str(value).ok())
        .context("snapshot directory is not a UUID generation")?;
    Ok(manifest.schema_version == "nblb.hermes-snapshot.v1"
        && manifest.generation == directory_generation
        && manifest.env_sha256 == sha256_file(&backup.join("hermes.env"))?
        && manifest.config_sha256 == sha256_file(&backup.join("config.yaml"))?)
}

fn retire_backup(paths: &Paths, generation: Uuid, provider_revoked: bool) -> Result<()> {
    if !provider_revoked {
        bail!("provider-side NVIDIA key revocation must be explicitly confirmed")
    }
    ensure_root_directory(&paths.backup_root, "Hermes cutover backup root")?;
    ensure_root_directory(&paths.receipt_root, "Hermes cutover receipt root")?;
    ensure_root_directory(
        &paths.retirement_root,
        "Hermes backup retirement receipt root",
    )?;
    let backup = paths.backup_root.join(generation.to_string());
    let retirement_path = paths.retirement_root.join(format!("{generation}.json"));

    if retirement_path.exists() {
        let receipt = read_retirement_receipt(&retirement_path)?;
        if receipt.generation != generation
            || receipt.embedded_commit != EMBEDDED_COMMIT
            || !receipt.provider_revocation_confirmed
        {
            bail!("existing retirement receipt does not match this generation and helper")
        }
        finish_retired_backup_cleanup(&paths.backup_root, &backup)?;
        println!("backup retirement: already confirmed generation={generation}");
        return Ok(());
    }

    validate_generation_directory(&backup)?;
    verify_snapshot(&backup)?;
    let journal_path = backup.join("journal.json");
    validate_secure_file(&journal_path, true)?;
    let journal: RecoveryJournal = serde_json::from_slice(&fs::read(&journal_path)?)
        .context("decode terminal Hermes journal")?;
    if journal.schema_version != "nblb.hermes-journal.v1" || journal.state != "reconciled" {
        bail!("only a reconciled Hermes generation can be retired")
    }
    let client_id = journal
        .client_id
        .context("reconciled Hermes journal is missing its client ID")?;
    let cutover_path = paths.receipt_root.join(format!("{generation}.json"));
    validate_secure_file(&cutover_path, true)?;
    let cutover: SafeReceipt = serde_json::from_slice(&fs::read(&cutover_path)?)
        .context("decode Hermes cutover receipt")?;
    if cutover.schema_version != "nblb.hermes-cutover.v1"
        || cutover.generation != generation
        || cutover.embedded_commit != EMBEDDED_COMMIT
        || cutover.client_id != client_id
        || !cutover.doctor
        || !cutover.marker
        || !cutover.tool
        || !cutover.rollback_rehearsal
    {
        bail!("Hermes cutover receipt is not terminal or does not match the journal")
    }
    let manifest: SnapshotManifest =
        serde_json::from_slice(&fs::read(backup.join("manifest.json"))?)
            .context("decode Hermes snapshot manifest")?;
    let retirement = RetirementReceipt {
        schema_version: "nblb.hermes-backup-retirement.v1".into(),
        generation,
        embedded_commit: EMBEDDED_COMMIT.into(),
        cutover_client_id: client_id,
        snapshot_env_sha256: manifest.env_sha256,
        snapshot_config_sha256: manifest.config_sha256,
        provider_revocation_confirmed: true,
        retired_at: chrono::Utc::now(),
    };
    atomic_write(
        &retirement_path,
        &serde_json::to_vec_pretty(&retirement)?,
        0o600,
    )?;
    sync_directory(&paths.retirement_root)?;
    finish_retired_backup_cleanup(&paths.backup_root, &backup)?;
    println!("backup retirement: committed generation={generation}");
    Ok(())
}

fn read_retirement_receipt(path: &Path) -> Result<RetirementReceipt> {
    validate_secure_file(path, true)?;
    let receipt: RetirementReceipt =
        serde_json::from_slice(&fs::read(path)?).context("decode retirement receipt")?;
    if receipt.schema_version != "nblb.hermes-backup-retirement.v1" {
        bail!("unsupported Hermes backup retirement receipt")
    }
    Ok(receipt)
}

fn validate_generation_directory(path: &Path) -> Result<()> {
    validate_generation_directory_with_identity(path, 0, 0)
}

fn validate_generation_directory_with_identity(
    path: &Path,
    expected_uid: u32,
    expected_gid: u32,
) -> Result<()> {
    let metadata = fs::symlink_metadata(path)
        .with_context(|| format!("inspect Hermes generation {}", path.display()))?;
    if !metadata.is_dir()
        || metadata.file_type().is_symlink()
        || metadata.uid() != expected_uid
        || metadata.gid() != expected_gid
        || metadata.mode() & 0o777 != 0o700
    {
        bail!("Hermes generation must be a root:root non-symlink directory with mode 0700")
    }
    reject_mountpoint(path).context("validate Hermes generation mount boundary")
}

fn finish_retired_backup_cleanup(backup_root: &Path, backup: &Path) -> Result<()> {
    if !backup.exists() {
        sync_directory(backup_root)?;
        return Ok(());
    }
    validate_generation_directory(backup)?;
    let allowed = ["hermes.env", "config.yaml", "manifest.json", "journal.json"];
    for entry in fs::read_dir(backup).context("inspect retired Hermes generation")? {
        let entry = entry?;
        let name = entry
            .file_name()
            .into_string()
            .map_err(|_| anyhow::anyhow!("retired Hermes generation contains a non-UTF8 entry"))?;
        if !allowed.contains(&name.as_str()) {
            bail!("retired Hermes generation contains an unexpected entry")
        }
        let metadata = fs::symlink_metadata(entry.path())?;
        if !metadata.is_file() || metadata.file_type().is_symlink() {
            bail!("retired Hermes generation contains a non-regular entry")
        }
    }
    for name in allowed {
        let path = backup.join(name);
        if path.exists() {
            validate_secure_file(&path, true)?;
            fs::remove_file(&path).with_context(|| format!("remove retired {name}"))?;
        }
    }
    sync_directory(backup)?;
    fs::remove_dir(backup).context("remove empty retired Hermes generation")?;
    sync_directory(backup_root)
}

fn update_dotenv(existing: &str, key: &str, value: &str) -> String {
    let mut found = false;
    let mut lines = existing
        .lines()
        .map(|line| {
            if line
                .split_once('=')
                .is_some_and(|(name, _)| name.trim() == key)
            {
                found = true;
                format!("{key}={value}")
            } else {
                line.to_owned()
            }
        })
        .collect::<Vec<_>>();
    if !found {
        lines.push(format!("{key}={value}"));
    }
    let mut result = lines.join("\n");
    result.push('\n');
    result
}

fn update_yaml(existing: &str) -> Result<String> {
    if existing.lines().any(|line| line.contains('\t')) {
        bail!("Hermes YAML must not use tab indentation")
    }
    let mut lines = existing.lines().map(str::to_owned).collect::<Vec<_>>();
    let start = match lines.iter().position(|line| line.trim_end() == "model:") {
        Some(index) => index,
        None => {
            if lines.last().is_some_and(|line| !line.is_empty()) {
                lines.push(String::new());
            }
            lines.push("model:".to_owned());
            lines.len() - 1
        }
    };
    if lines[start].starts_with(char::is_whitespace) {
        bail!("Hermes model mapping must be at the YAML root")
    }
    let end = lines
        .iter()
        .enumerate()
        .skip(start + 1)
        .find(|(_, line)| !line.is_empty() && !line.starts_with(' ') && !line.starts_with('#'))
        .map(|(index, _)| index)
        .unwrap_or(lines.len());
    let mut insert_at = end;
    for (key, value) in [
        ("provider", "nvidia"),
        ("default", "z-ai/glm-5.2"),
        ("base_url", "http://127.0.0.1:2456/v1"),
    ] {
        let prefix = format!("  {key}:");
        let matches = (start + 1..insert_at)
            .filter(|index| lines[*index].starts_with(&prefix))
            .collect::<Vec<_>>();
        if matches.len() > 1 {
            bail!("Hermes model mapping contains duplicate {key} fields")
        }
        if let Some(index) = matches.first().copied() {
            lines[index] = format!("  {key}: {value}");
        } else {
            lines.insert(insert_at, format!("  {key}: {value}"));
            insert_at += 1;
        }
    }
    let mut result = lines.join("\n");
    result.push('\n');
    Ok(result)
}

fn atomic_write(path: &Path, bytes: &[u8], mode: u32) -> Result<()> {
    let parent = path.parent().context("atomic target has no parent")?;
    let temporary = parent.join(format!(
        ".{}.{}.tmp",
        path.file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("file"),
        Uuid::new_v4()
    ));
    let result = (|| {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(mode)
            .open(&temporary)
            .context("create atomic temporary file")?;
        file.write_all(bytes)
            .context("write atomic temporary file")?;
        file.sync_all().context("sync atomic temporary file")?;
        fs::set_permissions(&temporary, fs::Permissions::from_mode(mode))?;
        fs::rename(&temporary, path).context("replace atomic target")?;
        sync_directory(parent)
    })();
    if result.is_err() && temporary.exists() {
        let _ = fs::remove_file(&temporary);
        let _ = sync_directory(parent);
    }
    result
}

fn sync_directory(path: &Path) -> Result<()> {
    File::open(path)
        .and_then(|file| file.sync_all())
        .with_context(|| format!("sync directory {}", path.display()))
}

fn read_secret(path: &Path) -> Result<String> {
    validate_secure_file(path, false)?;
    let value = fs::read_to_string(path).context("read root-only secret")?;
    let value = value.trim_end_matches(['\r', '\n']).to_owned();
    if value.is_empty() {
        bail!("root-only secret is empty")
    }
    Ok(value)
}

fn http_client() -> Result<Client> {
    Client::builder()
        .connect_timeout(Duration::from_secs(3))
        .timeout(Duration::from_secs(30))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .context("build loopback HTTP client")
}

async fn admin_get(client: &Client, token: &str, path: &str) -> Result<Value> {
    let response = client
        .get(format!("{ADMIN_BASE}{path}"))
        .bearer_auth(token)
        .send()
        .await
        .context("request admin API")?;
    if !response.status().is_success() {
        bail!("admin API request failed with status {}", response.status())
    }
    response.json().await.context("decode admin API response")
}

async fn validate_qa_run_target(
    client: &Client,
    admin_token: &str,
    requested_id: Uuid,
    expected_commit: &str,
) -> Result<QaRunTarget> {
    validate_qa_run_target_from(
        client,
        admin_token,
        requested_id,
        expected_commit,
        ADMIN_BASE,
    )
    .await
}

async fn validate_qa_run_target_from(
    client: &Client,
    admin_token: &str,
    requested_id: Uuid,
    expected_commit: &str,
    admin_base: &str,
) -> Result<QaRunTarget> {
    let response = client
        .get(format!("{admin_base}/qa/runs/{requested_id}"))
        .bearer_auth(admin_token)
        .send()
        .await
        .context("request Hermes QA run preflight")?;
    if !response.status().is_success() {
        bail!(
            "Hermes QA run preflight failed with status {}",
            response.status()
        )
    }
    let target: QaRunTarget = response
        .json()
        .await
        .context("decode Hermes QA run preflight")?;
    if target.id != requested_id {
        bail!("Hermes QA run preflight returned a different run")
    }
    if target.suite != "hermes-e2e" || !target.live || target.provider_identity != "nvidia_hosted" {
        bail!("Hermes QA run must have NVIDIA hosted live provenance")
    }
    if target.deployment_commit != EMBEDDED_COMMIT || target.deployment_commit != expected_commit {
        bail!("Hermes QA run deployment commit does not match the pinned helper")
    }
    if !matches!(target.status.as_str(), "running" | "passed" | "failed") {
        bail!("Hermes QA run must be running or an exact committed recovery target")
    }
    Ok(target)
}

fn qa_run_intent(status: &str, has_matching_recovery: bool) -> Result<QaRunIntent> {
    match (status, has_matching_recovery) {
        ("running", _) => Ok(QaRunIntent::Start),
        ("passed", true) => Ok(QaRunIntent::RecoverOrConfirmCommitted),
        ("failed", true) => Ok(QaRunIntent::RecoverOrConfirmCommitted),
        ("passed", false) => {
            bail!("passed Hermes QA run has no matching committed local recovery")
        }
        _ => bail!("Hermes QA run is not eligible for apply"),
    }
}

fn post_lock_qa_decision(status: &str, local_reconciled: bool) -> Result<PostLockQaDecision> {
    match (status, local_reconciled) {
        ("running", _) => Ok(PostLockQaDecision::Start),
        ("passed", true) => Ok(PostLockQaDecision::Complete),
        ("passed", false) => bail!("passed Hermes QA run has no reconciled local generation"),
        _ => bail!("Hermes QA run changed to a non-runnable state while waiting for the lock"),
    }
}

fn has_matching_local_generation(
    paths: &Paths,
    run_id: Uuid,
    accepted_states: &[&str],
) -> Result<bool> {
    if !paths.backup_root.exists() {
        return Ok(false);
    }
    validate_existing_root_directory(&paths.backup_root, "Hermes cutover backup root")?;
    for entry in fs::read_dir(&paths.backup_root).context("read Hermes cutover backup root")? {
        let entry = entry.context("read Hermes backup entry")?;
        let file_type = entry.file_type().context("inspect Hermes backup entry")?;
        if file_type.is_symlink() || !file_type.is_dir() {
            bail!(
                "Hermes backup root contains an unexpected non-directory entry: {}",
                entry.path().display()
            )
        }
        if staging_generation(&entry.path()).is_some() {
            continue;
        }
        validate_generation_directory(&entry.path())?;
        cleanup_atomic_temps(
            &entry.path(),
            &["hermes.env", "config.yaml", "manifest.json", "journal.json"],
        )
        .context("clean interrupted Hermes snapshot writes")?;
        verify_snapshot(&entry.path())?;
        let journal = read_recovery_journal(&entry.path())?;
        recovery_action(&journal)?;
        if !accepted_states.contains(&journal.state.as_str()) {
            continue;
        }
        let receipt = load_committed_receipt(paths, &entry.path(), &journal)?;
        if receipt.qa_run_id == Some(run_id) {
            return Ok(true);
        }
    }
    Ok(false)
}

async fn recover_incomplete_cutovers(
    paths: &Paths,
    client: &Client,
    admin_token: &str,
) -> Result<RecoverySummary> {
    let mut summary = RecoverySummary::default();
    if !paths.backup_root.exists() {
        return Ok(summary);
    }
    ensure_root_directory(&paths.backup_root, "Hermes cutover backup root")?;
    let mut backups = Vec::new();
    for entry in fs::read_dir(&paths.backup_root).context("read Hermes cutover backup root")? {
        let entry = entry.context("read Hermes backup entry")?;
        let file_type = entry.file_type().context("inspect Hermes backup entry")?;
        if file_type.is_symlink() || !file_type.is_dir() {
            bail!(
                "Hermes backup root contains an unexpected non-directory entry: {}",
                entry.path().display()
            )
        }
        if staging_generation(&entry.path()).is_some() {
            cleanup_snapshot_stage(&paths.backup_root, &entry.path())?;
            continue;
        }
        validate_generation_directory(&entry.path())?;
        verify_snapshot(&entry.path())?;
        backups.push(entry.path());
    }
    backups.sort();
    for backup in backups {
        let journal = read_recovery_journal(&backup)?;
        match recovery_action(&journal)? {
            RecoveryAction::None => {}
            RecoveryAction::ReconcileCommitted => {
                if let Some(run_id) =
                    reconcile_committed_cutover(paths, client, admin_token, &backup, &journal)
                        .await?
                {
                    summary.reconciled_qa_runs.insert(run_id);
                }
            }
            RecoveryAction::RollbackAndRevoke | RecoveryAction::RevokeAfterRollback => {
                let action = recovery_action(&journal)?;
                if action == RecoveryAction::RollbackAndRevoke {
                    rollback_after_failure(paths, &backup)
                        .context("recover interrupted Hermes cutover")?;
                }
                let client_id = if let Some(id) = journal.client_id {
                    Some(id)
                } else if let Some(label) = journal.operation_label.as_deref() {
                    find_active_client_by_label(client, admin_token, label).await?
                } else {
                    None
                };
                if let Some(id) = client_id {
                    revoke_client(client, admin_token, id)
                        .await
                        .context("revoke interrupted Hermes candidate client")?;
                }
                write_journal(
                    &backup,
                    if action == RecoveryAction::RevokeAfterRollback {
                        "rolled_back"
                    } else {
                        "recovered"
                    },
                    journal.operation_label.as_deref(),
                    client_id,
                )?;
            }
        }
    }
    Ok(summary)
}

fn staging_generation(path: &Path) -> Option<Uuid> {
    path.file_name()
        .and_then(|value| value.to_str())
        .and_then(|value| value.strip_prefix(".staging-"))
        .and_then(|value| Uuid::parse_str(value).ok())
}

fn cleanup_snapshot_stage(backup_root: &Path, stage: &Path) -> Result<()> {
    cleanup_snapshot_stage_with_identity(backup_root, stage, 0, 0)
}

fn cleanup_snapshot_stage_with_identity(
    backup_root: &Path,
    stage: &Path,
    expected_uid: u32,
    expected_gid: u32,
) -> Result<()> {
    if staging_generation(stage).is_none() || stage.parent() != Some(backup_root) {
        bail!("refusing to clean an invalid Hermes snapshot staging path")
    }
    normalize_empty_staging_directory(stage, expected_uid, expected_gid)?;
    validate_generation_directory_with_identity(stage, expected_uid, expected_gid)?;
    for entry in fs::read_dir(stage).context("inspect staged Hermes snapshot")? {
        let entry = entry.context("read staged Hermes snapshot entry")?;
        let name = entry
            .file_name()
            .into_string()
            .map_err(|_| anyhow::anyhow!("staged Hermes snapshot contains a non-UTF8 entry"))?;
        if !snapshot_stage_entry_allowed(&name) {
            bail!("staged Hermes snapshot contains an unexpected entry")
        }
        validate_secure_file_with_identity(&entry.path(), true, expected_uid, expected_gid)?;
        fs::remove_file(entry.path()).context("remove staged Hermes snapshot file")?;
    }
    fs::remove_dir(stage).context("remove staged Hermes snapshot directory")?;
    sync_directory(backup_root)
}

fn normalize_empty_staging_directory(
    stage: &Path,
    expected_uid: u32,
    expected_gid: u32,
) -> Result<()> {
    let metadata = fs::symlink_metadata(stage).context("inspect staged Hermes directory")?;
    if !metadata.is_dir()
        || metadata.file_type().is_symlink()
        || metadata.uid() != expected_uid
        || metadata.gid() != expected_gid
    {
        bail!("staged Hermes directory identity is invalid")
    }
    reject_mountpoint(stage).context("validate staged Hermes mount boundary")?;
    if metadata.mode() & 0o777 != 0o700 {
        if fs::read_dir(stage)
            .context("inspect interrupted staged Hermes directory")?
            .next()
            .is_some()
        {
            bail!("insecure staged Hermes directory is not empty")
        }
        fs::set_permissions(stage, fs::Permissions::from_mode(0o700))
            .context("repair interrupted staged Hermes directory mode")?;
        if let Some(parent) = stage.parent() {
            sync_directory(parent)?;
        }
    }
    Ok(())
}

fn cleanup_atomic_temps(directory: &Path, targets: &[&str]) -> Result<()> {
    cleanup_atomic_temps_with_identity(directory, targets, 0, 0)
}

fn cleanup_atomic_temps_with_identity(
    directory: &Path,
    targets: &[&str],
    expected_uid: u32,
    expected_gid: u32,
) -> Result<()> {
    if !directory.exists() {
        return Ok(());
    }
    validate_directory(directory)?;
    let mut removed = false;
    for entry in fs::read_dir(directory).context("inspect atomic-write directory")? {
        let entry = entry.context("read atomic-write directory entry")?;
        let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
            continue;
        };
        if !targets
            .iter()
            .any(|target| atomic_temp_name_matches(&name, target))
        {
            continue;
        }
        validate_secure_file_with_identity(&entry.path(), true, expected_uid, expected_gid)
            .context("validate interrupted atomic-write temporary")?;
        fs::remove_file(entry.path()).context("remove interrupted atomic-write temporary")?;
        removed = true;
    }
    if removed {
        sync_directory(directory)?;
    }
    Ok(())
}

fn atomic_temp_name_matches(name: &str, target: &str) -> bool {
    name.strip_prefix(&format!(".{target}."))
        .and_then(|value| value.strip_suffix(".tmp"))
        .and_then(|value| Uuid::parse_str(value).ok())
        .is_some()
}

fn snapshot_stage_entry_allowed(name: &str) -> bool {
    const COMPLETE: [&str; 4] = ["hermes.env", "config.yaml", "manifest.json", "journal.json"];
    if COMPLETE.contains(&name) {
        return true;
    }
    COMPLETE.iter().any(|target| {
        name.strip_prefix(&format!(".{target}."))
            .and_then(|rest| rest.strip_suffix(".tmp"))
            .and_then(|value| Uuid::parse_str(value).ok())
            .is_some()
    })
}

fn read_recovery_journal(backup: &Path) -> Result<RecoveryJournal> {
    let journal_path = backup.join("journal.json");
    if !journal_path.exists() {
        bail!(
            "Hermes backup generation has no recovery journal: {}",
            backup.display()
        )
    }
    validate_secure_file(&journal_path, true)?;
    serde_json::from_slice(&fs::read(&journal_path)?).context("decode Hermes recovery journal")
}

fn recovery_action(journal: &RecoveryJournal) -> Result<RecoveryAction> {
    if journal.schema_version != "nblb.hermes-journal.v1" {
        bail!("unsupported Hermes recovery journal schema")
    }
    match journal.state.as_str() {
        "reconciled" | "rolled_back" | "recovered" => Ok(RecoveryAction::None),
        "committed" | "reconcile_pending" => Ok(RecoveryAction::ReconcileCommitted),
        "rollback_cleanup_pending" => {
            if journal.client_id.is_none() {
                bail!("rollback cleanup journal is missing its candidate client")
            }
            Ok(RecoveryAction::RevokeAfterRollback)
        }
        "client_operation_prepared" | "candidate_ready" | "candidate_applied" => {
            Ok(RecoveryAction::RollbackAndRevoke)
        }
        _ => bail!("unknown Hermes recovery journal state"),
    }
}

#[cfg(test)]
fn journal_requires_recovery(state: &str) -> bool {
    !matches!(state, "reconciled" | "rolled_back" | "recovered")
}

async fn reconcile_committed_cutover(
    paths: &Paths,
    client: &Client,
    admin_token: &str,
    backup: &Path,
    journal: &RecoveryJournal,
) -> Result<Option<Uuid>> {
    let receipt = load_committed_receipt(paths, backup, journal)?;
    reconcile_remote_state(client, admin_token, &receipt, ADMIN_BASE).await?;
    write_journal(
        backup,
        "reconciled",
        journal.operation_label.as_deref(),
        Some(receipt.client_id),
    )?;
    Ok(receipt.qa_run_id)
}

fn load_committed_receipt(
    paths: &Paths,
    backup: &Path,
    journal: &RecoveryJournal,
) -> Result<SafeReceipt> {
    let generation = backup
        .file_name()
        .and_then(|value| value.to_str())
        .and_then(|value| Uuid::parse_str(value).ok())
        .context("committed Hermes generation directory is invalid")?;
    let receipt_path = paths.receipt_root.join(format!("{generation}.json"));
    validate_secure_file(&receipt_path, true)?;
    let receipt: SafeReceipt = serde_json::from_slice(&fs::read(&receipt_path)?)
        .context("decode committed Hermes receipt")?;
    if receipt.schema_version != "nblb.hermes-cutover.v1"
        || receipt.generation != generation
        || receipt.embedded_commit != EMBEDDED_COMMIT
        || Some(receipt.client_id) != journal.client_id
    {
        bail!("committed Hermes receipt does not match its journal")
    }
    Ok(receipt)
}

async fn issue_or_rotate_client(
    client: &Client,
    token: &str,
    label: &str,
) -> Result<MutationResponse> {
    let response = client
        .post(format!("{ADMIN_BASE}/clients"))
        .bearer_auth(token)
        .json(&json!({"label":label,"scopes":["models:read","chat:write"]}))
        .send()
        .await
        .context("create Hermes downstream client")?;
    if response.status().is_success() {
        return response
            .json()
            .await
            .context("decode created Hermes client");
    }
    if response.status() != StatusCode::CONFLICT {
        bail!(
            "Hermes client creation failed with status {}",
            response.status()
        )
    }
    let existing = list_clients(client, token)
        .await?
        .into_iter()
        .find(|item| item.active && item.label == label)
        .context("conflicting Hermes client was not found")?;
    client
        .post(format!("{ADMIN_BASE}/clients/{}/rotate", existing.id))
        .bearer_auth(token)
        .send()
        .await?
        .error_for_status()?
        .json()
        .await
        .context("decode rotated Hermes client")
}

async fn revoke_client(client: &Client, token: &str, id: Uuid) -> Result<()> {
    revoke_client_from(client, token, id, ADMIN_BASE).await
}

async fn revoke_client_from(
    client: &Client,
    token: &str,
    id: Uuid,
    admin_base: &str,
) -> Result<()> {
    let response = client
        .post(format!("{admin_base}/clients/{id}/revoke"))
        .bearer_auth(token)
        .send()
        .await
        .context("revoke Hermes downstream client")?;
    if response.status().is_success() || response.status() == StatusCode::CONFLICT {
        return Ok(());
    }
    bail!(
        "Hermes client revoke failed with status {}",
        response.status()
    )
}

async fn list_clients(client: &Client, token: &str) -> Result<Vec<ClientItem>> {
    list_clients_from(client, token, ADMIN_BASE).await
}

async fn list_clients_from(
    client: &Client,
    token: &str,
    admin_base: &str,
) -> Result<Vec<ClientItem>> {
    let mut before: Option<String> = None;
    let mut clients = Vec::new();
    loop {
        let suffix = before
            .as_deref()
            .map(|value| format!("&before={}", percent_encode_query(value)))
            .unwrap_or_default();
        let page: Page<ClientItem> = client
            .get(format!("{admin_base}/clients?limit=100{suffix}"))
            .bearer_auth(token)
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        clients.extend(page.items);
        before = page.next_before;
        if before.is_none() {
            break;
        }
    }
    Ok(clients)
}

fn percent_encode_query(value: &str) -> String {
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

async fn find_active_client_by_label(
    client: &Client,
    token: &str,
    label: &str,
) -> Result<Option<Uuid>> {
    Ok(list_clients(client, token)
        .await?
        .into_iter()
        .find(|item| item.active && item.label == label)
        .map(|item| item.id))
}

async fn active_hermes_client(client: &Client, token: &str) -> Result<Option<Uuid>> {
    let matches = list_clients(client, token)
        .await?
        .into_iter()
        .filter(|item| item.active && item.label.starts_with("hermes-cutover-"))
        .map(|item| item.id)
        .collect::<Vec<_>>();
    match matches.as_slice() {
        [id] => Ok(Some(*id)),
        [] => bail!("standalone verify requires one active Hermes cutover client"),
        _ => bail!("standalone verify found multiple active Hermes cutover clients"),
    }
}

async fn revoke_previous_clients_from(
    client: &Client,
    token: &str,
    keep: Uuid,
    admin_base: &str,
) -> Result<()> {
    for item in list_clients_from(client, token, admin_base).await? {
        if item.id != keep && item.active && item.label.starts_with("hermes-cutover-") {
            revoke_client_from(client, token, item.id, admin_base).await?;
        }
    }
    Ok(())
}

fn docker(args: &[&str]) -> Result<Output> {
    if !allowed_docker_args(args) {
        bail!("docker command is not allowlisted")
    }
    let mut child = Command::new("/usr/bin/docker")
        .args(args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .context("spawn allowlisted docker command")?;
    let stdout = child.stdout.take().context("capture docker stdout")?;
    let stderr = child.stderr.take().context("capture docker stderr")?;
    let stdout_reader = thread::spawn(move || read_bounded_output(stdout));
    let stderr_reader = thread::spawn(move || read_bounded_output(stderr));
    let status = child
        .wait()
        .context("wait for allowlisted docker command")?;
    let (stdout, stdout_exceeded) = stdout_reader
        .join()
        .map_err(|_| anyhow::anyhow!("docker stdout reader panicked"))??;
    let (stderr, stderr_exceeded) = stderr_reader
        .join()
        .map_err(|_| anyhow::anyhow!("docker stderr reader panicked"))??;
    if stdout_exceeded || stderr_exceeded {
        bail!("docker command output exceeded the 64KiB boundary")
    }
    if !status.success() {
        bail!("allowlisted docker command failed")
    }
    Ok(Output {
        status,
        stdout,
        stderr,
    })
}

fn read_bounded_output(mut input: impl Read) -> Result<(Vec<u8>, bool)> {
    let mut retained = Vec::with_capacity(OUTPUT_LIMIT.min(8 * 1024));
    let mut exceeded = false;
    let mut buffer = [0_u8; 8 * 1024];
    loop {
        let read = input.read(&mut buffer).context("read docker output")?;
        if read == 0 {
            break;
        }
        let remaining = OUTPUT_LIMIT.saturating_sub(retained.len());
        retained.extend_from_slice(&buffer[..read.min(remaining)]);
        exceeded |= read > remaining;
    }
    Ok((retained, exceeded))
}

fn allowed_docker_args(args: &[&str]) -> bool {
    matches!(args, ["stop", CONTAINER] | ["start", CONTAINER])
        || matches!(
            args,
            ["logs", APP_CONTAINER | CONTAINER | CLOUDFLARED_CONTAINER]
        )
        || matches!(
            args,
            ["inspect", "--format={{.State.Health.Status}}", CONTAINER]
        )
        || matches!(args, ["logs", "--tail", "100", CONTAINER])
        || matches!(
            args,
            ["logs", "--since", "24h", "--tail", "10000", APP_CONTAINER]
        )
        || matches!(
            args,
            ["inspect", "--format={{json .Config}}", APP_CONTAINER]
        )
        || matches!(
            args,
            [
                "inspect",
                "--format={{json .Config}}",
                CONTAINER | CLOUDFLARED_CONTAINER
            ]
        )
        || matches!(
            args,
            [
                "inspect",
                "--format={{.Image}}",
                APP_CONTAINER | CONTAINER | CLOUDFLARED_CONTAINER
            ]
        )
        || matches!(args, ["image", "inspect", "--format={{json .Config}}", image] if valid_image_id(image))
        || matches!(args, ["history", "--no-trunc", "--format={{json .}}", image] if valid_image_id(image))
        || matches!(args, ["exec", "--workdir", "/tmp", CONTAINER, ..])
            && allowed_exec_args(&args[4..])
}

fn valid_image_id(value: &str) -> bool {
    value.strip_prefix("sha256:").is_some_and(|digest| {
        digest.len() == 64 && digest.bytes().all(|byte| byte.is_ascii_hexdigit())
    })
}

fn allowed_exec_args(args: &[&str]) -> bool {
    matches!(args, ["/opt/hermes/.venv/bin/hermes", "doctor"])
        || matches!(
            args,
            [
                "/opt/hermes/.venv/bin/hermes",
                "chat",
                "-Q",
                "--source",
                "tool",
                "--max-turns",
                "1",
                "-q",
                "Reply with exactly: NBLB_HERMES_OK"
            ]
        )
        || args.first() == Some(&"/opt/hermes/.venv/bin/hermes")
            && args.get(1..8)
                == Some(&["chat", "-Q", "--source", "tool", "--max-turns", "4", "--yolo"])
            && args.get(8..10) == Some(&["-t", "terminal"])
            && args.get(10) == Some(&"-q")
            && args.len() == 12
            && args[11].starts_with("Use the terminal tool exactly once to run printf 'NBLB_TOOL_OK\\n' >> /tmp/nblb-hermes-e2e-")
            && args[11].ends_with(", then reply with exactly: NBLB_TOOL_OK")
        || matches!(args, ["/usr/bin/stat", "--format=%F:%s", path] if valid_marker_path(path))
        || matches!(args, ["/usr/bin/sha256sum", path] if valid_marker_path(path))
        || matches!(args, ["/usr/bin/rm", "-f", "--", path] if valid_marker_path(path))
        || matches!(args, ["/usr/bin/test", "!", "-e", path] if valid_marker_path(path))
}

fn valid_marker_path(path: &str) -> bool {
    path.strip_prefix("/tmp/nblb-hermes-e2e-")
        .and_then(|value| Uuid::parse_str(value).ok())
        .is_some()
}

fn wait_for_container_health() -> Result<()> {
    for _ in 0..30 {
        let output = docker(&["inspect", "--format={{.State.Health.Status}}", CONTAINER])?;
        if String::from_utf8_lossy(&output.stdout).trim() == "healthy" {
            return Ok(());
        }
        std::thread::sleep(Duration::from_secs(2));
    }
    let _ = docker(&["logs", "--tail", "100", CONTAINER]);
    bail!("agent-hermes did not become healthy")
}

fn marker_path(generation: Uuid) -> String {
    format!("/tmp/nblb-hermes-e2e-{generation}")
}

async fn verify_hermes(
    marker: &str,
    client: &Client,
    admin_token: &str,
    client_id: Option<Uuid>,
    _started_at: chrono::DateTime<chrono::Utc>,
) -> Result<Verification> {
    cleanup_marker(marker).context("prepare absent Hermes verification marker")?;
    let result = async {
        let doctor = docker(&[
        "exec",
        "--workdir",
        "/tmp",
        CONTAINER,
        "/opt/hermes/.venv/bin/hermes",
        "doctor",
        ])?
        .status
        .success();
        let marker_started_at = chrono::Utc::now();
        let marker_output = docker(&[
        "exec",
        "--workdir",
        "/tmp",
        CONTAINER,
        "/opt/hermes/.venv/bin/hermes",
        "chat",
        "-Q",
        "--source",
        "tool",
        "--max-turns",
        "1",
        "-q",
        "Reply with exactly: NBLB_HERMES_OK",
        ])?;
        let marker_finished_at = chrono::Utc::now();
        let marker_ok = bounded_text(&marker_output.stdout).trim() == "NBLB_HERMES_OK";
        let prompt = format!(
        "Use the terminal tool exactly once to run printf 'NBLB_TOOL_OK\\n' >> {marker}, then reply with exactly: NBLB_TOOL_OK"
        );
        let tool_started_at = chrono::Utc::now();
        let tool_output = docker(&[
        "exec",
        "--workdir",
        "/tmp",
        CONTAINER,
        "/opt/hermes/.venv/bin/hermes",
        "chat",
        "-Q",
        "--source",
        "tool",
        "--max-turns",
        "4",
        "--yolo",
        "-t",
        "terminal",
        "-q",
        &prompt,
        ])?;
        let tool_finished_at = chrono::Utc::now();
        let tool_reply_ok = bounded_text(&tool_output.stdout).trim() == "NBLB_TOOL_OK";
        let stat = docker(&[
        "exec",
        "--workdir",
        "/tmp",
        CONTAINER,
        "/usr/bin/stat",
        "--format=%F:%s",
        marker,
        ])?;
        let stat_ok = bounded_text(&stat.stdout).trim() == "regular file:13";
        let checksum = docker(&[
        "exec",
        "--workdir",
        "/tmp",
        CONTAINER,
        "/usr/bin/sha256sum",
        marker,
        ])?;
        let expected = hex::encode(Sha256::digest(TOOL_CONTENT));
        let checksum_text = bounded_text(&checksum.stdout);
        let checksum_ok = checksum_text.split_whitespace().next() == Some(expected.as_str());
        let marker_requests = correlated_requests(
            client,
            admin_token,
            client_id,
            marker_started_at,
            marker_finished_at,
            "marker",
        )
        .await?;
        if marker_requests.len() != 1 {
            bail!("Hermes marker phase must correlate exactly one request")
        }
        let tool_requests = correlated_requests(
            client,
            admin_token,
            client_id,
            tool_started_at,
            tool_finished_at,
            "tool",
        )
        .await?;
        if !(2..=4).contains(&tool_requests.len()) {
            bail!("Hermes tool phase must correlate two to four requests")
        }
        let mut requests = marker_requests;
        requests.extend(tool_requests);
        let unique = requests
            .iter()
            .map(|request| request.request_id)
            .collect::<HashSet<_>>();
        if unique.len() != requests.len() {
            bail!("Hermes marker and tool request windows overlap")
        }
        if !doctor || !marker_ok || !tool_reply_ok || !stat_ok || !checksum_ok {
            bail!("Hermes doctor, marker, or exactly-once tool proof failed")
        }
        Ok(Verification {
            doctor,
            marker: marker_ok,
            tool: tool_reply_ok && stat_ok && checksum_ok,
            tool_output_sha256: hex::encode(Sha256::digest(&tool_output.stdout)),
            lb_requests: requests,
        })
    }
    .await;
    let cleanup = cleanup_marker(marker);
    match (result, cleanup) {
        (Ok(verification), Ok(_)) => Ok(verification),
        (Err(error), Ok(())) => Err(error),
        (Ok(_), Err(error)) => Err(error.context("remove and verify Hermes verification marker")),
        (Err(error), Err(cleanup_error)) => bail!(
            "Hermes verification and marker cleanup both failed: verification={}; cleanup={}",
            bounded_error(&error),
            bounded_error(&cleanup_error)
        ),
    }
}

fn cleanup_marker(marker: &str) -> Result<()> {
    let removal = docker(&[
        "exec",
        "--workdir",
        "/tmp",
        CONTAINER,
        "/usr/bin/rm",
        "-f",
        "--",
        marker,
    ])
    .map(|_| ());
    let absence = docker(&[
        "exec",
        "--workdir",
        "/tmp",
        CONTAINER,
        "/usr/bin/test",
        "!",
        "-e",
        marker,
    ])
    .map(|_| ());
    match (removal, absence) {
        (Ok(()), Ok(())) => Ok(()),
        (Err(error), Ok(())) => Err(error.context("remove Hermes verification marker")),
        (Ok(()), Err(error)) => Err(error.context("verify Hermes verification marker absence")),
        (Err(removal_error), Err(absence_error)) => bail!(
            "marker removal and absence verification both failed: removal={}; absence={}",
            bounded_error(&removal_error),
            bounded_error(&absence_error)
        ),
    }
}

fn bounded_error(error: &anyhow::Error) -> String {
    error
        .chain()
        .last()
        .map(ToString::to_string)
        .unwrap_or_else(|| "unknown".to_owned())
        .chars()
        .take(160)
        .collect()
}

async fn reconcile_remote_state(
    client: &Client,
    admin_token: &str,
    receipt: &SafeReceipt,
    admin_base: &str,
) -> Result<()> {
    revoke_previous_clients_from(client, admin_token, receipt.client_id, admin_base).await?;
    if let Some(run_id) = receipt.qa_run_id {
        report_passed_hermes_run_to(client, admin_token, run_id, receipt, admin_base).await?;
    }
    Ok(())
}

async fn report_passed_hermes_run_to(
    client: &Client,
    admin_token: &str,
    run_id: Uuid,
    receipt: &SafeReceipt,
    admin_base: &str,
) -> Result<()> {
    let requests = receipt
        .lb_requests
        .iter()
        .map(|item| {
            json!({
                "request_id":item.request_id,
                "attempt_count":item.attempt_count,
                "outcome":item.outcome,
                "stage":item.stage
            })
        })
        .collect::<Vec<_>>();
    client
        .post(format!("{admin_base}/qa/runs/{run_id}/hermes-completion"))
        .bearer_auth(admin_token)
        .json(&json!({
            "status":"passed",
            "app_commit":receipt.embedded_commit,
            "generation":receipt.generation,
            "client_id":receipt.client_id,
            "doctor":receipt.doctor,
            "exact_marker":receipt.marker,
            "tool_task":receipt.tool,
            "request_correlation":true,
            "secret_scan":&receipt.secret_scan,
            "rollback_rehearsal":receipt.rollback_rehearsal,
            "reconcile_committed":true,
            "duration_ms":receipt.duration_ms,
            "lb_requests":requests
        }))
        .send()
        .await
        .context("submit Hermes QA completion")?
        .error_for_status()
        .context("Hermes QA completion was rejected")?;
    Ok(())
}

async fn report_failed_hermes_run(client: &Client, admin_token: &str, run_id: Uuid) -> Result<()> {
    client
        .post(format!("{ADMIN_BASE}/qa/runs/{run_id}/hermes-completion"))
        .bearer_auth(admin_token)
        .json(&json!({"status":"failed"}))
        .send()
        .await
        .context("submit failed Hermes QA completion")?
        .error_for_status()
        .context("failed Hermes QA completion was rejected")?;
    Ok(())
}

fn bounded_text(bytes: &[u8]) -> String {
    String::from_utf8_lossy(&bytes[..bytes.len().min(OUTPUT_LIMIT)]).into_owned()
}

async fn correlated_requests(
    client: &Client,
    admin_token: &str,
    client_id: Option<Uuid>,
    started_at: chrono::DateTime<chrono::Utc>,
    finished_at: chrono::DateTime<chrono::Utc>,
    stage: &str,
) -> Result<Vec<RequestReceipt>> {
    let client_id = client_id.context("Hermes request correlation requires a client ID")?;
    let mut before: Option<String> = None;
    let mut filtered = Vec::new();
    loop {
        let suffix = before
            .as_deref()
            .map(|value| format!("&before={}", percent_encode_query(value)))
            .unwrap_or_default();
        let page: Page<RequestItem> = client
            .get(format!("{ADMIN_BASE}/requests?limit=100{suffix}"))
            .bearer_auth(admin_token)
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        let oldest = page.items.iter().map(|item| item.started_at).min();
        filtered.extend(page.items.into_iter().filter(|item| {
            item.client_id == Some(client_id)
                && item.started_at >= started_at
                && item.started_at <= finished_at
                && item
                    .finished_at
                    .is_some_and(|finished| finished <= finished_at)
        }));
        before = page.next_before;
        if before.is_none() || oldest.is_some_and(|value| value < started_at) {
            break;
        }
    }
    let mut items = Vec::with_capacity(filtered.len());
    for item in filtered {
        let detail: RequestItem = client
            .get(format!("{ADMIN_BASE}/requests/{}", item.request_id))
            .bearer_auth(admin_token)
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        items.push(RequestReceipt {
            request_id: item.request_id,
            attempt_count: detail.attempts.unwrap_or_default().len(),
            outcome: item.outcome,
            stage: stage.to_owned(),
            started_at: item.started_at,
            finished_at: item
                .finished_at
                .context("correlated Hermes request is not terminal")?,
        });
    }
    items.sort_by_key(|item| (item.started_at, item.request_id));
    if items.iter().any(|item| item.outcome != "succeeded") {
        bail!("Hermes requests could not be correlated to the active downstream client")
    }
    Ok(items)
}

async fn collect_secret_scan(
    paths: &Paths,
    client: &Client,
    admin_token: &str,
    issued_token: &str,
) -> Result<SecretScanReceipt> {
    let persisted = admin_get(client, admin_token, "/qa/secret-scan").await?;
    if persisted["schema_version"].as_str() != Some("nblb.secret-scan.v1") {
        bail!("gateway returned an unsupported secret scan contract")
    }
    let database_matches = persisted["database_matches"]
        .as_u64()
        .context("gateway secret scan omitted database_matches")?;
    let pattern = nvidia_credential_prefix();
    let patterns = sensitive_patterns(issued_token, admin_token);
    let app_counts = docker_pattern_matches(&["logs", APP_CONTAINER], &patterns)?;
    let hermes_counts = docker_pattern_matches(&["logs", CONTAINER], &patterns)?;
    let cloudflared_counts = docker_pattern_matches(&["logs", CLOUDFLARED_CONTAINER], &patterns)?;
    let mut metadata_counts = vec![0_u64; patterns.len()];
    for container in [APP_CONTAINER, CONTAINER, CLOUDFLARED_CONTAINER] {
        let counts = docker_pattern_matches(
            &["inspect", "--format={{json .Config}}", container],
            &patterns,
        )?;
        add_counts(&mut metadata_counts, &counts);
        let image_id = container_image_id(container)?;
        for args in [
            [
                "image",
                "inspect",
                "--format={{json .Config}}",
                image_id.as_str(),
            ],
            [
                "history",
                "--no-trunc",
                "--format={{json .}}",
                image_id.as_str(),
            ],
        ] {
            let counts = docker_pattern_matches(&args, &patterns)?;
            add_counts(&mut metadata_counts, &counts);
        }
    }
    let public_counts = public_bundle_pattern_matches(client, &patterns).await?;
    let admin_counts = admin_response_pattern_matches(client, admin_token, &patterns).await?;
    let executable = env::current_exe().context("locate Hermes helper artifact")?;
    let executable_metadata =
        fs::symlink_metadata(&executable).context("inspect Hermes helper artifact")?;
    if !executable_metadata.is_file() || executable_metadata.file_type().is_symlink() {
        bail!("Hermes helper artifact must be a regular non-symlink file")
    }
    let helper_artifact_matches = [
        pattern.as_slice(),
        issued_token.as_bytes(),
        admin_token.as_bytes(),
    ]
    .into_iter()
    .try_fold(0_u64, |total, candidate| {
        Ok::<u64, anyhow::Error>(
            total.saturating_add(file_pattern_matches(&executable, candidate)?),
        )
    })?;
    let hermes_tree_matches = tree_pattern_matches(&paths.hermes_dir, &pattern)?;
    let issued_token_matches = [
        &app_counts,
        &hermes_counts,
        &cloudflared_counts,
        &metadata_counts,
        &public_counts,
        &admin_counts,
    ]
    .into_iter()
    .map(|counts| counts[3])
    .fold(0_u64, u64::saturating_add);
    let authorization_value_matches = [
        &app_counts,
        &hermes_counts,
        &cloudflared_counts,
        &metadata_counts,
        &public_counts,
        &admin_counts,
    ]
    .into_iter()
    .flat_map(|counts| counts[5..].iter().copied())
    .fold(0_u64, u64::saturating_add);
    let evidence = SecretScanReceipt {
        database_matches,
        app_log_matches: pattern_match_total(&app_counts),
        hermes_log_matches: pattern_match_total(&hermes_counts),
        cloudflared_log_matches: pattern_match_total(&cloudflared_counts),
        image_metadata_matches: pattern_match_total(&metadata_counts),
        helper_artifact_matches,
        hermes_tree_matches,
        public_bundle_matches: pattern_match_total(&public_counts),
        admin_response_matches: pattern_match_total(&admin_counts),
        issued_token_matches,
        authorization_value_matches,
    };
    if evidence.total_matches() != 0 {
        bail!("raw NVIDIA credential matches were found in secret scan surfaces")
    }
    Ok(evidence)
}

fn sensitive_patterns(issued_token: &str, admin_token: &str) -> Vec<Vec<u8>> {
    let downstream_prefix = b"nblb_ds_";
    let admin_prefix = b"nblb_admin_";
    vec![
        nvidia_credential_prefix().to_vec(),
        downstream_prefix.to_vec(),
        admin_prefix.to_vec(),
        issued_token.as_bytes().to_vec(),
        admin_token.as_bytes().to_vec(),
        authorization_pattern(true, &nvidia_credential_prefix()),
        authorization_pattern(true, downstream_prefix),
        authorization_pattern(true, admin_prefix),
        authorization_pattern(false, &nvidia_credential_prefix()),
        authorization_pattern(false, downstream_prefix),
        authorization_pattern(false, admin_prefix),
    ]
}

fn pattern_match_total(counts: &[u64]) -> u64 {
    counts.iter().copied().fold(0_u64, u64::saturating_add)
}

fn container_image_id(container: &str) -> Result<String> {
    let output = docker(&["inspect", "--format={{.Image}}", container])?;
    let image_id = String::from_utf8(output.stdout)
        .context("decode container image ID")?
        .trim()
        .to_owned();
    if !valid_image_id(&image_id) {
        bail!("container returned an invalid image ID")
    }
    Ok(image_id)
}

fn authorization_pattern(title_case: bool, credential_prefix: &[u8]) -> Vec<u8> {
    let mut pattern = if title_case {
        vec![66, 101, 97, 114, 101, 114, 32]
    } else {
        vec![98, 101, 97, 114, 101, 114, 32]
    };
    pattern.extend_from_slice(credential_prefix);
    pattern
}

fn add_counts(total: &mut [u64], next: &[u64]) {
    for (total, next) in total.iter_mut().zip(next) {
        *total = total.saturating_add(*next);
    }
}

fn docker_pattern_matches(args: &[&str], patterns: &[Vec<u8>]) -> Result<Vec<u64>> {
    if !allowed_docker_args(args) {
        bail!("docker scan command is not allowlisted")
    }
    let mut child = Command::new("/usr/bin/docker")
        .args(args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .context("execute allowlisted docker scan command")?;
    let stdout = child.stdout.take().context("capture docker scan stdout")?;
    let stderr = child.stderr.take().context("capture docker scan stderr")?;
    let stdout_patterns = patterns.to_vec();
    let stderr_patterns = patterns.to_vec();
    let stdout_scan = std::thread::spawn(move || count_patterns_reader(stdout, &stdout_patterns));
    let stderr_scan = std::thread::spawn(move || count_patterns_reader(stderr, &stderr_patterns));
    let status = child.wait().context("wait for docker scan command")?;
    let mut counts = stdout_scan
        .join()
        .map_err(|_| anyhow::anyhow!("docker stdout scan panicked"))??;
    let stderr_counts = stderr_scan
        .join()
        .map_err(|_| anyhow::anyhow!("docker stderr scan panicked"))??;
    if !status.success() {
        bail!("allowlisted docker scan command failed")
    }
    add_counts(&mut counts, &stderr_counts);
    Ok(counts)
}

fn count_patterns_reader(mut reader: impl Read, patterns: &[Vec<u8>]) -> Result<Vec<u64>> {
    if patterns.iter().any(Vec::is_empty) {
        bail!("secret scan patterns must not be empty")
    }
    let mut buffer = [0_u8; 64 * 1024];
    let mut overlaps = vec![Vec::new(); patterns.len()];
    let mut counts = vec![0_u64; patterns.len()];
    loop {
        let read = reader.read(&mut buffer)?;
        if read == 0 {
            return Ok(counts);
        }
        for (index, pattern) in patterns.iter().enumerate() {
            let mut window = Vec::with_capacity(overlaps[index].len() + read);
            window.extend_from_slice(&overlaps[index]);
            window.extend_from_slice(&buffer[..read]);
            counts[index] = counts[index].saturating_add(count_pattern(&window, pattern));
            let retained = pattern.len().saturating_sub(1).min(window.len());
            overlaps[index].clear();
            overlaps[index].extend_from_slice(&window[window.len() - retained..]);
        }
    }
}

async fn bounded_response_bytes(response: reqwest::Response, label: &str) -> Result<Vec<u8>> {
    let response = response
        .error_for_status()
        .with_context(|| format!("load {label}"))?;
    let mut stream = response.bytes_stream();
    let mut bytes = Vec::new();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.with_context(|| format!("read {label}"))?;
        if bytes.len().saturating_add(chunk.len()) > 16 * 1024 * 1024 {
            bail!("{label} exceeded the 16MiB scan boundary")
        }
        bytes.extend_from_slice(&chunk);
    }
    Ok(bytes)
}

async fn public_bundle_pattern_matches(client: &Client, patterns: &[Vec<u8>]) -> Result<Vec<u64>> {
    let index =
        bounded_response_bytes(client.get(PUBLIC_BASE).send().await?, "public index").await?;
    let mut counts = count_patterns_reader(index.as_slice(), patterns)?;
    for asset in public_asset_paths(&index)? {
        let bytes = bounded_response_bytes(
            client.get(format!("{PUBLIC_BASE}{asset}")).send().await?,
            "public bundle asset",
        )
        .await?;
        let next = count_patterns_reader(bytes.as_slice(), patterns)?;
        add_counts(&mut counts, &next);
    }
    for path in [
        "/api/public/v1/summary",
        "/api/public/v1/openapi.json",
        "/api/public/v1/metrics?window=24h&step=1h",
        "/api/public/v1/models",
    ] {
        let bytes = bounded_response_bytes(
            client.get(format!("{PUBLIC_BASE}{path}")).send().await?,
            "public API response",
        )
        .await?;
        add_counts(
            &mut counts,
            &count_patterns_reader(bytes.as_slice(), patterns)?,
        );
    }
    let mut before: Option<String> = None;
    for _ in 0..100 {
        let suffix = before
            .as_deref()
            .map(|value| format!("&before={}", percent_encode_query(value)))
            .unwrap_or_default();
        let bytes = bounded_response_bytes(
            client
                .get(format!(
                    "{PUBLIC_BASE}/api/public/v1/incidents?limit=100{suffix}"
                ))
                .send()
                .await?,
            "public incident response",
        )
        .await?;
        add_counts(
            &mut counts,
            &count_patterns_reader(bytes.as_slice(), patterns)?,
        );
        let page: Value = serde_json::from_slice(&bytes).context("decode public incident page")?;
        if let Some(items) = page.get("items").and_then(Value::as_array) {
            for slug in items
                .iter()
                .filter_map(|item| item.get("slug").and_then(Value::as_str))
            {
                let detail = bounded_response_bytes(
                    client
                        .get(format!("{PUBLIC_BASE}/api/public/v1/incidents/{slug}"))
                        .send()
                        .await?,
                    "public incident detail",
                )
                .await?;
                add_counts(
                    &mut counts,
                    &count_patterns_reader(detail.as_slice(), patterns)?,
                );
            }
        }
        before = page
            .get("next_before")
            .and_then(Value::as_str)
            .map(str::to_owned);
        if before.is_none() {
            return Ok(counts);
        }
    }
    bail!("public incident pagination exceeded 100 pages")
}

fn public_asset_paths(index: &[u8]) -> Result<Vec<String>> {
    let text = String::from_utf8_lossy(index);
    let mut assets = text
        .split(['\'', '"'])
        .filter(|value| {
            value.starts_with("/_app/")
                && !value.contains("..")
                && !value.contains(['\r', '\n', '\\'])
        })
        .map(str::to_owned)
        .collect::<Vec<_>>();
    assets.sort();
    assets.dedup();
    if assets.len() > 128 {
        bail!("public index referenced too many static assets")
    }
    Ok(assets)
}

async fn admin_response_pattern_matches(
    client: &Client,
    admin_token: &str,
    patterns: &[Vec<u8>],
) -> Result<Vec<u64>> {
    let mut counts = vec![0_u64; patterns.len()];
    for path in [
        "/overview",
        "/attentions",
        "/openapi.json",
        "/routing/policy",
        "/models",
        "/settings",
        "/qa/secret-scan",
    ] {
        let bytes = bounded_response_bytes(
            client
                .get(format!("{ADMIN_BASE}{path}"))
                .bearer_auth(admin_token)
                .send()
                .await?,
            "admin API response",
        )
        .await?;
        let next = count_patterns_reader(bytes.as_slice(), patterns)?;
        add_counts(&mut counts, &next);
    }
    for (path, detail_prefix, id_field) in [
        ("/upstreams", Some("/upstreams"), "id"),
        ("/clients", Some("/clients"), "id"),
        ("/requests", Some("/requests"), "request_id"),
        ("/probes", Some("/probes"), "id"),
        ("/incidents", None, "id"),
        ("/audit", None, "id"),
        ("/qa/runs", Some("/qa/runs"), "id"),
    ] {
        scan_admin_collection(
            client,
            admin_token,
            patterns,
            &mut counts,
            path,
            detail_prefix,
            id_field,
        )
        .await?;
    }
    Ok(counts)
}

async fn scan_admin_collection(
    client: &Client,
    admin_token: &str,
    patterns: &[Vec<u8>],
    counts: &mut [u64],
    path: &str,
    detail_prefix: Option<&str>,
    id_field: &str,
) -> Result<()> {
    let mut before: Option<String> = None;
    for _ in 0..100 {
        let suffix = before
            .as_deref()
            .map(|value| format!("&before={}", percent_encode_query(value)))
            .unwrap_or_default();
        let bytes = bounded_response_bytes(
            client
                .get(format!("{ADMIN_BASE}{path}?limit=100{suffix}"))
                .bearer_auth(admin_token)
                .send()
                .await?,
            "admin collection response",
        )
        .await?;
        add_counts(counts, &count_patterns_reader(bytes.as_slice(), patterns)?);
        let page: Value = serde_json::from_slice(&bytes).context("decode admin collection page")?;
        if let (Some(prefix), Some(items)) =
            (detail_prefix, page.get("items").and_then(Value::as_array))
        {
            for id in items
                .iter()
                .filter_map(|item| item.get(id_field).and_then(Value::as_str))
            {
                let detail = bounded_response_bytes(
                    client
                        .get(format!("{ADMIN_BASE}{prefix}/{id}"))
                        .bearer_auth(admin_token)
                        .send()
                        .await?,
                    "admin detail response",
                )
                .await?;
                add_counts(counts, &count_patterns_reader(detail.as_slice(), patterns)?);
            }
        }
        before = page
            .get("next_before")
            .and_then(Value::as_str)
            .map(str::to_owned);
        if before.is_none() {
            return Ok(());
        }
    }
    bail!("admin collection pagination exceeded 100 pages")
}

fn nvidia_credential_prefix() -> [u8; 6] {
    [110, 118, 97, 112, 105, 45]
}

fn scan_for_direct_nvidia_key(root: &Path) -> Result<()> {
    let matches = tree_pattern_matches(root, &nvidia_credential_prefix())?;
    if matches != 0 {
        bail!("Hermes data tree still contains a direct NVIDIA credential")
    }
    Ok(())
}

fn tree_pattern_matches(root: &Path, pattern: &[u8]) -> Result<u64> {
    let mut pending = vec![root.to_path_buf()];
    let mut matches = 0_u64;
    while let Some(path) = pending.pop() {
        let metadata = fs::symlink_metadata(&path)?;
        if metadata.file_type().is_symlink() {
            bail!("Hermes data tree contains a symlink: {}", path.display())
        }
        if metadata.is_dir() {
            for entry in fs::read_dir(&path)? {
                pending.push(entry?.path());
            }
        } else if metadata.is_file() {
            matches = matches.saturating_add(file_pattern_matches(&path, pattern)?);
        } else {
            bail!(
                "Hermes data tree contains a special file: {}",
                path.display()
            )
        }
    }
    Ok(matches)
}

#[cfg(test)]
fn file_contains_pattern(path: &Path, pattern: &[u8]) -> Result<bool> {
    Ok(file_pattern_matches(path, pattern)? != 0)
}

fn file_pattern_matches(path: &Path, pattern: &[u8]) -> Result<u64> {
    let mut file = File::open(path).with_context(|| format!("open {}", path.display()))?;
    let mut buffer = [0_u8; 64 * 1024];
    let mut overlap = Vec::new();
    let mut matches = 0_u64;
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            return Ok(matches);
        }
        let mut window = Vec::with_capacity(overlap.len() + read);
        window.extend_from_slice(&overlap);
        window.extend_from_slice(&buffer[..read]);
        matches = matches.saturating_add(
            window
                .windows(pattern.len())
                .filter(|candidate| *candidate == pattern)
                .count() as u64,
        );
        let retained = pattern.len().saturating_sub(1).min(window.len());
        overlap.clear();
        overlap.extend_from_slice(&window[window.len() - retained..]);
    }
}

fn count_pattern(bytes: &[u8], pattern: &[u8]) -> u64 {
    bytes
        .windows(pattern.len())
        .filter(|candidate| *candidate == pattern)
        .count() as u64
}

fn sha256_file(path: &Path) -> Result<String> {
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 16 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(hex::encode(hasher.finalize()))
}

fn write_journal(
    directory: &Path,
    state: &str,
    label: Option<&str>,
    client_id: Option<Uuid>,
) -> Result<()> {
    let value = json!({
        "schema_version":"nblb.hermes-journal.v1",
        "state":state,
        "operation_label":label,
        "client_id":client_id,
        "updated_at":chrono::Utc::now()
    });
    atomic_write(
        &directory.join("journal.json"),
        &serde_json::to_vec_pretty(&value)?,
        0o600,
    )
}

fn write_receipt(paths: &Paths, receipt: &SafeReceipt) -> Result<()> {
    ensure_root_directory(&paths.receipt_root, "Hermes cutover receipt root")?;
    atomic_write(
        &paths
            .receipt_root
            .join(format!("{}.json", receipt.generation)),
        &serde_json::to_vec_pretty(receipt)?,
        0o600,
    )
}

#[cfg(test)]
mod tests {
    use super::{
        CutoverFailure, CutoverFailureDisposition, EMBEDDED_COMMIT, RecoveryAction,
        RecoveryJournal, RequestReceipt, SafeReceipt, SecretScanReceipt, SnapshotManifest,
        allowed_docker_args, cleanup_atomic_temps_with_identity,
        cleanup_snapshot_stage_with_identity, count_patterns_reader, create_secure_directory,
        file_contains_pattern, journal_requires_recovery, list_clients_from, parse_action,
        parse_options, percent_encode_query, post_lock_qa_decision, public_asset_paths,
        qa_run_intent, read_bounded_output, read_recovery_journal, reconcile_remote_state,
        recovery_action, sensitive_patterns, sha256_file, snapshot_manifest_matches,
        snapshot_stage_entry_allowed, staging_generation, update_dotenv, update_yaml,
        validate_qa_run_target_from,
    };
    use actix_web::{App, HttpRequest, HttpResponse, HttpServer, web};
    use std::{
        fs,
        io::Cursor,
        os::unix::fs::{MetadataExt, PermissionsExt},
        sync::{
            Mutex,
            atomic::{AtomicBool, AtomicU16, AtomicUsize, Ordering},
        },
    };
    use uuid::Uuid;

    struct ReconcileFixture {
        old_id: Uuid,
        keep_id: Uuid,
        run_id: Uuid,
        fail_revoke: AtomicBool,
        events: Mutex<Vec<String>>,
    }

    struct QaTargetFixture {
        body: Mutex<serde_json::Value>,
        status: AtomicU16,
        requests: AtomicUsize,
    }

    async fn qa_target_response(state: web::Data<QaTargetFixture>) -> HttpResponse {
        state.requests.fetch_add(1, Ordering::SeqCst);
        let status = actix_web::http::StatusCode::from_u16(state.status.load(Ordering::SeqCst))
            .expect("fixture status");
        HttpResponse::build(status).json(state.body.lock().expect("lock QA target body").clone())
    }

    async fn reconcile_clients(state: web::Data<ReconcileFixture>) -> HttpResponse {
        HttpResponse::Ok().json(serde_json::json!({
            "items":[
                {"id":state.old_id,"label":"hermes-cutover-old","active":true},
                {"id":state.keep_id,"label":"hermes-cutover-current","active":true}
            ],
            "next_before":null
        }))
    }

    async fn reconcile_revoke(
        state: web::Data<ReconcileFixture>,
        id: web::Path<Uuid>,
    ) -> HttpResponse {
        if *id != state.old_id {
            return HttpResponse::NotFound().finish();
        }
        if state.fail_revoke.load(Ordering::SeqCst) {
            state
                .events
                .lock()
                .expect("lock failed reconcile events")
                .push("revoke_failed".into());
            return HttpResponse::InternalServerError().finish();
        }
        state
            .events
            .lock()
            .expect("lock reconcile events")
            .push("revoke".into());
        HttpResponse::Ok().finish()
    }

    async fn reconcile_completion(
        state: web::Data<ReconcileFixture>,
        run_id: web::Path<Uuid>,
        body: web::Json<serde_json::Value>,
    ) -> HttpResponse {
        let mut events = state.events.lock().expect("lock completion events");
        if *run_id != state.run_id
            || events.as_slice() != ["revoke"]
            || body["status"] != "passed"
            || body["client_id"] != state.keep_id.to_string()
            || body["reconcile_committed"] != true
        {
            return HttpResponse::Conflict().finish();
        }
        events.push("report".into());
        HttpResponse::Ok().finish()
    }

    #[test]
    fn command_surface_and_allowlist_fail_closed() {
        assert!(parse_action(["preflight".to_owned()].into_iter()).is_ok());
        assert!(parse_action(["retire-backup".to_owned()].into_iter()).is_ok());
        assert!(parse_action(std::iter::empty()).is_err());
        assert!(parse_action(["unknown".to_owned()].into_iter()).is_err());
        assert!(allowed_docker_args(&["stop", "agent-hermes"]));
        assert!(allowed_docker_args(&["start", "agent-hermes"]));
        assert!(allowed_docker_args(&["logs", "nvidia-build-lb-app-1"]));
        assert!(allowed_docker_args(&["logs", "agent-hermes"]));
        assert!(allowed_docker_args(&["logs", "cloudflared-apps"]));
        assert!(allowed_docker_args(&[
            "inspect",
            "--format={{json .Config}}",
            "nvidia-build-lb-app-1",
        ]));
        let image_id = format!("sha256:{}", "a".repeat(64));
        assert!(allowed_docker_args(&[
            "inspect",
            "--format={{.Image}}",
            "nvidia-build-lb-app-1",
        ]));
        assert!(allowed_docker_args(&[
            "image",
            "inspect",
            "--format={{json .Config}}",
            &image_id,
        ]));
        assert!(allowed_docker_args(&[
            "history",
            "--no-trunc",
            "--format={{json .}}",
            &image_id,
        ]));
        assert!(!allowed_docker_args(&[
            "history",
            "--no-trunc",
            "--format={{json .}}",
            "latest",
        ]));
        assert!(!allowed_docker_args(&["stop", "codex-lb"]));
        assert!(!allowed_docker_args(&["exec", "agent-hermes", "sh"]));
        assert!(!allowed_docker_args(&["logs", "codex-lb"]));
        assert!(!allowed_docker_args(&[
            "inspect",
            "--format={{json .Config}}",
            "codex-lb",
        ]));
        let run_id = Uuid::new_v4();
        let options = parse_options(vec![
            "apply".to_owned(),
            "--qa-run".to_owned(),
            run_id.to_string(),
        ])
        .expect("parse QA-linked apply");
        assert_eq!(options.qa_run_id, Some(run_id));
        assert!(parse_options(vec!["apply".to_owned()]).is_err());
        assert!(
            parse_options(vec![
                "verify".to_owned(),
                "--qa-run".to_owned(),
                run_id.to_string(),
            ])
            .is_err()
        );
        let generation = Uuid::new_v4();
        let retirement = parse_options(vec![
            "retire-backup".to_owned(),
            "--generation".to_owned(),
            generation.to_string(),
            "--confirm-provider-revoked".to_owned(),
        ])
        .expect("parse explicit backup retirement");
        assert_eq!(retirement.generation, Some(generation));
        assert!(retirement.provider_revoked);
        assert!(
            parse_options(vec![
                "retire-backup".to_owned(),
                "--generation".to_owned(),
                generation.to_string(),
            ])
            .is_err(),
            "provider revocation confirmation is mandatory",
        );
    }

    #[actix_web::test]
    async fn qa_run_preflight_requires_exact_live_recoverable_commit_identity() {
        let run_id = Uuid::new_v4();
        let valid = serde_json::json!({
            "id": run_id,
            "suite": "hermes-e2e",
            "live": true,
            "provider_identity": "nvidia_hosted",
            "deployment_commit": EMBEDDED_COMMIT,
            "status": "running"
        });
        let fixture = web::Data::new(QaTargetFixture {
            body: Mutex::new(valid.clone()),
            status: AtomicU16::new(200),
            requests: AtomicUsize::new(0),
        });
        let fixture_evidence = fixture.clone();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind QA target fixture");
        let address = listener.local_addr().expect("QA target fixture address");
        let server = HttpServer::new(move || {
            App::new()
                .app_data(fixture.clone())
                .route("/qa/runs/{id}", web::get().to(qa_target_response))
        })
        .listen(listener)
        .expect("listen QA target fixture")
        .run();
        let handle = server.handle();
        actix_web::rt::spawn(server);
        let base = format!("http://{address}");
        let client = reqwest::Client::new();

        validate_qa_run_target_from(&client, "admin", run_id, EMBEDDED_COMMIT, &base)
            .await
            .expect("accept exact QA target");
        *fixture_evidence
            .body
            .lock()
            .expect("replace QA target with passed recovery") = serde_json::json!({
            "id": run_id,
            "suite": "hermes-e2e",
            "live": true,
            "provider_identity": "nvidia_hosted",
            "deployment_commit": EMBEDDED_COMMIT,
            "status": "passed"
        });
        validate_qa_run_target_from(&client, "admin", run_id, EMBEDDED_COMMIT, &base)
            .await
            .expect("accept passed identity for the local committed-recovery gate");
        *fixture_evidence
            .body
            .lock()
            .expect("replace QA target with failed committed recovery") = serde_json::json!({
            "id": run_id,
            "suite": "hermes-e2e",
            "live": true,
            "provider_identity": "nvidia_hosted",
            "deployment_commit": EMBEDDED_COMMIT,
            "status": "failed"
        });
        validate_qa_run_target_from(&client, "admin", run_id, EMBEDDED_COMMIT, &base)
            .await
            .expect("defer failed-run eligibility to the exact local receipt gate");
        let invalid_shapes = [
            serde_json::json!({"id":Uuid::new_v4(),"suite":"hermes-e2e","live":true,"provider_identity":"nvidia_hosted","deployment_commit":EMBEDDED_COMMIT,"status":"running"}),
            serde_json::json!({"id":run_id,"suite":"smoke","live":true,"provider_identity":"nvidia_hosted","deployment_commit":EMBEDDED_COMMIT,"status":"running"}),
            serde_json::json!({"id":run_id,"suite":"hermes-e2e","live":false,"provider_identity":"fake","deployment_commit":EMBEDDED_COMMIT,"status":"running"}),
            serde_json::json!({"id":run_id,"suite":"hermes-e2e","live":true,"provider_identity":"unverified","deployment_commit":EMBEDDED_COMMIT,"status":"running"}),
            serde_json::json!({"id":run_id,"suite":"hermes-e2e","live":true,"provider_identity":"nvidia_hosted","deployment_commit":EMBEDDED_COMMIT,"status":"queued"}),
            serde_json::json!({"id":run_id,"suite":"hermes-e2e","live":true,"provider_identity":"nvidia_hosted","deployment_commit":"different","status":"running"}),
        ];
        for body in invalid_shapes {
            *fixture_evidence
                .body
                .lock()
                .expect("replace QA target body") = body;
            assert!(
                validate_qa_run_target_from(&client, "admin", run_id, EMBEDDED_COMMIT, &base)
                    .await
                    .is_err()
            );
        }
        fixture_evidence.status.store(404, Ordering::SeqCst);
        assert!(
            validate_qa_run_target_from(&client, "admin", run_id, EMBEDDED_COMMIT, &base)
                .await
                .is_err()
        );
        assert_eq!(fixture_evidence.requests.load(Ordering::SeqCst), 10);
        handle.stop(true).await;
    }

    #[test]
    fn passed_qa_run_reenters_only_for_an_exact_committed_recovery() {
        assert_eq!(
            qa_run_intent("running", false).expect("running run starts"),
            super::QaRunIntent::Start
        );
        assert_eq!(
            qa_run_intent("passed", true).expect("passed run recovers exact commit"),
            super::QaRunIntent::RecoverOrConfirmCommitted
        );
        assert_eq!(
            qa_run_intent("failed", true).expect("failed run recovers exact committed receipt"),
            super::QaRunIntent::RecoverOrConfirmCommitted
        );
        assert!(qa_run_intent("passed", false).is_err());
        assert!(qa_run_intent("failed", false).is_err());
        assert!(qa_run_intent("queued", true).is_err());
    }

    #[test]
    fn post_lock_revalidation_prevents_a_second_generation_for_the_same_run() {
        assert_eq!(
            post_lock_qa_decision("running", false).expect("unchanged run may start"),
            super::PostLockQaDecision::Start
        );
        assert_eq!(
            post_lock_qa_decision("passed", true)
                .expect("concurrent winner is an idempotent success"),
            super::PostLockQaDecision::Complete
        );
        assert!(post_lock_qa_decision("passed", false).is_err());
        assert!(post_lock_qa_decision("failed", true).is_err());
    }

    #[test]
    fn cutover_failure_disposition_preserves_the_commit_boundary() {
        let precommit = CutoverFailure::precommit(anyhow::anyhow!("before commit"));
        let committed = CutoverFailure::committed(anyhow::anyhow!("reconcile later"));
        assert_eq!(precommit.disposition, CutoverFailureDisposition::PreCommit);
        assert_eq!(
            committed.disposition,
            CutoverFailureDisposition::CommittedReconcilePending
        );
        assert!(committed.to_string().contains("reconcile later"));
    }

    #[test]
    fn configuration_update_is_exact_and_idempotent() {
        let env = update_dotenv("A=1\nNVIDIA_API_KEY=old\n", "NVIDIA_API_KEY", "issued_new");
        assert_eq!(env.matches("NVIDIA_API_KEY=").count(), 1);
        assert!(env.contains("NVIDIA_API_KEY=issued_new"));
        let yaml = update_yaml("model:\n  provider: custom\n  default: old\n").expect("valid YAML");
        let second = update_yaml(&yaml).expect("idempotent YAML");
        assert_eq!(yaml, second);
        assert!(yaml.contains("provider: nvidia"));
        assert!(yaml.contains("default: z-ai/glm-5.2"));
        assert!(yaml.contains("base_url: http://127.0.0.1:2456/v1"));
    }

    #[test]
    fn recovery_state_and_cursor_encoding_fail_closed() {
        for state in ["reconciled", "rolled_back", "recovered"] {
            assert!(!journal_requires_recovery(state));
        }
        for state in [
            "committed",
            "reconcile_pending",
            "snapshot",
            "client_issued",
            "configured",
            "rollback_cleanup_pending",
            "",
            "unknown",
        ] {
            assert!(journal_requires_recovery(state));
        }
        assert_eq!(
            percent_encode_query("2026-07-21T01:02:03Z|a/b"),
            "2026-07-21T01%3A02%3A03Z%7Ca%2Fb"
        );
    }

    #[test]
    fn recovery_actions_preserve_commit_and_rollback_boundaries() {
        let client_id = Uuid::new_v4();
        let journal = |state: &str, client_id| RecoveryJournal {
            schema_version: "nblb.hermes-journal.v1".into(),
            state: state.into(),
            operation_label: Some(format!("hermes-cutover-{}", Uuid::new_v4())),
            client_id,
        };
        for state in ["reconciled", "rolled_back", "recovered"] {
            assert_eq!(
                recovery_action(&journal(state, Some(client_id))).expect("terminal journal"),
                RecoveryAction::None,
            );
        }
        for state in ["committed", "reconcile_pending"] {
            assert_eq!(
                recovery_action(&journal(state, Some(client_id))).expect("committed journal"),
                RecoveryAction::ReconcileCommitted,
            );
        }
        for state in [
            "client_operation_prepared",
            "candidate_ready",
            "candidate_applied",
        ] {
            assert_eq!(
                recovery_action(&journal(state, Some(client_id))).expect("interrupted journal"),
                RecoveryAction::RollbackAndRevoke,
            );
        }
        assert_eq!(
            recovery_action(&journal("rollback_cleanup_pending", Some(client_id)))
                .expect("rollback cleanup journal"),
            RecoveryAction::RevokeAfterRollback,
        );
        assert!(recovery_action(&journal("rollback_cleanup_pending", None)).is_err());
        assert!(recovery_action(&journal("unknown", Some(client_id))).is_err());
        let mut wrong_schema = journal("candidate_ready", Some(client_id));
        wrong_schema.schema_version = "nblb.hermes-journal.v0".into();
        assert!(recovery_action(&wrong_schema).is_err());
    }

    #[test]
    fn recovery_distinguishes_safe_pre_mutation_staging_from_corrupt_final_generation() {
        let root = tempfile::tempdir().expect("journal-less snapshot root");
        let generation = Uuid::new_v4();
        let stage = root.path().join(format!(".staging-{generation}"));
        assert_eq!(staging_generation(&stage), Some(generation));
        for name in [
            "hermes.env",
            "config.yaml",
            "manifest.json",
            "journal.json",
            &format!(".hermes.env.{}.tmp", Uuid::new_v4()),
        ] {
            assert!(
                snapshot_stage_entry_allowed(name),
                "allowed staged entry {name}"
            );
        }
        for name in ["../hermes.env", "unexpected", ".hermes.env.not-a-uuid.tmp"] {
            assert!(
                !snapshot_stage_entry_allowed(name),
                "rejected staged entry {name}"
            );
        }

        let backup = root.path().join(generation.to_string());
        fs::create_dir(&backup).expect("journal-less generation");
        assert!(read_recovery_journal(&backup).is_err());
    }

    #[test]
    fn staged_snapshot_cleanup_deletes_only_the_exact_allowlist() {
        let root = tempfile::tempdir().expect("staging cleanup root");
        let generation = Uuid::new_v4();
        let stage = root.path().join(format!(".staging-{generation}"));
        fs::create_dir(&stage).expect("create staging directory");
        fs::set_permissions(&stage, fs::Permissions::from_mode(0o700))
            .expect("secure staging directory");
        let metadata = fs::symlink_metadata(&stage).expect("inspect staging identity");
        for name in [
            "hermes.env".to_owned(),
            "config.yaml".to_owned(),
            "manifest.json".to_owned(),
            "journal.json".to_owned(),
            format!(".journal.json.{}.tmp", Uuid::new_v4()),
        ] {
            let path = stage.join(name);
            fs::write(&path, b"fixture").expect("write staging entry");
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600))
                .expect("secure staging entry");
        }
        cleanup_snapshot_stage_with_identity(root.path(), &stage, metadata.uid(), metadata.gid())
            .expect("clean complete pre-mutation staging directory");
        assert!(!stage.exists());

        let rejected = root.path().join(format!(".staging-{}", Uuid::new_v4()));
        fs::create_dir(&rejected).expect("create rejected staging directory");
        fs::set_permissions(&rejected, fs::Permissions::from_mode(0o700))
            .expect("secure rejected staging directory");
        let unexpected = rejected.join("unexpected");
        fs::write(&unexpected, b"fixture").expect("write unexpected staging entry");
        fs::set_permissions(&unexpected, fs::Permissions::from_mode(0o600))
            .expect("secure unexpected staging entry");
        let metadata = fs::symlink_metadata(&rejected).expect("inspect rejected identity");
        assert!(
            cleanup_snapshot_stage_with_identity(
                root.path(),
                &rejected,
                metadata.uid(),
                metadata.gid(),
            )
            .is_err()
        );
        assert!(rejected.exists());
        assert!(unexpected.exists());
    }

    #[test]
    fn interrupted_secure_directory_and_atomic_write_states_recover_safely() {
        let root = tempfile::tempdir().expect("interrupted write root");
        let secure = root.path().join("secure");
        create_secure_directory(&secure).expect("create secure directory atomically");
        assert_eq!(
            fs::symlink_metadata(&secure)
                .expect("inspect secure directory")
                .mode()
                & 0o777,
            0o700,
        );

        let stage = root.path().join(format!(".staging-{}", Uuid::new_v4()));
        fs::create_dir(&stage).expect("create pre-fix interrupted stage");
        fs::set_permissions(&stage, fs::Permissions::from_mode(0o755))
            .expect("simulate create-before-chmod crash");
        let metadata = fs::symlink_metadata(&stage).expect("inspect interrupted stage");
        cleanup_snapshot_stage_with_identity(root.path(), &stage, metadata.uid(), metadata.gid())
            .expect("repair and remove empty interrupted stage");
        assert!(!stage.exists());

        let generation = root.path().join(Uuid::new_v4().to_string());
        fs::create_dir(&generation).expect("create generation directory");
        fs::set_permissions(&generation, fs::Permissions::from_mode(0o700))
            .expect("secure generation directory");
        let orphan = generation.join(format!(".journal.json.{}.tmp", Uuid::new_v4()));
        fs::write(&orphan, b"orphaned journal payload").expect("write orphan temp");
        fs::set_permissions(&orphan, fs::Permissions::from_mode(0o600))
            .expect("secure orphan temp");
        let metadata = fs::symlink_metadata(&orphan).expect("inspect orphan identity");
        cleanup_atomic_temps_with_identity(
            &generation,
            &["journal.json"],
            metadata.uid(),
            metadata.gid(),
        )
        .expect("remove exact interrupted journal temp");
        assert!(!orphan.exists());
    }

    #[test]
    fn docker_output_reader_retains_only_the_fixed_boundary() {
        let bytes = vec![b'x'; super::OUTPUT_LIMIT + 8192];
        let (retained, exceeded) =
            read_bounded_output(Cursor::new(bytes)).expect("read bounded fixture output");
        assert_eq!(retained.len(), super::OUTPUT_LIMIT);
        assert!(exceeded);
    }

    #[actix_web::test]
    async fn committed_reconciliation_revokes_before_reporting_and_stops_on_revoke_failure() {
        let old_id = Uuid::new_v4();
        let keep_id = Uuid::new_v4();
        let run_id = Uuid::new_v4();
        let fixture = web::Data::new(ReconcileFixture {
            old_id,
            keep_id,
            run_id,
            fail_revoke: AtomicBool::new(false),
            events: Mutex::new(Vec::new()),
        });
        let fixture_evidence = fixture.clone();
        let listener =
            std::net::TcpListener::bind("127.0.0.1:0").expect("bind reconciliation fixture");
        let address = listener.local_addr().expect("reconciliation address");
        let server = HttpServer::new(move || {
            App::new()
                .app_data(fixture.clone())
                .route("/clients", web::get().to(reconcile_clients))
                .route("/clients/{id}/revoke", web::post().to(reconcile_revoke))
                .route(
                    "/qa/runs/{id}/hermes-completion",
                    web::post().to(reconcile_completion),
                )
        })
        .listen(listener)
        .expect("listen reconciliation fixture")
        .run();
        let handle = server.handle();
        actix_web::rt::spawn(server);
        let receipt = SafeReceipt {
            schema_version: "nblb.hermes-cutover.v1".into(),
            generation: Uuid::new_v4(),
            embedded_commit: "fixture-commit".into(),
            client_id: keep_id,
            qa_run_id: Some(run_id),
            doctor: true,
            marker: true,
            tool: true,
            rollback_rehearsal: true,
            duration_ms: 1,
            tool_output_sha256: "fixture".into(),
            secret_scan: SecretScanReceipt {
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
            },
            lb_requests: vec![RequestReceipt {
                request_id: Uuid::new_v4(),
                attempt_count: 1,
                outcome: "succeeded".into(),
                stage: "marker".into(),
                started_at: chrono::Utc::now(),
                finished_at: chrono::Utc::now(),
            }],
        };
        let base = format!("http://{address}");
        reconcile_remote_state(&reqwest::Client::new(), "admin-fixture", &receipt, &base)
            .await
            .expect("reconcile committed remote state");
        assert_eq!(
            fixture_evidence
                .events
                .lock()
                .expect("read reconcile order")
                .as_slice(),
            ["revoke", "report"]
        );

        fixture_evidence
            .events
            .lock()
            .expect("reset reconcile events")
            .clear();
        fixture_evidence.fail_revoke.store(true, Ordering::SeqCst);
        assert!(
            reconcile_remote_state(&reqwest::Client::new(), "admin-fixture", &receipt, &base,)
                .await
                .is_err()
        );
        assert_eq!(
            fixture_evidence
                .events
                .lock()
                .expect("read failed reconcile order")
                .as_slice(),
            ["revoke_failed"]
        );
        handle.stop(true).await;
    }

    #[test]
    fn secret_scan_covers_large_files_and_chunk_boundaries() {
        let directory = tempfile::tempdir().expect("secret scan directory");
        let large = directory.path().join("large.log");
        let mut bytes = vec![b'x'; 17 * 1024 * 1024];
        bytes.extend_from_slice(b"nvapi-secret");
        fs::write(&large, bytes).expect("write large scan fixture");
        assert!(file_contains_pattern(&large, b"nvapi-").expect("scan large fixture"));

        let boundary = directory.path().join("boundary.log");
        let mut bytes = vec![b'x'; 64 * 1024 - 3];
        bytes.extend_from_slice(b"nvapi-secret");
        fs::write(&boundary, bytes).expect("write boundary scan fixture");
        assert!(file_contains_pattern(&boundary, b"nvapi-").expect("scan boundary fixture"));
    }

    #[test]
    fn streamed_secret_scan_counts_exact_tokens_across_chunk_boundaries() {
        let issued = format!("nblb_ds_{}", "a".repeat(80));
        let admin = format!("nblb_admin_{}", "b".repeat(80));
        let patterns = sensitive_patterns(&issued, &admin);
        let mut bytes = vec![b'x'; 64 * 1024 - 4];
        bytes.extend_from_slice(b"nvapi-secret\n");
        bytes.extend_from_slice(issued.as_bytes());
        bytes.extend_from_slice(admin.as_bytes());
        bytes.extend_from_slice(
            b"\nBearer nvapi-secret\nBearer nblb_ds_redacted\nBearer nblb_admin_redacted\n",
        );
        bytes.extend(std::iter::repeat_n(b'y', 128 * 1024));
        bytes.extend_from_slice(issued.as_bytes());

        let counts = count_patterns_reader(Cursor::new(bytes), &patterns)
            .expect("scan streamed exact-token fixture");
        assert_eq!(counts, vec![2, 3, 2, 2, 1, 1, 1, 1, 0, 0, 0]);
        assert!(count_patterns_reader(Cursor::new(b"safe"), &[Vec::new()]).is_err());
    }

    #[test]
    fn secret_scan_receipt_serializes_every_scalar_and_totals_safely() {
        let receipt = SecretScanReceipt {
            database_matches: 1,
            app_log_matches: 2,
            hermes_log_matches: 3,
            cloudflared_log_matches: 4,
            image_metadata_matches: 5,
            helper_artifact_matches: 6,
            hermes_tree_matches: 7,
            public_bundle_matches: 8,
            admin_response_matches: 9,
            issued_token_matches: 10,
            authorization_value_matches: 11,
        };
        assert_eq!(receipt.total_matches(), 66);
        let value = serde_json::to_value(receipt).expect("serialize secret scan receipt");
        let keys = value
            .as_object()
            .expect("secret scan receipt object")
            .keys()
            .map(String::as_str)
            .collect::<std::collections::BTreeSet<_>>();
        assert_eq!(
            keys,
            [
                "admin_response_matches",
                "app_log_matches",
                "authorization_value_matches",
                "cloudflared_log_matches",
                "database_matches",
                "helper_artifact_matches",
                "hermes_log_matches",
                "hermes_tree_matches",
                "image_metadata_matches",
                "issued_token_matches",
                "public_bundle_matches",
            ]
            .into_iter()
            .collect(),
        );
    }

    #[test]
    fn public_asset_inventory_is_deduplicated_bounded_and_traversal_safe() {
        let assets = public_asset_paths(
            br#"<script src='/_app/immutable/start.js'></script><link href="/_app/immutable/app.css"><script src='/_app/immutable/start.js'></script><script src='/_app/../secret'></script>"#,
        )
        .expect("parse public asset inventory");
        assert_eq!(
            assets,
            vec![
                "/_app/immutable/app.css".to_owned(),
                "/_app/immutable/start.js".to_owned(),
            ]
        );

        let excessive = (0..129)
            .map(|index| format!("<script src='/_app/{index}.js'></script>"))
            .collect::<String>();
        assert!(public_asset_paths(excessive.as_bytes()).is_err());
    }

    #[actix_web::test]
    async fn client_listing_follows_encoded_pagination_cursor() {
        let first_id = Uuid::new_v4();
        let second_id = Uuid::new_v4();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind pagination server");
        let address = listener.local_addr().expect("pagination address");
        let server = HttpServer::new(move || {
            App::new().route(
                "/clients",
                web::get().to(move |request: HttpRequest| async move {
                    if request.query_string().contains("before=cursor%2Fone") {
                        HttpResponse::Ok().json(serde_json::json!({
                            "items":[{"id":second_id,"label":"target","active":true}],
                            "next_before":null
                        }))
                    } else {
                        HttpResponse::Ok().json(serde_json::json!({
                            "items":[{"id":first_id,"label":"first","active":true}],
                            "next_before":"cursor/one"
                        }))
                    }
                }),
            )
        })
        .listen(listener)
        .expect("listen pagination server")
        .run();
        let handle = server.handle();
        actix_web::rt::spawn(server);
        let items = list_clients_from(
            &reqwest::Client::new(),
            "admin",
            &format!("http://{address}"),
        )
        .await
        .expect("list all client pages");
        assert_eq!(items.len(), 2);
        assert_eq!(items[1].id, second_id);
        handle.stop(true).await;
    }

    #[test]
    fn snapshot_manifest_rejects_generation_and_content_tampering() {
        let root = tempfile::tempdir().expect("snapshot test root");
        let generation = Uuid::new_v4();
        let backup = root.path().join(generation.to_string());
        fs::create_dir(&backup).expect("create snapshot test directory");
        fs::write(backup.join("hermes.env"), b"NVIDIA_API_KEY=downstream\n")
            .expect("write snapshot env");
        fs::write(backup.join("config.yaml"), b"model:\n  provider: nvidia\n")
            .expect("write snapshot config");
        let manifest = SnapshotManifest {
            schema_version: "nblb.hermes-snapshot.v1".into(),
            generation,
            env_sha256: sha256_file(&backup.join("hermes.env")).expect("hash env"),
            config_sha256: sha256_file(&backup.join("config.yaml")).expect("hash config"),
        };
        assert!(snapshot_manifest_matches(&backup, &manifest).expect("verify valid manifest"));

        fs::write(backup.join("config.yaml"), b"tampered\n").expect("tamper snapshot config");
        assert!(!snapshot_manifest_matches(&backup, &manifest).expect("verify tampered manifest"));
        let wrong_generation = SnapshotManifest {
            generation: Uuid::new_v4(),
            config_sha256: sha256_file(&backup.join("config.yaml")).expect("hash tampered config"),
            ..manifest
        };
        assert!(
            !snapshot_manifest_matches(&backup, &wrong_generation)
                .expect("verify wrong generation")
        );
    }
}
