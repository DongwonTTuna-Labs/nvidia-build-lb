# NVIDIA Build LB Architecture Contract

> **RETIRED BEHAVIOR ORACLE — NOT V2 SHIPPING AUTHORITY.** This document
> describes the Python/FastAPI implementation at PR head `d306620` and its
> subsequent uncommitted parity work. The user-approved Rust/Svelte/Bun/SQLx,
> multimodal, public-ingress product is governed only by `DESIGN.md`. No clause
> below may override that canonical v2 contract or serve as final completion
> evidence. The document remains temporarily so framework-neutral fixtures can
> preserve valid safety and behavior during the rewrite; it is removed or
> archived when parity migration completes.

This document freezes the implemented product boundary and its verification
contract. The repository contains the typed gateway, migrations, production
Compose surface, and local release gates. When an implementation and this
contract disagree, the implementation is the defect unless the approved design
is revised first.

All durations, byte limits, retry counts, and queue sizes below are local
product policy. They are not NVIDIA quotas, guarantees, or documented provider
limits.

## 1. Product boundary

- One upstream only: `https://integrate.api.nvidia.com/v1`.
- One public and upstream model only: `z-ai/glm-5.2`.
- Python 3.13, FastAPI, PostgreSQL, SQLAlchemy async, and Alembic.
- No provider abstraction, Responses API, OAuth bridge, public ingress, React,
  or migration from the existing codex-lb.
- Production listens only through `127.0.0.1:2456`; `127.0.0.1:2455` remains
  owned by codex-lb.

The service has four trust boundaries: public downstream API, owner admin API,
NVIDIA upstream API, and PostgreSQL plus secret files. Pydantic parses public
JSON once. Typed repositories own database state. Only the NVIDIA adapter may
handle upstream credentials.

## 2. Exact HTTP surface

### 2.1 Public, shell, and asset routes

| Method and path | Authentication | Success | Failure contract |
| --- | --- | --- | --- |
| `GET /health` | Explicit unauthenticated no-op | `200 application/json` with `{"status":"ok","ready":true}` | `503 application/json` with `{"status":"degraded","ready":false}` |
| `GET /v1/models` | Downstream bearer with exact `models:read` scope | `200 application/json` model list | Public error envelope |
| `POST /v1/chat/completions` | Downstream bearer with exact `chat:write` scope | `200 application/json` or `200 text/event-stream` | Public error envelope, or an SSE error event after response start |
| `GET /admin` | Explicit unauthenticated no-op | `200 text/html` secret-free login shell | `404` only if the product shell is absent |
| `GET /showcase` | Explicit unauthenticated no-op | `200 text/html` secret-free primitive showcase | `404` only if the showcase is absent |
| `GET /assets/admin.css` | Explicit unauthenticated no-op | `200 text/css` | `404` |
| `GET /assets/admin.js` | Explicit unauthenticated no-op | `200 text/javascript` | `404` |
| `GET /assets/favicon.svg` | Explicit unauthenticated no-op | `200 image/svg+xml` original neutral routing icon | `404` |
| `GET /assets/showcase.css` | Explicit unauthenticated no-op | `200 text/css` | `404` |
| `GET /assets/showcase.js` | Explicit unauthenticated no-op | `200 text/javascript` | `404` |

The seven shell and asset responses negotiate deterministic `Content-Encoding:
gzip` when `Accept-Encoding` explicitly permits it and otherwise return the
byte-exact identity representation. They always emit `Vary: Accept-Encoding`.
Malformed qvalues or encoding parameters select identity, and repeated header
fields are combined before negotiation.
This is route-local static compression: no admin API, public JSON API, or SSE
stream passes through a buffering compression middleware.

No other asset route exists. In particular, `/assets/{path}` is not a generic
filesystem mount: every other asset name is `404`. A method not listed for a
known path is `405`. Every `OPTIONS` request follows the global rule in section
9 and never reaches route dispatch.

`GET /v1/models` returns exactly:

```json
{"object":"list","data":[{"id":"z-ai/glm-5.2","object":"model","owned_by":"nvidia"}]}
```

A chat request whose `model` is not exactly `z-ai/glm-5.2` is rejected with
`404 model_not_found` before key selection. There is no alias or silent model
rewrite.

### 2.2 Admin API routes

Every route in this table requires the distinct admin bearer.

| Method and path | Successful result |
| --- | --- |
| `GET /admin/api/v1/dashboard` | `200 application/json` canonical transactionally coherent dashboard |
| `GET /admin/api/v1/operator-readiness` | `200 application/json` bounded host-operator readiness |
| `GET /admin/api/v1/overview` | `200 application/json` safe overview |
| `GET /admin/api/v1/upstream-keys` | `200 application/json` safe key list |
| `POST /admin/api/v1/upstream-keys` | `201 application/json`; new key is disabled |
| `POST /admin/api/v1/upstream-keys/{id}/enable` | Idempotent `204` after current successful verification |
| `POST /admin/api/v1/upstream-keys/{id}/disable` | Idempotent `204`, empty body |
| `POST /admin/api/v1/upstream-keys/{id}/probe` | `200 application/json` single-key result only after a durable probe terminal |
| `DELETE /admin/api/v1/upstream-keys/{id}` | `204` only while disabled |
| `GET /admin/api/v1/downstream-tokens` | `200 application/json` digest-free token list |
| `POST /admin/api/v1/downstream-tokens` | `201 application/json`; plaintext bearer appears once |
| `DELETE /admin/api/v1/downstream-tokens/{id}` | `204`, revoking that token |
| `GET /admin/api/v1/events` | `200 application/json` safe event list |

Duplicate upstream fingerprint, enabling an unverified/cooling/quarantined key,
and deleting an enabled key are `409`.
Deleting an unknown or already deleted key is `404`. Repeated downstream revoke
is `404`. Boundary validation is `422`; an unavailable database is `503`. Every
supported admin GET uses the same configured `1..5` second server deadline and
returns safe `504 admin_read_timeout` when the database does not settle. Health
uses the same repository-read budget and converges to the exact minimal degraded
503 response rather than waiting without bound.

Probe never chooses another key and never changes `enabled`. Its response has
only `id`, `enabled`, `probe_status`, and `observed_at`, where `probe_status` is
one of `valid`, `invalid_credential`, `rate_limited`, or
`upstream_unavailable`. A valid probe clears health failures and cooldown. A
negative probe applies the same-key transition in section 6.
Only a durably reserved probe with a committed terminal returns that four-field
`200` projection. A reservation or admission failure before any durable attempt
returns its mapped safe `503` error envelope (`ledger_capacity_exhausted` or
`database_unavailable`) and records neither a probe result nor a same-key
transition.

Admin list fields are closed allowlists:

- Upstream: `id`, `fingerprint`, `enabled`, `routing_state`, `health_state`, `cooldown_until`,
  `request_count`, `success_count`, `failure_count`, `last_status_class`,
  `last_used_at`, `created_at`, `updated_at`.
- Downstream: `id`, `label`, `scopes`, `revoked_at`, `request_count`,
  `last_used_at`, `created_at`.
- Events: opaque event/request IDs, internal key/token IDs, safe outcome and
  status classes, latency, and timestamps.

Plaintext, digest, nonce, ciphertext, headers, message bodies, upstream response
bodies, and filesystem paths are never admin read fields.

### 2.3 Exact administration wire DTOs

All administration request and response DTOs use `extra="forbid"`. IDs are
canonical lowercase UUID strings, timestamps are UTC RFC 3339 strings ending in
`Z`, nullable values are JSON `null`, and counters are non-negative JSON
integers backed by PostgreSQL `bigint`.

Framework validation detail is never returned by the administration API. Every
request-body or unsupported-query validation failure is exactly `422` with:

