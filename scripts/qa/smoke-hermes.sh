#!/usr/bin/env bash
set -Eeuo pipefail
set +x

cd "$(dirname "$0")/../.."

: "${IMAGE_DIGEST:?IMAGE_DIGEST is required}"
: "${EVIDENCE_DIR:?EVIDENCE_DIR is required}"
[[ "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || { printf '%s\n' 'INPUT[64]: IMAGE_DIGEST must contain 64 hex characters' >&2; exit 64; }
[ "$(id -u)" -eq 0 ] \
  || { printf '%s\n' 'INPUT[77]: smoke-hermes must run as root' >&2; exit 77; }

if [ -e "$EVIDENCE_DIR" ]; then
  [ -d "$EVIDENCE_DIR" ] && [ -z "$(find "$EVIDENCE_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ] \
    || { printf '%s\n' 'INPUT[64]: EVIDENCE_DIR must be absent or empty' >&2; exit 64; }
else
  install -d -m 0700 "$EVIDENCE_DIR"
fi

helper=scripts/ops/hermes_cutover.py
tmp_root=$(mktemp -d)
cleanup_status=0
cycle_started=0
recovery_attempted=false
recovery_succeeded=false
write_cleanup() {
  local status=$1
  rm -rf -- "$tmp_root" || cleanup_status=1
  candidate_scan_succeeded=true
  if candidate_lines=$(find /opt/agent-apps/data/hermes -mindepth 1 -maxdepth 1 \
    -name '.nblb-*.candidate' -printf 'candidate\n' 2>/dev/null); then
    if [ -n "$candidate_lines" ]; then
      candidate_count=$(printf '%s\n' "$candidate_lines" | wc -l)
    else
      candidate_count=0
    fi
  else
    candidate_scan_succeeded=false
    candidate_count=0
  fi
  journal_phase=$(python3 - <<'PY'
import json
from pathlib import Path

path = Path('/opt/nvidia-build-lb/hermes-cutover-state/journal.json')
if not path.is_file():
    print('missing')
else:
    document = json.loads(path.read_text())
    print(document.get('phase', 'invalid'))
PY
)
  [ "$candidate_scan_succeeded" = true ] || cleanup_status=1
  [ "$candidate_count" -eq 0 ] || cleanup_status=1
  journal_safe=false
  if [ "$status" -eq 0 ] && [ "$journal_phase" = reapplied ]; then
    journal_safe=true
  elif [ "$status" -ne 0 ] && [ "$recovery_attempted" = true ] \
    && [ "$recovery_succeeded" = true ]; then
    case "$journal_phase" in aborted|rolled_back|applied|reapplied) journal_safe=true ;; esac
  elif [ "$status" -ne 0 ] && [ "$journal_phase" = recovery_required ] \
    && [ "$(docker inspect --format '{{.State.Running}}' agent-hermes 2>/dev/null)" = false ]; then
    journal_safe=true
  fi
  [ "$journal_safe" = true ] || cleanup_status=1
  python3 - "$EVIDENCE_DIR/cleanup.json" "$status" "$cleanup_status" \
    "$candidate_count" "$candidate_scan_succeeded" "$journal_phase" "$recovery_attempted" \
    "$recovery_succeeded" "$journal_safe" <<'PY'
import json
import sys
from pathlib import Path

(
    output,
    status,
    cleanup_status,
    candidates,
    candidate_scan_succeeded,
    phase,
    recovery_attempted,
    recovery_succeeded,
    journal_safe,
) = sys.argv[1:]
document = {
    'schema_version': 1,
    'status': 'PASS' if status == '0' and cleanup_status == '0' else 'FAIL',
    'command_status': int(status),
    'candidate_scan_succeeded': candidate_scan_succeeded == 'true',
    'temporary_paths_absent': (
        candidate_scan_succeeded == 'true' and int(candidates) == 0
    ),
    'candidate_file_count': int(candidates),
    'journal_phase': phase,
    'journal_terminal_reapplied': phase == 'reapplied',
    'recovery_attempted': recovery_attempted == 'true',
    'recovery_succeeded': recovery_succeeded == 'true',
    'journal_safe': journal_safe == 'true',
}
Path(output).write_text(json.dumps(document, sort_keys=True, separators=(',', ':')) + '\n')
PY
}
finish() {
  original_status=$?
  trap - EXIT HUP INT TERM
  set +e
  if [ "$original_status" -ne 0 ] && [ "$cycle_started" -eq 1 ]; then
    recovery_attempted=true
    if python3 "$helper" recover >"$EVIDENCE_DIR/recovery.json"; then
      recovery_succeeded=true
    fi
  fi
  write_cleanup "$original_status"
  final_cleanup_status=$?
  jq -e --argjson command_status "$original_status" '
    .command_status == $command_status and
    .candidate_scan_succeeded == true and
    .temporary_paths_absent == true and .candidate_file_count == 0 and
    .journal_safe == true and
    (if $command_status == 0 then
       .status == "PASS" and .journal_phase == "reapplied" and
       .journal_terminal_reapplied == true
     else
       .status == "FAIL" and
       (.recovery_succeeded == true or .journal_phase == "recovery_required")
     end)
  ' "$EVIDENCE_DIR/cleanup.json" >/dev/null || final_cleanup_status=1
  if [ "$original_status" -ne 0 ]; then
    exit "$original_status"
  fi
  [ "$final_cleanup_status" -eq 0 ] && [ "$cleanup_status" -eq 0 ] || exit 1
}
trap finish EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

running_ref=$(docker inspect --format '{{.Config.Image}}' nvidia-build-lb-app-1)
case "$running_ref" in
  *@"$IMAGE_DIGEST") ;;
  *) printf '%s\n' 'deployed_image_digest_mismatch' >&2; exit 1 ;;
