#![forbid(unsafe_code)]

//! Bounded PostgreSQL volume bootstrap entrypoint used by `db-init`.

use std::{env, fs, io, os::unix::fs::PermissionsExt, path::PathBuf, process::Command};

fn main() -> io::Result<()> {
    let pgdata = PathBuf::from(
        env::var("PGDATA").unwrap_or_else(|_| "/var/lib/postgresql/data/pgdata".into()),
    );
    fs::create_dir_all(&pgdata)?;
    fs::set_permissions(&pgdata, fs::Permissions::from_mode(0o700))?;
    let pgdata_text = pgdata
        .to_str()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "PGDATA is not UTF-8"))?;
    let owner = Command::new("chown")
        .args(["70:70", pgdata_text])
        .status()?;
    if !owner.success() {
        return Err(io::Error::other(
            "failed to assign PostgreSQL data ownership",
        ));
    }
    // Keep the volume bootstrap marker outside PGDATA: initdb requires the
    // database directory itself to be empty on first initialization.
    let sentinel = pgdata
        .parent()
        .unwrap_or_else(|| std::path::Path::new("/var/lib/postgresql/data"))
        .join(".nblb-bootstrap-ready");
    if !sentinel.exists() {
        fs::write(sentinel, b"nvidia-build-lb-bootstrap-v1\n")?;
    }
    Ok(())
}
