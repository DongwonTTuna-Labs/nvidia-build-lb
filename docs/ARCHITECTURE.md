# Architecture

## Runtime graph

```text
Cloudflare tunnel
        ↓
Actix Rust gateway :2456 ── HTTPS ── NVIDIA hosted API
        │
        ├── PostgreSQL (SQLx migrations, durable routing/evidence)
        └── SvelteKit static admin (/admin)
```

`nblb-prestart`가 secret을 runtime tmpfs로 복사한 뒤 gateway와 migration을 UID
65532로 실행한다. gateway는 PostgreSQL 연결이 끊기면 supervisor가 재시작할 수
있도록 fail-closed로 종료한다.

## Source boundaries

`crates/gateway/src/main.rs`는 process bootstrap, shared state, route wiring,
database lifecycle과 cross-cutting authorization만 소유한다. HTTP 책임은 다음
모듈로 분리한다.

- `health.rs`: provider와 분리된 liveness 및 DB/eligible traffic readiness
- `request_id.rs`: dynamic API correlation과 public-host admin 차단 middleware
- `admin.rs`: evidence, key/client mutation과 운영 pagination DTO
- `proxy.rs`: OpenAI chat와 multimodal transport/failover orchestration
- `provider.rs`: upstream endpoint mapping, modality preparation과 response contract
- `streaming.rs`: SSE priming/terminal accounting과 이미지·오디오·비디오 payload 검증

이 경계는 기능별 targeted test와 독립적인 리뷰를 가능하게 하며, secret custody와
durable ledger 같은 공통 권위는 `AppState`를 통해서만 공유한다.

## HTTP surface

- `/health/live`: DB/provider와 독립적인 process liveness
- `/health/ready`, `/health`: DB와 eligible upstream traffic readiness
- `/v1/models`, `/v1/chat/completions`: OpenAI 호환 text/vision/streaming
- `/v1/embeddings`, `/v1/images/generations`, `/v1/videos/generations`,
  `/v1/audio/speech`, `/v1/audio/transcriptions`, `/v1/nvidia/inference`: profile별
  modality validation과 provider response validation
- `/admin/api/v1/*`: admin bearer + host boundary를 통과한 운영 DTO
- `/admin`: Svelte static UI. Rust가 build HTML inline bootstrap hash를 계산해
  CSP에 반영한다.

게이트웨이 계약 테스트는 `tests.rs`에 두어 process/bootstrap 코드와 분리하고,
관리 UI의 화면 단위는 `OverviewPanel`, `RoutingStatusPanel`, `ClientsPanel`,
`ModelsPanel`, `EvidencePanel`로 구성하고, route 파일은 상태 오케스트레이션과
접근성·custody lifecycle에 집중한다.

## 상태 모델

라우터는 profile별 cursor를 PostgreSQL에 저장한다. upstream 호출 실패는 outcome에
따라 key counter/cooldown을 갱신하며, 다른 eligible key가 있으면 한 번만 failover
한다. 재시작 시 미완료 attempt는 `abandoned_after_restart`로 닫고 cursor·key 상태를
복원한다.

## 운영 경계

운영 명령은 Rust 바이너리와 `docker compose` 선언만 사용한다. 이미지에는 Node가
runtime dependency로 들어가지 않으며 Svelte build stage에서만 사용한다.
