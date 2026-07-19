#![forbid(unsafe_code)]

//! Read-only Rust healthcheck for the gateway container.

use anyhow::{Context, Result, bail};
use serde::Deserialize;
use std::{env, fs, time::Duration};

#[derive(Debug, Deserialize)]
struct Health {
    ready: bool,
    traffic_ready: bool,
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
        .get(format!("http://127.0.0.1:{bind_port}/health"))
        .header("Host", format!("127.0.0.1:{host_port}"))
        .send()
        .await
        .context("request gateway health")?;
    if !response.status().is_success() {
        bail!("gateway health returned {}", response.status())
    }
    let health = response.json::<Health>().await.context("decode health")?;
    if !health.ready || !health.traffic_ready {
        bail!("gateway is not traffic-ready")
    }
    Ok(())
}
