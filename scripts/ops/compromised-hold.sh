#!/usr/bin/env bash
set -Eeuo pipefail

# IDs and closed receipts only; this script never reads a provider key value.
STATE_ROOT=${NBLB_HOLD_STATE_ROOT:-/opt/nvidia-build-lb/state}
JOURNAL=$STATE_ROOT/compromised-hold.json
LOCK=$STATE_ROOT/.compromised-hold.lock
FIXTURE=${NBLB_HOLD_FIXTURE:-0}

die() { printf 'compromised-hold: %s\n' "$1" >&2; exit 1; }
require_root() { [[ "$FIXTURE" == 1 || "$(id -u)" == 0 ]] || die 'root is required (use sudo)'; }
uuid_ok() { [[ "$1" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]; }

write_journal() {
  local phase=$1 id1=$2 id2=$3
  mkdir -p "$STATE_ROOT"; chmod 0700 "$STATE_ROOT"
  python3 - "$JOURNAL" "$phase" "$id1" "$id2" <<'PY'
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4
path, phase, first, second = sys.argv[1:]
ids = sorted((str(UUID(first)), str(UUID(second))))
now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
if Path(path).exists():
    body = json.loads(Path(path).read_text())
    if body.get("expected_revocation_ids") != ids:
        raise SystemExit("journal expected IDs do not match")
    body["phase"] = phase; body["updated_at"] = now
else:
    body = {"version":1,"transaction_id":str(uuid4()),"phase":phase,"created_at":now,"updated_at":now,"expected_revocation_ids":ids,"revocations":[None,None],"failure":None}
tmp = Path(f"{path}.tmp")
tmp.write_text(json.dumps(body, separators=(",", ":")) + "\n"); os.chmod(tmp, 0o600); os.replace(tmp, path)
PY
  chmod 0600 "$JOURNAL"
}

enter() {
  require_root; [[ $# -eq 2 ]] || die 'enter requires two non-secret UUIDs'
  uuid_ok "$1" && uuid_ok "$2" || die 'credential IDs must be canonical v4 UUIDs'
  [[ "$1" != "$2" ]] || die 'credential IDs must be distinct'
  mkdir -p "$STATE_ROOT"; exec 9>"$LOCK"; flock -n 9 || die 'another hold is active'
  if [[ "$FIXTURE" == 1 ]]; then
    write_journal prepared "$1" "$2"; write_journal revocation_pending "$1" "$2"
    printf '%s\n' '{"status":"PASS","phase":"revocation_pending","credentials":"ids-only"}'; return
  fi
  command -v systemctl >/dev/null || { write_journal failed_attention "$1" "$2"; die 'systemd unavailable; no service changed'; }
  write_journal failed_attention "$1" "$2"
  die 'host quiesce/revocation preconditions require explicit operator runbook'
}

status() {
  require_root; [[ -f "$JOURNAL" ]] || die 'no compromised-hold journal exists'
  python3 - "$JOURNAL" <<'PY'
import json, sys
from pathlib import Path
b = json.loads(Path(sys.argv[1]).read_text())
print(json.dumps({"phase":b["phase"],"expected_revocation_ids":b["expected_revocation_ids"],"revocations":b["revocations"]}, separators=(",", ":")))
PY
}

confirm() {
  require_root; [[ $# -eq 1 && -f "$1" ]] || die 'confirm-revocation requires one receipt JSON'
  [[ -f "$JOURNAL" ]] || die 'enter must be completed first'
  python3 - "$JOURNAL" "$1" <<'PY'
import json, os, sys
from pathlib import Path
from uuid import UUID
journal_path, receipt_path = sys.argv[1:]
journal = json.loads(Path(journal_path).read_text()); receipt = json.loads(Path(receipt_path).read_text())
if set(receipt) != {"version","credential_id","state","observed_at","evidence_sha256"} or receipt["version"] != 1 or receipt["state"] not in {"revoked","deleted"}:
    raise SystemExit("invalid revocation receipt")
if str(UUID(receipt["credential_id"])) != receipt["credential_id"] or len(receipt["evidence_sha256"]) != 64 or any(c not in "0123456789abcdef" for c in receipt["evidence_sha256"]):
    raise SystemExit("invalid revocation receipt identity")
ids = journal["expected_revocation_ids"]
if receipt["credential_id"] not in ids: raise SystemExit("receipt ID is not expected")
slot = ids.index(receipt["credential_id"])
if journal["revocations"][slot] not in (None, receipt): raise SystemExit("receipt conflicts with journal")
journal["revocations"][slot] = receipt; journal["phase"] = "complete" if all(journal["revocations"]) else "revocation_pending"; journal["updated_at"] = receipt["observed_at"]
tmp = Path(f"{journal_path}.tmp"); tmp.write_text(json.dumps(journal, separators=(",", ":")) + "\n"); os.chmod(tmp, 0o600); os.replace(tmp, journal_path)
print(json.dumps({"status":"PASS","phase":journal["phase"],"credential_id":receipt["credential_id"]}, separators=(",", ":")))
PY
}

case "${1:-}" in
  enter) shift; enter "$@" ;;
  status) [[ $# -eq 1 ]] || die 'status takes no arguments'; status ;;
  confirm-revocation) shift; confirm "$@" ;;
  *) die 'usage: compromised-hold.sh enter <uuid> <uuid> | status | confirm-revocation <receipt.json>' ;;
esac
