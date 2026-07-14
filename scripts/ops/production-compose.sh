#!/usr/bin/env bash
set -Eeuo pipefail

fail() {
    printf '%s\n' "${1:-production_compose_failed}" >&2
    exit 1
}

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)

command -v docker >/dev/null 2>&1 || fail docker_unavailable
[[ "${NBLB_APP_REGISTRY_DIGEST:-}" =~ ^[0-9a-f]{64}$ ]] \
    || fail app_registry_digest_invalid
[[ "${NBLB_POSTGRES_REGISTRY_DIGEST:-}" =~ ^[0-9a-f]{64}$ ]] \
    || fail postgres_registry_digest_invalid
[ "$#" -gt 0 ] || fail compose_command_required

exec docker compose --project-directory "$ROOT" -f "$ROOT/compose.yml" "$@"