```json
{
  "error": {
    "code": "invalid_request",
    "message": "request validation failed",
    "request_id": "opaque"
  }
}
```

The response contains no rejected value, `input`, validation context, field
path, header, or credential.

#### Upstream key DTOs

Creation accepts exactly one field:

```json
{"key":"opaque NVIDIA credential"}
```

`key` is a strict JSON string whose unchanged UTF-8 encoding is 1 through 4096
bytes. NUL, CR, and LF are forbidden. The service does not trim, case-fold,
Unicode-normalize, or apply an NVIDIA prefix, fixed-length, character-set, or
provider-format regular expression. The exact UTF-8 bytes are fingerprinted and
encrypted. PostgreSQL stores the full lowercase 64-hex SHA-256; the wire always
returns `sha256:` followed by all 64 hex characters. UI-only display may shorten
that value to the first 16 hex characters, but equality and duplicate detection
always use the full digest.

The `201` creation response and every list item have this exact shape:

```json
{
  "id": "00000000-0000-4000-8000-000000000001",
  "fingerprint": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "enabled": false,
  "routing_state": "disabled",
  "health_state": "unknown",
  "cooldown_until": null,
  "request_count": 0,
  "success_count": 0,
  "failure_count": 0,
  "last_status_class": null,
  "last_used_at": null,
  "created_at": "2026-01-01T00:00:00Z",
  "updated_at": "2026-01-01T00:00:00Z"
}
```

`routing_state` is exactly `disabled`, `eligible`, `cooldown`, or `quarantined`
and is computed from the same scheduler-selection fields at read time.
`health_state` is exactly `unknown`, `healthy`, or `degraded`.
`last_status_class` is null or
one of `success`, `invalid_credential`, `credits_exhausted`, `rate_limited`,
`request_rejected`, `timeout`, `upstream_unavailable`,
`upstream_bad_gateway`, `upstream_internal_error`, `upstream_protocol_error`,
`delivery_failed`, or `cancelled`. A new row has every initial value shown above and
`created_at == updated_at`. Enable requires healthy state, no quarantine, and no
active cooldown; otherwise it returns `409 resource_conflict` without mutation.

`GET /admin/api/v1/upstream-keys` returns exactly
`{"items":[<UpstreamKeyRead>,...]}`. It returns every nondeleted row ordered by
`(created_at ASC, id ASC)` and accepts no query, cursor, filter, limit, or
pagination parameter.

Probe returns only:

```json
{
  "id": "00000000-0000-4000-8000-000000000001",
  "enabled": false,
  "probe_status": "valid",
  "observed_at": "2026-01-01T00:00:00Z"
}
```

`probe_status` remains exactly `valid`, `invalid_credential`, `rate_limited`, or
`upstream_unavailable`; probing never changes `enabled`.

#### Downstream token DTOs

Issuance accepts exactly:

```json
{
  "label": "hermes-cutover:00000000-0000-4000-8000-000000000001",
  "scopes": ["models:read", "chat:write"]
}
```

`label` is a strict string of 1 through 128 Unicode scalar values. It is not
blank, has no control character or surrogate, and has no leading or trailing
whitespace. Storage does not trim, normalize, or case-fold it. A binary
exact-match unique constraint covers active and revoked rows. `scopes` contains
one or two unique values from exactly `models:read` and `chat:write`; duplicates
or any other value are `422`. Responses always order scopes as `models:read`,
then `chat:write`.

The successful `201` one-time response is exactly:

```json
{
  "id": "00000000-0000-4000-8000-000000000001",
  "label": "label",
  "scopes": ["models:read", "chat:write"],
  "token": "nblb_ds_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "revoked_at": null,
  "request_count": 0,
  "last_used_at": null,
  "created_at": "2026-01-01T00:00:00Z"
}
```

A list item is that shape with only `token` removed. The list response is
`{"items":[<DownstreamTokenRead>,...]}` and returns active plus revoked rows by
`(created_at ASC, id ASC)` with no query or pagination. Reusing an exact label is
`409`; blind retry with that label is forbidden.

#### Overview and event DTOs

`GET /admin/api/v1/operator-readiness` returns exactly:

```json
{
  "runtime_state": "operational",
  "readiness_cause": "ready",
  "ledger_status": "ok",
  "capacity_blocker": "none"
}
```

This endpoint is intentionally independent of dashboard collection size. One
read-only repeatable snapshot reads the ledger singleton, actual event/attempt/
pending counts, and one SQL eligible-key count. It never loads upstream key,
downstream token, or event rows as collections. Lifecycle readiness is sampled
before and after that snapshot, and an overlapping admin mutation fails through
the bounded admin-read error contract instead of publishing mixed evidence.

`GET /admin/api/v1/overview` returns exactly:

```json
{
  "status": "ok",
  "ready": true,
  "upstream_keys": {
    "total": 2,
    "enabled": 2,
    "eligible": 1,
    "cooling": 1,
    "degraded": 1
  },
  "downstream_tokens": {"total": 3, "active": 2, "revoked": 1},
  "request_count": 42,
  "last_event_at": "2026-01-01T00:00:00Z",
  "generated_at": "2026-01-01T00:00:01Z"
}
```

`status` is exactly `ok` or `degraded`, and it and `ready` reuse the `/health`
calculation. `request_count` is the count of distinct routed `request_id`
values; multiple key attempts during one bounded failover still count as one
logical request, and explicit probes do not count. An empty
database reports every count as zero, `last_event_at:null`, `status:"degraded"`,
and `ready:false`.

`GET /admin/api/v1/events` returns only the newest 100 rows ordered by
`(occurred_at DESC, id DESC)`:

```json
{
  "items": [
    {
      "id": "00000000-0000-4000-8000-000000000001",
      "request_id": "opaque",
      "event_type": "upstream_attempt",
      "upstream_key_id": null,
      "downstream_token_id": null,
      "outcome_class": "started",
      "status_class": null,
      "latency_ms": null,
      "occurred_at": "2026-01-01T00:00:00Z"
    }
  ]
}
```

`event_type` is exactly `upstream_key_created`, `upstream_key_enabled`,
`upstream_key_disabled`, `upstream_key_deleted`, `upstream_probe`,
`downstream_token_issued`, `downstream_token_revoked`, or `upstream_attempt`.
`outcome_class` is exactly `started`, `succeeded`, `failed`, or `cancelled`.
`status_class` uses the upstream status enum or null; `latency_ms` is a
non-negative integer or null. There is no cursor, filter, pagination, message,
label, body, header, digest, ciphertext, nonce, or filesystem path.

## 3. Chat request and response compatibility

The typed request boundary supports these named fields:

| Field | Contract |
| --- | --- |
| `model` | Required string; route requires exact `z-ai/glm-5.2` |
| `messages` | Required non-empty array of messages |
| `stream` | Boolean, default `false` |
| `max_tokens` | Integer greater than or equal to 1, or null |
| `temperature` | Number in inclusive range 0 through 2, or null |
| `top_p` | Number in inclusive range 0 through 1, or null |
| `frequency_penalty` | Number in inclusive range -2 through 2, or null |
| `presence_penalty` | Number in inclusive range -2 through 2, or null |
| `stop` | String, array of strings, or null |
| `seed` | Integer or null |
| `user` | String or null |
| `tools` | JSON array or null |
| `tool_choice` | Any JSON value, including null |
| `response_format` | Any JSON value, including null |

Scalar field types are strict at the JSON boundary: string and Boolean values
are not coerced into the documented integer, number, or Boolean types.

A message has named `role`, `content`, optional `name`, and optional
`tool_call_id` fields. `content` is any JSON value because current NVIDIA
examples and OpenAI-compatible clients use both strings and structured parts.

