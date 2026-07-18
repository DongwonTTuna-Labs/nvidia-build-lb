#![forbid(unsafe_code)]

//! Bounded PostgreSQL volume bootstrap entrypoint used by `db-init`.

use std::{env, fs, io, path::PathBuf};

fn main() -> io::Result<()> {
    let pgdata = PathBuf::from(
        env::var("PGDATA").unwrap_or_else(|_| "/var/lib/postgresql/data/pgdata".into()),
    );
    fs::create_dir_all(&pgdata)?;
    let sentinel = pgdata.join(".nblb-bootstrap-ready");
    if !sentinel.exists() {
        fs::write(sentinel, b"nvidia-build-lb-bootstrap-v1\n")?;
    }
    Ok(())
}
