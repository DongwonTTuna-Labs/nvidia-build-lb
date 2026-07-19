# syntax=docker/dockerfile:1.18@sha256:dabfc0969b935b2080555ace70ee69a5261af8a8f1b4df97b9e7fbcf6722eddf

# PostgreSQL uses its upstream Rust-independent entrypoint. Secret loading is
# provided by POSTGRES_PASSWORD_FILE in compose, and health is checked through
# the native pg_isready executable; no repository shell wrapper is needed.
FROM postgres@sha256:c7526c0f6c3f30260a563d7bcf8ad778effac59a44f8ffa86678c35418338609
