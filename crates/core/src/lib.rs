#![forbid(unsafe_code)]

//! Routing and encrypted credential custody for the gateway.

use aes_gcm::{AeadInPlace, Aes256Gcm, KeyInit, Nonce};
use anyhow::{Context, Result, anyhow, bail};
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use chrono::{DateTime, Duration, Utc};
use rand::{Rng, rng};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, HashSet},
    fs,
    fs::OpenOptions,
    io::Write,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
};

/// The hosted deployment intentionally has two independent upstream slots.
pub const MAX_UPSTREAM_KEYS: usize = 2;
use uuid::Uuid;

/// Advertised profile IDs in their stable manifest order.
pub const PROFILES: [&str; 8] = [
    "z-ai/glm-5.2",
    "microsoft/phi-4-multimodal-instruct",
    "nvidia/vila",
    "nvidia/nvclip",
    "black-forest-labs/flux.1-kontext-dev",
    "stabilityai/stable-video-diffusion",
    "nvidia/magpie-tts-multilingual",
    "nvidia/parakeet-ctc-1.1b",
];

/// A redacted credential summary.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct KeySummary {
    /// Stable key identifier.
    pub id: Uuid,
    /// Operator-facing label.
    pub label: String,
    /// SHA-256 fingerprint rendered as lowercase hex.
    pub fingerprint: String,
    /// Whether routing may select this key.
    pub enabled: bool,
    /// Whether the provider probe receipt is durable and current.
    pub verified: bool,
    /// Current cooldown deadline, if any.
    pub cooldown_until: Option<DateTime<Utc>>,
    /// Successful request count.
    pub request_count: u64,
    /// Failed request count.
    pub failure_count: u64,
}

/// A downstream bearer summary. The plaintext token is never stored or
/// returned by read operations.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct DownstreamSummary {
    /// Stable credential identifier.
    pub id: Uuid,
    /// Operator-facing label.
    pub label: String,
    /// Canonical scope order.
    pub scopes: Vec<String>,
    /// Whether the credential can still authenticate.
    pub active: bool,
    /// Number of accepted requests.
    pub request_count: u64,
    /// Last successful authentication time.
    pub last_used_at: Option<DateTime<Utc>>,
    /// Creation timestamp.
    pub created_at: DateTime<Utc>,
    /// Revocation timestamp, if revoked.
    pub revoked_at: Option<DateTime<Utc>>,
}

/// One-time issuance result. The token field must be discarded by callers
/// after presenting it to the operator.
#[derive(Clone, Debug)]
pub struct IssuedDownstream {
    /// Redacted credential metadata.
    pub summary: DownstreamSummary,
    /// Plaintext bearer, available only in the issuance response.
    pub token: String,
}

/// Encrypted upstream-key row representation used by the PostgreSQL adapter.
/// The plaintext credential is intentionally absent from this type.
#[derive(Clone, Debug)]
pub struct VaultKeyRecord {
    /// Stable key identifier.
    pub id: Uuid,
    /// Operator-facing label.
    pub label: String,
    /// SHA-256 fingerprint bytes.
    pub fingerprint: Vec<u8>,
    /// AES-GCM nonce bytes.
    pub nonce: Vec<u8>,
    /// AES-GCM ciphertext and authentication tag.
    pub ciphertext: Vec<u8>,
    /// Whether routing may select this key.
    pub enabled: bool,
    /// Whether a provider probe has verified this credential after custody.
    pub verified: bool,
    /// Whether this credential is retired but retained for request history.
    pub retired: bool,
    /// Current cooldown deadline, if any.
    pub cooldown_until: Option<DateTime<Utc>>,
    /// Successful request count.
    pub request_count: u64,
    /// Failed request count.
    pub failure_count: u64,
}

