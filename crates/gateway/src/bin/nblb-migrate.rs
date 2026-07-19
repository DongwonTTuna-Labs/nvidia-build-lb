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
        url = url.replacen('@', &format!(":{}@", percent_encode_userinfo(&password)), 1);
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
    seed_existing_routing_profiles(&pool).await?;
    sqlx::migrate!("../../migrations/sqlx")
        .run(&pool)
        .await
        .context("run SQLx migrations")?;
    Ok(())
}

async fn seed_existing_routing_profiles(pool: &sqlx::PgPool) -> Result<()> {
    let table_exists =
        sqlx::query_scalar::<_, bool>("SELECT to_regclass('nblb.routing_state') IS NOT NULL")
            .fetch_one(pool)
            .await
            .context("inspect routing state before migrations")?;
    if !table_exists {
        return Ok(());
    }
    sqlx::query(
        // The 0001 CHECK predates Parakeet; 0004 widens it and 0009 seeds
        // that eighth profile after the migration has run.
        "INSERT INTO nblb.routing_state (profile_id, next_slot, generation) VALUES ('z-ai/glm-5.2',1,0), ('microsoft/phi-4-multimodal-instruct',1,0), ('nvidia/vila',1,0), ('nvidia/nvclip',1,0), ('black-forest-labs/flux.1-kontext-dev',1,0), ('stabilityai/stable-video-diffusion',1,0), ('nvidia/magpie-tts-multilingual',1,0) ON CONFLICT (profile_id) DO NOTHING",
    )
    .execute(pool)
    .await
    .context("seed existing routing profiles before migrations")?;
    Ok(())
}

fn percent_encode_userinfo(value: &str) -> String {
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

#[cfg(test)]
mod tests {
    use super::percent_encode_userinfo;

    #[test]
    fn percent_encodes_database_password_delimiters() {
        assert_eq!(percent_encode_userinfo("p@ss:/#?"), "p%40ss%3A%2F%23%3F");
    }
}
