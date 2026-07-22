# NVIDIA Build LB canonical design

이 문서는 목표 동작과 API/UI 계약의 단일 기준축이다. 첨부 설계, 과거 계획, 현재
구현이 이 문서와 충돌하면 이 문서의 목표 계약을 우선하고 차이는 구현 결함 또는
아직 이행하지 않은 migration으로 기록한다. 저장소·runtime·로그는 “현재 무엇이
실제로 동작하는가”의 증거이며 목표 계약을 암묵적으로 바꾸지 않는다. 계약 변경은
이 문서를 먼저 수정하고 독립 리뷰를 통과해야 한다. 구현 소스는 Rust와
Svelte/TypeScript로 유지하고, PostgreSQL을 영속 상태의 권위 저장소로 사용한다.

## 제품 목표와 정보 원칙

사용자는 각 화면에서 `지금 상태 → 막힌 이유 → 다음 행동 → 상세 증거` 순서로
정보를 읽는다. 첫 화면은 한 개의 최우선 행동만 강조하고 나머지는 접을 수 있는
대기열로 제공한다. 확인되지 않은 provider 기능을 “지원됨”이나 “정상”으로 표현하지
않고 `catalogued`, `provider proof`, `available now`를 구분한다.

공개 화면은 운영 aggregate만 사용한다. upstream/downstream 식별자, request ID,
provider 오류 본문, prompt, media, generated content, credential은 공개 DTO에 존재하지
않는다. 관리자 화면과 API는 loopback host에서만 제공하며 public host의 `/admin`과
`/admin/*`는 token 유무와 관계없이 빈 `404`이고 `WWW-Authenticate`도 보내지 않는다.

## 사용자 여정과 목표 IA

현재 호환 surface인 public `/`, hash 기반 `/admin`, `/admin/api/v1/*`, `/health`,
`/v1/*`는 각 대체 surface가 검증될 때까지 유지한다. 목표 route inventory는 다음과
같다.

- Public primary(global header): `/`, `/status`, `/models`, `/docs`
- Public secondary(footer와 관련 문맥 링크): `/incidents`, `/incidents/[slug]`, `/security`
- Admin command center: `/admin`
- Admin capacity: `/admin/upstreams`, `/admin/upstreams/[id]`, `/admin/routing`,
  `/admin/models`, `/admin/probes`
- Admin access: `/admin/clients`, `/admin/clients/[id]`
- Admin operations: `/admin/requests`, `/admin/requests/[id]`, `/admin/incidents`,
  `/admin/qa`
- Admin governance: `/admin/audit`, `/admin/settings`

상세 route는 global navigation에 평면 노출하지 않는다. 데스크톱은 위 네 업무군,
모바일은 같은 그룹의 disclosure/drawer를 사용하고 breadcrumb와 명시적 뒤로 가기를
제공한다. `apps/admin`은 SvelteKit `paths.base=/admin`이므로 source route는
`src/routes/upstreams/**`처럼 base-relative로 두며 `src/routes/admin/**`를 만들지
않는다. gateway는 loopback에서 `/admin/{tail:.*}` deep link를 admin build로
fallback하고 public host에서는 같은 경로를 404로 닫는다.

모든 data page의 상태 우선순위는 unauthorized → invalid input/cursor → API
unavailable → stale data → loading(empty only) → empty → data다. stale에서는 마지막
성공 데이터를 지우지 않고 `마지막 확인 <time> · 새로 고침 실패` banner와 retry
button을 보여준다. 최초 요청 실패는 오류 설명과 retry button을, background refresh
실패는 assertive 알림 없이 polite live region에 한 번만 알린다. retry 중에는 기존
데이터를 유지하고 button에 busy 상태를 준다.

작은 본문은 WCAG AA 4.5:1 이상, component/focus indicator는 3:1 이상을 지킨다.
색만으로 상태를 구분하지 않고 텍스트와 icon/shape를 함께 쓴다. interactive target은
최소 44×44 CSS px이고, 320px 폭과 200% zoom에서 horizontal page overflow가 없어야
한다. endpoint/code/table은 내부 wrap 또는 labelled horizontal region을 쓴다.
키보드 focus-visible과 skip link를 제공하며 motion은 `prefers-reduced-motion`에서
제거한다. 모바일 drawer는 trigger에 `aria-expanded`/`aria-controls`, open 시 첫
nav item focus, Tab focus containment, Escape close, close 후 trigger focus return을
구현한다. 자동 갱신은 `aria-live=polite`와 `aria-busy`로 알리고 endpoint path를
모바일에서 숨기지 않고 wrap/stack한다.

## API 버전과 DTO 경계

기존 UI가 이미 사용하는 `/admin/api/v1/*`는 legacy 호환 API다. 새 route 기반 운영
콘솔은 `/admin/api/v2/*`를 사용한다. v1은 v2 application service의 adapter로 점진
전환하되 기존 method, status, 필드를 두 릴리스 동안 보존한다. 같은 method/path에
서로 다른 handler를 중복 등록하지 않는다.

Public API는 `/api/public/v1/*`이며 admin DTO를 serialize-filter해 재사용하지 않고
별도 query와 struct를 사용한다. 모든 timestamp는 RFC3339 UTC, duration/latency는
정수 millisecond, rate는 `0.0..=1.0`이다. 공통 `snapshot`은
`{observed_at:string,generated_at:string,stale:boolean}`이다.

