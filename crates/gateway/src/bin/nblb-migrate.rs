#![forbid(unsafe_code)]
//! Runs the checked-in SQLx migrations and exits.

use anyhow::{Context, Result};
use sqlx::postgres::PgPoolOptions;
use std::{env, fs};

#[tokio::main]
async fn main() -> Result<()> {
    let mut url = env::var("NBLB_DATABASE_URL").context("NBLB_DATABASE_URL is required")?;
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
        let password = env::var("NBLB_DATABASE_PASSWORD").ok().or_else(|| {
            [
                "/run/nvidia-build-lb/secrets/db_password",
                "/run/canonical-secrets/db_password",
            ]
            .into_iter()
            .find_map(|path| fs::read_to_string(path).ok())
        });
        let password = password
            .map(|value| value.trim_end_matches(['\r', '\n']).to_owned())
            .filter(|value| !value.is_empty())
            .ok_or_else(|| anyhow::anyhow!("database password is required"))?;
        url = url.replacen('@', &format!(":{password}@"), 1);
    }
    if !url
        .split_once('?')
        .is_some_and(|(_, query)| query.split('&').any(|part| part.starts_with("sslmode=")))
    {
        url.push(if url.contains('?') { '&' } else { '?' });
        url.push_str("sslmode=disable");
    }
    let pool = PgPoolOptions::new()
        .max_connections(2)
        .connect(&url)
        .await
        .context("connect PostgreSQL")?;
    sqlx::migrate!("../../migrations/sqlx")
        .run(&pool)
        .await
        .context("run SQLx migrations")?;
    Ok(())
}