Both the request and every message use `extra="allow"`. Extra JSON members such
as `chat_template_kwargs`, `stream_options`, reasoning options, and future
NVIDIA extensions survive parse and serialization unchanged. The proxy does not
invent semantics for them, and non-JSON Python values never cross the boundary.

Successful upstream JSON is compatibility-pass-through. Opaque IDs, empty
choices, optional `delta.content`, `reasoning_content`, `tool_calls`, and unknown
JSON members are preserved. The fixed model is not remapped to another model.
Successful SSE is framed as section 7 specifies and otherwise preserves event
fields and data bytes.

## 4. Authentication and stable errors

Admin credentials use `nblb_admin_` plus 64 lowercase hexadecimal characters.
Downstream credentials use the distinct `nblb_ds_` prefix plus 64 lowercase
hexadecimal characters. Each suffix represents 256 random bits. The two formats
never cross-authenticate. Downstream persistence stores only SHA-256 digests;
plaintext is returned only by the successful issue response and cannot be
retrieved later.

Missing, wrong, or revoked downstream auth is `401` and includes:

```text
WWW-Authenticate: Bearer realm="nvidia-build-lb"
```

A valid token without the exact route scope is `403 insufficient_scope`.
Missing or wrong admin auth is `401` and includes:

```text
WWW-Authenticate: Bearer realm="nvidia-build-lb-admin"
```

Except for the deliberately minimal `/health` readiness body, safe errors use:

```json
{"error":{"code":"stable_code","message":"safe message","request_id":"opaque"}}
```

| Boundary or outcome | HTTP status | Stable code | Media type |
| --- | ---: | --- | --- |
| Host rejection | 403 | `host_forbidden` | `application/json` |
| Origin rejection | 403 | `origin_forbidden` | `application/json` |
| Downstream auth rejection | 401 | `unauthorized` | `application/json` |
| Admin auth rejection | 401 | `admin_unauthorized` | `application/json` |
| Valid token missing scope | 403 | `insufficient_scope` | `application/json` |
| Wrong fixed model | 404 | `model_not_found` | `application/json` |
| Unknown admin resource | 404 | `resource_not_found` | `application/json` |
| Duplicate or invalid state transition | 409 | `resource_conflict` | `application/json` |
| Pydantic boundary rejection | 422 | `invalid_request` | `application/json` |
| No eligible key | 503 | `no_upstream_keys` | `application/json` |
| Database unavailable | 503 | `database_unavailable` | `application/json` |
| Upstream 401 or 403 | 502 | `upstream_auth_error` | `application/json` |
| Upstream 402 | 503 | `upstream_credits_exhausted` | `application/json` |
| Upstream 408 or connect/read/write timeout | 504 | `upstream_timeout` | `application/json` before response start |
| Upstream 429 | 429 | `upstream_rate_limited` | `application/json` |
| Upstream request 4xx other than 401/402/403/408/429 | Same 4xx | `upstream_request_rejected` | `application/json` |
| Upstream 500 | 502 | `upstream_internal_error` | `application/json` |
| Upstream 502 | 502 | `upstream_bad_gateway` | `application/json` |
| Upstream 503 | 503 | `upstream_unavailable` | `application/json` |
| Upstream 504 | 504 | `upstream_timeout` | `application/json` |
| Connect failure before request send | 503 | `upstream_unavailable` | `application/json` |
| Malformed response, missing request ID, or rejected result URL | 502 | `upstream_protocol_error` | `application/json` |
| Poll deadline | 504 | `poll_timeout` | `application/json` |

Upstream `application/json`, `application/problem+json`, and `text/plain` error
bodies are classified but not forwarded. Their safe downstream representation
is always the JSON envelope above. After an SSE response has started, HTTP
status cannot change; section 7 defines the terminal event.

## 5. NVIDIA HTTP client rules

The NVIDIA POST target is exactly
`https://integrate.api.nvidia.com/v1/chat/completions`. Automatic redirects and
transport retries are zero. Only the typed routing state machine may choose the
one permitted alternate. Authorization is a Bearer header built inside the
adapter and is never logged or returned.

The client uses HTTP/1.1 only with HTTP/2 disabled, bounded split timeouts, and a
bounded pool. Its transport retry count for NVIDIA POST, poll GET, and result
GET remains zero. A retrying transport underneath the routing layer is a
contract breach.

## 6. Key routing state machine

An eligible key is enabled, not quarantined, and has no active cooldown. The
full stable key ring is ordered by `(created_at, id)`; disabled, quarantined,
cooling, or request-excluded rows remain cursor-position anchors but can never
be selected. One concurrency-safe persisted cursor selects the first eligible
successor in that ring. Reservation reads the stable ring while holding the
scheduler lock, then locks only the selected key. Reservation, finalization,
enable, disable, and deletion all lock the scheduler singleton before exactly
one selected or mutated key row. One public request makes at most two sequential
key attempts: the selected key and one distinct safe alternate. Keys are never
raced in parallel.

| Outcome | Same-key transition | Alternate before response start | Exhausted result |
| --- | --- | --- | --- |
| Manual disable | `enabled=false` | Select another without attempting it | `503 no_upstream_keys` |
| Successful public response while the key is not quarantined | Healthy; clear cooldown and consecutive failures | No | Original success |
| Successful explicit admin probe | Healthy; clear quarantine, cooldown, and consecutive failures | No | Original success |
| 401 or 403 | Quarantine until a successful explicit admin probe | Yes, only before any upstream body byte | `502 upstream_auth_error` |
| 402 | Quarantine as credits exhausted until a successful explicit admin probe | Yes, only before any upstream body byte | `503 upstream_credits_exhausted` |
| 429 | Set rate cooldown | One sequential alternate before any upstream body byte | `429 upstream_rate_limited` |
| 408, 502, 503, or 504 | Set short transient cooldown | One sequential alternate before any upstream body byte | Mapped 502/503/504 |
| Connect failure or connect timeout before request send | Set short transient cooldown | One sequential alternate | `503` or `504` |
| 422 or another request 4xx | No health penalty | No | Same 4xx mapped client error |
| 500 | Increment failure count only | No | `502 upstream_internal_error` |
| Request write timeout, response read timeout/error, or any upstream body byte | Increment failure count only | No | `502`/`504`, or SSE termination |
| 202 with valid request ID | Reserve the same key until polling terminates | No resubmit and no alternate | Final result or `504 poll_timeout` |

A public success that finishes after another concurrent public attempt has
quarantined the same key records its own successful attempt, but does not clear
the quarantine, health reason, cooldown, or `last_status_class`. Only a
successful explicit admin probe can recover an already quarantined key.

`Retry-After` accepts either non-negative decimal seconds or IMF-fixdate. A
valid result is clamped to the product-policy range 1 through 300 seconds.
Missing or malformed `Retry-After` uses equal jitter: for consecutive 429 count
`n` starting at 1, `cap = min(60 seconds, 2 seconds * 2^(n-1))` and the delay is
uniform in `[cap/2, cap]`. Transient 408/502/503/504/connect cooldown uses the
same formula with a 1-second base and a 15-second cap. Tests inject the clock
and random source. These values are not provider quota claims.

When all candidates are cooling after a 429, the response includes a rounded-up
delta-seconds `Retry-After` for the earliest safe local cooldown expiry. It does
not include fabricated RPM, remaining-request, or NVIDIA quota data.

### 6.1 Durable attempt and counter timing

One logical attempt is one public-chat key try or one admin probe. Poll and
result GETs after a `202` remain part of that attempt; trying an alternate key is
a new attempt.

Before any NVIDIA network operation, one database transaction increments the
key's `request_count`, sets `last_used_at`, inserts an `upstream_attempt` event
with `outcome_class="started"`, and advances the scheduler cursor. The network
operation must not start unless that transaction commits.