| Method/path | Exact success payload |
|---|---|
| `GET /api/public/v1/summary` | `{schema_version:"public.v1",snapshot,state:{status:"operational"|"degraded"|"maintenance"|"unknown",traffic_ready:boolean,reason_code:string|null},capacity:{eligible:0|1|2,target:2},metrics_24h:PublicMetric,endpoints:PublicEndpoint[]}` |
| `GET /api/public/v1/metrics?window=1h|24h|7d&step=1m|5m|1h` | `{schema_version:"public.v1",snapshot,window:string,step:string,points:(PublicMetric & {at:string})[]}` |
| `GET /api/public/v1/models` | `{schema_version:"public.v1",snapshot,items:PublicModel[]}` |
| `GET /api/public/v1/incidents` | `{schema_version:"public.v1",snapshot,items:PublicIncident[]}` |
| `GET /api/public/v1/incidents/{slug}` | `{schema_version:"public.v1",snapshot,item:PublicIncident}` |

`PublicMetric`은 `{sample_count:u64,success_rate:number|null,failover_rate:number|null,
latency_p95_ms:u64|null,ttfb_p95_ms:u64|null}`다. 표본이 없으면 모든 rate/percentile은
`null`이다. UI는 수치 대신 `최근 기간에 요청 없음`과 em dash를 표시하며 성공/장애로
색칠하지 않는다. `PublicEndpoint`는 `{kind:string,state:"verified"|"proof_required"|
"unavailable"|"maintenance"}`다. `PublicModel`은 `{id:string,endpoint:string,input_modalities:string[],
output_modalities:string[],streaming:boolean,tool_calling:boolean,advertised:boolean,
proof_status:"pair_verified"|"provider_verified"|"proof_required"|"unavailable",
available_now:boolean,verified_age_seconds:u64|null}`다. `PublicIncident`는
`{slug:string,title:string,status:"investigating"|"identified"|"monitoring"|"resolved",
severity:"minor"|"major"|"critical",started_at:string,resolved_at:string|null,updates:
{status:string,message:string,published_at:string}[]}`이며 public text만 포함한다.

Admin v2 overview는 다음 필드를 명시적으로 갖는다. 각 nullable 값은 추측으로 채우지
않는다.

- `snapshot`: observed/generated timestamp, stale
- `runtime`: live, database_ready, owner_lease_ready, traffic_ready
- `capacity`: configured_slots, verified_slots, eligible_slots, pair_ready
- `profiles`: catalogued, advertised, proven, available
- `clients`: active_count
- `qa`: required, passed, complete (현재 app deployment commit의 live suite만 집계)
- `recent`: last_nvidia_success_at, last_hermes_e2e_at (nullable)
- `metrics_24h`: sample_count와 nullable success/failover/latency/TTFB
- `primary_action`: severity, code, title, reason, label, href (nullable)
- `attention_count`

정확한 `GET /admin/api/v2/overview` payload는 다음 구조다. 모든 count는 음이 아닌
정수, timestamp는 `string|null`, millisecond와 rate는 public과 같은 단위를 쓴다.

```json
{
  "snapshot": {"observed_at":"...","generated_at":"...","stale":false},
  "runtime": {"live":true,"database_ready":true,"owner_lease_ready":true,"traffic_ready":false},
  "capacity": {"configured_slots":0,"verified_slots":0,"eligible_slots":0,"pair_ready":false},
  "profiles": {"catalogued":8,"advertised":8,"proven":0,"available":0},
  "clients": {"active_count":0},
  "qa": {"required":6,"passed":0,"complete":false},
  "recent": {"last_nvidia_success_at":null,"last_hermes_e2e_at":null},
  "metrics_24h": {"sample_count":0,"success_rate":null,"failover_rate":null,"latency_p95_ms":null,"ttfb_p95_ms":null},
  "primary_action": {"severity":"critical","code":"no_eligible_upstream","title":"...","reason":"...","label":"upstream 설정","href":"/admin/upstreams"},
  "attention_count":1
}
```

`primary_action`은 action이 없으면 `null`이다. 모든 admin list는
`{snapshot,items,next_before:string|null}`이고 `before`는 opaque cursor,
`limit`은 1..100(default 50)이다. 일반 mutation response는
`{item:T,audit_event_id:Uuid}`다. client create/rotate만
`{item:Client,audit_event_id:Uuid,token:string}`이며 plaintext token은 이 응답에서 한
번만 존재한다. accepted probe/QA는 `{item:ProbeRun|QaRun,audit_event_id:Uuid}`다.
create는 201, accepted QA/probe는 202, update/retire/revoke는 200을 사용한다.

| Surface | v2 methods |
|---|---|
| Overview | `GET /overview`, `GET /attentions` |
| Upstreams | `GET,POST /upstreams`; `GET /upstreams/{id}`; `POST /upstreams/{id}/{probe|probe-profiles|enable|disable|retire}` |
| Clients | `GET,POST /clients`; `GET,PATCH /clients/{id}`; `POST /clients/{id}/{rotate|revoke}` |
| Routing | `GET,PATCH /routing/policy`; `POST /routing/simulate` |
| Models | `GET /models`; `POST /models/sync`; `POST /models/probe` with model ID in JSON body |
| Requests | `GET /requests`; `GET /requests/{request_id}` |
| Probes | `GET /probes`; `GET /probes/{id}` |
| Incidents | `GET,POST /incidents`; `PATCH /incidents/{id}`; `POST /incidents/{id}/updates` |
| Governance/QA | `GET /audit`; `GET,POST /qa/runs`; `GET /qa/secret-scan`; `GET /qa/runs/{id}`; `POST /qa/runs/{id}/hermes-completion`; `GET,PATCH /settings` |

Canonical resource shapes are:

- `Upstream`: id UUID, slot_no 1|2, label, enabled/verified/retired/eligible_now bool,
  cooldown_until nullable timestamp, request_count/failure_count u64, proofs `ProfileProof[]`.
