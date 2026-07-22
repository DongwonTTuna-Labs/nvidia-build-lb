#![forbid(unsafe_code)]
//! Keeps compile-time SQLx migrations synchronized with container images.

fn main() {
    // `sqlx::migrate!` embeds the migration set at compile time. Cargo does
    // not otherwise notice a newly added SQL file when Docker reuses the
    // shared target cache, which can produce a binary that rejects a database
    // already migrated by a newer image. Treat the complete directory as an
    // explicit build input so image rebuilds and restart migrations stay in
    // lockstep.
    println!("cargo:rerun-if-changed=../../migrations/sqlx");
}