After termination, a separate transaction increments `success_count` for
success or `failure_count` for any terminal failure or cancellation, then
records final event outcome, safe status class, latency, and health fields.
Therefore `request_count >= success_count + failure_count` always holds. A
process death after the pre-network commit leaves a durable crash gap and its
`started` event. Startup never decrements, backfills, auto-completes, or replays
that POST.

The local fixed `GET /v1/models` response never changes an upstream key counter.
A downstream token's `request_count` increments durably immediately after auth
and scope succeed and before either the route response or NVIDIA call begins.

Physical response retirement is hard-bounded. A retirement timeout or pinned
runtime/transport drift withdraws readiness and cancels the process root. When
a safe HTTP status, transport/deadline outcome, JSON success, or live-stream
failure has already been selected, its terminal receipt is committed under a
shield before that fail-stop cancellation is triggered. Any fatal adapter
exception or unresolved pre-handoff close, including the initial or an
intermediate `202` response, first commits a durable `CANCELLED` terminal under
the routing coordinator's shield and only then triggers fail-stop. Ordinary
close errors never replace an already selected terminal.

## 7. SSE contract

Input is parsed as UTF-8 Server-Sent Events. Lines may end in LF or CRLF. A
complete event ends at the first empty line. Comment lines and the standard
`event`, `data`, `id`, and `retry` fields are preserved, including repeated
`data` lines and heartbeat/comment events. An event, including delimiters, may
contain at most 1,048,576 bytes. Exceeding that internal bound is
`upstream_protocol_error`; it is not described as an NVIDIA limit.

Before sending downstream response-start, the adapter buffers exactly one
complete bounded event. Response-start is sent immediately before that event.
A forwarded comment or heartbeat is visible and therefore closes the failover
window just like a data event. Receiving even one partial upstream body byte
also forbids replay, even if no complete downstream event exists yet.

After response-start:

- Backpressure is awaited. At most one complete event is pending in memory;
  the next upstream read waits until the downstream send completes.
- `data: [DONE]` is forwarded once, then both streams close. A duplicate or data
  after `[DONE]` is not forwarded.
- Client disconnect cancels the POST, same-key poll, and result fetch through
  the same AnyIO cancel scope. Cancellation never selects another key.
- A mid-stream upstream failure is never replayed. If the downstream connection
  is still writable, one bounded event is emitted and the stream closes:

```text
event: error
data: {"error":{"code":"upstream_stream_error","message":"upstream stream ended unexpectedly","request_id":"opaque"}}

```

If the downstream is already gone, no synthetic event is attempted. A stream
that ends before `[DONE]` is not reported as a successful completion.

## 8. NVIDIA 202 polling and result URL policy

An origin `202` response, as exposed after the pinned H1 parser's field-value
OWS normalization, must contain exactly one `NVCF-REQID` header. Its normalized
semantic value is 1 through 128 ASCII bytes and matches
`[A-Za-z0-9][A-Za-z0-9_-]{0,127}` exactly. A poll `202` may omit the header or
repeat exactly one byte-identical origin ID; multiple, comma-joined, or
different poll IDs fail. Leading and trailing wire SP or HTAB are removed by
the parser and are not part of the semantic value. Remaining whitespace,
slash, percent encoding, query characters, and control bytes fail as
`upstream_protocol_error`.

The service constructs, rather than accepts, the poll URL:

```text
https://api.nvcf.nvidia.com/v2/nvcf/pexec/status/{NVCF-REQID}
```

The scheme is HTTPS, host is exactly `api.nvcf.nvidia.com`, port is exactly
443, and the path prefix is exactly `/v2/nvcf/pexec/status/`. There is no
userinfo, query, or fragment. Poll GETs use the same reserved NVIDIA key, have
redirects and retries disabled, and never resubmit the original POST.

Polling uses monotonic time with a 60-second total deadline. For poll index `n`
starting at zero, `cap = min(2 seconds, 0.25 seconds * 2^n)` and equal jitter
selects a delay uniformly in `[cap/2, cap]`. A `202` continues. A `200` is the
terminal success. A terminal 4xx/5xx uses section 4 without changing key or
selecting an alternate. Disconnect or cancellation stops polling immediately.

### 8.1 302 result allowlist

The frozen result host/path allowlist is **empty**. Approved local plan, draft,
design, and attachment inputs prove the status endpoint but do not prove one
exact official result host/path. Therefore every current 302 fails closed as
`502 upstream_protocol_error`; no result GET is issued.

A future approved contract may add an exact host/path pair only with current
official evidence. Even then, acceptance requires all of the following:

1. Canonical HTTPS URL, explicit or default port 443, exact allowlisted host and
   path, no userinfo, and no fragment.
2. Proxy use disabled. All DNS A/AAAA answers must be public global-unicast
   addresses; loopback, private, link-local, multicast, reserved, unspecified,
   and documentation ranges fail closed.
3. The TLS peer address must equal one of the approved fresh DNS answers and
   certificate/SNI/Host must match the approved host.
4. Exactly one credential-free GET, with redirects and retries zero, a
   15-second total deadline, and an 8,388,608-byte response-body limit.
5. No Authorization, Cookie, Proxy-Authorization, upstream key, or downstream
   credential is forwarded. Location and query text are never logged.

The deadline and byte bound are internal SSRF/resource policy, not provider
quotas.

## 9. Admin browser and request boundary

For every framework-parseable non-`OPTIONS` request, checks run in this order:

```text
Host -> Origin -> auth/authorization or explicit unauthenticated no-op -> route
```

Normal production accepts exactly `127.0.0.1:2456`; an explicit isolated
restore accepts its validated alternate loopback port. Missing, duplicate,
comma-joined, or different Host is `403 host_forbidden` before credentials are
parsed. If Origin is present, it must use that same exact HTTP authority.
`null`, duplicate/comma-joined, HTTPS, or any other origin is
`403 origin_forbidden`. Origin-less CLI calls and top-level browser
navigation remain allowed after Host validation.

Every `OPTIONS` request terminates at:

```text
Host -> Origin -> global 405
```

It never authenticates or dispatches a route. Invalid Host or Origin remains
403; valid same-origin or origin-less OPTIONS is 405. No response contains
`Access-Control-Allow-Origin` or another CORS grant.

Shell, exact assets, and admin API responses include these exact policies:

```text
Cache-Control: no-store
Referrer-Policy: no-referrer
X-Content-Type-Options: nosniff
Content-Security-Policy: default-src 'none'; base-uri 'none'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; img-src 'self'; object-src 'none'; script-src 'self'; style-src 'self'
```

The seven static shell and exact-asset responses additionally include `Vary:
Accept-Encoding`. Admin API responses do not negotiate compression and do not
add that representation-selection header.

Inline/eval/third-party script, style, font, image, and connection origins are
forbidden. Bearer headers plus no cookies form the CSRF boundary; Host and
Origin checks protect the loopback surface from DNS rebinding and cross-site
browser attempts.

The admin password field copies its value into one per-tab JavaScript module
variable, clears the field, and never writes URL, query, cookie, DOM after
submit, localStorage, sessionStorage, logs, or cached response. Reload and
logout erase it. Video recording is disabled for the entire ordinary and native
browser-QA phases. Screenshot capture is disabled before any admin bearer or
upstream key enters a form control, and while a one-time downstream token is
present. Screenshot capture may resume only after the applicable success,
Cancel, Escape, dismissal, logout, or reload path has observably erased the
secret from the live field, serialized DOM and attributes, every ephemeral
JavaScript reference, and the clipboard when copy occurred. Post-capture
deletion or redaction is never evidence. Authenticated browser QA never enables
trace, HAR, or raw network capture. Collectors project only allowlisted method/path/status or
type/source/level and never read or store query text, headers, bodies, timing
payloads, or console arguments. Exactly three intentional UA network
diagnostics (wrong-admin 401, initial-Overview 503, offline refresh failure)
are distinct from zero application-console, runtime-exception, page, and
CSP/JavaScript errors.

