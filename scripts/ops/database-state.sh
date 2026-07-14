#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

fail() {
    printf '%s\n' 'database_state_failed' >&2
    exit 1
}

[ "$#" -eq 1 ] || fail
DB_CONTAINER=$1
[[ "$DB_CONTAINER" =~ ^[0-9a-f]{64}$ ]] || fail
for command in docker jq sha256sum; do
    command -v "$command" >/dev/null 2>&1 || fail
done

running=$(docker inspect --format '{{.State.Running}}' "$DB_CONTAINER" 2>/dev/null) || fail
[ "$running" = true ] || fail
component_label=$(docker inspect \
    --format '{{index .Config.Labels "nvidia-build-lb.component"}}' \
    "$DB_CONTAINER" 2>/dev/null) || fail
[ "$component_label" = database ] || fail
compose_service=$(docker inspect \
    --format '{{index .Config.Labels "com.docker.compose.service"}}' \
    "$DB_CONTAINER" 2>/dev/null) || fail
[ "$compose_service" = db ] || fail
compose_project=$(docker inspect \
    --format '{{index .Config.Labels "com.docker.compose.project"}}' \
    "$DB_CONTAINER" 2>/dev/null) || fail
[ -n "$compose_project" ] && [ "$compose_project" != '<no value>' ] || fail

psql_query() {
    docker exec --user 70 "$DB_CONTAINER" \
        psql --no-psqlrc --set ON_ERROR_STOP=1 --tuples-only --no-align \
        --username nvidia_build_lb --dbname nvidia_build_lb \
        --command "$1"
}

revision=$(psql_query 'SELECT version_num FROM alembic_version;') || fail
[[ "$revision" =~ ^[0-9a-z_]{1,64}$ ]] || fail

verifier_record=$(psql_query \
    "SELECT encode(verifier_salt,'hex') || E'\\t' || encode(verifier_digest,'hex') FROM vault_key_verifier WHERE singleton_id=1 AND initialized_at IS NOT NULL;") || fail
IFS=$'\t' read -r vault_verifier_salt vault_verifier_digest extra <<< "$verifier_record"
[[ "$vault_verifier_salt" =~ ^[0-9a-f]{64}$ ]] || fail
[[ "$vault_verifier_digest" =~ ^[0-9a-f]{64}$ ]] || fail
[ -z "${extra:-}" ] || fail

upstream_json=$(psql_query \
    "SELECT COALESCE(jsonb_agg(jsonb_build_object('id',id::text,'fingerprint','sha256:'||fingerprint) ORDER BY id)::text,'[]') FROM upstream_keys;") || fail
upstream_json=$(printf '%s' "$upstream_json" | jq -cS \
    'if type == "array" and all(.[]; (.id | type == "string") and (.fingerprint | test("^sha256:[0-9a-f]{64}$"))) then . else error("invalid") end') || fail
upstream_count=$(printf '%s' "$upstream_json" | jq -er 'length') || fail
upstream_identity_sha256=$(printf '%s' "$upstream_json" | sha256sum \
    | awk 'NR == 1 {print $1} END {if (NR != 1) exit 1}') || fail

downstream_count=$(psql_query 'SELECT count(*) FROM downstream_tokens;') || fail
[[ "$downstream_count" =~ ^[0-9]+$ ]] || fail
downstream_digest_sha256=$(psql_query \
    "SELECT id::text || E'\\t' || encode(token_digest,'hex') FROM downstream_tokens ORDER BY id;" \
    | sha256sum | awk 'NR == 1 {print $1} END {if (NR != 1) exit 1}') || fail

jq -n \
    --arg revision "$revision" \
    --arg vault_verifier_salt "$vault_verifier_salt" \
    --arg vault_verifier_digest "$vault_verifier_digest" \
    --argjson upstream_count "$upstream_count" \
    --arg upstream_identity_sha256 "$upstream_identity_sha256" \
    --argjson upstream_keys "$upstream_json" \
    --argjson downstream_count "$downstream_count" \
    --arg downstream_digest_sha256 "$downstream_digest_sha256" \
    '{schema_version:2,alembic_revision:$revision,vault_verifier_salt:$vault_verifier_salt,vault_verifier_digest:$vault_verifier_digest,upstream_count:$upstream_count,upstream_identity_sha256:$upstream_identity_sha256,upstream_keys:$upstream_keys,downstream_count:$downstream_count,downstream_digest_sha256:$downstream_digest_sha256}' \
    || fail
