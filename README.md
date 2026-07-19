# NVIDIA Build LB

Rust Actix gateway와 SvelteKit 관리자 화면으로 구성된 NVIDIA hosted API
load balancer입니다. PostgreSQL(SQLx migration)이 운영 상태의 권위 저장소이며,
두 개의 암호화된 NVIDIA 자격 증명을 rate-aware round-robin으로 분산하고
401/402/429·전송 오류를 cooldown/failover로 처리합니다.

## 구성

- `crates/core`: AES-256-GCM vault, downstream 토큰 해시, 라우터와 cooldown 상태
- `crates/gateway`: OpenAI 호환 chat/streaming 및 embeddings·image·video·audio API,
  관리자 API, PostgreSQL 동기화, Rust prestart/healthcheck/migration 바이너리
- `apps/admin`: 접근성·반응형 SvelteKit 관리자 UI (`/admin`)
- `migrations/sqlx`: SQLx 단일 migration 원본
- `compose.yml`: PostgreSQL + migration + gateway 독립 스택

저장소의 구현 소스는 Rust와 Svelte/TypeScript만 사용합니다. 저장소에는 Python,
셸 스크립트, 수동 JavaScript 소스를 두지 않으며, SvelteKit이 빌드 과정에서
생성하는 브라우저 번들은 Git에 저장하지 않고 이미지에만 포함합니다.

## 빠른 검증

수정 중에는 영향 범위만 실행합니다.

```bash
cargo fmt --all -- --check
cargo test -p nvidia-build-lb-core
cargo test -p nvidia-build-lb-gateway --bins
test -z "$(git ls-files -- '*.py' '*.sh' '*.js')"
npm --prefix apps/admin run check
npm --prefix apps/admin run build
```

최종 게이트에서만 `cargo test --workspace`, `cargo clippy --workspace --all-targets
-- -D warnings`, Svelte format/knip, 두 Docker image build와 live smoke를 함께
실행합니다.

## 로컬 이미지

```bash
docker build --file Dockerfile.rust --tag nvidia-build-lb:local .
docker build --file docker/postgres.Dockerfile --tag nvidia-build-lb-postgres:local .
```

운영 compose는 root-only secret directory에 `admin_token`, `vault_master_key`,
`db_password`를 둔 뒤 `NBLB_APP_REGISTRY_DIGEST`와
`NBLB_POSTGRES_REGISTRY_DIGEST`를 immutable digest로 지정합니다.

## 관리자 UI 보안

Svelte static HTML의 inline bootstrap hash를 Rust가 기동 시 계산해 CSP
`script-src`에 추가합니다. `/admin/showcase`는 명시적인 Rust route로 제공하며
`/admin/showcase/`는 canonical URL로 redirect합니다.