### 9.1 Exact browser UI geometry and order

At an unzoomed 1280x900 outer window, `/admin` and `/showcase` each render a
224px left rail plus a fluid 12-column main grid, 24px main gutters, and 32px
outer inline inset. Product identity, the visible non-affiliation notice, and
route navigation are in the rail; the narrow-layout top disclosure is hidden.
Static DOM/CSS assertions and computed-browser assertions both verify geometry,
visibility, section spans, labels, and order.

Admin Overview contains exactly five cells in this order: `Gateway readiness`,
`Eligible keys`, `Cooling keys`, `Logical requests`, `Last event/freshness`.
`generated_at` is heading metadata, not a sixth cell. Active downstream counts
stay in the downstream-token section. Showcase contains zero operational
Overview cells. Its rail contains exactly `Buttons`, `Inputs`, `Statuses`,
`Tables`, `Dialogs`, `System states`; every section spans the main grid, and the
internal specimen/status/system-state desktop grids are exactly 3/4/3 columns.

At native 200% zoom, the fixed outer 1280x900 window has effective CSS width
640. Both routes hide the desktop rail, expose the mobile top disclosure, and
use narrow single-column reflow.

### 9.2 Browser runtime contract

Browser QA uses Python `playwright==1.61.0`, whose managed-browser descriptor
pins Chromium revision `1228`. The cache root is the absolute
`PLAYWRIGHT_BROWSERS_PATH` when configured, otherwise the current operating
user's standard `~/.cache/ms-playwright`; relative overrides are rejected.
Ordinary QA may use only the existing
descriptor-selected `chromium-1228` or `chromium_headless_shell-1228`; native
zoom uses only full `chromium-1228`. Neither phase uses an alternate index,
channel, executable override, system fallback, separate Chrome manifest, or
browser install/download. A missing required revision-1228 directory is a
blocking diagnostic.

Every browser-QA invocation runs exactly two serialized, nonoverlapping phases
in this fixed order: ordinary first, then native. The ordinary phase launches
exactly one shared Playwright-managed
browser process. Each viewport or task-flow scenario receives exactly one fresh
nonpersistent `BrowserContext` and exactly one `Page`; maximum live ordinary
contexts/pages are each one, and nested `finally` closes them before the next
scenario. One pytest test may sequence scenarios because context/page ownership
is scenario-scoped rather than test-scoped.

Native 200% is the sole additional browser-process phase. It starts only after
the ordinary `Page`, `BrowserContext`, shared browser process, and Playwright
driver have each been closed by nested-`finally` cleanup and independently
observed zero. Native-first execution is forbidden. Native launches exactly one
task-owned full managed Chromium revision 1228 with
`launch_persistent_context`, `headless=False`, sole browser argument
`--headless=new`, and outer 1280x900. It uses one fresh mode-0700 user-data
directory containing only `Default/Preferences`; the sole preference is
`partition.per_host_zoom_levels.x.127.0.0.1.zoom_level=3.8017840169239308`.
The launch-created context has exactly its one launch-created page. Shared
browser use, `new_context`, `new_page`, and popup creation are forbidden.

Native rejects `chromium_headless_shell-1228`, system/channel/executable-path
override, device-scale factor, post-launch resize, CSS zoom/transform, keyboard
zoom, and mutating CDP emulation or page-scale commands. Read-only
`Page.getLayoutMetrics` is allowed. All navigation/action timeouts are bounded;
sleep, retry, trace, HAR, and raw network capture are disabled. Video recording
is disabled for the entire ordinary and native phases.

Todo 4 native QA visits `/showcase` and completes the same deterministic fake
owner journey on that one page. Todo 6B repeats the complete low-vision owner
journey against the exact Todo 6A candidate digest. A 640px viewport, device
emulation, or equivalent scaling cannot satisfy native evidence.

Native PASS observes outer 1280x900, layout zoom 2, `innerWidth == 640`, DPR 2,
`visualViewport.scale == 1`, and `(max-width: 767px)` true. For every stable
state, `documentElement.scrollWidth <= documentElement.clientWidth` and a
maximum horizontal-scroll attempt leaves `scrollX == 0`. Local vendored axe
runs in the actual page, returns serious/critical zero, and makes zero network
requests. Natural keyboard traversal reaches every enabled control; each
focused border-plus-outline rectangle stays inside the visual viewport, has
clipped/hidden/covered counts zero, and is not intersected by a fixed/sticky
nonancestor. Each stable state's serialized-DOM SHA-256 is equal before and
after read-only observation. Cross-run comparison normalizes only UUIDs,
timestamps, full SHA fingerprints, a `Key <first-eight-hex>` handle bound to a
full fingerprint observed by the typed QA admin client during that run, long
opaque hexadecimal IDs, and observed event latency. Invalid sidecar values fail
closed, and an eight-hex phrase outside that sidecar remains significant.
Reduced-motion QA observes matching media true,
all computed transition duration/delay zero, and active transform none.
Initial-503 recovery naturally Tabs to visible Retry, presses Enter, and then
observes dashboard-title focus; direct focus injection is not evidence.

### 9.3 Packaged web resource contract

Web HTML, CSS, and JavaScript are package resources, not repository-working-
directory files. `importlib.resources` receives only a closed `WebResource` enum
mapped to fixed package and filename pairs. No request path, query, user input,
filesystem path, traversal segment, dynamic filename, or fallback directory can
reach that loader. Unknown resources fail closed.

The built wheel must include every allowlisted template and static file. Todo 1
proves this by building the wheel, installing it into a private temporary
environment, changing outside the repository working directory, importing the
installed distribution, and loading both showcase HTML and CSS through the
public resource loader. Later admin resources join the same explicit allowlist
and wheel-install proof; no generic static mount is introduced. The browser
resource set is exactly `/admin`, `/showcase`, `/assets/admin.css`,
`/assets/admin.js`, `/assets/favicon.svg`, `/assets/showcase.css`, and
`/assets/showcase.js`; an extra browser asset or route
fails the closed-set assertion.

### 9.4 Browser evidence integrity and cleanup

Each browser target accepts only a fresh absent task-owned evidence leaf under
its own gate root. It rejects symlinks, a nonempty/reused leaf, another gate's
evidence, or an arbitrary caller path, and never recursively deletes a
caller-provided directory. A gate-owned immutable manifest fixes required
capture names, order, and count before launch. Immediately before receipt, the
gate reopens every required capture, recomputes byte count and SHA-256, and
rejects any missing, extra, renamed, or drifted file.

Native captures include both secret-free full-page and focused-control images,
each indexed with `native_zoom:true`, capture ID, observed byte count, and
SHA-256. Video remains disabled throughout both phases. Screenshot capture
follows the Section 9 custody rule and cannot resume until every applicable
secret surface is observably clear. Trace, HAR, and raw network capture remain
off.

The native receipt is written only after nested-`finally` cleanup independently
observes task browser descendants, Playwright drivers, contexts, pages,
persistent profiles, temporary paths, fake-server threads, port listeners,
clipboard content, and blackout state all zero. One close failure is aggregated
but cannot skip later cleanup. The receipt records contexts/pages started
exactly one, maximum live each one, `shared_browser_used:false`, preference
value plus `Default/Preferences` SHA-256 without a temp path, executable/revision
provenance, metrics, axe/focus/overflow results, and capture IDs. Cleanup values
are observations, never receipt literals or worker self-claims.

Two fresh task-owned runs must independently reproduce required capture
name/order/count, stable DOM hashes, native metrics, preference hash, and zero
cleanup observations. An observer compares both receipts; a run's own
determinism assertion is insufficient.

## 10. Vault and secret custody

