#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"

EVIDENCE_DIR=${EVIDENCE_DIR:-.omo/evidence/task-7-nvidia-build-lb}
IMAGE_DIGEST=${IMAGE_DIGEST:-}
POSTGRES_IMAGE_DIGEST=${POSTGRES_IMAGE_DIGEST:-}
SOURCE_MANIFEST=${SOURCE_MANIFEST:-}
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
TEMP_DIR=""
GITLEAKS_IMAGE='ghcr.io/gitleaks/gitleaks@sha256:cdbb7c955abce02001a9f6c9f602fb195b7fadc1e812065883f695d1eeaba854'
TRIVY_IMAGE='aquasec/trivy@sha256:bcc376de8d77cfe086a917230e818dc9f8528e3c852f7b1aff648949b6258d1c'
PYTHON_BASE_IMAGE=""
HISTORY_COMMITS=0
IMAGE_VULNERABILITIES=-1
IMAGE_SECRETS=-1
POSTGRES_IMAGE_VULNERABILITIES=-1
POSTGRES_IMAGE_SECRETS=-1
FILESYSTEM_MISCONFIGURATIONS=-1
FILESYSTEM_SECRETS=-1
BASE_PUBLIC_GPG_KEY_VERIFIED=false

fail() {
    printf '%s\n' "${1:-scan_release_failed}" >&2
    exit 1
}

container_count() {
    docker ps -aq --filter "label=nvidia-build-lb.run=$RUN_ID" | wc -l | tr -d ' '
}

cleanup() {
    trigger_status=$?
    trap - EXIT HUP INT TERM
    set +e
    cleanup_error=0
    if [ -n "$TEMP_DIR" ] && [ -d "$TEMP_DIR" ]; then
        rm -rf -- "$TEMP_DIR" >/dev/null 2>&1 || cleanup_error=1
    fi
    containers=$(container_count) || { containers=-1; cleanup_error=1; }
    temporary_directories=0
    [ -z "$TEMP_DIR" ] || [ ! -e "$TEMP_DIR" ] || temporary_directories=1
    cleanup_status=PASS
    final_status=$trigger_status
    if [ "$cleanup_error" -ne 0 ] || [ "$containers" -ne 0 ] \
        || [ "$temporary_directories" -ne 0 ]; then
        cleanup_status=FAIL
        final_status=1
    fi
    if [ ! -e "$EVIDENCE_DIR/manual-qa.json" ]; then
        jq -n --arg run_id "$RUN_ID" --arg status FAIL \
            '{schema_version:1,run_id:$run_id,status:$status}' \
            > "$EVIDENCE_DIR/manual-qa.json" 2>/dev/null || final_status=1
    fi
    if [ ! -e "$EVIDENCE_DIR/adversarial.json" ]; then
        jq -n --arg run_id "$RUN_ID" --arg status FAIL \
            '{schema_version:1,run_id:$run_id,status:$status}' \
            > "$EVIDENCE_DIR/adversarial.json" 2>/dev/null || final_status=1
    fi
    jq -n \
        --arg run_id "$RUN_ID" --arg status "$cleanup_status" \
        --argjson trigger_status "$trigger_status" --argjson final_status "$final_status" \
        --argjson cleanup_error "$cleanup_error" --argjson containers "$containers" \
        --argjson temporary_directories "$temporary_directories" \
        '{schema_version:1,run_id:$run_id,status:$status,trigger_exit_status:$trigger_status,final_exit_status:$final_status,cleanup_command_error:($cleanup_error != 0),remaining:{containers:$containers,temp_directories:$temporary_directories}}' \
        > "$EVIDENCE_DIR/cleanup.json" 2>/dev/null || final_status=1
    exit "$final_status"
}

for command in awk cmp docker flock git grep jq mktemp realpath sed sha256sum stat uv; do
    command -v "$command" >/dev/null 2>&1 || fail required_command_unavailable