- `Client`: id UUID, label, scopes string[], active bool, prefix string,
  expires_at nullable timestamp, model_allowlist string[]|null, rpm_limit/max_concurrency/
  request_limit_day u32|null, request_count u64, last_used_at nullable timestamp.
- `RoutingPolicy`: version u32, active bool, retryable_statuses u16[], default_cooldown_seconds
  u32, stream_failover_before_first_frame_only true, generation_retry false.
- `ProxyRequest`: request_id UUID, client_id UUID|null, endpoint/profile/modality strings,
  stream bool, outcome enum, status_code u16|null, error_class string|null, duration_ms/
  ttfb_ms u64|null, failover_count u8, timestamps, attempts `Attempt[]` only on detail.
- `ProbeRun`, `Incident`, `AuditEvent`, `QaRun` contain identifiers, enum state, sanitized
  timing/count/evidence only; never content or provider body.

JSON wire types are the following TypeScript contract. `Uuid` and `Timestamp` are canonical
lowercase UUID and RFC3339 UTC strings. Fields not declared here are rejected on writes and
not emitted on reads.

```ts
type Uuid = string;
type Timestamp = string;
type Snapshot = { observed_at: Timestamp; generated_at: Timestamp; stale: boolean };
type Page<T> = { snapshot: Snapshot; items: T[]; next_before: string | null };
type Attention = { severity: "critical" | "warning" | "info"; code: string;
  title: string; reason: string; action: { label: string; href: string } };
type ProfileProof = { profile_id: string; status: "pair_verified" | "provider_verified" |
  "proof_required" | "unavailable"; verified_key_count: 0 | 1 | 2;
  last_verified_at: Timestamp | null; stale: boolean };
type Upstream = { id: Uuid; slot_no: 1 | 2; label: string; enabled: boolean;
  verified: boolean; retired: boolean; eligible_now: boolean;
  cooldown_until: Timestamp | null; request_count: number; failure_count: number;
  proofs: ProfileProof[] };
type Client = { id: Uuid; label: string; scopes: string[]; active: boolean;
  prefix: string; expires_at: Timestamp | null; model_allowlist: string[] | null;
  rpm_limit: number | null; max_concurrency: number | null;
  request_limit_day: number | null; request_count: number;
  last_used_at: Timestamp | null; created_at: Timestamp; revoked_at: Timestamp | null };
type Attempt = { id: Uuid; attempt_no: number; upstream_id: Uuid;
  outcome: "started" | "succeeded" | "failed" | "cancelled" |
    "abandoned_after_restart"; status_code: number | null; error_class: string | null;
  latency_ms: number | null; ttfb_ms: number | null; response_started: boolean;
  cooldown_applied_until: Timestamp | null; bytes_out: number | null;
  started_at: Timestamp; finished_at: Timestamp | null };
type ProxyRequest = { request_id: Uuid; client_id: Uuid | null; endpoint: string;
  profile_id: string; modality: string; stream: boolean;
  outcome: "started" | "succeeded" | "failed" | "cancelled" | "rejected" |
    "abandoned_after_restart"; status_code: number | null; error_class: string | null;
  duration_ms: number | null; ttfb_ms: number | null; failover_count: number;
  started_at: Timestamp; finished_at: Timestamp | null; attempts?: Attempt[] };
type ProbeRun = { id: Uuid; kind: "credential" | "profile" | "catalog";
  upstream_id: Uuid | null; profile_id: string | null;
  status: "queued" | "running" | "passed" | "failed" | "cancelled";
  status_code: number | null; latency_ms: number | null; error_class: string | null;
  billable: boolean; requested_by: "local_admin" | "scheduler" | "live_qa" |
    "request_success"; created_at: Timestamp; started_at: Timestamp | null;
  finished_at: Timestamp | null };
type AdminModel = { id: string; endpoint: string; input_modalities: string[];
  output_modalities: string[]; streaming: boolean; tool_calling: boolean;
  advertised: boolean; proof_status: "pair_verified" | "provider_verified" |
    "proof_required" | "unavailable"; verified_key_count: 0 | 1 | 2;
  available_now: boolean; verified_age_seconds: number | null;
  proofs: ProfileProof[] };
type IncidentUpdate = { id: Uuid; status: "investigating" | "identified" |
  "monitoring" | "resolved"; public_message: string; published_at: Timestamp };
type Incident = { id: Uuid; slug: string; title: string;
  status: "investigating" | "identified" | "monitoring" | "resolved";
  severity: "minor" | "major" | "critical"; public: boolean;
  started_at: Timestamp; resolved_at: Timestamp | null; created_at: Timestamp;
  updated_at: Timestamp; updates?: IncidentUpdate[] };
type AuditEvent = { id: Uuid; action: string; resource_kind: string;
  resource_id: Uuid | null; actor_kind: "local_admin" | "scheduler" | "system";
  request_id: Uuid | null; outcome: "succeeded" | "failed";
  detail: Record<string, boolean | number | string | null>; created_at: Timestamp };
type QaCase = { id: Uuid; name: string; status: "pending" | "running" | "passed" |
  "failed" | "skipped"; evidence: Record<string, boolean | number | string | null>;
  started_at: Timestamp | null; finished_at: Timestamp | null };
type QaRun = { id: Uuid; suite: "smoke" | "distribution" | "failover" |
  "persistence" | "multimodal" | "hermes-e2e"; live: boolean;
  deployment_commit: string;
  status: "queued" | "running" | "passed" | "failed" | "cancelled";
  created_at: Timestamp; started_at: Timestamp | null; finished_at: Timestamp | null;
  cases: QaCase[] };
type QaCompletion = { suite: QaRun["suite"]; passed: boolean;
  passed_run: QaRun | null; latest_run: QaRun | null };
type QaRunsPage = { snapshot: Snapshot; deployment_commit: string; items: QaRun[];
  completion: QaCompletion[]; next_before: string | null };
type Settings = { proof_freshness_seconds: number; request_retention_days: number;
  metric_retention_days: number; public_incidents_enabled: boolean };
type RoutingPolicy = { version: number; active: boolean; retryable_statuses: number[];
  default_cooldown_seconds: number; stream_failover_before_first_frame_only: true;
  generation_retry: false };
```