The vault master key file contains exactly 32 raw bytes for AES-256-GCM. Each
upstream row stores a version integer, row UUID, random 12-byte nonce,
ciphertext plus the 16-byte GCM tag, and a SHA-256 hexadecimal fingerprint.
Plaintext is never a row, response field, log field, screenshot, evidence value,
or command argument.

Version 1 uses this exact AAD byte sequence:

```text
b"nvidia-build-lb:vault:v1\x00" + row_uuid.bytes
```

The prefix is the displayed ASCII bytes followed by one NUL byte; UUID bytes
are the 16 network-order bytes from the canonical UUID, not its text form. The
row version must be integer `1` and must agree with the `v1` prefix. Unknown
version, wrong master key, nonce/tag tamper, or moving ciphertext to another row
fails closed with no partial plaintext. Nonce generation uses the operating
system CSPRNG for every encryption; row identity in AAD prevents row swapping.

Canonical host files are
`/opt/nvidia-build-lb/secrets/{admin_token,vault_master_key,db_password}` with
root:root ownership and mode 0600. Only a bounded root prestart with no network
copies each consumer's required value into private container tmpfs as mode 0400
for the fixed runtime UID, then immediately execs the non-root process with
capabilities and supplementary groups cleared. App and database cannot read
each other's runtime files. Stop/reboot destroys tmpfs copies. Rotation is an
atomic canonical-file replace plus restart of only the consuming service.

Non-secret production authority is the root-owned, non-writable
`/etc/nvidia-build-lb/runtime.env`. It contains the exact app/PostgreSQL registry
digests, canonical secret-directory path, and three ledger-cap values. The
production wrapper parses a closed key set on every invocation and overwrites
transient image/cap environment values before Compose runs. Its atomic update
commands preserve every unrelated field, so credential rotation, rollback, and
capacity recovery cannot silently reset caps or drift an image reference. A
stable root-owned `runtime.env.lock` inode serializes all writers and excludes
Compose readers during a write. Each replacement is also a SHA-256 compare-and-
swap against the generation loaded under that lock. Ledger-capacity recovery is
one exclusive operation: it verifies the current immutable app reference and
image ID even when a prior failed attempt left the app stopped, recreates that
same image with candidate caps, and verifies the new container still uses it. It
then polls the bearer-authenticated bounded operator-readiness endpoint over host
loopback with the root-owned canonical token file until the runtime is operational and the ledger
is nonblocked, accepting both ready and no-eligible-key states, and only then
commits the runtime file. The host-owned probe is independent of the target app
image. It connects to any selected loopback port with the canonical service
`Host`, and caps every response with a two-second absolute deadline and 2 MiB
body limit. Current responses must satisfy the exact four-field DTO and closed
runtime/readiness/ledger coherence. Runtime mode is used by backup, restore, and
rollback. Only an operator-readiness `404` permits the exact authenticated legacy
overview: a ready result passes immediately; degraded/no-key must remain exact
and reachable in a second sample 30 seconds later on the same container ID and
Docker `StartedAt` generation, beyond the prior image's fatal grace plus bounded
retirement budget. Ledger-capacity mode has no
fallback and additionally requires a nonblocked ledger. Paired receipts bind
the restored ledger state, so these flows accept intentional degraded states
without accepting a database or lifecycle outage. Any post-recreation failure
or signal stops the app so the candidate generation cannot continue accepting
traffic without durable config; a later invocation can safely reenter that
same-image withdrawn state.

Ledger row capacity and durable evidence health are separate admission terms.
`orphaned_pending` and `legacy_unlinked` persist as hard admission blockers even
when later cap values leave free rows. Dashboard readiness uses the same
predicate. Only a reviewed forward repair followed by a successful maintenance
assessment that observes the anomaly absent may clear the persisted blocker;
cap changes, restarts, and projection logic cannot clear it.

Migration `0004_vault_key_verifier` adds one singleton database binding for the
vault key. The binding is a fresh 32-byte salt plus HMAC-SHA-256 over the fixed
`nvidia-build-lb:vault-key-verifier:v1` context; it is not a plaintext key or a
reversible key derivative. An unbound upgraded database is initialized only
after every existing upstream ciphertext decrypts successfully under the
configured key. A bound database rejects every later startup whose key does
not match, including a different but correctly sized 32-byte value.

## 11. PostgreSQL migration and backup custody

The current Alembic head is `0005_admin_dashboard_ledger`: `0001_baseline` remains
schema-neutral, `0002_vault_auth` adds encrypted vault/auth tables,
`0003_nvidia_routing` adds durable routing state, and
`0004_vault_key_verifier` adds the singleton vault-key binding. Complete 0004 is
the legacy V2 backup/restore shape only. `0005_admin_dashboard_ledger` adds the
canonical dashboard ledger, exact attempt-terminal linkage, bounded evidence
retention, and the current V3 backup/restore shape.

Backups are three separately custodied root-only artifacts:

1. PostgreSQL logical dump containing encrypted vault rows and token digests.
2. The matching vault master-key secret, never embedded in the dump archive.
3. A strict manifest binding both hashes to the full safe database identity,
   including the Alembic head and vault verifier salt/digest.

Each has mode 0600, its own checksum and retention record, and a distinct path.
Restore combines the matching pair only inside an isolated drill stack with a
different network, port, container names, and volume. The drill proves safe key
IDs/fingerprints and downstream digests, then removes only drill-owned
resources. Normal backup, restore, rollback, or QA never deletes the production
volume. A missing/mismatched key fails closed.

## 12. Hermes durable two-file transaction

The lock is the stable root-owned mode-0600 host-only file
`/opt/nvidia-build-lb/hermes-cutover-state/cutover.lock`. The secret-free
transaction journal is in the same root-owned mode-0700 directory, and immutable
rollback generations are under the separate root-owned mode-0700
`/opt/nvidia-build-lb/hermes-cutover-backups/` directory. Neither directory is
within `/opt/agent-apps`, physically backed by the Hermes tree through a bind
mount, or mounted into Hermes. Backup generations also reject nested mount
points before read, restore, or retirement. Every cutover, rollback,
reapply, and recovery invocation opens the lock once. A full `cycle` keeps the
same FD-backed exclusive `flock` from pre-issuance reconciliation through final
prior-downstream-token revocation, when one exists, and terminal journal
persistence. A restored direct-upstream source is also a valid cycle start; its
provider credential is revoked separately only after final reapply. The live
matrix and the delayed agent-app updater acquire this same inode before reading
or changing Hermes state, so no Hermes writer runs concurrently.

The operator `preflight` reconciles any nonterminal journal under that lock and
returns `issuance_allowed=true` only when a new candidate may be created. The
supported `cycle` command repeats that gate, persists a unique issuance label,
and then creates the scoped candidate through the admin API while still holding
the same lock. No production CLI command accepts a bearer through stdin or
argv. An `ACTION_REQUIRED` journal therefore returns nonzero before issuance,
and every successfully created candidate has durable reconciliation authority.

Under the lock, the transaction:

1. Captures `config.yaml` and the root-only live `.env` as tuples
   `(sha256, uid, gid, mode, regular-file/no-symlink)`.
2. Creates unique immutable host-only backups whose directory is mode `0700`
   and whose three exact regular files are root-owned, single-link, mode `0600`,
   and hash-bound to the manifest. Same-directory candidate temps for the two
   live files exist only while Hermes is stopped. It fsyncs every file and
   affected directory before mutation.
   An exact root-owned single-link mode-0600 atomic-manifest temp left by process
   death is removed under the transaction lock before validation or partial
   generation discard; any other entry still fails closed.