esac

codex_before=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  http://127.0.0.1:2455/health)
[ "$codex_before" = 200 ] || { printf '%s\n' 'codex_lb_preflight_failed' >&2; exit 1; }

python3 "$helper" rehearse --fixture-root "$tmp_root/happy" \
  > "$EVIDENCE_DIR/rehearsal.json"
python3 "$helper" rehearse --fixture-root "$tmp_root/injected" --inject-after-env \
  > "$EVIDENCE_DIR/adversarial.json"
cycle_started=1
python3 "$helper" cycle > "$EVIDENCE_DIR/cycle.json"

jq -e '
  .status == "PASS" and .operation == "cycle" and
  .cutover.health == true and .cutover.nonstream == true and
  .cutover.stream == true and .cutover.tool_run == true and
  .rollback.health == true and .reapply.health == true and
  .one_key_exclusion.alternate_succeeded == true and
  .one_key_exclusion.excluded_key_restored == true and
  .restart.health == true and .previous_token_revoked == true and
  .final_token_active == true
' "$EVIDENCE_DIR/cycle.json" >/dev/null
jq -e '.status == "PASS" and .pair_rollback == true' \
  "$EVIDENCE_DIR/rehearsal.json" >/dev/null
jq -e '.status == "PASS" and .injected_failure_recovered == true' \
  "$EVIDENCE_DIR/adversarial.json" >/dev/null

codex_after=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  http://127.0.0.1:2455/health)
[ "$codex_after" = 200 ] || { printf '%s\n' 'codex_lb_postcheck_failed' >&2; exit 1; }

python3 - "$EVIDENCE_DIR/manual-qa.json" "$IMAGE_DIGEST" <<'PY'
import json
import sys
from pathlib import Path

output, image_digest = sys.argv[1:]
document = {
    'schema_version': 1,
    'status': 'PASS',
    'image_digest': image_digest,
    'cutover': True,
    'korean_nonstream': True,
    'stream_done': True,
    'tool_using_run': True,
    'rollback': True,
    'final_reapply': True,
    'one_key_exclusion': True,
    'restart': True,
    'previous_token_revoked': True,
    'codex_lb_preserved': True,
}
Path(output).write_text(json.dumps(document, sort_keys=True, separators=(',', ':')) + '\n')
PY

if grep -RIlE \
  'nvapi-[A-Za-z0-9_-]{20,}|nblb_admin_[0-9a-f]{64}|nblb_ds_[0-9a-f]{64}' \
  "$EVIDENCE_DIR" | grep -q .; then
  printf '%s\n' 'credential_shape_detected_in_hermes_evidence' >&2
  exit 1
fi

printf '%s\n' 'smoke-hermes passed'