Write DTOs are exact: upstream create `{label,credential}`; credential probe has no body;
profile probe `{profile_ids:string[],confirm_billable:boolean}`; enable/disable/retire have no
body. Client create requires `{label,scopes}` and accepts nullable policy fields from `Client`;
patch accepts only scopes/expiration/allowlist/limits, rotate/revoke have no body. Routing patch
is `RoutingPolicy` without version/active; simulate is `{profile_id,endpoint,stream}` and returns
`{eligible_order:Uuid[],retry_allowed:boolean,reason_codes:string[]}`. Model sync has no body and
returns `{snapshot:Snapshot,items:AdminModel[],discovered_count:number}`; model probe uses POST
`/models/probe` with `{model_id:string,upstream_ids:Uuid[],confirm_billable:boolean}` and returns
`{model:AdminModel,probe_runs:ProbeRun[],audit_event_id:Uuid}` so model IDs containing `/` are
never path-decoded. `GET /models` returns `Page<AdminModel>`. Incident create is
`{slug,title,status,severity,public,public_message}`, patch accepts title/status/severity/public,
incident update is `{status,public_message}`. QA create is `{suite,live,confirm_billable}`.
`hermes-e2e`는 `live:true`만 허용하며 fake run은 API와 DB 양쪽에서 거부한다. queued 또는
running QA run은 전체 DB에서 하나만 존재할 수 있고, 경합한 create는 409
`qa_run_active`와 `details.active_run_id`를 반환한다. `GET /qa/runs`는 `limit=1..100`과
opaque `before` cursor를 받아 `{snapshot,items,next_before}` keyset page를 반환한다.
invalid cursor는 422이며 case는 page별 batch load해 N+1 query를 만들지 않는다. current
deployment commit의 suite completion은 이 paginated history를 모두 순회해 계산한다.
`passed_run`은 최신 성공 증거, `latest_run`은 성공 여부와 무관한 최신 실행이므로 과거
PASS 뒤 최신 FAIL을 같은 timestamp로 오인하지 않는다. `GET /qa/secret-scan`은
`{schema_version:"nblb.secret-scan.v1",database_matches:number}`만 반환한다.
Hermes completion은 root helper 전용 exact body
`{status:"passed"|"failed",generation?:Uuid,client_id?:Uuid,doctor?:boolean,
exact_marker?:boolean,tool_task?:boolean,request_correlation?:boolean,
secret_scan?:boolean,rollback_rehearsal?:boolean,duration_ms?:number,
lb_requests?:{request_id:Uuid,attempt_count:number,outcome:string}[]}`다. passed는 모든 필드와
boolean true, 3..5개의 unique terminal request가 필요하고 gateway가 DB에서 client,
run start, attempt count, outcome, complete time window를 다시 검증한다.
Settings patch is `Partial<Settings>` with at least one key. Every string is trimmed, bounded by
the SQL constraint, and unknown JSON keys produce 422.

v1 adapter mapping is explicit: `upstream-keys→upstreams`, `downstream-credentials`와
`downstream-tokens→clients`, `operations/evidence/events→requests/audit`,
`upstream-slots/model-capabilities/generation-readiness→overview/models/probes` application
services. v1의 `/overview`, `/attentions`, `/operator-readiness`는 기존 response field와
status를 유지하는 adapter이고 v2 DTO를 그대로 반환하지 않는다.

Method-level compatibility oracle is the existing route inventory: GET/POST
`upstream-keys`, DELETE `upstream-keys/{id}`, POST `{id}/state|enable|disable|probe` map to
the corresponding v2 query/command; GET/POST `downstream-credentials|downstream-tokens`, POST
`downstream-credentials/{id}/revoke`, DELETE `downstream-tokens/{id}` map to client
query/create/revoke; GET `overview|operator-readiness|upstream-slots|model-capabilities|
generation-readiness|operations|operations/{id}|attentions|events|events/{id}|evidence|
evidence/{id}` map to the named v2 query service. PR1 freezes exact current status/body with a
table-driven compatibility test before service extraction; future adapters must pass that oracle.

Action queue 우선순위는 DB/owner lease → eligible 0 → probe 미완료 → 두 번째 slot
미구성 → profile proof → downstream credential → 전체 QA → Hermes → stale proof → 높은
429/failover 순서다.

Public health reason mapping은 다음으로 고정한다. 여러 code가 있으면 표 순서에서 가장
위의 항목 하나를 primary로 렌더링하고 나머지는 상세 상태에만 보인다.

| code | severity | public title/message | CTA |
|---|---|---|---|
| `database_unavailable` | critical | `상태 확인 불가` / `게이트웨이 저장소 연결을 확인하고 있습니다.` | `상태 새로 고침` → current route |
| `no_eligible_upstream` | warning | `운영 준비 중` / `현재 요청을 전달할 provider 용량을 준비하고 있습니다.` | `지원 모델 보기` → `/models` |
| `pair_not_ready` | info | `제한된 용량으로 운영 중` / `요청은 가능하지만 이중화 용량을 준비하고 있습니다.` | `상세 상태 보기` → `/status` |
| unknown | info | `운영 상태 확인 중` / `확인된 상태 정보가 갱신될 때까지 잠시 기다려 주세요.` | `상태 새로 고침` → current route |