3. Durably writes the root-only phase journal
   `/opt/nvidia-build-lb/hermes-cutover-state/journal.json` through a
   same-directory mode-0600 temp, file fsync, atomic rename, and parent fsync.
   The journal contains only attempt ID, source/target tuples, immutable backup
   IDs, unique issuance label, nullable candidate downstream token ID,
   candidate provenance, candidate-revocation confirmation, manual-candidate
   review intent, reconciliation terminal phase, source/target tuples, commit
   decision, any pending one-key exclusion intent, and phase. It never contains
   token plaintext.
4. Before issuing a token, persists the attempt UUID, exact unique label
   `hermes-cutover:<attempt-uuid>`, and null token ID. It POSTs exactly once. On
   success it durably records the token ID and `helper_issued` provenance while
   plaintext remains process-only.
   For an ambiguous result it lists every token and matches the exact label. A
   unique row is revoked and verified because its one-time plaintext cannot be
   recovered; multiple rows fail closed. It never retries issuance with the same
   label.
5. Stops only the Hermes service, verifies that the container is no longer
   running, recomputes both live tuples, validates exact candidate token state
   and scopes, and proves the process-held helper-issued bearer belongs to that
   token ID using an attributed request-count probe. Only then may it create
   candidate files. The retired manual-bearer command is not exposed by the
   production parser, but recovery still accepts its already-durable journals.
   If a legacy manual probe may have succeeded but its response or following
   journal write was lost, recovery restores the protected source and stays at
   `candidate_reconciliation_required`. It returns the safe token ID,
   `manual_candidate_requires_review=true`, and
   `candidate_revoked_confirmed=false`; no cutover, rollback, cycle, or new
   issuance may advance until the owner revokes that exact UI row and recovery
   verifies `revoked_at`.
6. If either pre-write CAS drifts, leaves both live files untouched,
   keeps Hermes stopped, and revokes only a helper-issued or positively bound
   candidate token. Unknown tuples are never replaced by an old backup.
7. Fsyncs candidates, journals before and after each rename, atomically renames
   the pair one file at a time, and fsyncs the destination directory after each
   rename.
8. Restarts only the Hermes gateway. It verifies the running generation's
   effective provider, base URL `http://127.0.0.1:2456/v1`, model
   `z-ai/glm-5.2`, and token ID against the journal target before intake opens,
   then runs Korean non-streaming, `[DONE]` streaming, and a `/v1/runs` task
   that must emit both `tool.started` and `tool.completed`.

Recovery recognizes only the journal's exact source, target, or recorded
intermediate mixed tuple between the two renames. It uses fresh same-directory
temps. Any unknown tuple or unknown mixed pair is never overwritten and leaves
intake fail-closed as `recovery_required`.

A token may be revoked only after both files and the running generation prove
it unreferenced. Ambiguous references keep intake closed as
`recovery_required`. Verified rollback-only exit revokes its bound candidate.
Verified final reapply writes and reads back `commit_decided` before revoking a
previous downstream token. Recovery before that decision restores the source;
recovery after it completes the target and previous-token revocation. A direct
upstream source has no downstream token to revoke and remains subject to the
separate provider-revocation gate. Recovery receipts expose only safe backup and
token IDs plus an exact `next_action`. An unresolved manual token returns
`ACTION_REQUIRED` and `review_and_revoke_candidate_token`; only a subsequent
receipt with `candidate_revoked_confirmed=true` may return
`issue_new_candidate`. Loss of the original stdout therefore does not remove
rollback, reconciliation, or retirement authority.

Rollback and final reapply are separately journalled paired replacements inside
the same cycle lock. Each repeats `close -> dual CAS -> durable pair replace ->
gateway-only restart -> effective generation verification`. Targeted fixtures
cover first-rename interruption, recorded mixed-pair recovery, unknown-drift
preservation, backup integrity/type rejection, bearer/ID binding, ambiguous
issuance reconciliation, and single-lock cycle continuity. Live gates add the
real restart, rollback, reapply, token-revocation, one-key exclusion, and Hermes
tool-run evidence. One-key exclusion journals the excluded and alternate IDs
before disable, restores and verifies the original two-key eligible projection
on normal exit, exception, or fresh recovery, and never relies on an in-memory
`finally` alone.

`scripts/ops/hermes_cutover.py retire-backup` is the only normal retirement
path. It accepts one exact generated backup ID, holds the same lock, requires a
terminal manifest, verifies any prior downstream token is revoked, and refuses
an upstream-bearing generation unless the operator explicitly confirms provider
revocation. It then atomically renames the generation to a direct-child
retirement tombstone, keeps the validated manifest until last, removes the
credential-bearing file first with directory fsyncs, and removes the empty
tombstone with a parent fsync. A repeated command resumes any interrupted
tombstone while rejecting links, mounts, unexpected entries, metadata drift, or
hash drift. A pre-existing tombstone is first stabilized with a backup-parent
fsync and an `original absent / tombstone present` recheck; no child unlink
precedes that durability gate. A separate root-only secret-free pending/complete receipt is durable
before deletion; if unlink and parent fsync finish before stdout, retrying the
same ID promotes the pending receipt to complete and returns the prior PASS.

## 13. Stable Make verification contract

`EVIDENCE_DIR` is a non-secret output directory. `IMAGE_DIGEST` and
`POSTGRES_IMAGE_DIGEST` are the immutable local image IDs emitted together by
one `build-candidate` run. `SOURCE_MANIFEST` is that run's exact manifest file;
later gates require byte equality before and after execution. `MODE` is exactly
`one-key` or `two-key`. Both modes require exactly two registered upstream rows;
the names mean exactly one or two rows are eligible at entry. No target accepts
a credential through argv, and no target may print a credential-bearing
environment. Database-only sensors run `psql` as the container's PostgreSQL OS
user over its local peer-authenticated socket; no database password is placed in
argv or a process environment.

Every completed target writes `manual-qa.json`, `adversarial.json`, and
`cleanup.json` under its evidence directory. Artifacts contain only exit codes,
hashes, internal IDs/fingerprints, safe status classes, and redacted counts.
Cleanup proves all target-created PIDs, ports, containers, browsers, and temp
paths absent. On HUP, INT, TERM, failure, or a first app-recreate error, bounded
cleanup/recovery still runs before exit.
Browser targets apply Section 9.4's stricter fresh task-owned leaf, pre-receipt
capture rehash, observed nested cleanup, and two-fresh-run determinism rules.

