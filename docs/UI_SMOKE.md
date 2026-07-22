# Public/Admin UI 브라우저 smoke

실제 gateway가 제공하는 Svelte 산출물과 SPA fallback을 확인할 때 사용하는 최소 센서입니다. public과
인증 전/후 admin 화면을 320px, 375px, 200% 배율에서 검증하고 빈 화면, 가로 잘림,
JS 오류, focus 손실, form polling 덮어쓰기 회귀를 잡습니다.

```bash
(
set -Eeuo pipefail

project_name="nblb-ui-smoke"
lock_path="${XDG_RUNTIME_DIR:-/tmp}/nvidia-build-lb-ui-smoke.lock"
exec 9>"$lock_path"
flock --nonblock 9 || {
  printf '%s\n' 'another nvidia-build-lb UI smoke owns the fixed image refs' >&2
  exit 75
}
# Test image refs are deliberately constant. Rebuilding replaces these two
# component refs instead of creating one tag per run.
export NBLB_CI_APP_IMAGE="nvidia-build-lb:local-smoke"
export NBLB_CI_POSTGRES_IMAGE="nvidia-build-lb-postgres:local-smoke"
test_image_label="io.dongwontuna.nvidia-build-lb.test-scope=local-smoke"
export NBLB_APP_REGISTRY_DIGEST="$(printf '0%.0s' {1..64})"
export NBLB_POSTGRES_REGISTRY_DIGEST="$(printf '0%.0s' {1..64})"
export NBLB_PORT=63778
secret_dir=""

remove_test_images() {
  local image
  local image_ids
  local residual_ids
  local -a stale_image_ids
  for image in "$NBLB_CI_APP_IMAGE" "$NBLB_CI_POSTGRES_IMAGE"; do
    if ! image_ids="$(docker image ls --quiet --no-trunc --filter "reference=$image")"; then
      return 1
    fi
    if [[ -n "$image_ids" ]]; then
      docker image rm "$image" || return 1
    fi
  done
  if ! image_ids="$(docker image ls --quiet --no-trunc --filter "label=$test_image_label")"; then
    return 1
  fi
  if [[ -n "$image_ids" ]]; then
    if ! image_ids="$(sort -u <<<"$image_ids")"; then
      return 1
    fi
    mapfile -t stale_image_ids <<<"$image_ids"
    docker image rm "${stale_image_ids[@]}" || return 1
  fi
  for image in "$NBLB_CI_APP_IMAGE" "$NBLB_CI_POSTGRES_IMAGE"; do
    if ! residual_ids="$(docker image ls --quiet --no-trunc --filter "reference=$image")"; then
      return 1
    fi
    if [[ -n "$residual_ids" ]]; then
      return 1
    fi
  done
  if ! residual_ids="$(docker image ls --quiet --no-trunc --filter "label=$test_image_label")"; then
    return 1
  fi
  [[ -z "$residual_ids" ]]
}

cleanup() {
  status=$?
  cleanup_status=0
  trap - EXIT
  set +e
  if [[ -n "$secret_dir" ]]; then
    NBLB_SECRET_DIR="$secret_dir" docker compose --project-name "$project_name" \
      --file compose.yml --file compose.ci.yml down --volumes --remove-orphans || cleanup_status=1
    if ! postgres_image_ids="$(docker image ls --quiet --no-trunc \
      --filter "reference=$NBLB_CI_POSTGRES_IMAGE")"; then
      cleanup_status=1
    elif [[ -n "$postgres_image_ids" ]]; then
      docker run --rm --entrypoint rm \
        --mount "type=bind,source=$secret_dir,target=/secrets" \
        "$NBLB_CI_POSTGRES_IMAGE" -f \
        /secrets/admin_token /secrets/vault_master_key /secrets/db_password || cleanup_status=1
    fi
    rmdir "$secret_dir" 2>/dev/null || cleanup_status=1
  fi
  if ! remove_test_images; then
    printf '%s\n' 'nvidia-build-lb test image cleanup left residual state' >&2
    cleanup_status=1
  fi
  if ((status == 0 && cleanup_status != 0)); then
    status=$cleanup_status
  fi
  exit "$status"
}
trap cleanup EXIT

# Recover containers and volumes from an earlier force-killed run before the
# fixed refs are rebuilt. This never targets the production project name.
NBLB_SECRET_DIR=/tmp docker compose --project-name "$project_name" \
  --file compose.yml --file compose.ci.yml down --volumes --remove-orphans
remove_test_images

bun install --cwd apps/admin --frozen-lockfile --ignore-scripts --no-progress
bun run --cwd apps/admin check
bun run --cwd apps/admin build
bun install --cwd apps/public --frozen-lockfile --ignore-scripts --no-progress
bun run --cwd apps/public check
bun run --cwd apps/public build

# 현재 checkout을 실제 image로 빌드한다. release .env나 운영 digest를 재사용하지 않는다.
docker build --label "$test_image_label" --file Dockerfile.rust --tag "$NBLB_CI_APP_IMAGE" .
docker build --label "$test_image_label" --file docker/postgres.Dockerfile \
  --tag "$NBLB_CI_POSTGRES_IMAGE" .

secret_dir=$(mktemp -d)
chmod 0700 "$secret_dir"
printf 'nblb_admin_%s' "$(openssl rand -hex 32)" > "$secret_dir/admin_token"
openssl rand 32 > "$secret_dir/vault_master_key"
openssl rand -hex 32 > "$secret_dir/db_password"
chmod 0600 "$secret_dir"/*
# Compose의 bind-backed secret은 uid/gid/mode를 적용하지 않는다. 서비스는
# cap_drop: ALL 상태이므로 host 파일도 production 계약과 같은 root:root여야 한다.
docker run --rm --entrypoint chown \
  --mount "type=bind,source=$secret_dir,target=/secrets" \
  "$NBLB_CI_POSTGRES_IMAGE" root:root \
  /secrets/admin_token /secrets/vault_master_key /secrets/db_password

NBLB_SECRET_DIR="$secret_dir" \
docker compose --project-name "$project_name" \
  --file compose.yml --file compose.ci.yml up --detach --wait
# 격리 UI smoke에는 provider key를 넣지 않으므로 liveness만 200이어야 한다.
# readiness는 no_eligible_upstream/pair_not_ready를 담은 503이 정상이다.
curl --fail --silent --show-error http://127.0.0.1:63778/health/live
test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
  http://127.0.0.1:63778/health/ready)" = 503

/home/dongwonttuna/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome \
  --headless=new --no-sandbox --disable-gpu --virtual-time-budget=5000 \
  --window-size=320,800 --screenshot=/tmp/nvidia-build-lb-public-320.png \
  http://127.0.0.1:63778/status
/home/dongwonttuna/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome \
  --headless=new --no-sandbox --disable-gpu --virtual-time-budget=5000 \
  --window-size=375,800 --screenshot=/tmp/nvidia-build-lb-admin-375.png \
  http://127.0.0.1:63778/admin/
)
```

