#!/usr/bin/env bash
set -Eeuo pipefail
set +x

cd "$(dirname "$0")/../.."

: "${MODE:?MODE is required}"
: "${IMAGE_DIGEST:?IMAGE_DIGEST is required}"
: "${EVIDENCE_DIR:?EVIDENCE_DIR is required}"
case "$MODE" in one-key|two-key) ;; *) printf '%s\n' 'INPUT[64]: invalid MODE' >&2; exit 64 ;; esac
[[ "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || { printf '%s\n' 'INPUT[64]: IMAGE_DIGEST must be a sha256 digest' >&2; exit 64; }
[ "$(id -u)" -eq 0 ] \
  || { printf '%s\n' 'INPUT[77]: smoke-live must run as root' >&2; exit 77; }

state_root=/opt/nvidia-build-lb/hermes-cutover-state
lock_path=$state_root/cutover.lock
if [ -L "$state_root" ] || { [ -e "$state_root" ] && [ ! -d "$state_root" ]; }; then
  printf '%s\n' 'cutover_state_root_invalid' >&2
  exit 1
fi
if [ ! -e "$state_root" ]; then
  install -d -o root -g root -m 0700 "$state_root"
fi
[ "$(stat -c '%u:%g:%a' "$state_root")" = 0:0:700 ] \
  || { printf '%s\n' 'cutover_state_root_metadata_invalid' >&2; exit 1; }
if [ -L "$lock_path" ]; then
  printf '%s\n' 'cutover_lock_invalid' >&2
  exit 1
fi
if [ ! -e "$lock_path" ]; then
  install -o root -g root -m 0600 /dev/null "$lock_path"
fi
[ -f "$lock_path" ] && [ ! -L "$lock_path" ] \
  && [ "$(stat -c '%u:%g:%a:%h' "$lock_path")" = 0:0:600:1 ] \
  || { printf '%s\n' 'cutover_lock_metadata_invalid' >&2; exit 1; }
exec 9<>"$lock_path"
flock -x 9

if [ -e "$EVIDENCE_DIR" ]; then
  [ -d "$EVIDENCE_DIR" ] && [ -z "$(find "$EVIDENCE_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ] \
    || { printf '%s\n' 'INPUT[64]: EVIDENCE_DIR must be absent or empty' >&2; exit 64; }
else
  install -d -m 0700 "$EVIDENCE_DIR"
fi

tmp_root=
hermes_was_running=0
cleanup_status=0
finish() {
  original_status=$?
  trap - EXIT HUP INT TERM
  set +e
  if [ -n "$tmp_root" ]; then
    rm -rf -- "$tmp_root" || cleanup_status=1
  fi
  if [ "$hermes_was_running" -eq 1 ]; then
    /opt/agent-apps/tools/agent-compose up -d --no-deps hermes >/dev/null 2>&1 \
      || cleanup_status=1
  fi
  hermes_health=false
  if [ "$hermes_was_running" -eq 0 ]; then
    if [ "$(docker inspect --format '{{.State.Running}}' agent-hermes 2>/dev/null)" = false ]; then
      hermes_health=true
    fi
  else
    for _ in $(seq 1 120); do
      if curl --fail --silent --show-error --output /dev/null \
        http://127.0.0.1:8642/health; then
        hermes_health=true
        break
      fi
      sleep 1
    done
  fi
  [ "$hermes_health" = true ] || cleanup_status=1
  gateway_health=false
  if [ "$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
      http://127.0.0.1:2456/health 2>/dev/null)" = 200 ]; then
    gateway_health=true
  else
    scripts/ops/production-compose.sh up -d --force-recreate --no-deps app \
      >/dev/null 2>&1 || cleanup_status=1
    for _ in $(seq 1 120); do
      if [ "$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
          http://127.0.0.1:2456/health 2>/dev/null)" = 200 ]; then
        gateway_health=true
        break
      fi
      sleep 1
    done
  fi
  [ "$gateway_health" = true ] || cleanup_status=1
  python3 - "$EVIDENCE_DIR/cleanup.json" "$original_status" "$cleanup_status" \
    "$hermes_health" "$gateway_health" <<'PY'
import json
import sys
from pathlib import Path

output, command_status, cleanup_status, hermes_health, gateway_health = sys.argv[1:]
document = {
    'schema_version': 1,
    'status': 'PASS' if command_status == '0' and cleanup_status == '0' else 'FAIL',
    'command_status': int(command_status),
    'temporary_paths_absent': cleanup_status == '0',
    'hermes_prior_state_restored': hermes_health == 'true',
    'gateway_healthy': gateway_health == 'true',
}
Path(output).write_text(json.dumps(document, sort_keys=True, separators=(',', ':')) + '\n')
PY
  jq -e '
    .status == "PASS" and .command_status == 0 and
    .temporary_paths_absent == true and .hermes_prior_state_restored == true and
    .gateway_healthy == true
  ' "$EVIDENCE_DIR/cleanup.json" >/dev/null || cleanup_status=1
  if [ "$original_status" -ne 0 ]; then
    exit "$original_status"
  fi
  [ "$cleanup_status" -eq 0 ] || exit 1
}
trap finish EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

tmp_root=$(mktemp -d)
hermes_running=$(docker inspect --format '{{.State.Running}}' agent-hermes 2>/dev/null) \
  || { printf '%s\n' 'hermes_state_preflight_failed' >&2; exit 1; }
case "$hermes_running" in
  true) hermes_was_running=1 ;;
  false) hermes_was_running=0 ;;
  *) printf '%s\n' 'hermes_state_preflight_invalid' >&2; exit 1 ;;
esac

codex_before=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  http://127.0.0.1:2455/health)
[ "$codex_before" = 200 ] || { printf '%s\n' 'codex_lb_preflight_failed' >&2; exit 1; }

if [ "$hermes_was_running" -eq 1 ]; then
  /opt/agent-apps/tools/agent-compose stop hermes >/dev/null
  [ "$(docker inspect --format '{{.State.Running}}' agent-hermes)" = false ] \
    || { printf '%s\n' 'hermes_intake_close_unconfirmed' >&2; exit 1; }
fi

python3 scripts/qa/live_smoke.py \
  --mode "$MODE" \
  --image-digest "$IMAGE_DIGEST" \
  --repetitions 3 > "$EVIDENCE_DIR/manual-qa.json"

jq -e --arg mode "$MODE" '
  .status == "PASS" and .mode == $mode and
  (.core_repetitions | length) == 3 and
  all(.core_repetitions[]; .models and .nonstream and .stream) and
  .restart_persistence.same_image == true and
  .restart_persistence.key_state_persistent == true and
  .restart_persistence.scheduler_cursor_persistent == true and
  .restart_persistence.container_recreated == true and
  .restart_persistence.old_logs_scanned == true and
  .restart_persistence.new_logs_scanned == true and
  .restart_persistence.unrelated_containers_unchanged == true and
  all(.restart_persistence.post_restart_per_key_success[]; . == true) and
  .scope_and_revoke.revoked_rejected == true and
  .secret_scan.logs == true and
  .secret_scan.filtered_args == true and
  .secret_scan.filtered_environment == true and
  .cleanup.task_active_token_count == 0 and
  .cleanup.synthetic_upstream_row_count == 0 and
  .cleanup.upstream_row_count == 2 and
  .cleanup.upstream_state_restored == true and
  (if $mode == "two-key" then
     (.round_robin_deltas | length) == 2 and
     all(.round_robin_deltas[]; . == 3) and
     .round_robin.request_count == 6 and
     (.round_robin.sequence | length) == 6 and
     all(.round_robin.per_key_counts[]; . == 3) and
     .round_robin.alternating == true and
     .controlled_failure.alternate_succeeded == true and
     .controlled_cooldown.alternate_succeeded == true and
     .controlled_cooldown.cooled_attempt_delta == 0 and
     .controlled_cooldown.cooldown_preserved == true
   else
     .round_robin == null and .round_robin_deltas == null and
     .controlled_failure == null and .controlled_cooldown == null
   end)
' "$EVIDENCE_DIR/manual-qa.json" >/dev/null

jq '{
  schema_version,
  status,
  mode,
  controlled_failure,
  controlled_cooldown,
  disabled_key_exclusion: .per_key_success,
  scope_and_revoke
}' "$EVIDENCE_DIR/manual-qa.json" > "$EVIDENCE_DIR/adversarial.json"

codex_after=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  http://127.0.0.1:2455/health)
[ "$codex_after" = 200 ] || { printf '%s\n' 'codex_lb_postcheck_failed' >&2; exit 1; }

if grep -RIlE \
  'nvapi-[A-Za-z0-9_-]{20,}|nblb_admin_[0-9a-f]{64}|nblb_ds_[0-9a-f]{64}' \
  "$EVIDENCE_DIR" | grep -q .; then
  printf '%s\n' 'credential_shape_detected_in_live_evidence' >&2
  exit 1
fi

printf '%s\n' 'smoke-live passed'
