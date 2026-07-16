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

schema_shape=$(psql_query "
WITH expected_columns(table_name,column_name,data_type,character_maximum_length,is_nullable) AS (
    VALUES
      ('admin_events','upstream_key_fingerprint','character varying',64::bigint,'YES'),
      ('admin_events','writer_generation','smallint',NULL::bigint,'YES'),
      ('admin_ledger_state','singleton_id','smallint',NULL::bigint,'NO'),
      ('admin_ledger_state','rolled_up_routed_request_count','bigint',NULL::bigint,'NO'),
      ('admin_ledger_state','last_maintenance_completed_at','timestamp with time zone',NULL::bigint,'NO'),
      ('admin_ledger_state','last_pruned_event_rows','bigint',NULL::bigint,'NO'),
      ('admin_ledger_state','last_pruned_attempt_rows','bigint',NULL::bigint,'NO'),
      ('admin_ledger_state','last_capacity_blocker','character varying',32::bigint,'NO')
), observed_columns AS (
    SELECT table_name,column_name,data_type,character_maximum_length,is_nullable
    FROM information_schema.columns
    WHERE table_schema='public' AND (
      table_name='admin_ledger_state' OR
      (table_name='admin_events' AND column_name IN ('upstream_key_fingerprint','writer_generation'))
    )
), expected_constraints(table_name,constraint_name,constraint_type,is_validated,definition) AS (
    VALUES
      ('admin_ledger_state','admin_ledger_state_pkey','p',true,'PRIMARY KEY (singleton_id)'),
      ('admin_events','ck_admin_event_fingerprint_pair','c',false,'CHECK (writer_generation IS NULL OR (upstream_key_id IS NULL) = (upstream_key_fingerprint IS NULL)) NOT VALID'),
      ('admin_events','ck_admin_event_writer_generation','c',false,'CHECK (writer_generation IS NOT NULL AND writer_generation = 5) NOT VALID'),
      ('admin_ledger_state','ck_admin_ledger_capacity_blocker','c',true,'CHECK (last_capacity_blocker::text = ANY (ARRAY[''none''::character varying, ''active_attempts''::character varying, ''reconciliation_grace''::character varying, ''lock_contention''::character varying, ''orphaned_pending''::character varying, ''legacy_unlinked''::character varying]::text[]))'),
      ('admin_ledger_state','ck_admin_ledger_pruned_attempts','c',true,'CHECK (last_pruned_attempt_rows >= 0)'),
      ('admin_ledger_state','ck_admin_ledger_pruned_events','c',true,'CHECK (last_pruned_event_rows >= 0)'),
      ('admin_ledger_state','ck_admin_ledger_rolled_up_count','c',true,'CHECK (rolled_up_routed_request_count >= 0)'),
      ('admin_ledger_state','ck_admin_ledger_singleton','c',true,'CHECK (singleton_id = 1)'),
      ('upstream_attempt_receipts','ck_attempt_receipt_distinct_events','c',true,'CHECK (started_event_id <> terminal_event_id)'),
      ('upstream_attempt_receipts','ck_attempt_receipt_terminal_order','c',true,'CHECK (terminal_committed_at IS NULL OR terminal_committed_at >= started_at)'),
      ('admin_events','fk_admin_event_exact_attempt_terminal','f',true,'FOREIGN KEY (attempt_started_event_id, id) REFERENCES upstream_attempt_receipts(started_event_id, terminal_event_id) ON DELETE RESTRICT'),
      ('upstream_attempt_receipts','uq_attempt_receipt_started_terminal','u',true,'UNIQUE (started_event_id, terminal_event_id)')
), observed_constraints AS (
    SELECT constraint_row.conrelid::regclass::text,constraint_row.conname,
      constraint_row.contype::text,constraint_row.convalidated,
      pg_get_constraintdef(constraint_row.oid,true)
    FROM pg_catalog.pg_constraint constraint_row
    JOIN pg_catalog.pg_namespace namespace_row ON namespace_row.oid=constraint_row.connamespace
    WHERE namespace_row.nspname='public' AND constraint_row.conname IN (
      SELECT constraint_name FROM expected_constraints
    )
), expected_indexes(table_name,index_name,definition) AS (
    VALUES
      ('admin_events','ix_admin_events_request_type','CREATE INDEX ix_admin_events_request_type ON public.admin_events USING btree (request_id, event_type, occurred_at, id)'),
      ('admin_events','ix_admin_events_upstream_key','CREATE INDEX ix_admin_events_upstream_key ON public.admin_events USING btree (upstream_key_id, id)'),
      ('upstream_attempt_receipts','ix_attempt_receipts_request','CREATE INDEX ix_attempt_receipts_request ON public.upstream_attempt_receipts USING btree (request_id, terminal_committed_at, started_event_id)'),
      ('upstream_attempt_receipts','ix_attempt_receipts_terminal_age','CREATE INDEX ix_attempt_receipts_terminal_age ON public.upstream_attempt_receipts USING btree (terminal_committed_at, started_event_id)')
), observed_indexes AS (
    SELECT tablename,indexname,indexdef FROM pg_catalog.pg_indexes
    WHERE schemaname='public' AND indexname IN (SELECT index_name FROM expected_indexes)
), exact_shape AS (
    SELECT
      NOT EXISTS ((SELECT * FROM expected_columns EXCEPT SELECT * FROM observed_columns)
        UNION ALL (SELECT * FROM observed_columns EXCEPT SELECT * FROM expected_columns))
      AND NOT EXISTS ((SELECT * FROM expected_constraints EXCEPT SELECT * FROM observed_constraints)
        UNION ALL (SELECT * FROM observed_constraints EXCEPT SELECT * FROM expected_constraints))
      AND NOT EXISTS ((SELECT * FROM expected_indexes EXCEPT SELECT * FROM observed_indexes)
        UNION ALL (SELECT * FROM observed_indexes EXCEPT SELECT * FROM expected_indexes)) AS valid
), absent_shape AS (
    SELECT NOT EXISTS (SELECT 1 FROM observed_columns)
      AND NOT EXISTS (SELECT 1 FROM observed_constraints)
      AND NOT EXISTS (SELECT 1 FROM observed_indexes)
      AND to_regclass('public.admin_ledger_state') IS NULL AS valid
)
SELECT (SELECT valid::int FROM exact_shape)::text || E'\\t' ||
       (SELECT valid::int FROM absent_shape)::text;") || fail
IFS=$'\t' read -r v3_exact v3_absent extra <<< "$schema_shape"
[[ "$v3_exact" =~ ^[01]$ ]] || fail
[[ "$v3_absent" =~ ^[01]$ ]] || fail
[ -z "${extra:-}" ] || fail
if [ "$revision" = 0004_vault_key_verifier ] \
    && [ "$v3_absent" = 1 ]; then
    state_version=2
elif [ "$revision" = 0005_admin_dashboard_ledger ] \
    && [ "$v3_exact" = 1 ]; then
    state_version=3
else
    fail
fi

verifier_record=$(psql_query \
    "SELECT encode(verifier_salt,'hex') || E'\\t' || encode(verifier_digest,'hex') FROM vault_key_verifier WHERE singleton_id=1 AND initialized_at IS NOT NULL;") || fail
IFS=$'\t' read -r vault_verifier_salt vault_verifier_digest extra <<< "$verifier_record"
[[ "$vault_verifier_salt" =~ ^[0-9a-f]{64}$ ]] || fail
[[ "$vault_verifier_digest" =~ ^[0-9a-f]{64}$ ]] || fail
[ -z "${extra:-}" ] || fail

upstream_json=$(psql_query \
    "SELECT COALESCE(jsonb_agg(jsonb_build_object('id',id::text,'fingerprint','sha256:'||fingerprint) ORDER BY id)::text,'[]') FROM upstream_keys;") || fail
upstream_json=$(printf '%s' "$upstream_json" | jq -acS \
    'if type == "array" and all(.[]; (.id | type == "string") and (.fingerprint | test("^sha256:[0-9a-f]{64}$"))) then . else error("invalid") end') || fail
upstream_count=$(printf '%s' "$upstream_json" | jq -er 'length') || fail
upstream_identity_sha256=$(printf '%s' "$upstream_json" | sha256sum \
    | awk 'NR == 1 {print $1} END {if (NR != 1) exit 1}') || fail

if [ "$state_version" -eq 2 ]; then
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
    exit 0
fi

downstream_json=$(psql_query \
    "SELECT COALESCE(jsonb_agg(jsonb_build_array(id::text,encode(token_digest,'hex')) ORDER BY id)::text,'[]') FROM downstream_tokens;") || fail
downstream_json=$(printf '%s' "$downstream_json" | jq -ace \
    'if type == "array" and all(.[]; type == "array" and length == 2 and (.[0] | test("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")) and (.[1] | test("^[0-9a-f]{64}$"))) then . else error("invalid") end') || fail
downstream_count=$(printf '%s' "$downstream_json" | jq -er 'length') || fail
downstream_digest_sha256=$(printf '%s' "$downstream_json" | sha256sum \
    | awk 'NR == 1 {print $1} END {if (NR != 1) exit 1}') || fail

admin_event_json=$(psql_query "
SELECT COALESCE(jsonb_agg(jsonb_build_array(
    id::text,
    request_id,
    event_type,
    upstream_key_id::text,
    upstream_key_fingerprint,
    downstream_token_id::text,
    outcome_class,
    status_class,
    latency_ms,
    to_char(occurred_at AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'),
    attempt_started_event_id::text,
    writer_generation
) ORDER BY id)::text,'[]') FROM admin_events;") || fail
admin_event_json=$(printf '%s' "$admin_event_json" | jq -ace \
    'if type == "array" and all(.[]; type == "array" and length == 12 and (.[0] | test("^[0-9a-f-]{36}$")) and (.[1] | type == "string") and (.[2] | type == "string") and (.[3] == null or (.[3] | test("^[0-9a-f-]{36}$"))) and (.[4] == null or (.[4] | test("^[0-9a-f]{64}$"))) and (.[5] == null or (.[5] | test("^[0-9a-f-]{36}$"))) and (.[6] | type == "string") and (.[7] == null or (.[7] | type == "string")) and (.[8] == null or (.[8] | type == "number")) and (.[9] | test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\\.[0-9]{6}Z$")) and (.[10] == null or (.[10] | test("^[0-9a-f-]{36}$"))) and (.[11] == null or (.[11] | type == "number"))) then . else error("invalid") end') || fail
admin_event_count=$(printf '%s' "$admin_event_json" | jq -er 'length') || fail
admin_event_identity_sha256=$(printf '%s' "$admin_event_json" | sha256sum \
    | awk 'NR == 1 {print $1} END {if (NR != 1) exit 1}') || fail

attempt_receipt_json=$(psql_query "
SELECT COALESCE(jsonb_agg(jsonb_build_array(
    receipt.started_event_id::text,
    receipt.terminal_event_id::text,
    receipt.upstream_key_id::text,
    receipt.request_id,
    receipt.service_epoch::text,
    receipt.explicit_probe_key_id::text,
    (SELECT COALESCE(jsonb_agg(excluded_id::text ORDER BY excluded_id::text),'[]'::jsonb)
       FROM unnest(receipt.excluded_key_ids) AS excluded_id),
    to_char(receipt.started_at AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'),
    receipt.rate_limit_streak,
    receipt.transient_failure_streak,
    receipt.terminal_outcome,
    receipt.terminal_status_class,
    receipt.terminal_latency_ms,
    to_char(receipt.terminal_cooldown_until AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'),
    receipt.terminal_cooldown_kind,
    to_char(receipt.terminal_committed_at AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')
) ORDER BY receipt.started_event_id)::text,'[]') FROM upstream_attempt_receipts receipt;") || fail
attempt_receipt_json=$(printf '%s' "$attempt_receipt_json" | jq -ace \
    'if type == "array" and all(.[]; type == "array" and length == 16 and (.[0] | test("^[0-9a-f-]{36}$")) and (.[1] | test("^[0-9a-f-]{36}$")) and (.[2] | test("^[0-9a-f-]{36}$")) and (.[3] | type == "string") and (.[4] | test("^[0-9a-f-]{36}$")) and (.[5] == null or (.[5] | test("^[0-9a-f-]{36}$"))) and (.[6] | type == "array") and all(.[6][]; test("^[0-9a-f-]{36}$")) and (.[7] | test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\\.[0-9]{6}Z$")) and (.[8] | type == "number") and (.[9] | type == "number") and (.[10] == null or (.[10] | type == "string")) and (.[11] == null or (.[11] | type == "string")) and (.[12] == null or (.[12] | type == "number")) and (.[13] == null or (.[13] | test("Z$"))) and (.[14] == null or (.[14] | type == "string")) and (.[15] == null or (.[15] | test("Z$")))) then . else error("invalid") end') || fail
attempt_receipt_count=$(printf '%s' "$attempt_receipt_json" | jq -er 'length') || fail
attempt_receipt_identity_sha256=$(printf '%s' "$attempt_receipt_json" | sha256sum \
    | awk 'NR == 1 {print $1} END {if (NR != 1) exit 1}') || fail
pending_attempt_count=$(printf '%s' "$attempt_receipt_json" \
    | jq -er '[.[] | select(.[15] == null)] | length') || fail

live_pin_json=$(psql_query "
SELECT COALESCE(jsonb_agg(jsonb_build_array(
    started_event_id::text,
    upstream_key_id::text,
    service_epoch::text,
    to_char(pinned_at AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')
) ORDER BY started_event_id)::text,'[]') FROM upstream_live_pins;") || fail
live_pin_json=$(printf '%s' "$live_pin_json" | jq -ace \
    'if type == "array" and all(.[]; type == "array" and length == 4 and (.[0] | test("^[0-9a-f-]{36}$")) and (.[1] | test("^[0-9a-f-]{36}$")) and (.[2] | test("^[0-9a-f-]{36}$")) and (.[3] | test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\\.[0-9]{6}Z$"))) then . else error("invalid") end') || fail
live_pin_count=$(printf '%s' "$live_pin_json" | jq -er 'length') || fail
live_pin_identity_sha256=$(printf '%s' "$live_pin_json" | sha256sum \
    | awk 'NR == 1 {print $1} END {if (NR != 1) exit 1}') || fail

admin_ledger_json=$(psql_query "
SELECT jsonb_build_array(
    singleton_id,
    rolled_up_routed_request_count,
    to_char(last_maintenance_completed_at AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'),
    last_pruned_event_rows,
    last_pruned_attempt_rows,
    last_capacity_blocker
)::text FROM admin_ledger_state WHERE singleton_id=1;") || fail
admin_ledger_json=$(printf '%s' "$admin_ledger_json" | jq -ace \
    'if type == "array" and length == 6 and .[0] == 1 and (.[1] | type == "number") and (.[2] | test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\\.[0-9]{6}Z$")) and (.[3] | type == "number") and (.[4] | type == "number") and (.[5] | type == "string") then . else error("invalid") end') || fail
rolled_up_routed_request_count=$(printf '%s' "$admin_ledger_json" | jq -er '.[1]') || fail
admin_ledger_state_sha256=$(printf '%s' "$admin_ledger_json" | sha256sum \
    | awk 'NR == 1 {print $1} END {if (NR != 1) exit 1}') || fail

jq -n \
    --arg revision "$revision" \
    --arg vault_verifier_salt "$vault_verifier_salt" \
    --arg vault_verifier_digest "$vault_verifier_digest" \
    --argjson upstream_count "$upstream_count" \
    --arg upstream_identity_sha256 "$upstream_identity_sha256" \
    --argjson upstream_keys "$upstream_json" \
    --argjson downstream_count "$downstream_count" \
    --arg downstream_digest_sha256 "$downstream_digest_sha256" \
    --argjson admin_event_count "$admin_event_count" \
    --arg admin_event_identity_sha256 "$admin_event_identity_sha256" \
    --argjson attempt_receipt_count "$attempt_receipt_count" \
    --argjson pending_attempt_count "$pending_attempt_count" \
    --arg attempt_receipt_identity_sha256 "$attempt_receipt_identity_sha256" \
    --argjson live_pin_count "$live_pin_count" \
    --arg live_pin_identity_sha256 "$live_pin_identity_sha256" \
    --argjson rolled_up_routed_request_count "$rolled_up_routed_request_count" \
    --arg admin_ledger_state_sha256 "$admin_ledger_state_sha256" \
    '{schema_version:3,alembic_revision:$revision,vault_verifier_salt:$vault_verifier_salt,vault_verifier_digest:$vault_verifier_digest,upstream_count:$upstream_count,upstream_identity_sha256:$upstream_identity_sha256,upstream_keys:$upstream_keys,downstream_count:$downstream_count,downstream_digest_sha256:$downstream_digest_sha256,admin_event_count:$admin_event_count,admin_event_identity_sha256:$admin_event_identity_sha256,attempt_receipt_count:$attempt_receipt_count,pending_attempt_count:$pending_attempt_count,attempt_receipt_identity_sha256:$attempt_receipt_identity_sha256,live_pin_count:$live_pin_count,live_pin_identity_sha256:$live_pin_identity_sha256,rolled_up_routed_request_count:$rolled_up_routed_request_count,admin_ledger_state_sha256:$admin_ledger_state_sha256}' \
    || fail
