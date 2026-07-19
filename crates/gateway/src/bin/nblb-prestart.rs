#![forbid(unsafe_code)]

//! Root-owned secret staging and privilege drop for the gateway image.

use anyhow::{Context, Result, bail};
use std::{
    env, fs,
    fs::OpenOptions,
    io::Write,
    os::unix::{
        fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
        process::CommandExt,
    },
    path::Path,
    process::Command,
};

const CANONICAL_DIR: &str = "/run/canonical-secrets";
const RUNTIME_DIR: &str = "/run/nvidia-build-lb/secrets";

fn main() -> Result<()> {
    let mode = env::var("NVIDIA_BUILD_LB_MODE").unwrap_or_else(|_| "app".to_owned());
    let names: &[&str] = match mode.as_str() {
        "migrate" | "db-init" => &["db_password"],
        "app" => &["admin_token", "vault_master_key", "db_password"],
        other => bail!("unsupported NVIDIA_BUILD_LB_MODE: {other}"),
    };
    fs::create_dir_all(RUNTIME_DIR).context("create runtime secret directory")?;
    fs::set_permissions(RUNTIME_DIR, fs::Permissions::from_mode(0o700))
        .context("secure runtime secret directory")?;
    for name in names {
        let source = Path::new(CANONICAL_DIR).join(name);
        let bytes = read_secret(&source, name)?;
        validate_secret(name, &bytes)?;
        let target = Path::new(RUNTIME_DIR).join(name);
        write_runtime_secret(&target, &bytes)?;
        chown_runtime(&target)?;
    }
    chown_runtime(Path::new(RUNTIME_DIR))?;

    let mut args = env::args_os().skip(1);
    let program = args
        .next()
        .unwrap_or_else(|| "/usr/local/bin/nvidia-build-lb-gateway".into());
    let command_args = args.collect::<Vec<_>>();
    let mut command = if mode == "db-init" {
        Command::new(&program)
    } else {
        let mut command = Command::new("/usr/bin/setpriv");
        command.args([
            "--reuid=65532",
            "--regid=65532",
            "--clear-groups",
            "--inh-caps=-all",
            "--ambient-caps=-all",
            "--bounding-set=-all",
            "--no-new-privs",
        ]);
        command.arg(&program);
        command
    };
    command.args(command_args);
    Err(command.exec()).context("replace prestart with Rust process")
}

fn read_secret(path: &Path, name: &str) -> Result<Vec<u8>> {
    let metadata =
        fs::symlink_metadata(path).with_context(|| format!("secret {name} is missing"))?;
    if !metadata.file_type().is_file() || metadata.file_type().is_symlink() {
        bail!("secret {name} must be a regular non-symlink file")
    }
    let mode = metadata.mode() & 0o777;
    let owner_allowed = metadata.uid() == 0
        || (env::var("NBLB_QA_ALLOW_HOST_SECRET_OWNER").ok().as_deref() == Some("1")
            && mode == 0o444);
    if !owner_allowed || !matches!(mode, 0o400 | 0o444 | 0o600) {
        bail!("secret {name} has an unsafe owner or mode")
    }
    fs::read(path).with_context(|| format!("read secret {name}"))
}

fn validate_secret(name: &str, bytes: &[u8]) -> Result<()> {
    match name {
        "vault_master_key" if bytes.len() != 32 => bail!("vault master key must be 32 bytes"),
        "admin_token" => {
            let value = std::str::from_utf8(bytes)
                .context("admin token must be UTF-8")?
                .trim_end_matches(['\r', '\n']);
            let suffix = value.strip_prefix("nblb_admin_").unwrap_or_default();
            if suffix.len() != 64 || !suffix.bytes().all(|byte| byte.is_ascii_hexdigit()) {
                bail!("admin token has an invalid shape")
            }
        }
        "db_password"
            if trim_secret_line_endings(bytes).is_empty()
                || trim_secret_line_endings(bytes).len() > 1024 =>
        {
            bail!("database password has an invalid size")
        }
        "db_password"
            if trim_secret_line_endings(bytes)
                .iter()
                .any(u8::is_ascii_control) =>
        {
            bail!("database password contains control characters")
        }
        _ => {}
    }
    Ok(())
}

fn trim_secret_line_endings(bytes: &[u8]) -> &[u8] {
    let mut end = bytes.len();
    while end > 0 && matches!(bytes[end - 1], b'\r' | b'\n') {
        end -= 1;
    }
    &bytes[..end]
}

fn write_runtime_secret(path: &Path, bytes: &[u8]) -> Result<()> {
    let mut file = OpenOptions::new()
        .create(true)
        .truncate(true)
        .write(true)
        .mode(0o400)
        .open(path)
        .with_context(|| format!("open runtime secret {}", path.display()))?;
    file.write_all(bytes).context("write runtime secret")?;
    file.sync_all().context("sync runtime secret")?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o400))
        .context("secure runtime secret")?;
    Ok(())
}

fn chown_runtime(path: &Path) -> Result<()> {
    let status = Command::new("/bin/chown")
        .arg("65532:65532")
        .arg(path)
        .status()
        .with_context(|| format!("chown runtime path {}", path.display()))?;
    if !status.success() {
        bail!("chown failed for {}", path.display())
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::validate_secret;

    #[test]
    fn validates_admin_token_shape() {
        let valid = format!("nblb_admin_{}", "a".repeat(64));
        assert!(validate_secret("admin_token", valid.as_bytes()).is_ok());
        assert!(validate_secret("admin_token", b"short").is_err());
    }

    #[test]
    fn validates_vault_key_size() {
        assert!(validate_secret("vault_master_key", &[0; 32]).is_ok());
        assert!(validate_secret("vault_master_key", &[0; 31]).is_err());
    }

    #[test]
    fn accepts_database_password_line_endings_but_not_controls() {
        assert!(validate_secret("db_password", b"password\n").is_ok());
        assert!(validate_secret("db_password", b"password\r\n").is_ok());
        assert!(validate_secret("db_password", b"pass\0word").is_err());
    }
}