/// Hashed downstream-token row representation used by the PostgreSQL adapter.
/// The bearer token itself is intentionally absent from this type.
#[derive(Clone, Debug)]
pub struct VaultDownstreamRecord {
    /// Stable credential identifier.
    pub id: Uuid,
    /// Operator-facing label.
    pub label: String,
    /// Canonical authorization scopes.
    pub scopes: Vec<String>,
    /// SHA-256 digest of the bearer token.
    pub token_digest: Vec<u8>,
    /// Whether authentication is still accepted.
    pub active: bool,
    /// Accepted request count.
    pub request_count: u64,
    /// Last successful authentication time.
    pub last_used_at: Option<DateTime<Utc>>,
    /// Creation timestamp.
    pub created_at: DateTime<Utc>,
    /// Revocation timestamp, if revoked.
    pub revoked_at: Option<DateTime<Utc>>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct StoredKey {
    id: Uuid,
    label: String,
    fingerprint: String,
    nonce: String,
    ciphertext: String,
    enabled: bool,
    #[serde(default)]
    verified: bool,
    #[serde(default)]
    retired: bool,
    cooldown_until: Option<DateTime<Utc>>,
    request_count: u64,
    failure_count: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct StoredDownstream {
    id: Uuid,
    label: String,
    scopes: Vec<String>,
    token_digest: String,
    active: bool,
    request_count: u64,
    last_used_at: Option<DateTime<Utc>>,
    created_at: DateTime<Utc>,
    revoked_at: Option<DateTime<Utc>>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
struct VaultFile {
    version: u8,
    keys: Vec<StoredKey>,
    #[serde(default)]
    downstream: Vec<StoredDownstream>,
    #[serde(default)]
    router_cursor: usize,
    #[serde(default)]
    router_cursors: BTreeMap<String, usize>,
}

/// AES-256-GCM encrypted file-backed vault.
#[derive(Clone, Debug)]
pub struct Vault {
    path: PathBuf,
    master_key: [u8; 32],
    state: VaultFile,
}

impl Vault {
    /// Opens an existing vault or creates an empty version-one vault.
    pub fn open(path: impl AsRef<Path>, master_key: [u8; 32]) -> Result<Self> {
        let path = path.as_ref().to_path_buf();
        let state = if path.exists() {
            serde_json::from_slice(&fs::read(&path).context("read vault")?)
                .context("decode vault")?
        } else {
            VaultFile {
                version: 1,
                keys: Vec::new(),
                downstream: Vec::new(),
                router_cursor: 0,
                router_cursors: BTreeMap::new(),
            }
        };
        if state.version != 1 {
            bail!("unsupported vault version")
        }
        if state.keys.iter().filter(|key| !key.retired).count() > MAX_UPSTREAM_KEYS {
            bail!("at most two upstream credentials are supported")
        }
        let mut fingerprints = HashSet::with_capacity(state.keys.len());
        for key in &state.keys {
            if !fingerprints.insert(key.fingerprint.clone()) {
                bail!("upstream credentials must have distinct fingerprints")
            }
        }
        Ok(Self {
            path,
            master_key,
            state,
        })
    }

    /// Reconstructs an in-memory vault from encrypted database rows.
    ///
    /// `path` remains a local durability fallback for mutations made through
    /// the core API; the gateway's database repository treats the rows as the
    /// authoritative source and calls `records` after each successful change.
    pub fn from_records(
        path: impl AsRef<Path>,
        master_key: [u8; 32],
        keys: Vec<VaultKeyRecord>,
        downstream: Vec<VaultDownstreamRecord>,
        router_cursor: usize,
    ) -> Result<Self> {
        let mut cursors = BTreeMap::new();
        cursors.insert("__legacy__".to_owned(), router_cursor);
        Self::from_records_with_cursors(path, master_key, keys, downstream, cursors)
    }

    /// Reconstructs an in-memory vault with one durable cursor per routing
    /// profile. The legacy scalar cursor is retained only as a migration
    /// fallback for vault files written by the first release.
    pub fn from_records_with_cursors(
        path: impl AsRef<Path>,
        master_key: [u8; 32],
        keys: Vec<VaultKeyRecord>,
        downstream: Vec<VaultDownstreamRecord>,
        router_cursors: BTreeMap<String, usize>,
    ) -> Result<Self> {
        if keys.iter().filter(|key| !key.retired).count() > MAX_UPSTREAM_KEYS {
            bail!("at most two upstream credentials are supported")
        }
        let mut fingerprints = HashSet::with_capacity(keys.len());
        for key in &keys {
            if key.fingerprint.len() != 32 || !fingerprints.insert(key.fingerprint.clone()) {
                bail!("upstream credentials must have distinct fingerprints")
            }
        }
        let keys = keys
            .into_iter()
            .map(|key| {
                if key.nonce.len() != 12
                    || key.ciphertext.len() <= 16
                    || key.fingerprint.len() != 32
                {
                    bail!("invalid encrypted upstream row")
                }
                Ok(StoredKey {
                    id: key.id,
                    label: key.label,
                    fingerprint: hex::encode(key.fingerprint),
                    nonce: URL_SAFE_NO_PAD.encode(key.nonce),
                    ciphertext: URL_SAFE_NO_PAD.encode(key.ciphertext),
                    enabled: key.enabled,
                    verified: key.verified,
                    retired: key.retired,
                    cooldown_until: key.cooldown_until,
                    request_count: key.request_count,
                    failure_count: key.failure_count,
                })
            })
            .collect::<Result<Vec<_>>>()?;
        let downstream = downstream
            .into_iter()
            .map(|item| {
                if item.token_digest.len() != 32 {
                    bail!("invalid downstream digest")
                }
                Ok(StoredDownstream {
                    id: item.id,
                    label: item.label,
                    scopes: item.scopes,
                    token_digest: hex::encode(item.token_digest),
                    active: item.active,
                    request_count: item.request_count,
                    last_used_at: item.last_used_at,
                    created_at: item.created_at,
                    revoked_at: item.revoked_at,
                })
            })
            .collect::<Result<Vec<_>>>()?;
        Ok(Self {
            path: path.as_ref().to_path_buf(),
            master_key,
            state: VaultFile {
                version: 1,
                keys,
                downstream,
                router_cursor: router_cursors
                    .get("__legacy__")
                    .copied()
                    .unwrap_or_default(),
                router_cursors,
            },
        })
    }

    /// Exports encrypted upstream rows without decrypting credentials.
    pub fn key_records(&self) -> Result<Vec<VaultKeyRecord>> {
        self.state
            .keys
            .iter()
            .map(|key| {
                Ok(VaultKeyRecord {
                    id: key.id,
                    label: key.label.clone(),
                    fingerprint: hex::decode(&key.fingerprint).context("decode key fingerprint")?,
                    nonce: URL_SAFE_NO_PAD
                        .decode(&key.nonce)
                        .context("decode key nonce")?,
                    ciphertext: URL_SAFE_NO_PAD
                        .decode(&key.ciphertext)
                        .context("decode key ciphertext")?,
                    enabled: key.enabled,
                    verified: key.verified,
                    retired: key.retired,
                    cooldown_until: key.cooldown_until,
                    request_count: key.request_count,
                    failure_count: key.failure_count,
                })
            })
            .collect()
    }

    /// Exports hashed downstream-token rows without exposing bearer tokens.
    pub fn downstream_records(&self) -> Result<Vec<VaultDownstreamRecord>> {
        self.state
            .downstream
            .iter()
            .map(|item| {
                Ok(VaultDownstreamRecord {
                    id: item.id,
                    label: item.label.clone(),
                    scopes: item.scopes.clone(),
                    token_digest: hex::decode(&item.token_digest)
                        .context("decode downstream digest")?,
                    active: item.active,
                    request_count: item.request_count,
                    last_used_at: item.last_used_at,
                    created_at: item.created_at,
                    revoked_at: item.revoked_at,
                })
            })
            .collect()
    }

    /// Adds a credential and returns only its redacted identity.
    pub fn add(&mut self, label: impl Into<String>, credential: &str) -> Result<KeySummary> {
        let label = label.into();
        validate_label(&label)?;
        if !valid_upstream_credential(credential) {
            bail!("credential shape is invalid")
        }
        let credential_fingerprint = fingerprint(credential.as_bytes());
        if self
            .state
            .keys
            .iter()
            .any(|key| key.fingerprint == credential_fingerprint)
        {
            bail!("upstream credential already exists")
        }
        let active_count = self.state.keys.iter().filter(|key| !key.retired).count();
        let replacement = (active_count >= MAX_UPSTREAM_KEYS)
            .then(|| {
                self.state
                    .keys
                    .iter()
                    .position(|key| !key.retired && !key.enabled)
            })
            .flatten();
        if active_count >= MAX_UPSTREAM_KEYS && replacement.is_none() {
            bail!("at most two upstream credentials are supported")
        }
        if let Some(index) = replacement {
            self.state.keys[index].retired = true;
            self.state.keys[index].enabled = false;
        }
        let id = Uuid::new_v4();
        let mut nonce_bytes = [0_u8; 12];
        rng().fill(&mut nonce_bytes);
        let cipher = Aes256Gcm::new_from_slice(&self.master_key).context("cipher init")?;
        let mut payload = credential.as_bytes().to_vec();
        cipher
            .encrypt_in_place(Nonce::from_slice(&nonce_bytes), id.as_bytes(), &mut payload)
            .map_err(|_| anyhow!("credential encryption failed"))?;
        let entry = StoredKey {
            id,
            label,
            fingerprint: credential_fingerprint,
            nonce: URL_SAFE_NO_PAD.encode(nonce_bytes),
            ciphertext: URL_SAFE_NO_PAD.encode(payload),
            // A newly stored credential is not eligible until an operator
            // explicitly probes it against the configured provider.
            enabled: false,
            verified: false,
            retired: false,
            cooldown_until: None,
            request_count: 0,
            failure_count: 0,
        };
        self.state.keys.push(entry.clone());
        self.persist()?;
        Ok(summary(&entry))
    }

    /// Lists redacted credentials.
    pub fn list(&self) -> Vec<KeySummary> {
        self.state
            .keys
            .iter()
            .filter(|key| !key.retired)
            .map(summary)
            .collect()
    }

    /// Returns the persisted routing cursor used for restart continuity.
    pub fn router_cursor(&self) -> usize {
        self.state
            .router_cursors
            .get("__legacy__")
            .copied()
            .unwrap_or(self.state.router_cursor)
    }

    /// Stores the next routing cursor atomically with vault state.
    pub fn set_router_cursor(&mut self, cursor: usize) -> Result<()> {
        self.state.router_cursor = cursor;
        self.state
            .router_cursors
            .insert("__legacy__".to_owned(), cursor);
        self.persist()
    }

    /// Returns a profile-local cursor, defaulting to the legacy cursor for
    /// old vault files and to slot zero for a new profile.
    pub fn router_cursor_for(&self, profile: &str) -> usize {
        self.state
            .router_cursors
            .get(profile)
            .copied()
            .or_else(|| self.state.router_cursors.get("__legacy__").copied())
            .unwrap_or(self.state.router_cursor)
    }

    /// Stores one profile-local cursor atomically with vault state.
    pub fn set_router_cursor_for(&mut self, profile: &str, cursor: usize) -> Result<()> {
        self.state.router_cursors.insert(profile.to_owned(), cursor);
        self.persist()
    }

    /// Issues a scoped downstream bearer and stores only its digest.
    pub fn issue_downstream(
        &mut self,
        label: &str,
        requested_scopes: &[String],
    ) -> Result<IssuedDownstream> {
        validate_downstream_label(label)?;
        let scopes = canonical_scopes(requested_scopes)?;
        if self
            .state
            .downstream
            .iter()
            .any(|item| item.active && item.label.as_bytes() == label.as_bytes())
        {
            bail!("downstream label already exists")
        }
        let id = Uuid::new_v4();
        let mut bytes = [0_u8; 32];
        rng().fill(&mut bytes);
        let token = format!("nblb_ds_{}", hex::encode(bytes));
        let now = Utc::now();
        let entry = StoredDownstream {
            id,
            label: label.to_owned(),
            scopes: scopes.clone(),
            token_digest: fingerprint(token.as_bytes()),
            active: true,
            request_count: 0,
            last_used_at: None,
            created_at: now,
            revoked_at: None,
        };
        self.state.downstream.push(entry.clone());
        self.persist()?;
        Ok(IssuedDownstream {
            summary: downstream_summary(&entry),
            token,
        })
    }

    /// Lists downstream credentials without exposing token digests.
    pub fn list_downstream(&self) -> Vec<DownstreamSummary> {
        self.state
            .downstream
            .iter()
            .map(downstream_summary)
            .collect()
    }

    /// Revokes one downstream credential. Repeated revocation is idempotent.
    pub fn revoke_downstream(&mut self, id: Uuid) -> Result<DownstreamSummary> {
        let (summary, changed) = {
            let item = self
                .state
                .downstream
                .iter_mut()
                .find(|item| item.id == id)
                .ok_or_else(|| anyhow!("downstream credential not found"))?;
            let changed = item.active;
            if changed {
                item.active = false;
                item.revoked_at = Some(Utc::now());
            }
            (downstream_summary(item), changed)
        };
        if changed {
            self.persist()?;
        }
        Ok(summary)
    }

    /// Authenticates a downstream bearer and checks one required scope.
    pub fn authenticate_downstream(
        &mut self,
        token: &str,
        required_scope: &str,
    ) -> Result<DownstreamSummary> {
        if token.is_empty() || !token.starts_with("nblb_ds_") {
            bail!("invalid downstream credential")
        }
        let digest = fingerprint(token.as_bytes());
        let item = self
            .state
            .downstream
            .iter_mut()
            .find(|item| item.active && item.token_digest == digest)
            .ok_or_else(|| anyhow!("invalid downstream credential"))?;
        if !item.scopes.iter().any(|scope| scope == required_scope) {
            bail!("insufficient scope")
        }
        item.request_count = item.request_count.saturating_add(1);
        item.last_used_at = Some(Utc::now());
        let result = downstream_summary(item);
        self.persist()?;
        Ok(result)
    }

    /// Enables or disables a key.
    pub fn set_enabled(&mut self, id: Uuid, enabled: bool) -> Result<KeySummary> {
        let key = self
            .state
            .keys
            .iter_mut()
            .find(|key| key.id == id && !key.retired)
            .ok_or_else(|| anyhow!("key not found"))?;
        if enabled && !key.verified {
            bail!("provider probe is required before enabling key")
        }
        if enabled && key.cooldown_until.is_some_and(|until| until > Utc::now()) {
            bail!("key is cooling down")
        }
        key.enabled = enabled;
        let result = summary(key);
        self.persist()?;
        Ok(result)
    }

    /// Records a successful provider probe without enabling routing. Enabling
    /// remains an explicit operator action after the probe receipt is durable.
    pub fn mark_verified(&mut self, id: Uuid) -> Result<KeySummary> {
        let key = self
            .state
            .keys
            .iter_mut()
            .find(|key| key.id == id && !key.retired)
            .ok_or_else(|| anyhow!("key not found"))?;
        key.verified = true;
        key.cooldown_until = None;
        key.failure_count = 0;
        let result = summary(key);
        self.persist()?;
        Ok(result)
    }

    /// Deletes a key.
    pub fn delete(&mut self, id: Uuid) -> Result<()> {
        let key = self
            .state
            .keys
            .iter_mut()
            .find(|key| key.id == id && !key.retired)
            .ok_or_else(|| anyhow!("key not found"))?;
        // Keep the encrypted row so request_attempts foreign keys and audit
        // history remain valid, but make the credential permanently absent
        // from routing and admin read surfaces.
        key.retired = true;
        key.enabled = false;
        key.verified = false;
        key.cooldown_until = None;
        self.persist()
    }

    /// Permanently excludes a credential from routing after authentication or
    /// entitlement failure. Operators must explicitly re-enable it after
    /// rotating or repairing the provider credential.
    pub fn quarantine(&mut self, id: Uuid) -> Result<KeySummary> {
        let key = self
            .state
            .keys
            .iter_mut()
            .find(|key| key.id == id && !key.retired)
            .ok_or_else(|| anyhow!("key not found"))?;
        key.enabled = false;
        key.verified = false;
        key.cooldown_until = None;
        key.failure_count = 0;
        let result = summary(key);
        self.persist()?;
        Ok(result)
    }

    /// Decrypts one key for one outbound request.
    pub fn credential(&self, id: Uuid) -> Result<String> {
        let key = self
            .state
            .keys
            .iter()
            // Retired ciphertext remains decryptable for an in-flight retry
            // or audit recovery, but it is never returned by `list()` and is
            // therefore never selected for new traffic.
            .find(|key| key.id == id)
            .ok_or_else(|| anyhow!("key not found"))?;
        let nonce = URL_SAFE_NO_PAD.decode(&key.nonce).context("decode nonce")?;
        if nonce.len() != 12 {
            bail!("invalid credential nonce")
        }
        let mut ciphertext = URL_SAFE_NO_PAD
            .decode(&key.ciphertext)
            .context("decode ciphertext")?;
        let cipher = Aes256Gcm::new_from_slice(&self.master_key).context("cipher init")?;
        cipher
            .decrypt_in_place(Nonce::from_slice(&nonce), id.as_bytes(), &mut ciphertext)
            .map_err(|_| anyhow!("credential decryption failed"))?;
        String::from_utf8(ciphertext).context("credential utf8")
    }

    /// Persists a successful request count.
    pub fn record_request(&mut self, id: Uuid) -> Result<()> {
        let key = self
            .state
            .keys
            .iter_mut()
            .find(|key| key.id == id && !key.retired)
            .ok_or_else(|| anyhow!("key not found"))?;
        key.request_count = key.request_count.saturating_add(1);
        key.failure_count = 0;
        key.cooldown_until = None;
        self.persist()
    }

    /// Applies an exponential, bounded cooldown.
    pub fn record_failure(&mut self, id: Uuid, retry_after: Option<Duration>) -> Result<()> {
        let key = self
            .state
            .keys
            .iter_mut()
            .find(|key| key.id == id && !key.retired)
            .ok_or_else(|| anyhow!("key not found"))?;
        key.failure_count = key.failure_count.saturating_add(1);
        let delay = retry_after
            .map(|value| value.clamp(Duration::zero(), Duration::seconds(300)))
            .unwrap_or_else(|| Duration::seconds(1_i64 << key.failure_count.min(6)));
        let until = Utc::now() + delay;
        key.cooldown_until = Some(key.cooldown_until.map_or(until, |old| old.max(until)));
        self.persist()
    }

    fn persist(&self) -> Result<()> {
        let tmp = self.path.with_extension("tmp");
        let bytes = serde_json::to_vec(&self.state)?;
        let mut file = OpenOptions::new()
            .create(true)
            .truncate(true)
            .write(true)
            .open(&tmp)
            .context("open vault temp")?;
        file.write_all(&bytes).context("write vault")?;
        file.sync_all().context("sync vault")?;
        drop(file);
        fs::set_permissions(&tmp, fs::Permissions::from_mode(0o600)).context("protect vault")?;
        fs::rename(&tmp, &self.path).context("commit vault")?;
        if let Some(parent) = self.path.parent() {
            OpenOptions::new()
                .read(true)
                .open(parent)
                .context("open vault directory")?
                .sync_all()
                .context("sync vault directory")?;
        }
        Ok(())
    }

    /// Rewrites the encrypted file after a failed PostgreSQL synchronization.
    /// The gateway uses this only while restoring a previously cloned state.
    pub fn persist_for_rollback(&self) -> Result<()> {
        self.persist()
    }
}

/// Round-robin selector that excludes disabled and cooling keys.
#[derive(Debug, Default)]
pub struct Router {
    next_slot: usize,
}

impl Router {
    /// Creates a selector from a persisted cursor.
    pub fn with_next_slot(next_slot: usize) -> Self {
        Self { next_slot }
    }

    /// Returns the next cursor after the most recent selection.
    pub fn next_slot(&self) -> usize {
        self.next_slot
    }

    /// Restores a cursor when durable synchronization rejects a selection.
    pub fn set_next_slot(&mut self, next_slot: usize) {
        self.next_slot = next_slot;
    }

    /// Selects the next key, returning `None` when all are unavailable.
    pub fn select(&mut self, keys: &[KeySummary]) -> Option<Uuid> {
        let now = Utc::now();
        if keys.is_empty() {
            return None;
        }
        for offset in 0..keys.len() {
            let index = (self.next_slot + offset) % keys.len();
            let candidate = &keys[index];
            if candidate.enabled
                && candidate.verified
                && candidate.cooldown_until.is_none_or(|until| until <= now)
            {
                self.next_slot = (index + 1) % keys.len();
                return Some(candidate.id);
            }
        }
        None
    }
}

fn summary(key: &StoredKey) -> KeySummary {
    KeySummary {
        id: key.id,
        label: key.label.clone(),
        fingerprint: key.fingerprint.clone(),
        enabled: key.enabled,
        verified: key.verified,
        cooldown_until: key.cooldown_until,
        request_count: key.request_count,
        failure_count: key.failure_count,
    }
}
fn downstream_summary(item: &StoredDownstream) -> DownstreamSummary {
    DownstreamSummary {
        id: item.id,
        label: item.label.clone(),
        scopes: item.scopes.clone(),
        active: item.active,
        request_count: item.request_count,
        last_used_at: item.last_used_at,
        created_at: item.created_at,
        revoked_at: item.revoked_at,
    }
}
fn fingerprint(value: &[u8]) -> String {
    Sha256::digest(value)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn valid_upstream_credential(value: &str) -> bool {
    let bytes = value.as_bytes();
    (38..=198).contains(&bytes.len())
        && value.starts_with("nvapi-")
        && bytes[6..]
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

fn validate_label(label: &str) -> Result<()> {
    let count = label.chars().count();
    if !(1..=128).contains(&count) || label.trim() != label || label.chars().any(char::is_control) {
        bail!("label is invalid")
    }
    Ok(())
}

fn validate_downstream_label(label: &str) -> Result<()> {
    validate_label(label)?;
    if label.chars().count() > 120 {
        bail!("downstream label is invalid")
    }
    Ok(())
}

fn canonical_scopes(scopes: &[String]) -> Result<Vec<String>> {
    if scopes.is_empty()
        || scopes.len() > 6
        || scopes.iter().any(|scope| {
            !matches!(
                scope.as_str(),
                "models:read"
                    | "chat:write"
                    | "embeddings:write"
                    | "images:write"
                    | "audio:write"
                    | "media:write"
            )
        })
    {
        bail!("scopes are invalid")
    }
    let mut result = scopes.to_vec();
    result.sort_by_key(|scope| {
        [
            "models:read",
            "chat:write",
            "embeddings:write",
            "images:write",
            "audio:write",
            "media:write",
        ]
        .iter()
        .position(|candidate| candidate == scope)
        .unwrap_or(usize::MAX)
    });
    result.dedup();
    if result.len() != scopes.len() {
        bail!("scopes must be unique")
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn vault_round_trip_is_encrypted() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("vault.json");
        let mut vault = Vault::open(&path, [7; 32]).expect("open");
        let credential = "nvapi-abcdefghijklmnopqrstuvwxyz123456";
        let key = vault.add("one", credential).expect("add");
        assert!(!String::from_utf8_lossy(&fs::read(path).expect("read")).contains(credential));
        assert_eq!(vault.credential(key.id).expect("decrypt"), credential);
    }
    #[test]
    fn router_round_robins() {
        let mut router = Router::default();
        let keys = [
            KeySummary {
                id: Uuid::from_u128(1),
                label: "a".into(),
                fingerprint: "a".into(),
                enabled: true,
                verified: true,
                cooldown_until: None,
                request_count: 0,
                failure_count: 0,
            },
            KeySummary {
                id: Uuid::from_u128(2),
                label: "b".into(),
                fingerprint: "b".into(),
                enabled: true,
                verified: true,
                cooldown_until: None,
                request_count: 0,
                failure_count: 0,
            },
        ];
        assert_eq!(router.select(&keys), Some(keys[0].id));
        assert_eq!(router.select(&keys), Some(keys[1].id));
    }

    #[test]
    fn router_skips_cooling_or_disabled_keys_for_failover() {
        let mut router = Router::default();
        let keys = [
            KeySummary {
                id: Uuid::from_u128(1),
                label: "cooling".into(),
                fingerprint: "a".into(),
                enabled: true,
                verified: true,
                cooldown_until: Some(Utc::now() + Duration::seconds(30)),
                request_count: 0,
                failure_count: 1,
            },
            KeySummary {
                id: Uuid::from_u128(2),
                label: "healthy".into(),
                fingerprint: "b".into(),
                enabled: true,
                verified: true,
                cooldown_until: None,
                request_count: 0,
                failure_count: 0,
            },
            KeySummary {
                id: Uuid::from_u128(3),
                label: "disabled".into(),
                fingerprint: "c".into(),
                enabled: false,
                verified: false,
                cooldown_until: None,
                request_count: 0,
                failure_count: 0,
            },
        ];
        assert_eq!(router.select(&keys), Some(keys[1].id));
        assert_eq!(router.select(&keys), Some(keys[1].id));
        assert_eq!(router.select(&keys[..1]), None);
    }

    #[test]
    fn router_skips_enabled_keys_without_probe_receipt() {
        let mut router = Router::default();
        let keys = [
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
                cooldown_until: None,
                request_count: 0,
                failure_count: 0,
            },
        ];
        assert_eq!(router.select(&keys), Some(keys[1].id));
        assert_eq!(router.select(&keys[..1]), None);
    }

    #[test]
    fn failure_cooldown_and_profile_cursor_survive_restart() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("vault.json");
        let credential = "nvapi-abcdefghijklmnopqrstuvwxyz123456";
        let mut vault = Vault::open(&path, [11; 32]).expect("open");
        let key = vault.add("one", credential).expect("add");
        vault
            .record_failure(key.id, Some(Duration::seconds(30)))
            .expect("failure");
        vault
            .set_router_cursor_for("nvidia/vila", 1)
            .expect("cursor");
        let reopened = Vault::open(&path, [11; 32]).expect("reopen");
        let summary = &reopened.list()[0];
        assert!(summary.cooldown_until.is_some());
        assert_eq!(reopened.router_cursor_for("nvidia/vila"), 1);
        assert_eq!(reopened.credential(key.id).expect("decrypt"), credential);
    }

    #[test]
    fn successful_request_and_probe_clear_failure_cooldown() {
        let dir = tempfile::tempdir().expect("tempdir");
        let mut vault = Vault::open(dir.path().join("vault.json"), [13; 32]).expect("open");
        let key = vault
            .add("one", "nvapi-abcdefghijklmnopqrstuvwxyz123456")
            .expect("add");
        vault.mark_verified(key.id).expect("probe");
        vault.set_enabled(key.id, true).expect("enable");
        vault
            .record_failure(key.id, Some(Duration::seconds(30)))
            .expect("failure");
        assert!(vault.list()[0].cooldown_until.is_some());
        vault.record_request(key.id).expect("success");
        assert_eq!(vault.list()[0].failure_count, 0);
        assert!(vault.list()[0].cooldown_until.is_none());
        vault
            .record_failure(key.id, Some(Duration::seconds(30)))
            .expect("failure again");
        vault.mark_verified(key.id).expect("probe reset");
        assert_eq!(vault.list()[0].failure_count, 0);
        assert!(vault.list()[0].cooldown_until.is_none());
    }

    #[test]
    fn cooling_key_cannot_be_enabled_until_probe_reset() {
        let dir = tempfile::tempdir().expect("tempdir");
        let mut vault = Vault::open(dir.path().join("vault.json"), [14; 32]).expect("open");
        let key = vault
            .add("one", "nvapi-abcdefghijklmnopqrstuvwxyz123456")
            .expect("add");
        vault.mark_verified(key.id).expect("probe");
        vault
            .record_failure(key.id, Some(Duration::seconds(30)))
            .expect("failure");
        assert!(vault.set_enabled(key.id, true).is_err());
        vault.mark_verified(key.id).expect("probe reset");
        vault.set_enabled(key.id, true).expect("enable after reset");
    }

    #[test]
    fn quarantine_disables_invalid_credentials_across_restart() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("vault.json");
        let mut vault = Vault::open(&path, [12; 32]).expect("open");
        let key = vault
            .add("invalid", "nvapi-abcdefghijklmnopqrstuvwxyz123456")
            .expect("add");
        let summary = vault.quarantine(key.id).expect("quarantine");
        assert!(!summary.enabled);
        assert!(summary.cooldown_until.is_none());
        assert!(!Vault::open(&path, [12; 32]).expect("reopen").list()[0].enabled);
    }

    #[test]
    fn downstream_plaintext_is_one_time_and_scope_checked() {
        let dir = tempfile::tempdir().expect("tempdir");
        let mut vault = Vault::open(dir.path().join("vault.json"), [3; 32]).expect("open");
        let issued = vault
            .issue_downstream("hermes", &["chat:write".into(), "models:read".into()])
            .expect("issue");
        assert_eq!(issued.summary.scopes, vec!["models:read", "chat:write"]);
        assert!(
            vault
                .authenticate_downstream(&issued.token, "chat:write")
                .is_ok()
        );
        assert!(
            vault
                .authenticate_downstream(&issued.token, "models:read")
                .is_ok()
        );
        assert!(
            vault
                .authenticate_downstream(&issued.token, "audio:read")
                .is_err()
        );
        let raw = fs::read_to_string(dir.path().join("vault.json")).expect("read");
        assert!(!raw.contains(&issued.token));
    }

    #[test]
    fn encrypted_records_reconstruct_without_plaintext() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("vault.json");
        let credential = "nvapi-abcdefghijklmnopqrstuvwxyz123456";
        let mut source = Vault::open(&path, [9; 32]).expect("open");
        let summary = source.add("primary", credential).expect("add");
        let records = source.key_records().expect("records");
        assert_eq!(records.len(), 1);
        assert!(
            !records[0]
                .ciphertext
                .windows(credential.len())
                .any(|window| window == credential.as_bytes())
        );
        let restored = Vault::from_records(
            dir.path().join("restored.json"),
            [9; 32],
            records,
            Vec::new(),
            source.router_cursor(),
        )
        .expect("restore");
        assert_eq!(
            restored.credential(summary.id).expect("decrypt"),
            credential
        );
    }

    #[test]
    fn upstream_slots_are_limited_to_two_distinct_credentials() {
        let dir = tempfile::tempdir().expect("tempdir");
        let mut vault = Vault::open(dir.path().join("vault.json"), [5; 32]).expect("open");
        let one = vault
            .add("one", "nvapi-abcdefghijklmnopqrstuvwxyz123456")
            .expect("first");
        vault.mark_verified(one.id).expect("probe first");
        vault.set_enabled(one.id, true).expect("enable first");
        assert!(
            vault
                .add("duplicate", "nvapi-abcdefghijklmnopqrstuvwxyz123456")
                .is_err()
        );
        let two = vault
            .add("two", "nvapi-zyxwvutsrqponmlkjihgfedcba654321")
            .expect("second");
        vault.mark_verified(two.id).expect("probe second");
        vault.set_enabled(two.id, true).expect("enable second");
        assert!(
            vault
                .add("three", "nvapi-0123456789abcdefghijklmnopqrstuvwxyz")
                .is_err()
        );
    }

    #[test]
    fn enabling_requires_probe_and_disabled_slot_can_rotate() {
        let dir = tempfile::tempdir().expect("tempdir");
        let mut vault = Vault::open(dir.path().join("vault.json"), [6; 32]).expect("open");
        let first = vault
            .add("first", "nvapi-abcdefghijklmnopqrstuvwxyz123456")
            .expect("first");
        assert!(vault.set_enabled(first.id, true).is_err());
        vault.mark_verified(first.id).expect("probe");
        vault.set_enabled(first.id, true).expect("enable");
        let second = vault
            .add("second", "nvapi-zyxwvutsrqponmlkjihgfedcba654321")
            .expect("second");
        vault.mark_verified(second.id).expect("probe second");
        vault.set_enabled(second.id, true).expect("enable second");
        vault.quarantine(first.id).expect("quarantine");
        let replacement = vault
            .add("replacement", "nvapi-0123456789abcdefghijklmnopqrstuvwxyz")
            .expect("replace disabled slot");
        assert_ne!(replacement.id, first.id);
        assert!(!replacement.enabled);
        let records = vault.key_records().expect("records");
        assert_eq!(records.len(), 3);
        assert!(
            records
                .iter()
                .any(|record| record.id == first.id && record.retired)
        );
        assert_eq!(
            vault
                .credential(replacement.id)
                .expect("decrypt replacement"),
            "nvapi-0123456789abcdefghijklmnopqrstuvwxyz"
        );
        assert_eq!(
            vault.credential(first.id).expect("decrypt retired key"),
            "nvapi-abcdefghijklmnopqrstuvwxyz123456"
        );
    }

    #[test]
    fn deleting_key_retires_row_without_breaking_history() {
        let dir = tempfile::tempdir().expect("tempdir");
        let mut vault = Vault::open(dir.path().join("vault.json"), [7; 32]).expect("open");
        let key = vault
            .add("history", "nvapi-abcdefghijklmnopqrstuvwxyz123456")
            .expect("add");
        vault.mark_verified(key.id).expect("probe");
        vault.set_enabled(key.id, true).expect("enable");
        vault.delete(key.id).expect("retire");

        assert!(vault.list().is_empty());
        assert_eq!(vault.key_records().expect("records").len(), 1);
        assert!(
            vault
                .key_records()
                .expect("records")
                .into_iter()
                .next()
                .expect("retired row")
                .retired
        );
        assert!(vault.set_enabled(key.id, false).is_err());
    }
}