Admin action은 같은 code라도 내부 reason과 정확한 수정 route를 제공한다. 공개 화면은
key/probe/cooldown identity나 내부 cause를 추가로 노출하지 않는다.

Admin primary action mapping is exact and ordered top-to-bottom:

| code | severity | title / reason | label → href |
|---|---|---|---|
| `database_unavailable` | critical | `데이터베이스 연결 필요` / `운영 상태를 읽거나 저장할 수 없습니다.` | `런타임 확인` → `/admin` |
| `owner_lease_stale` | critical | `게이트웨이 lease 확인 필요` / `요청 정리와 소유권 heartbeat가 최신이 아닙니다.` | `런타임 확인` → `/admin` |
| `no_eligible_upstream` | critical | `요청 가능한 upstream 없음` / `검증되고 활성화된 slot이 없습니다.` | `upstream 설정` → `/admin/upstreams` |
| `probe_required` | warning | `provider probe 필요` / `저장된 credential의 실제 호출 증거가 없습니다.` | `probe 실행` → `/admin/upstreams` |
| `second_slot_missing` | warning | `두 번째 slot 필요` / `장애 전환과 분산을 위한 slot이 비어 있습니다.` | `slot 추가` → `/admin/upstreams` |
| `profile_proof_missing` | warning | `model proof 필요` / `광고된 profile 중 실제 provider 증거가 없는 항목이 있습니다.` | `model 검증` → `/admin/models` |
| `downstream_client_missing` | warning | `client credential 필요` / `호출에 사용할 active downstream client가 없습니다.` | `client 발급` → `/admin/clients` |
| `qa_incomplete` | warning | `현재 배포 Live QA 미완료` / `필수 여섯 suite 중 아직 통과하지 않은 검증이 있습니다.` | `QA 완료` → `/admin/qa` |
| `hermes_unverified` | info | `Hermes E2E 필요` / `현재 배포에서 실제 agent 작업 증거가 없습니다.` | `QA 실행` → `/admin/qa` |
| `proof_stale` | warning | `오래된 proof 갱신 필요` / `provider 검증 유효기간을 넘긴 profile이 있습니다.` | `probe 갱신` → `/admin/probes` |
| `elevated_failover` | warning | `failover 증가 확인` / `최근 failover 비율이 설정된 임계값을 넘었습니다.` | `라우팅 확인` → `/admin/routing` |
| `elevated_rate_limit` | warning | `rate limit 증가 확인` / `최근 NVIDIA 429 비율이 설정된 임계값을 넘었습니다.` | `cooldown 확인` → `/admin/routing` |

동점이면 resource UUID lexical order가 아니라 이 표 순서와 oldest observed_at 순서를
사용한다. `GET /attentions`는 같은 mapping의 나머지 항목을 순서대로 반환한다.

## Health 계약

`/health/live`는 HTTP process만 확인한다. DB, vault key, NVIDIA provider를 읽지 않고
항상 `200`과 `{status:"ok",live:true,version,observed_at}`를 반환한다.

`/health/ready`는 DB query와 eligible upstream을 확인한다. DB probe는 1초 timeout을
사용한다. `/health`는 동일 handler와 동일 body/status를 사용하는 traffic-readiness
호환 endpoint다. `ready`는 `traffic_ready`와 동일하며 기존의 “두 slot 구성” 의미는
새 `pair_ready`로 이동한다. 현재 Svelte UI는 같은 PR에서 `pair_ready`를 읽도록
바꾼다.

| DB | eligible | configured pair | HTTP | ready/traffic_ready | pair_ready | reason_codes |
|---|---:|---|---:|---|---|---|
| down | any | any | 503 | false | false | `database_unavailable` |
| up | 0 | no | 503 | false | false | `no_eligible_upstream`, `pair_not_ready` |
| up | 0 | yes | 503 | false | false | `no_eligible_upstream`, `pair_not_ready` |
| up | 1 | no | 200 | true | false | `pair_not_ready` |
| up | 1 | yes | 200 | true | false | `pair_not_ready` |
| up | 2 | yes | 200 | true | true | empty |

`pair_ready`는 두 active slot 모두 verified, enabled, cooldown 밖일 때만 true다.
configured count만으로 readiness를 주장하지 않는다. public 문구는 sanitized
`reason_codes`를 매핑하고 모르는 원인은 “운영 상태를 확인 중”으로 표시한다.

컨테이너 healthcheck의 권위점은 `Dockerfile.rust`가 실행하는 인자 없는 Rust binary
`/usr/local/bin/nblb-healthcheck`다. 이 binary는 내부적으로 `/health/live`를 호출해
성공 status와 `live:true`를 검증한다. key가 0개여도 container는 healthy이고
`/health`와 `/health/ready`는 503이다.

## Request ID와 오류 계약

Actix ingress middleware가 모든 dynamic API 요청에 server-generated UUID v4를 한 번
부여한다. 외부 `x-request-id`는 신뢰하거나 재사용하지 않는다. 성공/실패 응답 모두
동일한 `x-request-id` header를 갖고, PR2의 `proxy_requests.request_id`와 attempts도
그 UUID를 사용한다. static asset 응답에는 강제하지 않는다.

- `/v1/*`: OpenAI shape `{error:{message,type,param,code}}`; request ID는 header에만
  둔다.
- `/api/public/*`: sanitized `{error:{code,message,request_id,retryable}}`.
- local `/admin/api/v2/*`: operations envelope
  `{error:{code,message,request_id,retryable,details?}}`; body/header ID가 같다.