200% 검사는 `--force-device-scale-factor`로 대체하지 않습니다. Playwright가 위 Chromium을
persistent context로 실행하고 `Control+Equal` browser-zoom shortcut을 반복해 200%로
올린 뒤, 750px 물리 창에서 `window.innerWidth`가 375 CSS px인지 확인합니다. zoom 전후
`innerWidth`, `devicePixelRatio`, `visualViewport.width`, screenshot을 함께 기록하며, 단순
HiDPI/device-scale 변화만 생기면 실패입니다. 위 명령은 app/migrate용 한 개와 PostgreSQL용
한 개의 **고정 test image ref**만 재사용합니다. 성공·실패 시 EXIT trap이 격리된 QA
project와 volume, root-owned 임시 secret, 두 test image를 정리하고 고정 ref와 test 전용
label 기준 잔존 0을 검증합니다. 프로세스가 강제 종료되어 trap이 실행되지 않아도 다음
실행은 고정 project를 내린 뒤 같은 두 ref와 test label을 가진 이전 image ID를 먼저
제거합니다. 전역 image prune은 사용하지 않으며 운영 project·volume·immutable digest는
대상이 아닙니다.

증거는 세 PNG와 DOM의 public navigation, `관리 인증이 필요합니다`,
`관리 token`, `열기`입니다. 실제 gateway에서 다음을 추가 검증합니다.

1. public의 status → models → docs → incidents → security를 keyboard로 이동하고 visible
   focus, heading 순서, 44px target, no horizontal scroll을 확인합니다.
2. admin 인증 후 overview → upstream → client 편집 → routing simulation → request detail →
   QA로 이동하며 skip link와 route title을 확인합니다.
3. client/incident input을 수정한 채 30초 이상 두어 polling이 draft를 덮어쓰지 않는지,
   되돌리기와 changed-field 저장이 동작하는지 확인합니다.
4. dialog가 열리면 focus가 dialog 안으로 이동하고 Escape/닫기 뒤 trigger로 돌아오는지,
   mutation·조회 오류와 stale last-good 데이터가 구분되는지 확인합니다.
5. 320px/375px와 browser zoom 200%에서 정보가 잘리지 않고 keyboard focus가 화면 밖으로
   사라지지 않는지 screenshot, DOM, console로 남깁니다.

테스트 후 gateway·tunnel을 정적 서버로 대체하지 않습니다.