| Target | Exact command owned by Make | Required inputs | PASS and artifact | Cleanup | Implementation state |
| --- | --- | --- | --- | --- | --- |
| `contract-red` | `uv run python tests/contracts/verify_intentional_red.py --evidence-dir "$(EVIDENCE_DIR)"` | `EVIDENCE_DIR` | Collection exit 0; expected node IDs/classes equal observed; no bootstrap/skip/xfail; `intentional-red.json` | Verifier removes only its temp collection/run files | Lane 3 supplies the verifier; recipe is frozen |
| `test-vault-auth` | `EVIDENCE_DIR="$(EVIDENCE_DIR)" uv run pytest -m vault_auth -q` | `EVIDENCE_DIR` | Migration/crypto/CRUD/token separation/scope/revoke marker suite exits 0 | Test-owned PostgreSQL/temp secrets absent | Direct marker frozen for Todo 2 |
| `test-nvidia-routing` | `EVIDENCE_DIR="$(EVIDENCE_DIR)" uv run pytest -m nvidia_routing -q` | `EVIDENCE_DIR` | Fake-wire polling/SSE/RR/cooldown/failover suite exits 0 and ambiguous POST count is one | Fake server, port, task group, temp files absent | Direct marker frozen for Todo 3 |
| `test-ui-fake` | `EVIDENCE_DIR="$(EVIDENCE_DIR)" uv run pytest -m ui_fake -q` | fresh task-4-owned `EVIDENCE_DIR` | Exact 1280 admin/showcase geometry/order plus serialized ordinary and native-200% fake-owner journeys pass; local axe serious/critical and axe-network counts zero | Nested cleanup observes browser descendants/driver/contexts/pages/profile/temp/fake thread/listener/clipboard/blackout all zero | Direct marker frozen for Todo 4 |
| `test-api` | `EVIDENCE_DIR="$(EVIDENCE_DIR)" uv run pytest -m api -q` | `EVIDENCE_DIR` | Curl/OpenAI SDK stream, non-stream, scopes, errors, and log redaction exit 0 | API/fake upstream/DB/temp secrets absent | Direct marker frozen for Todo 5 |
| `build-candidate` | `EVIDENCE_DIR="$(EVIDENCE_DIR)" scripts/qa/build-candidate.sh` | `EVIDENCE_DIR` | Manifest-bound read-only source snapshot builds and verifies one healthy immutable app/PG pair; both receipts and source manifest are recorded | QA containers/network/volume/ports/temp secrets absent; candidate images retained for later gates | Implemented; exclusive evidence directory required |
| `test-browser-prod` | `IMAGE_DIGEST="$(IMAGE_DIGEST)" POSTGRES_IMAGE_DIGEST="$(POSTGRES_IMAGE_DIGEST)" FIXTURE_IMAGE_DIGEST="$(FIXTURE_IMAGE_DIGEST)" SOURCE_MANIFEST="$(SOURCE_MANIFEST)" EVIDENCE_DIR="$(EVIDENCE_DIR)" scripts/qa/test-browser-prod.sh` | exact app/PG/fixture triplet, byte-identical 6A manifest, fresh task-6b-owned `EVIDENCE_DIR`; set `NBLB_BROWSER_QA_PORT` when production owns 2456 | Exact 1280 structures, ordinary owner journeys, and native-200% low-vision repeat on the same pair pass deterministic runs, cold audits, and two artifact-bound visual reviews | Same Section 9.4 nested zero-resource observations; all candidate image IDs remain unchanged | Implemented; missing required input exits 64 |
| `verify-local` | `IMAGE_DIGEST="$(IMAGE_DIGEST)" POSTGRES_IMAGE_DIGEST="$(POSTGRES_IMAGE_DIGEST)" FIXTURE_IMAGE_DIGEST="$(FIXTURE_IMAGE_DIGEST)" SOURCE_MANIFEST="$(SOURCE_MANIFEST)" EVIDENCE_DIR="$(EVIDENCE_DIR)" scripts/qa/verify-local.sh` | exact app/PG/fixture triplet, manifest, evidence directory | Static/tests/migration/compose/restart/backup/isolated restore and exact DB-down 503 pass; codex-lb remains 200 | All QA stack and restore resources absent; all candidate IDs remain | Implemented; missing required input exits 64 |
| `scan-release` | `IMAGE_DIGEST="$(IMAGE_DIGEST)" POSTGRES_IMAGE_DIGEST="$(POSTGRES_IMAGE_DIGEST)" SOURCE_MANIFEST="$(SOURCE_MANIFEST)" EVIDENCE_DIR="$(EVIDENCE_DIR)" scripts/qa/scan-release.sh` | exact app/PG pair, manifest, evidence directory | Dependency/source/history/action-pin plus both-image vulnerability/secret scans exit 0 without ignoring unfixed findings | Scanner containers/temp exports absent; manifest bytes and both image IDs unchanged | Implemented; missing required input exits 64 |
| `smoke-live` | `MODE="$(MODE)" IMAGE_DIGEST="$(IMAGE_DIGEST)" EVIDENCE_DIR="$(EVIDENCE_DIR)" scripts/qa/smoke-live.sh` | All three variables; root custody; exactly two registered rows and MODE-selected eligible count | Per-key meaningful real responses, three core repetitions, exact six-attempt alternating 3:3 routing, disabled exclusion, controlled synthetic 401 failover, controlled future-cooldown exclusion, scope/revoke, and post-recreate per-key calls pass; one-key never claims distribution | Actual task-label active-token count and synthetic-row count are zero; full safe upstream projection, scheduler cursor, old/new app logs from matrix start, filtered args/env, prior Hermes state, and healthy gateway are restored | Implemented; invalid input 64, non-root 77, missing executable regression 78 |
| `smoke-hermes` | `IMAGE_DIGEST="$(IMAGE_DIGEST)" EVIDENCE_DIR="$(EVIDENCE_DIR)" scripts/qa/smoke-hermes.sh` | Both variables; root custody | New dedicated token cutover, Korean stream/non-stream, tool event proof, rollback, final reapply, one-key exclusion, and restart pass | Lock released; replaced token revoke confirmed; candidate-file count exactly zero and journal exactly terminal `reapplied` in cleanup gate | Implemented; invalid input 64, non-root 77, missing executable regression 78 |

Recipe status 64 means a required non-secret Make input is invalid. Status 77
means the two live targets lack root custody. Status 78 means a required live
operator executable was removed from the release. GNU Make reports these failed
recipes as process exit 2 while preserving the exact `Error 64`, `Error 77`, or
`Error 78` classification and the stable diagnostic.
These explicit failures are never PASS evidence. `make help` must always parse
and exit zero.

## 14. Release operations contract

Online Alembic execution owns the two-int session advisory lock
`(1312967746, 2)`. The service epoch lock uses the same first key and second key
`1`, so migration and runtime ownership are separately observable. Migration
sets a bounded statement timeout before blocking lock acquisition, clears that
timeout after acquisition, runs the repository head on the same connection, and
releases in `finally`. Connection loss also releases the session lock.

A fatal service-epoch monitor observation withdraws lifecycle readiness
synchronously. `/health` then short-circuits without database I/O and returns
the exact degraded 503 body for a bounded two-second observability window before
the process root is cancelled. This grace applies only to the database monitor;
unresolved durable attempt commits and runtime/transport drift retain their
immediate fail-stop path.

Backups are quiesced while the app container is stopped. A backup is valid only
as the tuple of a PostgreSQL custom dump, the exact vault master key in a
separate root, and a strict manifest in a third root. The manifest binds hashes,
sizes, Alembic revision, ordered upstream IDs/fingerprints, and aggregate hashes
of ordered upstream identities and downstream IDs/digests, plus the database
vault-verifier salt/digest. Before copying the key, backup verifies it against
that database binding inside a network-disabled helper. The manifest contains no
plaintext credential, ciphertext, nonce, raw downstream digest, DB password, or
provider payload.

The state oracle dispatches strictly between V2 for the complete 0004 schema
and V3 for the complete 0005 schema; any partial shape fails closed. V3 hashes
fixed-position, compact ASCII JSON projections of every event, exact attempt
receipt, live pin, and the ledger singleton, and separately binds their counts,
pending attempts, and the routed-request rollup. Restore compares the exact
field set for the captured version, so a same-count identity change cannot pass.

Restore is forbidden against a database unless it is running, empty of all user
schemas, relations, functions, and types, and explicitly labeled
`nvidia-build-lb.restore-isolated=true`. Both artifacts are
rehash-verified before mutation. The master key is installed atomically in the
isolated target secret directory; `pg_restore --exit-on-error` loads the dump;
then the exact safe database state must equal the manifest. Any mismatch is a
hard failure and the isolated partial target is disposable. The production DB
is labeled restore-isolated false.

The production Compose surface requires immutable application and PostgreSQL
image references, binds host loopback by default, disables Watchtower, retains
read-only root filesystems and tmpfs handoff, and separates the internal data
network from app egress. CI uses full commit-pinned Actions and explicit minimum
permissions. Publication builds the app/PG pair once from the same
manifest-bound snapshot, scans those exact local image IDs, and only then tags
and pushes the same IDs. GHCR emits only commit-addressed application and
PostgreSQL tags; no mutable `latest` tag is produced.