- legacy `/admin/api/v1/*`: 두 릴리스 호환 기간에는 기존 status/body field를 그대로
  유지하고 새 server-generated request ID는 response header에만 추가한다. v1 body에
  원래 없던 `request_id`나 `retryable`을 강제로 주입하지 않는다.
- public host `/admin*`: empty 404이며 오류 body와 request-id 노출을 요구하지 않는다.

401은 올바른 `WWW-Authenticate`, 403은 insufficient scope, 409는 상태 충돌, 422는
검증 오류, 429는 bounded `Retry-After`, retry 가능한 dependency/provider 5xx는
`retryable:true`를 사용한다. provider response body, credential, user content를
`details`나 로그에 넣지 않는다.

## 데이터·라우팅 불변조건

- `nblb.upstream_keys`, `downstream_credentials`, `routing_state`, `request_attempts`와
  이후 additive operations tables가 PostgreSQL source of truth다.
- NVIDIA credential은 AES-256-GCM ciphertext/nonce로, downstream token은 digest로만
  저장하고 plaintext는 생성 응답에서 한 번만 보여준다.
- admin bearer는 현재 탭의 TypeScript module memory에만 두며 URL, cookie, DOM,
  localStorage, sessionStorage에 저장하지 않는다. reload·잠금·탭 종료는 즉시 지우고,
  one-time downstream token을 clipboard에 복사했다면 dialog를 닫기 전에 clipboard도
  비운다.
- active upstream은 최대 두 slot이며 add → probe → enable을 우회하지 않는다.
- rate-aware round-robin cursor와 cooldown은 재시작 후 유지한다.
- streaming failover는 첫 downstream SSE frame 전까지만 허용하며 이후 replay하거나
  다른 provider 출력을 이어붙이지 않는다.
- validation 4xx는 failover하지 않고, 401/403은 quarantine, 429는 Retry-After 기반
  cooldown 후 아직 전송되지 않은 요청만 다른 eligible slot으로 넘긴다.
- prompt/messages, tool arguments, image/audio/video bytes or URL, generated output,
  Authorization/cookie, provider response body는 DB와 로그에 저장하지 않는다.
- `request_attempts.owner_id`와 `gateway_instances.last_seen_at` lease를 유지하고 살아
  있는 다른 process의 streaming attempt를 restart cleanup이 종료하지 않는다.

### 미결정값 해소와 구현 권위

첨부 구현 팩은 scaffolding이며 신규 admin namespace를 v1으로 적은 부분은 폐기한다.
이 문서의 `/admin/api/v2/*`가 유일한 신규 계약이고 `/admin/api/v1/*`는 legacy
adapter다. model ID는 path segment로 받지 않고 `POST /admin/api/v2/models/probe` JSON
body로만 받는다. Hermes 최종 cutover 권위는 public custom-endpoint 예제가 아니라 이
문서의 Rust helper와 loopback `provider:nvidia` 구성이다.

stable upstream slot은 PostgreSQL `slot_no` 1 또는 2로 식별한다. active/non-retired
slot에 unique constraint를 두고, add transaction이 advisory lock 아래 가장 작은 빈
slot을 배정한다. retire 뒤 새 key가 빈 번호를 재사용할 수 있지만 과거 attempt는 key
UUID를 보존하므로 역사적 identity가 바뀌지 않는다. UI와 분산 계산은 현재 key 정렬
순서로 slot을 추론하지 않는다.

first-attempt 선택만 profile의 round-robin cursor와 generation을 한 번 전진시킨다.
같은 downstream request의 failover attempt는 cursor/generation을 추가 전진시키지
않고 아직 시도하지 않은 eligible slot을 stable slot 순서로 선택한다. first selection은
`routing_state FOR UPDATE`, hard eligibility(profile receipt 포함), cursor/generation,
parent request, first attempt를 한 transaction으로 저장한다. no-eligible도 parent를
명시적 terminal로 남긴다.

downstream client 제한은 다음과 같이 고정한다.

- expiration은 인증 시점과 request permit 획득 시점 모두 검사한다.
- RPM은 현재 UTC minute `[date_trunc('minute', now()), +1 minute)`에 시작한 request
  수이며 초과 시 다음 minute까지 1..60초 `Retry-After`를 반환한다.
- request/day는 현재 UTC calendar day에 시작한 request 수다.
- max concurrency는 `downstream_request_permits`의 unreleased row 수다. permit 획득은
  client UUID advisory lock, 정책 재확인, count, permit INSERT를 한 transaction으로
  수행한다. request terminal/Drop에서 release하고 stale owner lease만 restart cleanup이
  release한다.
- model allowlist는 body validation 후 provider 호출과 request evidence 생성 전에
  검사한다. scope와 allowlist 부족은 403, expiration은 401, rate/day/concurrency는 429다.

운영 설정 기본값과 범위는 다음으로 고정한다.

- proof freshness: 기본 604800초(7일), 허용 300..2592000초
- request retention: 기본 30일, 허용 7..90일
- metric retention: 기본 90일, 허용 7..365일
- public incident 표시: 기본 true
- elevated failover/rate-limit attention: 최근 24시간 표본 20개 이상이며 각각 10% 이상
- browser last-good snapshot stale 기준: 마지막 성공 후 30초; background 실패는 기존
  snapshot을 유지하고 한 번의 polite announcement만 낸다.
- public metric privacy suppression: 집계 point 또는 summary의 sample이 5 미만이면
  `sample_count`는 실제 count를 내보내지 않고 0, rate/percentile은 null로 반환한다.

routing policy bootstrap은 migration이 version 1 default document를 active로 원자
seed한다. patch는 advisory lock 아래 기존 active를 내리고 `max(version)+1`을 active로
insert하므로 lost update가 없다. `stream_failover_before_first_frame_only`는 항상 true,
`generation_retry`는 항상 false이며 patch로 바꿀 수 없다.

