# NVIDIA Build LB

Rust Actix gateway와 SvelteKit 관리자 화면으로 구성된 NVIDIA hosted API
load balancer입니다. PostgreSQL(SQLx migration)이 운영 상태의 권위 저장소이며,
두 개의 암호화된 NVIDIA 자격 증명을 rate-aware round-robin으로 분산하고
401/402/429·전송 오류를 cooldown/failover로 처리합니다.

## 구성

- `crates/core`: AES-256-GCM vault, downstream 토큰 해시, 라우터와 cooldown 상태
- `crates/gateway`: OpenAI 호환 chat/streaming 및 embeddings·image·video·audio API,
  관리자 API, PostgreSQL 동기화, Rust prestart/healthcheck/migration 바이너리.
  `main.rs`는 bootstrap/공통 경계, `admin.rs`는 운영 API, `proxy.rs`는
  OpenAI·멀티모달 orchestration, `provider.rs`는 endpoint/response contract,
  `streaming.rs`는 SSE·바이너리 검증을 담당합니다.
- `apps/public`: 인증 없이 aggregate health와 지원 API를 보여주는 공개 SvelteKit
  상태 대시보드 (`/`)
- `apps/admin`: 접근성·반응형 SvelteKit 관리자 UI (`/admin`, loopback 전용)
- `migrations/sqlx`: SQLx 단일 migration 원본
- `compose.yml`: PostgreSQL + migration + gateway 독립 스택

Git 추적 구현·배포 입력 표면은 Rust와 Svelte/TypeScript만 사용합니다. Git에는 Python,
셸 스크립트, 수동 JavaScript 소스를 두지 않으며, SvelteKit이 빌드 과정에서
생성하는 브라우저 번들은 Git에 저장하지 않고 이미지에만 포함합니다. Docker
context에서도 `.venv`, `node_modules`, `target`, 빌드 산출물과 과거 증거 디렉터리를
제외해 레거시 파일이 이미지 빌드 경계에 들어오지 않도록 합니다.

## 빠른 검증

수정 중에는 영향 범위만 실행합니다.

```bash
cargo fmt --all -- --check
cargo test -p nvidia-build-lb-core
cargo test -p nvidia-build-lb-gateway --bins
test -z "$(git ls-files | awk 'tolower($0) ~ /\\.(py|pyw|sh|bash|zsh|js|mjs|cjs|jsx)$/ {print}')"
bun run --cwd apps/admin check
bun run --cwd apps/admin build
bun run --cwd apps/public check
bun run --cwd apps/public build
```

최종 게이트에서만 `cargo test --workspace`, `cargo clippy --workspace --all-targets
-- -D warnings`, Svelte format/knip, 두 Docker image build와 live smoke를 함께
실행합니다.

정적 관리자 화면의 375px·200% 브라우저 smoke 절차는
[`docs/UI_SMOKE.md`](docs/UI_SMOKE.md)에 있습니다.

## 로컬 이미지

로컬 image를 raw `docker build` 명령으로 따로 쌓지 않습니다. app과 migrate는
`nvidia-build-lb:local-smoke` 하나를 공유하고, DB는 역할이 다른
`nvidia-build-lb-postgres:local-smoke` 하나만 재사용합니다. 빌드·사전 reconciliation·종료
cleanup은 [`docs/UI_SMOKE.md`](docs/UI_SMOKE.md)의 단일 절차로만 실행합니다. 이 절차는
고정 Compose project와 동시실행 lock을 사용하고 test 전용 label의 이전 image ID까지
삭제한 뒤 잔존 0개를 확인합니다. 수정 중에는 image를 다시 만들지 않고 Rust/Svelte 영향
테스트만 실행합니다.

운영 compose는 root-only secret directory에 `admin_token`, `vault_master_key`,
`db_password`를 둔 뒤 `NBLB_APP_REGISTRY_DIGEST`와
`NBLB_POSTGRES_REGISTRY_DIGEST`를 immutable digest로 지정합니다.
Magpie TTS의 NVCF invocation endpoint를 교체해야 하는 배포는
`NBLB_MAGPIE_TTS_ENDPOINT`로 명시적으로 덮어씁니다.

## 관리자 UI 보안

Svelte static HTML의 inline bootstrap hash를 Rust가 기동 시 계산해 CSP
`script-src`에 추가합니다. 공개 운영 상태는 `/`에서 제공하고, 운영 관리자 화면은
`/admin` 한 경로로 loopback에서만 제공합니다. 공개 대시보드는 upstream key,
credential, request evidence를 읽지 않습니다.

## Health 계약

- `/health/live`: HTTP process liveness. DB나 NVIDIA key를 읽지 않으며 컨테이너
  healthcheck가 사용합니다.
- `/health/ready`: DB와 최소 한 개의 eligible upstream이 있을 때만 `200`입니다.
- `/health`: 기존 monitor 호환 alias로 `/health/ready`와 같은 status/body를 반환합니다.

응답의 `ready`와 `traffic_ready`는 실제 요청 가능 여부이고, `pair_ready`는 두 slot
모두 분산·장애 전환에 참여할 수 있는지를 뜻합니다. key가 0개인 초기 설정 상태에서는
컨테이너는 healthy지만 readiness endpoint는 `503`입니다.

## Operations와 Hermes QA

공개 Svelte 화면은 `/`, `/status`, `/models`, `/docs`, `/incidents`, `/security`에서
sanitized API `/api/public/v1/*`만 사용합니다. loopback 관리자 화면은 `/admin/*`,
typed API는 `/admin/api/v2/*`입니다. `/admin/api/v1/*`는 기존 배포 호환 adapter이며
신규 UI 계약이 아닙니다.

QA history는 `GET /admin/api/v2/qa/runs?limit=1..100&before=<opaque>`로 조회하며 응답의
`next_before`만 다음 요청에 전달합니다. queued/running run은 배포 전체에서 하나만
허용됩니다. 동시 create의 409 `qa_run_active`는 `details.active_run_id`로 이미 실행 중인
run을 가리킵니다. `hermes-e2e`는 실제 helper 증거가 필요한 `live:true`만 허용합니다.

`hermes-e2e` QA는 UI에서 run을 먼저 생성한 뒤 root-only Rust helper에 해당 ID를
전달합니다.

```bash
sudo NBLB_EXPECTED_COMMIT=<40자리_APP_COMMIT_SHA> \
  /usr/local/sbin/nblb-hermes-cutover apply --qa-run <QA_RUN_UUID>
```

helper는 rollback rehearsal과 최종 Hermes 검증을 마친 뒤 receipt를 먼저 원자 저장하고,
safe scalar evidence만 loopback completion API에 제출합니다. gateway는 제출된 client,
request, attempt, terminal outcome을 PostgreSQL에서 다시 확인합니다. 30분 timeout,
gateway 재시작, helper 실패는 run을 `failed`로 닫으며 실행 중 상태로 방치하지 않습니다.
helper는 QA/commit identity를 어떤 lock·snapshot·client·Docker mutation보다 먼저
검증합니다. `committed` journal 뒤에는 rollback이나 failed completion을 하지 않고 이전
client revoke와 idempotent PASS 제출만 재시도합니다.
