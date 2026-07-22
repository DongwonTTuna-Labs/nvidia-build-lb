# Architecture

## Runtime graph

```text
Cloudflare tunnel
        ↓
Actix Rust gateway :2456 ── HTTPS ── NVIDIA hosted API
        │
        ├── PostgreSQL (SQLx migrations, durable routing/evidence)
        ├── SvelteKit static public product (/)
        └── SvelteKit static admin console (/admin)
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
- `operations/admin.rs`: loopback-only typed admin v2 route와 validation
- `operations/admin_repository.rs`: PostgreSQL query/command와 mutation audit
- `operations/public.rs`: content-free public status/model/incident projection
- `operations/qa.rs`: async traffic QA, restart persistence, armed Hermes completion
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
- `/api/public/v1/*`: 인증 없는 sanitized summary/metric/model/incident/OpenAPI
- `/admin/api/v2/*`: admin bearer와 loopback host boundary를 통과한 typed operations API
- `/admin/api/v1/*`: 이전 배포를 위한 호환 adapter
- `/`, `/status`, `/models`, `/docs`, `/incidents*`, `/security`: 공개 Svelte route
- `/admin/*`: route 기반 관리자 Svelte console. Rust가 build HTML inline bootstrap
  hash를 계산해 CSP에 반영한다.

게이트웨이 통합 계약 테스트는 `tests.rs`, 기능 단위 테스트는 소유 모듈에 둡니다.
관리 UI는 overview, upstreams, clients, routing, models, requests, probes, incidents,
audit, QA, settings의 독립 route로 구성합니다. 공통 shell, auth control, data state,
pagination, status badge만 `$lib/components`에서 공유하고 route가 조회·편집 lifecycle을
소유합니다. polling은 비중첩·hidden-tab skip이며, dirty form과 mutation 중에는 서버
refresh가 사용자 입력을 덮어쓰지 않습니다.

## 상태 모델

라우터는 profile별 cursor를 PostgreSQL에 저장한다. upstream 호출 실패는 outcome에
따라 key counter/cooldown을 갱신하며, 다른 eligible key가 있으면 한 번만 failover
한다. 재시작 시 미완료 attempt는 `abandoned_after_restart`로 닫고 cursor·key 상태를
복원한다.

## 운영 증거 모델

인증·validation을 통과해 routing을 시작한 각 OpenAI-compatible 요청은 content를
제외한 `proxy_requests` parent row 하나를
갖고, 실제 provider 선택마다 `request_attempts` child row를 하나씩 갖는다. 두 row는
서버가 발급한 request UUID로 상관되고 attempt에는 순번, upstream key ID, 결과,
latency, TTFB와 downstream response 시작 여부처럼 운영 판단에 필요한 metadata만
기록한다. 최종 attempt와 parent request, 성공 profile receipt는 한 PostgreSQL
transaction에서 함께 terminal 상태로 전환한다. streaming body가 정상 종료되거나
오류가 나거나 client가 연결을 끊는 경로도 같은 원자적 종료 또는 Drop 복구
transaction을 사용한다. HTTP body handoff 전 Drop은 `failed/handler_abandoned`, handoff
후 실제 downstream Drop만 `cancelled/downstream_cancelled`로 분류한다. gateway owner
lease가 만료된 열린 row는 다음 process가
`abandoned_after_restart`로 정리한다.

완료된 raw request는 advisory transaction lock과 `FOR UPDATE SKIP LOCKED` batch로
minute bucket에 정확히 한 번 반영하고 `rolled_up_at`을 같은 transaction에서 기록한다.
따라서 공개 지표는 raw request table을 읽지 않고 집계 table만으로 제공할 수 있다.
duration/TTFB는 합계와 non-null sample count, cumulative histogram을 함께 유지해 raw
row 없이 평균과 p95 upper bound를 계산한다. worker가 여러 gateway process에서
실행되어도 advisory lock이 중복 집계를 막는다.

운영 DB에는 prompt, tool argument, image/audio/video payload, request/provider response
body, Authorization header, plaintext upstream key나 downstream token을 저장하지 않는다.
operator metadata와 QA evidence JSON은 최상위 scalar 값만 허용하고 key 수와 문자열
길이를 제한한다.

Hermes E2E run은 gateway가 모든 case를 `running`으로 arm하고 30분 동안 외부 root-only
Rust helper를 기다립니다. helper는 receipt 저장과 rollback rehearsal 뒤
`POST /admin/api/v2/qa/runs/{id}/hermes-completion`에 boolean/UUID/count만 제출합니다.
gateway는 request IDs가 같은 downstream client, run 이후 시각, terminal success,
동일 attempt count와 완전한 request window인지 한 transaction에서 다시 검증한 뒤에만
run을 `passed`로 닫습니다. fake Hermes run은 API/DB에서 금지하고 queued/running QA는
partial unique index로 global single-flight입니다. 모든 run은 `fake` 또는
`nvidia_hosted` provider identity를 보존하며 canonical NVIDIA/NVCF HTTPS origin에서
생성·실행된 `nvidia_hosted` run만 completion 권위에 포함됩니다. 과거 출처 미확인 live
history는 `unverified`로 격리합니다. history는 `(started_at,id)` opaque
keyset pagination과 case batch load를 사용합니다. helper는 host mutation 전 exact
live/running/commit identity를 검증하고 committed 이후에는 reconciliation만 수행합니다.
helper pre-commit failure, timeout, gateway restart는 fail-closed입니다.

보존 기간은 terminal request/attempt 30일, minute metric/probe 90일,
audit/terminal QA run 180일이다. 열린 request, 진행 중인 probe/QA run과 incident는
시간만으로 삭제하지 않는다. 30~90일 사이의 미집계 request는 삭제하지 않고,
metric 보존 기간까지도 집계되지 못한 request만 원본과 함께 만료한다.

## 운영 경계

운영 명령은 Rust 바이너리와 `docker compose` 선언만 사용한다. 이미지에는 Node가
runtime dependency로 들어가지 않으며 Svelte build stage에서만 사용한다.