성공 mutation은 resource change와 audit event가 같은 transaction에서 commit되어야
한다. validation/auth rejection은 resource transaction을 열지 않는다. dependency나
동시성 실패처럼 transaction이 rollback된 mutation은 secret/content 없는 normalized
failure code만 별도 best-effort audit transaction으로 남기며, audit 실패가 원래 오류를
가리지 않는다. `audit_events.detail`과 `qa_cases.evidence`는 action/suite별 typed scalar
allowlist만 허용하고 임의 key/value JSON을 handler에서 받지 않는다.

## 단계와 책임

1. Health/API foundation: health split, request ID/error foundation, public-host admin 404,
   healthcheck, 현재 public 문구/접근성 교정.
2. Evidence/schema: additive operations migration, proxy request/attempt 상관, metric/audit
   기반, 기존 first-frame guard 계측과 retention. 라우팅 행동은 바꾸지 않는다.
3. Public product: public DTO/API와 multi-page status/models/docs/incidents/security,
   proof-aware copy, aggregate privacy.
4. Admin onboarding: grouped route shell, command center, upstream/client lifecycle,
   one-time token, 모든 mutation audit.
5. Routing/evidence UX: policy/simulation, per-profile probes, request timeline, retry/stream
   boundary 표현과 필요한 routing behavior 변경.
6. Operations completion: incidents, QA runner, live two-key/multimodal/persistence/secret scan,
   Hermes E2E.

PR1, PR3, PR6에는 각각 `home-server-infra` companion PR을 둔다. PR1은
`/health/live|ready`, PR3는 public page/API allowlist를 추가한다. 앱 PR merge와
immutable image digest가 나온 뒤 infra digest를 갱신하고, Cloudflare origin/public
smoke를 통과한 뒤에만 배포 완료로 센다. 어떤 PR도 Codex가 직접 merge하지 않는다.

PR6 app은 tracked Python/shell 없이 Rust binary `nblb-hermes-cutover`와 fixture tests를
제공한다. PR6 infra는 존재하지 않는 `scripts/ops/hermes_cutover.py`, Python test,
과거 commit pin을 권위로 삼는 문서를 제거하고 이 Rust binary와 아래 절차를 유일한
cutover source of truth로 문서화한다.

1. Preconditions: local `/health/ready=200`, exactly two eligible keys,
   `/opt/agent-apps/data/hermes/.env`와 `config.yaml`이
   root-owned regular file mode 0600이고 symlink/hardlink/mount가 아님을 확인한다.
2. Snapshot: `/opt/nvidia-build-lb/hermes-cutover-backups/<generation>/`에 두 파일의
   root-only immutable copy, safe metadata/hash manifest와 journal을 fsync한다. secret이나
   파일 내용은 receipt/log/stdout에 넣지 않는다.
3. Stop only `agent-hermes`; 다른 agent와 `codex-lb`는 건드리지 않는다.
4. Issue and apply: helper는 `/opt/nvidia-build-lb/secrets/admin_token`을 root-only로
   읽고 local admin API에 deterministic label `hermes-cutover-<generation UUID>`로
   `models:read+chat:write` client를 생성한다. operation ID/label을 POST 전에 journal에
   fsync하고 create response의 one-time token은 process memory에서 `.env` candidate로
   직접 쓸 뿐 argv/stdout/log/temp file에 넣지 않는다. crash 후 label이 이미 있으면
   그 client를 rotate해 새 plaintext를 얻고 이전 digest를 revoke한 뒤 계속하므로
   plaintext 복구를 시도하지 않는다. `.env`의 `NVIDIA_API_KEY` 값은 이 token으로,
   `config.yaml`의 dotted fields는 `model.provider: nvidia`,
   `model.default: z-ai/glm-5.2`, `model.base_url: http://127.0.0.1:2456/v1`로 원자
   교체한다. direct `nvapi-` credential이 Hermes data tree에 남아 있으면 실패한다.
5. Start `agent-hermes`; container health, `hermes doctor`, exact marker chat, 실제
   tool-using task와 LB request/attempt 상관을 검증한다.
6. Rollback rehearsal: Hermes를 멈추고 snapshot pair를 원자 복원해 health를 확인한 뒤
   candidate pair를 다시 적용·검증한다. 어느 단계든 실패하면 journal state에 따라
   정확한 pair 하나만 복원하고 Hermes를 시작하거나, 검증 불가 시 stopped 상태로
   두고 safe next action을 반환한다.
7. Commit: final generation과 active downstream token ID만 safe receipt로 남기고 이전
   downstream token은 final E2E 뒤 revoke한다. provider에 직접 노출됐던 NVIDIA key는
   provider-side revoke 확인 뒤에만 upstream-bearing backup을 retire한다.

실행 순서는 app PR6 → 사용자 merge → CI image digest → infra PR6(digest, Rust cutover
runbook, 폐기 문서 제거) → 사용자 merge → 배포 → preflight → cycle/rollback rehearsal
→ E2E/secret scan이다. rollback은 DB/vault volume을 보존하고 직전 immutable image
digest와 검증된 Hermes file pair를 함께 복구한다. `down --volumes`, raw token 출력,
manual 한 파일만 교체하는 partial rollback은 금지한다.

