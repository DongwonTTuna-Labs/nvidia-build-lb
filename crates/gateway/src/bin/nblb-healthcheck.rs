#![forbid(unsafe_code)]

//! Read-only Rust healthcheck for the gateway container.

use anyhow::{Context, Result, bail};
use serde::Deserialize;
use std::{env, fs, time::Duration};

#[derive(Debug, Deserialize)]
struct Health {
    live: bool,
}

fn validate_health(status: reqwest::StatusCode, health: &Health) -> Result<()> {
    if !status.is_success() {
        bail!("gateway health returned {status}")
    }
    if !health.live {
        bail!("gateway is not live")
    }
    Ok(())
}

#[tokio::main]
async fn main() -> Result<()> {
    let uid = fs::read_to_string("/proc/1/status")
        .context("read init process status")?
        .lines()
        .find_map(|line| line.strip_prefix("Uid:").map(str::split_whitespace))
        .and_then(|mut values| values.next().map(str::to_owned));
    if uid.as_deref() != Some("65532") {
        bail!("gateway is not running as UID 65532")
    }
    let bind_port = env::var("NVIDIA_BUILD_LB_BIND_PORT").unwrap_or_else(|_| "2456".into());
    let host_port = env::var("NVIDIA_BUILD_LB_PUBLIC_PORT").unwrap_or_else(|_| bind_port.clone());
    let response = reqwest::Client::builder()
        .timeout(Duration::from_secs(1))
        .build()
        .context("build health client")?
        .get(format!("http://127.0.0.1:{bind_port}/health/live"))
        .header("Host", format!("127.0.0.1:{host_port}"))
        .send()
        .await
        .context("request gateway health")?;
    let status = response.status();
    let health = response.json::<Health>().await.context("decode health")?;
    validate_health(status, &health)
}

#[cfg(test)]
mod tests {
    use super::{Health, validate_health};

    #[test]
    fn pr1_healthcheck_accepts_liveness_without_traffic_readiness() {
        assert!(validate_health(reqwest::StatusCode::OK, &Health { live: true }).is_ok());
        assert!(validate_health(reqwest::StatusCode::OK, &Health { live: false }).is_err());
        assert!(
            validate_health(
                reqwest::StatusCode::SERVICE_UNAVAILABLE,
                &Health { live: true }
            )
            .is_err()
        );
    }
}