done
[[ "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || fail image_digest_invalid
[[ "$POSTGRES_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail postgres_image_digest_invalid
[ -f "$SOURCE_MANIFEST" ] && [ ! -L "$SOURCE_MANIFEST" ] \
    || fail source_manifest_invalid
SOURCE_MANIFEST=$(realpath -e "$SOURCE_MANIFEST")
observed_image=$(docker image inspect --format '{{.Id}}' "$IMAGE_DIGEST" 2>/dev/null) \
    || fail image_unavailable
[ "$observed_image" = "$IMAGE_DIGEST" ] || fail image_digest_mismatch
observed_postgres_image=$(docker image inspect --format '{{.Id}}' \
    "$POSTGRES_IMAGE_DIGEST" 2>/dev/null) || fail postgres_image_unavailable
[ "$observed_postgres_image" = "$POSTGRES_IMAGE_DIGEST" ] \
    || fail postgres_image_digest_mismatch
repository_shallow=$(git rev-parse --is-shallow-repository 2>/dev/null) \
    || fail git_history_unavailable
[ "$repository_shallow" = false ] || fail git_history_shallow
git rev-parse --verify HEAD >/dev/null 2>&1 || fail git_history_unavailable

mkdir -p "$(dirname "$EVIDENCE_DIR")"
exec 9>"$EVIDENCE_DIR.claim"
flock -n 9 || fail evidence_exists
if [ -e "$EVIDENCE_DIR" ] || ! mkdir "$EVIDENCE_DIR" 2>/dev/null; then
    fail evidence_exists
fi
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

TEMP_DIR=$(mktemp -d /tmp/nblb-scan-release.XXXXXX)
mkdir "$TEMP_DIR/metadata-raw" "$TEMP_DIR/metadata-scan" \
    "$TEMP_DIR/trivy-cache" "$TEMP_DIR/adversarial"
uv run python -m scripts.qa.source_manifest \
    --root "$ROOT" --output "$TEMP_DIR/source-manifest-current.json"
cmp "$SOURCE_MANIFEST" "$TEMP_DIR/source-manifest-current.json" \
    || fail source_manifest_mismatch
cp "$SOURCE_MANIFEST" "$EVIDENCE_DIR/source-manifest.json"
chmod 0444 "$EVIDENCE_DIR/source-manifest.json"
source_sha256=$(jq -er '.source_tree_sha256' "$SOURCE_MANIFEST")
source_manifest_sha256=$(sha256sum "$SOURCE_MANIFEST" \
    | awk 'NR == 1 {print $1} END {if (NR != 1) exit 1}')
app_source_sha256=$(docker image inspect \
    --format '{{index .Config.Labels "nvidia-build-lb.source-sha256"}}' \
    "$IMAGE_DIGEST")
postgres_source_sha256=$(docker image inspect \
    --format '{{index .Config.Labels "nvidia-build-lb.source-sha256"}}' \
    "$POSTGRES_IMAGE_DIGEST")
[ "$app_source_sha256" = "$source_sha256" ] \
    && [ "$postgres_source_sha256" = "$source_sha256" ] \
    || fail image_source_mismatch
uv lock --check
uv run python scripts/qa/check_action_pins.py .github/workflows
uv run python -m scripts.qa.scan_sensitive_patterns "$ROOT"
uv export --frozen --all-groups --no-emit-project --no-hashes \
    --output-file "$TEMP_DIR/requirements.txt" >/dev/null
uv run pip-audit --strict --progress-spinner off \
    --requirement "$TEMP_DIR/requirements.txt"

docker image inspect --format '{{json .Config}}' "$IMAGE_DIGEST" \
    > "$TEMP_DIR/metadata-raw/image-config.json"
docker history --no-trunc --format '{{.CreatedBy}}' "$IMAGE_DIGEST" \
    > "$TEMP_DIR/metadata-raw/image-history.txt"
docker image inspect --format '{{json .Config}}' "$POSTGRES_IMAGE_DIGEST" \
    > "$TEMP_DIR/metadata-raw/postgres-image-config.json"
docker history --no-trunc --format '{{.CreatedBy}}' "$POSTGRES_IMAGE_DIGEST" \
    > "$TEMP_DIR/metadata-raw/postgres-image-history.txt"

mapfile -t python_base_images < <(
    awk '$1 == "FROM" && $2 ~ /^python:[^@]+@sha256:[0-9a-f]{64}$/ {print $2}' \
        Dockerfile
)
[ "${#python_base_images[@]}" -eq 2 ] \
    && [ "${python_base_images[0]}" = "${python_base_images[1]}" ] \
    || fail python_base_image_contract_invalid
PYTHON_BASE_IMAGE=${python_base_images[0]}
docker image inspect --format '{{json .Config}}' "$PYTHON_BASE_IMAGE" \
    > "$TEMP_DIR/metadata-raw/base-image-config.json" \
    || fail python_base_image_unavailable
docker history --no-trunc --format '{{.CreatedBy}}' "$PYTHON_BASE_IMAGE" \
    > "$TEMP_DIR/metadata-raw/base-image-history.txt"

candidate_gpg_entry=$(jq -er \
    '[(.Env // [])[] | select(startswith("GPG_KEY="))]
     | select(length == 1) | .[0] | select(length > 8)' \
    "$TEMP_DIR/metadata-raw/image-config.json") \
    || fail candidate_public_gpg_key_invalid
base_gpg_entry=$(jq -er \
    '[(.Env // [])[] | select(startswith("GPG_KEY="))]
     | select(length == 1) | .[0] | select(length > 8)' \
    "$TEMP_DIR/metadata-raw/base-image-config.json") \
    || fail base_public_gpg_key_invalid
[ "$candidate_gpg_entry" = "$base_gpg_entry" ] \
    || fail candidate_public_gpg_key_drift

grep -F 'GPG_KEY=' "$TEMP_DIR/metadata-raw/image-history.txt" \
    > "$TEMP_DIR/metadata-raw/candidate-gpg-history.txt"
grep -F 'GPG_KEY=' "$TEMP_DIR/metadata-raw/base-image-history.txt" \
    > "$TEMP_DIR/metadata-raw/base-gpg-history.txt"
[ -s "$TEMP_DIR/metadata-raw/candidate-gpg-history.txt" ] \
    && cmp "$TEMP_DIR/metadata-raw/candidate-gpg-history.txt" \
        "$TEMP_DIR/metadata-raw/base-gpg-history.txt" \
    || fail candidate_public_gpg_history_drift

jq --arg verified "$base_gpg_entry" \
    '.Env |= map(if . == $verified
        then "GPG_KEY=<verified-pinned-base-public-key>" else . end)' \
    "$TEMP_DIR/metadata-raw/image-config.json" \
    > "$TEMP_DIR/metadata-scan/image-config.json"
sed -E 's/GPG_KEY=[^ ]+/GPG_KEY=<verified-pinned-base-public-key>/g' \
    "$TEMP_DIR/metadata-raw/image-history.txt" \
    > "$TEMP_DIR/metadata-scan/image-history.txt"
cp "$TEMP_DIR/metadata-raw/postgres-image-config.json" \
    "$TEMP_DIR/metadata-scan/postgres-image-config.json"
cp "$TEMP_DIR/metadata-raw/postgres-image-history.txt" \
    "$TEMP_DIR/metadata-scan/postgres-image-history.txt"
if grep -Fq "$base_gpg_entry" "$TEMP_DIR/metadata-scan/image-config.json" \
    || grep -Fq "$base_gpg_entry" "$TEMP_DIR/metadata-scan/image-history.txt"; then
    fail public_gpg_key_normalization_failed
fi
BASE_PUBLIC_GPG_KEY_VERIFIED=true

docker run --rm --network none --read-only \
    --label "nvidia-build-lb.run=$RUN_ID" \
    --user "$(id -u):$(id -g)" \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --mount "type=bind,source=$ROOT,target=/repo,readonly" \
    "$GITLEAKS_IMAGE" dir /repo --config /repo/.gitleaks.toml \
    --no-banner --redact --exit-code 1 > "$TEMP_DIR/gitleaks-source.log" 2>&1
if grep -Fqi 'permission denied' "$TEMP_DIR/gitleaks-source.log"; then
    fail gitleaks_source_incomplete
fi
docker run --rm --network none --read-only \
    --label "nvidia-build-lb.run=$RUN_ID" \
    --user "$(id -u):$(id -g)" \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --mount "type=bind,source=$TEMP_DIR/metadata-scan,target=/scan,readonly" \
    --mount "type=bind,source=$ROOT/.gitleaks.toml,target=/config/gitleaks.toml,readonly" \
    "$GITLEAKS_IMAGE" dir /scan --config /config/gitleaks.toml \
    --no-banner --redact --exit-code 1 > "$TEMP_DIR/gitleaks-metadata.log" 2>&1
if grep -Fqi 'permission denied' "$TEMP_DIR/gitleaks-metadata.log"; then
    fail gitleaks_metadata_incomplete
fi

HISTORY_COMMITS=$(git rev-list --count HEAD)
docker run --rm --network none --read-only \
    --label "nvidia-build-lb.run=$RUN_ID" \
    --user "$(id -u):$(id -g)" \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --mount "type=bind,source=$ROOT,target=/repo,readonly" \
    "$GITLEAKS_IMAGE" git /repo --config /repo/.gitleaks.toml \
    --no-banner --redact --exit-code 1 > "$TEMP_DIR/gitleaks-history.log" 2>&1
if grep -Fqi 'permission denied' "$TEMP_DIR/gitleaks-history.log"; then
    fail gitleaks_history_incomplete
fi

docker run --rm \
    --label "nvidia-build-lb.run=$RUN_ID" \
    --user "$(id -u):$(id -g)" \
    --group-add "$(stat -c '%g' /var/run/docker.sock)" \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock \
    --env XDG_CACHE_HOME=/cache \
    --mount "type=bind,source=$TEMP_DIR/trivy-cache,target=/cache" \
    "$TRIVY_IMAGE" image --scanners vuln,secret \
    --cache-dir /cache --skip-version-check --severity HIGH,CRITICAL \
    --exit-code 0 --format json \
    "$IMAGE_DIGEST" > "$TEMP_DIR/trivy-image.json"
IMAGE_VULNERABILITIES=$(jq \
    '[.Results[]?.Vulnerabilities[]?] | length' "$TEMP_DIR/trivy-image.json")
IMAGE_SECRETS=$(jq '[.Results[]?.Secrets[]?] | length' "$TEMP_DIR/trivy-image.json")
[ "$IMAGE_VULNERABILITIES" -eq 0 ] && [ "$IMAGE_SECRETS" -eq 0 ] \
    || fail image_scan_failed

docker run --rm \
    --label "nvidia-build-lb.run=$RUN_ID" \
    --user "$(id -u):$(id -g)" \
    --group-add "$(stat -c '%g' /var/run/docker.sock)" \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock \
    --env XDG_CACHE_HOME=/cache \
    --mount "type=bind,source=$TEMP_DIR/trivy-cache,target=/cache" \
    "$TRIVY_IMAGE" image --scanners vuln,secret \
    --cache-dir /cache --skip-version-check --severity HIGH,CRITICAL \
    --exit-code 0 --format json \
    "$POSTGRES_IMAGE_DIGEST" > "$TEMP_DIR/trivy-postgres-image.json"
POSTGRES_IMAGE_VULNERABILITIES=$(jq \
    '[.Results[]?.Vulnerabilities[]?] | length' "$TEMP_DIR/trivy-postgres-image.json")
POSTGRES_IMAGE_SECRETS=$(jq \
    '[.Results[]?.Secrets[]?] | length' "$TEMP_DIR/trivy-postgres-image.json")
[ "$POSTGRES_IMAGE_VULNERABILITIES" -eq 0 ] \
    && [ "$POSTGRES_IMAGE_SECRETS" -eq 0 ] \
    || fail postgres_image_scan_failed

docker run --rm \
    --label "nvidia-build-lb.run=$RUN_ID" \
    --user "$(id -u):$(id -g)" \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --mount "type=bind,source=$ROOT,target=/repo,readonly" \
    --env XDG_CACHE_HOME=/cache \
    --mount "type=bind,source=$TEMP_DIR/trivy-cache,target=/cache" \
    "$TRIVY_IMAGE" fs --scanners misconfig,secret \
    --cache-dir /cache --skip-version-check --ignorefile /repo/.trivyignore \
    --severity HIGH,CRITICAL --exit-code 0 --format json \
    --skip-dirs /repo/.git --skip-dirs /repo/.omo --skip-dirs /repo/.venv \
    --skip-dirs /repo/node_modules /repo > "$TEMP_DIR/trivy-filesystem.json"
FILESYSTEM_MISCONFIGURATIONS=$(jq \
    '[.Results[]?.Misconfigurations[]?] | length' "$TEMP_DIR/trivy-filesystem.json")
FILESYSTEM_SECRETS=$(jq \
    '[.Results[]?.Secrets[]?] | length' "$TEMP_DIR/trivy-filesystem.json")
[ "$FILESYSTEM_MISCONFIGURATIONS" -eq 0 ] \
    && [ "$FILESYSTEM_SECRETS" -eq 0 ] \
    || fail filesystem_scan_failed

mkdir "$TEMP_DIR/adversarial/workflows" "$TEMP_DIR/adversarial/source"
printf '%s\n' 'steps:' '  - uses: actions/checkout@v4' \
    > "$TEMP_DIR/adversarial/workflows/unpinned.yml"
if uv run python scripts/qa/check_action_pins.py \
    "$TEMP_DIR/adversarial/workflows" >/dev/null 2>&1; then
    fail action_pin_adversarial_failed
fi
git -C "$TEMP_DIR/adversarial/source" init -q
printf 'nblb_ds_%064d\n' 0 > "$TEMP_DIR/adversarial/source/credential.txt"
if uv run python -m scripts.qa.scan_sensitive_patterns \
    "$TEMP_DIR/adversarial/source" >/dev/null 2>&1; then
    fail credential_scan_adversarial_failed
fi
rm -rf -- "$TEMP_DIR/adversarial"

uv run python -m scripts.qa.source_manifest \
    --root "$ROOT" --output "$TEMP_DIR/source-manifest-final.json"
cmp "$SOURCE_MANIFEST" "$TEMP_DIR/source-manifest-final.json" \
    || fail source_manifest_changed
[ "$(docker image inspect --format '{{.Id}}' "$IMAGE_DIGEST")" = "$IMAGE_DIGEST" ] \
    || fail image_digest_changed
[ "$(docker image inspect --format '{{.Id}}' "$POSTGRES_IMAGE_DIGEST")" = "$POSTGRES_IMAGE_DIGEST" ] \
    || fail postgres_image_digest_changed

jq -n \
    --arg run_id "$RUN_ID" --arg status PASS --arg image_digest "$IMAGE_DIGEST" \
    --arg postgres_image_digest "$POSTGRES_IMAGE_DIGEST" \
    --arg source_tree_sha256 "$source_sha256" \
    --arg source_manifest_sha256 "$source_manifest_sha256" \
    --arg gitleaks_image "$GITLEAKS_IMAGE" --arg trivy_image "$TRIVY_IMAGE" \
    --arg python_base_image "$PYTHON_BASE_IMAGE" \
    --argjson base_public_gpg_key_verified "$BASE_PUBLIC_GPG_KEY_VERIFIED" \
    --argjson history_commits "$HISTORY_COMMITS" \
    --argjson image_vulnerabilities "$IMAGE_VULNERABILITIES" \
    --argjson image_secrets "$IMAGE_SECRETS" \
    --argjson postgres_image_vulnerabilities "$POSTGRES_IMAGE_VULNERABILITIES" \
    --argjson postgres_image_secrets "$POSTGRES_IMAGE_SECRETS" \
    --argjson filesystem_misconfigurations "$FILESYSTEM_MISCONFIGURATIONS" \
    --argjson filesystem_secrets "$FILESYSTEM_SECRETS" \
    '{schema_version:2,run_id:$run_id,status:$status,image_digest:$image_digest,postgres_image_digest:$postgres_image_digest,source_tree_sha256:$source_tree_sha256,source_manifest_sha256:$source_manifest_sha256,gates:{source_credential_patterns:"PASS",filesystem_gitleaks:"PASS",git_history_complete:"PASS",git_history_gitleaks:"PASS",python_dependency_audit:"PASS",action_sha_pins:"PASS",image_history_and_config_gitleaks:"PASS",pinned_base_public_gpg_key_verified:$base_public_gpg_key_verified,image_trivy:"PASS",postgres_image_trivy:"PASS",filesystem_trivy:"PASS",source_manifest_final_byte_exact:"PASS"},scanner_images:{gitleaks:$gitleaks_image,trivy:$trivy_image,python_base:$python_base_image},counts:{history_commits:$history_commits,image_high_critical_vulnerabilities:$image_vulnerabilities,image_secrets:$image_secrets,postgres_image_high_critical_vulnerabilities:$postgres_image_vulnerabilities,postgres_image_secrets:$postgres_image_secrets,filesystem_high_critical_misconfigurations:$filesystem_misconfigurations,filesystem_secrets:$filesystem_secrets}}' \
    > "$EVIDENCE_DIR/manual-qa.json"
jq -n --arg run_id "$RUN_ID" --arg status PASS \
    '{schema_version:1,run_id:$run_id,status:$status,probes:{mutable_action_reference_rejected:true,product_credential_shape_rejected:true}}' \
    > "$EVIDENCE_DIR/adversarial.json"