CI는 PR6 commit에서 static-linked `nblb-hermes-cutover` artifact와 SHA-256 manifest를
발행한다. infra PR은 app commit, image digest, helper SHA-256을 함께 pin한다. 운영자는
artifact를 root-only staging file로 받아 checksum과 executable type을 검증한 뒤
`/usr/local/sbin/nblb-hermes-cutover`에 root:root 0755로 원자 install하고, binary의
embedded commit이 pinned commit과 같은지 preflight에서 확인한다. helper는 host에서
root로 실행하며 Docker socket을 mount하지 않는다. 필요한 container lifecycle은
allowlisted exact argv로 host `/usr/bin/docker stop|start|inspect|logs agent-hermes`와
아래 여섯 `/usr/bin/docker exec --workdir /tmp agent-hermes` command만 실행한다.

- `/opt/hermes/.venv/bin/hermes doctor`
- `/opt/hermes/.venv/bin/hermes chat -Q --source tool --max-turns 1 -q
  "Reply with exactly: NBLB_HERMES_OK"`
- `/opt/hermes/.venv/bin/hermes chat -Q --source tool --max-turns 4 --yolo -t terminal -q
  "Use the terminal tool exactly once to run printf 'NBLB_TOOL_OK\\n' >>
  /tmp/nblb-hermes-e2e-<generation UUID>, then reply with exactly: NBLB_TOOL_OK"`
- `/usr/bin/stat --format=%F:%s /tmp/nblb-hermes-e2e-<generation UUID>`
- `/usr/bin/sha256sum /tmp/nblb-hermes-e2e-<generation UUID>`
- `/usr/bin/rm -- /tmp/nblb-hermes-e2e-<generation UUID>`
- `/usr/bin/test ! -e /tmp/nblb-hermes-e2e-<generation UUID>`

helper는 prompt를 상수로 compile하고 caller 입력을 argv에 넣지 않는다. exec stdout/stderr는
각 64KiB로 제한해 memory에서 marker와 exit status만 판정하고 파일/journal/receipt에
본문을 저장하지 않는다. receipt에는 doctor/marker/tool booleans, duration, output
SHA-256와 ordered `lb_requests:[{request_id,attempt_count,outcome}]`만 기록한다. token,
Authorization, prompt, response, tool arguments는 기록하지 않는다. marker/tool 요청은
발급한 Hermes client와 시작/종료 timestamp로 admin request evidence를 조회한다.
marker 실행은 성공 request가 정확히 1개여야 한다. tool 실행은 first completion,
terminal tool result, final completion 때문에 ordered 성공 request가 2..4개일 수 있으며
그 window에 failed/rejected request가 없어야 한다. helper는 task 전에 generation marker
부재를 확인하고, task 뒤 regular file size가 정확히 13 bytes이며 content SHA-256이
`NBLB_TOOL_OK\n`과 같은지 확인한다. append command를 여러 번 실행하면 size가 달라져
실패하므로 terminal execution exactly once의 증거가 된다. 검증 성공/실패 모두 marker를
exact-path `rm`으로 정리하고 absence를 재확인한다. final marker도 일치해야 하며 범위를
벗어나거나 상관되지 않은 request가 있으면 E2E는 실패한다. helper network는 loopback
`127.0.0.1:2456`만 사용한다.
다른 container 이름, exec argv, remote URL은 거부한다.

Admin에서 `hermes-e2e` run을 생성하면 gateway는 case를 running으로 arm하고 즉시
판정하지 않는다. helper는 `apply --qa-run <UUID>`로 receipt와 run을 연결하며 receipt를
원자 저장한 뒤 completion API를 호출한다. helper는 expected/embedded/deployment commit과
exact live/running run identity를 lock, snapshot, client 발급, Docker 조작보다 먼저
read-only preflight한다. receipt와 `committed` journal 이후 오류는 rollback, candidate
revoke, failed completion으로 되돌아가지 않고 committed reconciliation으로만 복구한다.
reconciliation은 이전 cutover client revoke 뒤 PASS completion을 제출하며 PASS 제출의
동일 generation/app commit 재시도는 idempotent다. 마지막 journal write만 실패한 경우
같은 `apply --qa-run` 재실행이 exact local committed receipt/journal을 확인한 뒤 복구한다.
동시 apply는 host lock 뒤 run을 다시 조회하며, 먼저 끝난 동일 run의 reconciled generation을
확인하면 새 snapshot/client/Docker mutation 없이 성공 종료한다. 30분 timeout,
gateway 재시작, pre-commit helper failure, completion 검증 실패는 fail-closed이고
interrupted persistence resume 오류도 run/case를 terminal failed로 닫는다.

## 빠른 검증 계약

수정 중에는 영향 범위만 실행한다.

- PR1 Rust: `pr1_` filter의 health/request-id/admin-boundary 테스트,
  `cargo check -p nvidia-build-lb-gateway --bins`, Docker image/zero-key smoke
- Svelte 변경: 변경된 app의 `check`와 `build`
- Compose/infra 변경: 해당 compose config와 경로별 curl
- SQL 변경: 임시 PostgreSQL에 migration 적용과 schema/privacy assertions

전체 workspace, clippy, 양쪽 Svelte 정적 검사, image build와 live matrix는 마지막
PR에서 한 번 수행한다. 테스트를 약화·skip하거나 매 수정마다 전체 suite를 반복하지
않는다.

최종 live QA는 정상 두 key 호출과 분산, attempt-1 실패→attempt-2 성공 failover,
한 slot disable 중 연속성, app 재시작 후 encrypted persistence, credential/log/bundle
비노출, 지원되는 text/image/video/audio/generation/transcription 최소 매트릭스, Hermes
실제 작업을 증거로 남긴다. disable은 연속성 증거이지 실패 후 failover 증거로
오표현하지 않는다. 비용이 생길 수 있는 image/video/audio live probe는 최소 요청 수와
출력 크기로 제한하고 실행 전에 운영자에게 비용 범위를 알린다.
