# NVIDIA Build LB Design System

## 0. Product Decision Axis

The product axis is **judgment -> action -> evidence**. Every operational view
uses the same sentence order: **state -> meaning or cause -> one safest next
action -> confirmed result**. A technically complete screen fails this contract
when the operator must translate raw identifiers, timestamps, counters, or event
rows before knowing what to do.

### Product promise

Within ten seconds after authentication, an operator must be able to answer:

1. Is the gateway ready to accept requests, and how fresh is that conclusion?
2. How many keys can actually receive a request, and what excludes the others?
3. What is the latest condition that needs attention and which stable key or
   client handle does it affect?
4. What is the one safest action available now?
5. Did the last action finish, what changed, and what should happen next?

The first content viewport presents those answers before audit detail. A normal
state says that no action is required instead of inventing work. A degraded,
stale, or failed state states what is known, what is unknown, the traffic
impact, and the exact safe recovery action in one located surface.

### Information hierarchy and budget

| Layer | Purpose | Default treatment |
| --- | --- | --- |
| L0 | Current judgment, freshness, blocking cause | Always visible |
| L1 | Affected resource, impact, one next action, last confirmed result | Visible in the relevant overview, section, or row |
| L2 | Counters, exact UTC time, short fingerprint and internal ID | One explicit details disclosure from its L1 resource |
| L3 | Raw event IDs, request IDs, exact status classes, complete audit history | One explicit audit disclosure from the activity summary |

Each page heading has the route name and one current-purpose sentence. Each
resource section has one state or count, one short explanation, and one primary
task at most. Repeated visible captions, implementation vocabulary, QA language,
and raw enum names do not consume the primary information budget. Machine data
remains available for audit without competing with the operating decision.

### Core journey ladder

The console derives one recommended step from server-confirmed state:

1. Authenticate locally.
2. Add the first upstream credential; it remains disabled.
3. Probe that stable key handle successfully.
4. Enable the verified key for routing.
5. Issue a downstream token with the minimum needed scopes.
6. Operate normally, or address the newest exception before reading full audit
   history.

The recommended step is the sole strongest action. Section and row controls
remain discoverable but visually secondary. Enable stays unavailable until a
successful probe is represented by current server state. A disabled, cooling,
quarantined, stale, or unknown key explains the prerequisite beside its action.
Once at least one key is eligible, issuing the first downstream credential takes
priority over non-blocking pool maintenance. An active cooldown states its
automatic return condition and exposes no immediate primary action before that
condition is reached; the matching row-level Probe is also unavailable until
then. A rejected credential is retired immediately when another key is eligible;
this does not reduce serving capacity because the rejected key was already
excluded. When no key is eligible, add, probe, and enable a replacement before
retirement. After cleanup, a one-key pool recommends adding and verifying the
second key so round-robin failover is restored without duplicate replacement
loops.

### State, action, and result contract

- Every mutation owns a target-specific progress label, for example `Probing
  Key bc68b4f8...`.
- In-progress and unknown outcomes use a separate `Current operation` surface.
  They never overwrite or appear beneath the `Last confirmed result` label;
  only a terminal response or fresh-state reconciliation advances that slot.
- Success remains visibly located after refresh and states the postcondition
  and next step. Failure names the target and action, states that success was
  not assumed, and offers the action that is actually performed.
- A mutation 2xx and its follow-up full-snapshot read are separate facts. If the
  mutation is confirmed but refresh fails, the confirmed result remains visible
  while every retained row and count becomes `Last confirmed`; the UI never
  demotes the mutation to unknown or leaves an in-progress label behind.
- Authentication expiry preserves a non-secret description of the interrupted
  target and action. Reauthentication asks the operator to verify current state
  before retrying; it never claims the ambiguous action failed or succeeded.
- While authentication is pending, the submitted bearer field is immutable and
  cannot accept a replacement value that would be silently discarded when the
  response remounts the login surface. A new attempt begins only after the prior
  request reaches a terminal response.
- A lost add response is reconciled only by the submitted key's exact SHA-256
  fingerprint, never by assuming that one newly visible row is the same request.
  A lost probe, enable, disable, delete, or revoke response is reconciled against
  a fresh target state and states only the observed postcondition; it never
  attributes a concurrent change to the missing response.
- A lost response uses one outcome-recovery surface inside the active dialog.
  It never competes with a generic stale warning or marks a discarded secret
  field invalid. The copy states exactly what was erased and what a fresh read
  can correlate.
- Generic dashboard refresh is labeled `Refresh`, never `Retry probe` or another
  mutation it does not execute. Recovery controls are not destructive controls.
- A destructive cleanup step is described in the decision brief, but its global
  recommendation only moves focus to the exact row control. Disable, Delete, and
  Revoke begin only from that row and never occupy the primary-action position.
- Form errors identify the field or fieldset to fix, remain linked by
  `aria-describedby`, and preserve all non-secret safe input where custody
  permits.
- Validation and resource conflict may belong to a field. Transport, service,
  strict-response, and ambiguous-outcome failures belong to one dialog or page
  recovery summary, never mark valid input invalid, and persist across input
  edits until Cancel or fresh reconciliation.
- A one-time credential dialog says visibly what Copy and Dismiss do. If the
  Clipboard API was never available and no copy occurred, Dismiss still clears
  the page and exits safely. A possibly copied value requires verified clipboard
  cleanup before exit.
- The one-time value is a native read-only multiline text control. Its accessible
  name, value, caret, exact Select All behavior, manual Copy event, and cleanup
  are browser-native; ARIA does not recreate textbox semantics on generic markup.
- While a one-time credential, a possible clipboard copy, or a clipboard write
  is unresolved, reload and tab close receive the same explicit exit guard as
  an unresolved mutation. The credential value supports exact keyboard
  selection; a manual copy enters the same verified-cleanup path as Copy.
- Before any async task disables its focused control, focus moves to a visible
  local progress status. Login, manual/recommended Refresh, row actions, dialog
  mutations, Copy, and Dismiss all preserve focus inside the active task and
  restore it deterministically after completion. Submitted dialog fields remain
  immutable while their request is pending.
- A settlement-not-confirmed response states only that the snapshot read
  overlapped an administration change. It never claims that change is still in
  progress or asks the operator to wait. Any locally known non-secret target and
  attempted action remains a separate L1 unconfirmed operation, while the copy
  states that the service cannot correlate it with the overlapping change. The
  local outcome stays unknown, every mutation stays locked, and immediate
  Refresh is the only action; the surface never invents correlation, collapses
  to an anonymous "previous request", or re-enables the unresolved mutation.
- A confirmed deliberate Disable or final-token Revoke is not immediately
  reversed by an Enable or Issue primary action. Enable is recommended only
  when this journey has current probe/replacement evidence; first-token Issue
  is onboarding only when no token history exists. Otherwise explicit row or
  section controls remain available without inventing urgent work.
- Local probe and deliberate-pause intent binds only to server evidence from
  the confirmed operation. A probe may bind to a refreshed key version no
  later than its returned `observed_at`; a pause may bind only when the fresh
  key `updated_at` matches its successful disable event. Any later version
  drift invalidates that local intent. A tracked replacement still requires
  this current probe evidence before Enable becomes the primary action.

### Freshness and progressive disclosure

- A successful full refresh starts a bounded current-snapshot interval. When it
  expires or refresh fails, prior data is explicitly stale and all mutations
  lock until a new full snapshot succeeds. Read and recovery navigation remain
  available.
- Starting a refresh does not cancel the prior snapshot deadline. The prior
  snapshot remains current only until its original deadline, then becomes stale
  even while the new read is pending. A pending mutation exclusively owns its
  outcome: every Refresh entry remains locked until that response settles.
- When a snapshot is stale, readiness is unknown and every retained count is
  labeled as a last-confirmed value. Relative event time is not presented as
  current freshness; the exact prior event time remains snapshot evidence.
- Fresh relative time uses the server snapshot time advanced by the measured
  full-read latency. Stale key rows and activity summaries never retain `in ...`,
  `Just now`, or another present-tense relative-time claim; they use exact
  last-confirmed timestamps in evidence.
- Snapshot currency ends at the earlier of one minute or the earliest known
  future `cooldown_until`. Once that transition is due, old cooling state is
  stale until Refresh. A refreshed past cooldown is described as ended rather
  than still waiting, with enabled/disabled postconditions kept distinct.
- The canonical `GET /admin/api/v1/dashboard` supplies overview, key, token,
  ledger, runtime, deadline, and event facts from one database transaction.
  Legacy individual reads remain bounded compatibility surfaces, but the UI
  never composes them into current truth. Internal counter/readiness mismatch in
  the canonical DTO is unavailable/stale, never a current snapshot.
- The five locked Overview cells remain exactly five. A separate decision brief
  translates them into cause, impact, and next action.
- Stable operator handles use a short irreversible fingerprint; full UUIDs,
  fingerprints, counters, and exact timestamps are evidence details.
- Recent activity first summarizes failures, cancellations, and operational
  changes. Routine raw events and the complete newest-100 audit remain available
  through one keyboard-operable disclosure that reports how many items it
  contains.
- Attempt starts are paired with their terminal by exact
  `attempt_started_event_id`; request identity groups one logical routed request
  but is never sufficient evidence of an attempt edge. A completed attempt
  appears once. Because the newest-100 event window alone cannot prove a start
  is still pending, an unpaired start is labeled completion unconfirmed with
  age and an explicit no-retry warning. Operator probes are named separately
  from routed traffic.
- Progressive disclosure changes priority, not truth. Hidden evidence is never
  hover-only, is reachable in one activation, preserves DOM reading order, and
  returns focus deterministically.

### Responsive decision order

At 375px, 768px, unzoomed 1280px, and native 200 percent, the order remains:
current judgment, next action, affected resource, confirmed result, then audit
evidence. Narrow navigation is closed by default and does not precede the first
decision after login. Responsive success requires more than zero horizontal
overflow: the first content viewport exposes the judgment and next action, each
core section is one navigation activation away, and secondary evidence is one
additional activation away.

### Reusable expert-review prompt

Every PdM, product-design, accessibility, content/design-system, and operator
review uses this same prompt before declaring LGTM:

> For each core journey and every default, loading, empty, success, stale,
> offline, unauthorized, validation, upstream-failure, and destructive state:
> identify the user's exact question; the L0/L1 information needed to answer it;
> the one safest next action; the server-confirmed result; and the L2/L3 evidence
> that may be disclosed. Verify the answer at 375px, 768px, unzoomed 1280px, and
> native 200 percent with keyboard, low-vision, distracted-operator, and incident
> personas. Report any competing action, unexplained state, raw-data-first
> hierarchy, focus loss, dead end, ambiguous outcome, or hidden prerequisite as
> CHANGES_REQUIRED with a located required outcome. LGTM requires every core
> journey to satisfy judgment -> action -> evidence without weakening secret
> custody, authorization, audit access, or the locked layout/API contracts.

This section is the authority for future UI judgment. Later examples and
component rules specialize it; they do not override it.

### Verification cadence

Implementation uses a fixed evidence ladder so feedback stays fast without
weakening the release gate:

1. Reproduce and rerun only the exact failing node while the cause is changing.
2. Once that node is green, run the directly affected contract, unit, type, lint,
   or browser group in one serial process where tests share a fixed authority.
3. Do not run broad browser, native, container, security, or full regression
   gates while source is still moving. Review agents inspect current source and
   focused evidence; they do not independently repeat the full gate.
4. Freeze one candidate source/image digest, then run the complete release gate.
   Any source change after that gate invalidates its result and requires one new
   complete gate against the newly frozen candidate, not full-suite reruns after
   every intermediate edit.

Tests remain sensors: no skip, weakening, broadened assertion, or fabricated
result may replace a root-cause fix.

### Backup evidence contract

Operational recovery follows the same **judgment -> action -> evidence** axis.
The judgment is the exact captured schema generation; the action is a quiesced
paired backup or isolated restore; the evidence is a version-matched manifest
comparison. V2 is valid only for the complete 0004 schema. V3 is valid only for
the complete 0005 schema and binds canonical counts and SHA-256 projections for
events, attempt receipts, pending attempts, live pins, logical routed-request
rollup, and the ledger singleton. A partial schema, version mismatch, equal
counts with different identities, or any projection mismatch fails closed.
Receipts expose only stable field names, counts, and irreversible digests.
`orphaned_pending` and `legacy_unlinked` are durable evidence blockers: both the
dashboard and server admission stay closed even if an operator raises row caps.
The L1 decision names the concrete cause, requires a paired backup and withdrawn
gateway, and points only to a reviewed evidence-preserving forward repair. A cap
increase or restart is never presented as repair for either state.

Runtime recovery also has one authority boundary. Every production Compose read
holds a shared lock on the stable root-owned `runtime.env.lock` inode; a runtime
update or ledger-capacity recovery holds it exclusively and compare-checks the
loaded runtime-file digest before replacement. Transient capacity recovery is
one operation from current-image verification through same-image recreation,
bounded authenticated ledger convergence, and final config commit. The
convergence proof is independent of eligible-key readiness so an empty first-run
key set does not deadlock administration. No intermediate cap value is presented
as durable success. A post-recreation image, capacity, CAS, signal, or commit
failure withdraws the app, and the same operation can reenter that stopped
same-image state. Permanent evidence blockers never enter this path. Backup,
restore, and rollback use the same target-image-independent host checker in
runtime-only mode. Each request uses the canonical service `Host` independently
of the host connection port, a two-second absolute deadline, and a 2 MiB body
cap. Current images expose an authenticated, fixed four-field
`GET /admin/api/v1/operator-readiness` DTO containing only `runtime_state`,
`readiness_cause`, `ledger_status`, and `capacity_blocker`. Its repeatable-read
query reads the ledger singleton, the three admission counts, and one eligible-
key count; it never materializes upstream, downstream, or event collections.
Only an `operator-readiness` `404` permits an exact legacy-overview fallback for
older images. Legacy ready passes immediately. A degraded/no-key result requires
a second exact sample 30 seconds later on the same container ID and Docker
`StartedAt` generation, beyond the prior image's fatal grace plus bounded
retirement budget.
`ledger-capacity` never falls back and requires a nonblocked
ledger. Their paired evidence binds the preserved ledger generation, so
intentional ready, no-key, transient-capacity, and permanent-blocker states all
pass while a database or lifecycle outage does not.

## 0A. Research Log

- Embedded refs: shortlisted `nvidia.md`, `sentry.md`, and `hashicorp.md`. Picked Layer A `taste-skill.md` plus Layer B `nvidia.md` because an owner-only gateway console needs operational restraint, technical density, fast scanning, and sharp industrial geometry. `sentry.md` was rejected for its purple product identity and dashboard-card bias. `hashicorp.md` was rejected for its lighter enterprise-marketing posture. The selected references are source material, not a cloning target.
- Lazyweb: 4 desktop queries, 12 screens viewed. Queries were `AI infrastructure API key management dashboard`, `load balancer operations dashboard health status`, `developer API gateway admin dashboard`, and `GPU cloud infrastructure management dashboard`. The harvested grammar is a narrow navigation rail, state and freshness first, one nearby action band, compact summary values, stable key and event columns, explicit empty/loading/error geometry, filters before evidence, and a deliberate one-time credential dismissal flow. The screen-by-screen hashes and observations are in `.omo/evidence/task-1-nvidia-build-lb/design/research-manifest.md`.
- Skipped Imagen drafts - the task-specific imagegen exclusion applies because this code-native operational surface is built and judged directly in semantic HTML/CSS; no bitmap visual is requested.

The reference conflict rule is explicit: the project plan wins. This project uses one dark graphite theme, off-black and off-white instead of true black and pure white, system fonts only, no brand assets, NVIDIA green as a small signal only, and no public marketing hero. It rejects copied logos, proprietary type, product imagery, alternating light sections, blue or teal brand hover shifts, oversized charts, and exact reference layouts.

## 1. Atmosphere & Identity

This is an owner-only technical operations console, read as a quiet instrument panel under incident pressure. It feels dense, exact, and trustworthy without looking theatrical. The signature is the diagnostic signal edge: a restrained 2px line and short status label appear only where current state or an available action needs attention. Most hierarchy comes from graphite tonal steps, alignment, and type weight. Green is never atmosphere.

- Design variance: **3**. Stable alignment and predictable scanning outrank visual novelty.
- Motion intensity: **2**. The interface is effectively static except for immediate input and dialog feedback.
- Visual density: **7**. Current health, key eligibility, counters, and recent events remain visible without large decorative containers.
- Theme: one dark graphite theme for every route and section. There is no light-mode branch and no section-level theme inversion.
- Geometry: 2px geometry on controls and bounded surfaces, with square internal table geometry.
- Spacing: 4px spacing base with compact, repeatable increments.
- Stack: server-rendered semantic HTML, tokenized CSS, and small vanilla JavaScript. No React, external browser runtime, external script, tracker, or web-font request.

### Identity and non-affiliation

The UI uses the text product name `NVIDIA Build LB` only as a factual upstream descriptor. It is not rendered as a logo or imitative wordmark. Every login, dashboard, and showcase view includes this visible non-affiliation notice in the document flow:

> Independent operations tool; not affiliated with or endorsed by NVIDIA.

The notice is body-size text, remains visible at 200% zoom, and is not hidden in a tooltip, dialog, or hover state. No NVIDIA logo, wordmark, NVIDIA-EMEA font, trademark imagery, font asset, proprietary icon, external script, or external font may be copied or loaded. No exact marketing copy, public marketing hero, product render, green-black brand clone, or reference screenshot is permitted. Only the abstract industrial grammar is adapted.

### Content voice

- Use direct operator language: `2 keys eligible`, `Key cooling down`, `Last successful request 18s ago`.
- Lead with the state, then the cause, then the smallest safe action.
- Use sentence case. Avoid tiny all-uppercase eyebrows, slogans, metaphors, and promotional claims.
- Keep internal IDs in L2 evidence except for destructive confirmation, where the stable
  short-fingerprint handle plus short internal ID is the explicit high-consequence exception.
  Never name a target with plaintext credential material.
- Error text is calm and actionable: `Probe failed. The key remains disabled. Try again after checking upstream access.`
- Unknown values read `Not available`. Empty collections read `No upstream keys registered`. Do not show fabricated zeroes.
- Do not invent quota, rate, latency, or success data. Every number shown comes from the API and includes its time context.
- Korean and English use the same plain register. Translation must preserve operational meaning rather than mirror English word order.

## 2. Color

### Palette

The palette is a single cool graphite ramp. All future CSS colors must reference these semantic tokens. Raw color values may appear only in the token declaration block.

| Role | Token | Value | Usage |
| --- | --- | --- | --- |
| Canvas | `--surface-canvas` | `#0F1214` | Page background |
| Navigation rail | `--surface-rail` | `#12171A` | Primary navigation and login shell |
| Panel | `--surface-panel` | `#171C20` | Section bands, tables, form groups |
| Raised | `--surface-raised` | `#1D2328` | Dialogs, menus, selected work areas |
| Hover | `--surface-hover` | `#242B30` | Interactive hover surface only |
| Pressed | `--surface-pressed` | `#2A3238` | Active control surface |
| Scrim | `--surface-scrim` | `rgb(6 8 9 / 78%)` | Modal backdrop |
| Text primary | `--text-primary` | `#F2F4EF` | Headings, values, control labels |
| Text secondary | `--text-secondary` | `#C1C7C0` | Body text and table values |
| Text muted | `--text-muted` | `#929B93` | Metadata and hints |
| Text disabled | `--text-disabled` | `#919B92` | Disabled labels, never required instructions |
| Border subtle | `--border-subtle` | `#707D85` | Section and row separators; lowest compliant boundary |
| Border default | `--border-default` | `#78858D` | Inputs, buttons, table boundaries |
| Border strong | `--border-strong` | `#98A5AD` | High-importance neutral boundaries |
| Signal primary | `--signal-primary` | `#8CCB2A` | Ready state, action outline, selected indicator |
| Signal hover | `--signal-hover` | `#A2DF43` | Hovered signal outline or text |
| Signal active | `--signal-active` | `#78B51F` | Pressed signal outline or text |
| Focus | `--focus-ring` | `#C2F58A` | Keyboard focus ring |
| Status healthy | `--status-healthy` | `#8CCB2A` | Healthy text and 2px marker |
| Status info | `--status-info` | `#7DB8F7` | Informational text and marker |
| Status warning | `--status-warning` | `#F0BD5A` | Degraded, cooling, or stale text and marker |
| Status error | `--status-error` | `#FF7A78` | Error and destructive text or outline |
| Status revoked | `--status-revoked` | `#C7A7D9` | Revoked credential text and marker |
| Skeleton base | `--skeleton-base` | `#22292E` | Static loading placeholder |
| Skeleton edge | `--skeleton-edge` | `#323B42` | Static loading placeholder boundary |

### Color rules

- NVIDIA green is signal-only. It may occupy a 2px edge, short text label, focus-adjacent action outline, or small status marker. It never fills a page, section, panel, chart, or large button.
- Healthy and primary action may share green because both are explicit positive signals. Their labels and structure distinguish meaning.
- Warning, error, information, revoked, disabled, and stale states use their own token plus text. Color never carries status alone.
- Primary button backgrounds stay graphite. Hover and active shift the graphite surface while the signal outline changes token.
- Destructive actions use the error token only on the label, outline, and compact state marker. Confirmation dialogs remain graphite.
- Disabled text is never the sole carrier of required information and must remain distinguishable from enabled controls by label plus disabled semantics.
- Focus is visible independently of hover and is never represented only by a color change inside an existing border.
- Every meaningful input, button, table, and control boundary uses a border token and measures at least 3.0:1 against every actual adjacent canvas, rail, panel, raised, hover, pressed, or composited-scrim surface. The subtle token is the minimum permitted structural boundary; a lower-contrast tonal shift cannot replace it.
- Primary, secondary, muted, disabled, signal, and status text meets 4.5:1 against every surface on which it can appear. The focus token meets 3.0:1 against every adjacent surface independently of the control border.
- There are no gradients, glows, glass effects, pure black, pure white, large accent washes, or decorative status dots.
- Contrast must be measured in the built browser surface. The floor is 4.5:1 for normal text, 3:1 for large text and graphical control boundaries, and 3:1 between focus indication and adjacent colors.

## 3. Typography

### Font stacks

- Primary: system sans, implemented as `system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", "Noto Sans CJK KR", "Apple SD Gothic Neo", "Malgun Gothic", sans-serif`.
- Mono: system mono, implemented as `ui-monospace, "SFMono-Regular", Consolas, "Liberation Mono", "Noto Sans Mono CJK KR", monospace`.
- Serif: none.

These stacks use fonts already available on the device. The app does not download, bundle, or request font files. System fallback is part of the contract, so layout must tolerate metric differences.

### Scale

| Level | Size | Weight | Line height | Tracking | Usage |
| --- | --- | --- | --- | --- | --- |
| Page title | 28px | 700 | 1.25 | -0.01em | Login or dashboard title |
| H1 | 24px | 700 | 1.3 | -0.01em | Route heading |
| H2 | 20px | 650 | 1.35 | 0 | Primary section heading |
| H3 | 16px | 650 | 1.4 | 0 | Subsection and dialog heading |
| Body | 15px | 400 | 1.55 | 0 | Default prose and values |
| Body strong | 15px | 650 | 1.5 | 0 | Important state or label |
| Body small | 14px | 400 | 1.5 | 0 | Secondary information |
| Label | 14px | 600 | 1.4 | 0 | Form and control labels |
| Metadata | 13px | 500 | 1.45 | 0 | Noncritical timestamp or helper text |
| Mono value | 14px | 500 | 1.45 | 0 | IDs, fingerprints, counts, timestamps |

### Typography rules

- Functional instructions and controls are at least 14px. The 13px metadata level is never used for an error, required instruction, or sole status label.
- Use no more than 700 weight and no more than four weights in one view.
- Use tabular numerals for counters, times, status codes, and latency.
- Mono is reserved for machine values. Body paragraphs and buttons remain sans.
- Headings remain sentence case and wrap naturally. No forced line breaks, decorative italics, or uppercase tracking.
- Korean prose uses `word-break: keep-all` with `overflow-wrap: anywhere` as the emergency fallback. Do not add letter spacing to CJK.
- Internal IDs, fingerprints, and long request IDs may use `overflow-wrap: anywhere`. `word-break: break-all` is limited to machine identifiers.
- Controls and rows have content-driven height. No fixed text container may clip fallback fonts or CJK glyphs.
- At 200% zoom, type follows browser scaling without a CSS transform, fixed viewport font size, or maximum text-size lock.

## 4. Spacing & Layout

### Base unit and tokens

The 4px spacing base is mandatory. Every inset, gap, and section interval maps to this scale.

| Token | Value | Usage |
| --- | --- | --- |
| `--space-1` | 4px | Marker to label, tight inline gap |
| `--space-2` | 8px | Control internals, dense row gap |
| `--space-3` | 12px | Input inset, related controls |
| `--space-4` | 16px | Mobile page edge, standard group |
| `--space-5` | 20px | Compact section inset |
| `--space-6` | 24px | Desktop section inset |
| `--space-8` | 32px | Major group separation |
| `--space-10` | 40px | Route-section separation |
| `--space-12` | 48px | Login-shell breathing room |
| `--space-16` | 64px | Maximum vertical separation |

Geometry tokens are `--radius-control: 2px`, `--border-hairline: 1px`, and `--border-signal: 2px`. There are no pill controls, mixed corner systems, or arbitrary spacing values.

### Shell and grid

- The document uses `min-block-size: 100dvh`, never a fixed viewport height.
- The DOM order is skip link, product identity and non-affiliation, navigation, main heading, current status, actions, credential sections, events, and footer.
- Maximum content width is 1440px. The shell may use the full viewport below that width.
- At an unzoomed 1280x900 outer window, both `/admin` and `/showcase` use a 224px navigation rail plus a fluid 12-column main grid. Main gutters are 24px and outer inline inset is 32px.
- Tablet uses an 8-column main grid with 24px outer inset. The rail becomes a compact top navigation disclosure so the content keeps useful width.
- Mobile uses a 4-column grid with 16px outer inset and a single visual column.
- The `/admin` health and freshness band spans the main width and contains exactly the five locked Overview values below in one divided band. `/showcase` has no operational Overview cells.
- Each major resource section is a heading, optional short description, action toolbar, and table or state surface. No generic cards are used to group ordinary data.
- Tables retain their headings and state surface while loading, empty, or failed so page geometry remains stable.
- Z-index levels are fixed: base 0, sticky navigation 10, scrim 20, dialog 30, transient live message 40. No other values are allowed without updating this contract.

### Responsive behavior

#### 1280px and wider

- Show the 224px left rail and fluid 12-column main grid with 24px gutters and 32px outer inline inset on both `/admin` and `/showcase`.
- Keep product identity, the visible non-affiliation notice, and route navigation in the rail. Keep the narrow-layout top disclosure hidden.
- Put exactly five Admin Overview cells in one horizontal band, in this order: `Gateway readiness`, `Eligible keys`, `Cooling keys`, `Logical requests`, `Last event/freshness`. Render `generated_at` as heading metadata, never as a sixth cell; keep active downstream counts in the downstream-token section.
- Give `/showcase` zero operational Overview cells. Its rail has exactly `Buttons`, `Inputs`, `Statuses`, `Tables`, `Dialogs`, `System states` in that order. Every named section spans the main grid; its internal `.specimen-grid`, `.status-grid`, and `.state-sections` remain exactly 3, 4, and 3 columns respectively.
- Keep key and event tables in table form. Actions occupy the final column and never wrap into machine-value columns.
- Keep `#recommended-action` as the sole primary action for the authenticated page.
  Section Add/Issue, manual Refresh, and row controls remain secondary or quiet;
  destructive actions remain row-local.
- Static DOM/CSS tests assert the exact structure, labels, grid declarations, and order; computed-browser tests assert the rendered rail width, column count, gutter, inset, disclosure visibility, section spans, and Overview/showcase cell counts.

#### 768px to 1279px

- Replace the rail with a top navigation disclosure and use the 8-column grid.
- Summary values wrap into two columns inside one divided band.
- Keep tables when columns fit. Less important columns move below the primary value within the same cell before horizontal scrolling is considered.
- Dialog width is min(560px, available inline size) with 24px page clearance.

#### 375px to 767px

- Use a single column and 16px page inset.
- Status, primary action, upstream keys, downstream tokens, and events remain in that order.
- Semantic tables become labeled row groups at the CSS layer. The table header remains available to assistive technology, and each cell exposes its column label without duplicating the whole table.
- Row actions follow the row facts and use a full-width action group when needed.
- Dialogs become edge-inset sheets with 16px clearance, not full-viewport traps.
- Long IDs wrap inside their row. The page itself never scrolls horizontally.

#### 200% zoom

- Native browser zoom turns the fixed 1280x900 outer window into an effective 640px CSS layout. Both `/admin` and `/showcase` hide the desktop rail, expose the mobile top disclosure, and use narrow single-column reflow. Reflow, not CSS/device scaling, is the response.
- There is no two-dimensional page scrolling. A table may use a clearly labeled local overflow region only when labeled-row reflow would destroy necessary comparison, and that exception must be proven keyboard usable.
- Navigation, dialogs, and live messages never cover the focused control.

### Density rules

- Minimum interactive target is 44px by 44px, even when the visible border is smaller.
- Data-row block size starts at 48px and grows with wrapping.
- Compactness comes from shared bands, alignment, and fewer containers, not from smaller text or targets.
- Decorative whitespace does not separate routine rows. Hairlines or a tonal shift do.
- Charts are absent by default. A later chart requires a named operational question, real time context, text equivalent, and design-contract update.

## 5. Components

### Global interaction state contract

| State | Required treatment |
| --- | --- |
| **Default** | Neutral graphite surface, legible label, stable border, and no decorative emphasis |
| **Hover** | Pointer-only surface shift and relevant border or text token; no hidden information appears |
| **Focus** | 2px external focus ring with 2px offset, visible without hover and never clipped |
| **Active** | Immediate pressed surface plus at most 1px vertical transform; action is not declared complete early |
| **Disabled** | Native disabled semantics, disabled label plus visible reason nearby, no pointer or keyboard activation |
| Loading | Preserve dimensions, set `aria-busy="true"`, and announce one concise progress message |
| Empty | Preserve headings and columns, state why it is empty, and offer one relevant action if authorized |
| Error | Keep prior safe data when possible, show a located message plus recovery action, and never expose secret material |

### Button

- **Structure**: native `button` with explicit `type`, text label, and optional nonessential CSS marker. Links are not styled as buttons unless they navigate.
- **Variants**: primary signal-outline, secondary neutral, quiet, and destructive error-outline.
- **Spacing**: 44px minimum block size, 12px inline inset for compact controls, 16px for primary actions, and 8px label gap.
- **States**: Default uses a graphite surface and role border. Hover uses `--surface-hover`. Focus uses `--focus-ring`. Active uses `--surface-pressed` and `translateY(1px)`. Disabled uses the native attribute and explains the prerequisite. Loading keeps the label width stable, uses `aria-busy`, and changes text to a concrete verb such as `Probing...`.
- **Accessibility**: visible text names the action and target. Icon-only buttons are not used. Destructive labels name the operation, not a vague `Confirm`.
- **Motion**: active transform is 80ms maximum and disappears under reduced motion. Color changes are immediate.

### Input

- **Structure**: `label` above `input`, `select`, or `textarea`; optional helper below; located error below that. Placeholder text never replaces a label.
- **Variants**: plain text, password, select, checkbox scope choice, and read-only machine value.
- **Spacing**: 44px minimum block size, 12px inline inset, 8px label gap, and 4px helper gap.
- **States**: Default uses the default border. Hover raises only the border. Focus uses the external ring. Active checkbox/select state uses the pressed surface and explicit checked/selected semantics. Disabled keeps its label and reason. Invalid uses `aria-invalid="true"` and an associated error message. Read-only is visibly distinct from disabled and remains selectable.
- **Accessibility**: use `autocomplete="off"` and `spellcheck="false"` for bearer and key fields. Scope controls use a `fieldset` and `legend`. Errors connect through `aria-describedby` and the summary links to the field.
- **Secret behavior**: admin bearer and upstream key values use a password field while entered. After successful submission, the value is cleared and the node is removed or reset before dashboard rendering. No reveal toggle exists.
- **Motion**: none.

### Status

- **Structure**: short sentence-case state word, optional short cause, and freshness.
  Visible copy uses `Ready`, `Reduced capacity`, `Cooling`, `Stale`, or `Revoked` rather
  than raw enums or all-uppercase implementation markers.
- **Variants**: healthy, degraded, cooldown, disabled, failed, stale, no data, and revoked.
- **Spacing**: 4px marker gap, 8px state-to-detail gap, and 12px compact inset when bounded.
- **States**: a noninteractive status has no Hover, Focus, or Active styling. A status filter is a real button and inherits the Button state contract. Disabled and revoked remain distinct text states.
- **Accessibility**: color is supplementary. Dynamic state changes use a polite live region except failures that block the current action, which use an assertive error summary.
- **Motion**: no pulsing, blinking, or automatic animation.

### Table

- **Structure**: `table` with `caption`, `thead`, scoped `th` cells, `tbody`, and one action cell per row. Empty, loading, and error states occupy a full-width row without removing headings.
- **Variants**: upstream keys, downstream tokens, recent events, and compact summary comparison.
- **Spacing**: 12px vertical and 16px horizontal cell inset on desktop; 12px all around in mobile row groups.
- **States**: Default rows use a single bottom separator. Hover appears only for rows with an actionable control and does not imply row clickability. Focus is on the actual row control. Active selection uses `aria-current` or checked semantics, not background alone. Disabled row actions stay visible with a reason. Loading, Empty, and Error preserve the table contract.
- **Accessibility**: no `role="grid"` and no custom arrow-key model. Native table navigation remains intact. Sorting buttons announce direction. Filters have labels and a clear action.
- **Responsive**: below 768px, CSS exposes each heading beside its value while preserving one accessible table. A localized scroll fallback requires a focusable wrapper with an accessible name.
- **Motion**: none. Sorting and filtering replace rows without animated reordering.

### Dialog

- **Structure**: native `dialog` opened with `showModal()`, a heading referenced by `aria-labelledby`, description referenced by `aria-describedby`, body, and explicit cancel plus action footer.
- **Variants**: add upstream key, issue downstream token, confirm disable, confirm delete, confirm revoke, and one-time credential.
- **Spacing**: 24px inset on desktop, 20px on mobile, 16px between sections, and 8px between action buttons.
- **States**: Default open state uses the raised surface and scrim. Hover, Focus, Active, and Disabled behavior comes from contained controls. Loading disables repeat submission but keeps cancel available unless cancellation is unsafe. Error stays inside the dialog and focuses the error summary. Closing restores focus deterministically.
- **Accessibility**: initial focus lands on the heading for an informational one-time credential or the first invalid/required field for a form. Tab remains inside the modal. Escape cancels before submission; after a mutation is sent, Cancel and Escape wait for the authoritative response because discarding it would create an ambiguous result. Destructive confirmation names the stable handle and short internal ID.
- **Motion**: optional 120ms opacity plus 4px transform on entry, 80ms opacity on exit. Reduced motion makes both immediate.

### Loading

- **Structure**: stable destination geometry, static neutral bars or text, and one visually hidden status message.
- **Variants**: initial dashboard, table refresh, row action, and dialog submission.
- **States**: Loading never replaces the entire shell. A delayed request becomes stale before it becomes error. Retry preserves safe prior data.
- **Accessibility**: use `aria-busy` on the smallest affected region. Announce start once and outcome once. Do not repeatedly update a live region.
- **Motion**: no shimmer, spinner, pulse, or indeterminate animation.

### Empty

- **Structure**: heading or table state label, one sentence explaining why no data exists, and at most one relevant action.
- **Variants**: no upstream keys, no downstream tokens, no recent events, and no filtered results.
- **States**: Empty is not error. If the operator lacks permission or the service is unavailable, use Error instead.
- **Accessibility**: plain text, no decorative illustration, and the action follows the explanation in reading order.
- **Motion**: none.

### Error

- **Structure**: form-field error, dialog error summary, or section-level error band with stable error code, safe message, request ID, and retry or navigation action.
- **Variants**: authentication, validation, conflict, unavailable database, upstream failure, offline, and stale-data warning.
- **States**: Error persists until resolved or dismissed deliberately. Hover, Focus, Active, and Disabled behavior belongs to its recovery controls. Retained prior data is labeled stale rather than presented as current.
- **Accessibility**: blocking errors receive focus at the summary. Field errors link back to fields. Status code, color, and wording are all present. No toast is the sole record of a failure.
- **Secret safety**: messages never contain a bearer, key, token, ciphertext, digest, file path, raw request body, or response body.
- **Motion**: none.

### Summary band

- **Structure**: `/admin` has one section with a heading and a definition list containing exactly five cells, in order: `Gateway readiness`, `Eligible keys`, `Cooling keys`, `Logical requests`, and `Last event/freshness`. `generated_at` is metadata beside the heading rather than a summary cell. Active downstream-token counts belong only in the downstream-token section. `/showcase` has no operational summary band or Overview cell.
- **Surface**: shared panel with internal separators. It is not a row of generic cards.
- **Behavior**: each value links to its controlling section only when navigation is useful. A missing value reads `Not available` rather than zero.

### Navigation

- **Structure**: product text identity, non-affiliation notice, route links, and logout action in `nav`.
- **Admin routes**: Overview, Upstream keys, Downstream tokens, and Events. Labels match the visible section headings. `/showcase` remains a direct QA route and never competes with operator navigation.
- **Showcase routes**: exactly Buttons, Inputs, Statuses, Tables, Dialogs, and System states, in that order.
- **Behavior**: each rail link targets a section within the current document, so the selected section uses `aria-current="location"` and a 2px signal edge. Initial markup and runtime navigation keep exactly one current location. Tablet and mobile use a native disclosure pattern, not a custom animated drawer.

### Administration data projection

- The UI parses only the exact `extra="forbid"` admin DTOs. It does not invent cursor, server query filters, health state, probe state, scope, routing state, or event fields. Local progressive disclosure may prioritize already validated items without changing their values or server order.
- Upstream rows lead with the stable operator handle `Key <first eight fingerprint hex>`, exact `disabled|eligible|cooldown|quarantined` routing state, and the next safe action. Full canonical fingerprint, internal ID, counters, last safe status, and exact timestamps remain accessible evidence details. Actions and equality retain `sha256:` plus all 64 hex characters.
- Downstream rows expose ID, exact unchanged label, canonical scope order, revoke state, counters, and timestamps. The one-time `nblb_ds_` token is accepted only by the issuance dialog and is impossible in list state.
- Overview projects only the five locked cells: gateway readiness, eligible-key count, cooling-key count, routed logical-request count, and last-event/freshness. Per-key attempt counters remain L2 evidence and are never summed to infer routed requests. Generation time remains heading metadata and active downstream counts remain in the downstream-token section. Empty storage is visibly degraded/not ready rather than fabricated healthy zero data.
- Events summarize the newest validated failures, cancellations, and operational changes first. One explicit audit disclosure preserves at most the newest 100 in server order and only ID, request ID, event type, internal resource IDs, outcome/status class, latency, and occurrence time. There are no message, label, body, header, digest, ciphertext, nonce, or path columns.
- Safe validation errors always show only `invalid_request`, `request validation failed`, and an opaque request ID. Rejected form content and framework validation detail never enter the DOM.

### Admin login journey

1. `GET /admin` serves the shell, visible non-affiliation notice, short custody explanation, and labeled admin bearer password field. No operational data or secret is present.
2. Submit copies the field value into one per-tab JavaScript module variable only, clears the field node, and sends an Authorization header to the canonical dashboard endpoint. It never writes URL, query, cookie, DOM after submit, localStorage, sessionStorage, log, or response cache.
3. On 401, clear the module variable, restore the password field, show a linked inline error, and focus the error summary. Do not say whether a token prefix or shape was close.
4. If the first authenticated dashboard load returns 503, keep the safe error visible and do not script-focus Retry. Natural Tab order must reach the visible Retry control; Enter retries, and a successful response focuses the dashboard-title H1.
5. On immediate success, remove the login form from the rendered DOM, render the dashboard, and focus the dashboard H1 without scrolling it under navigation.
6. Reload starts unauthenticated because the module variable is gone. Logout aborts pending admin requests, clears the variable and any ephemeral credential state, returns to login, and focuses the bearer field.

### Dashboard journey

1. Read the five-cell gateway snapshot and the decision brief that states freshness, impact, and the one recommended next action.
2. Follow add -> probe -> enable -> issue in server-confirmed order. The recommended step is the sole strongest action; section and row controls remain visible and never hover-only.
3. Add an upstream key in a password field. It is created disabled. Cancel or Escape
   before submission erases the field. Once sent, Cancel and Escape are disabled until
   the response establishes a result; logout/reload clear credential memory and require
   reauthentication plus a full refresh. A lost response is `unknown`, never assumed
   failed. The success state shows only the stable handle, state, and disclosed evidence.
4. Probe, enable, disable, or delete through row-local actions. Enable is blocked until a current successful probe leaves the key healthy with no quarantine or active cooldown. Delete is blocked for an enabled key. Confirmation names only the stable short-fingerprint handle and short internal ID.
5. Issue a one-time downstream token only after at least one upstream key is eligible.
   The token appears once in the one-time credential dialog, is never recoverable, and
   is never included in screenshot, HAR, console, or response-body logging. If the issue
   response is lost but a fresh list finds the new label, revoke that credential as the
   sole safe action before issuing a replacement.
6. Copy is an explicit user gesture. Dismissal removes token text from the DOM, clears the module reference, resets the copy control, and performs clipboard cleanup in the same user gesture when copy occurred. If clipboard cleanup is unavailable or fails, the UI says so without echoing the token and the visual capture gate remains blocked until an external secret-safe clipboard check confirms absence.
7. Revoke a token by internal ID. Revoked remains a visible text state and cannot be used again.
8. Read the latest attention/change summary last; disclose raw recent events only for audit. Event records retain request ID, internal key ID, safe outcome class, and exact timestamp.

### Authenticated browser capture contract

- Authenticated tabs keep trace, HAR, and raw network capture disabled from admin-bearer submission until logout or reload proves the per-tab module bearer cleared. No authenticated-tab exception may enable one of those capture modes temporarily.
- During that interval, collectors may project only allowlisted `method/path/status` network fields or `type/source/level` diagnostic fields. They never read or store query text, request/response headers, bodies, timing payloads, or console arguments.
- Video recording is disabled for the entire ordinary and native browser-QA phases. Screenshot capture is disabled before any admin bearer or upstream key enters a form control, and while a one-time downstream token is present. Screenshot capture may resume only after the applicable success, Cancel, Escape, dismissal, logout, or reload path has observably erased the secret from the live field, serialized DOM and attributes, every ephemeral JavaScript reference, and the clipboard when copy occurred. Post-capture deletion or redaction is never evidence. Trace, HAR, and raw capture remain disabled because the module bearer remains live.
- These controls prevent capture rather than sanitize it later. Capturing first and deleting, redacting, or clearing a buffer afterward is never evidence of non-capture.
- Exactly three intentional user-agent network diagnostics are expected: one wrong-admin 401, one initial-Overview 503, and one offline-refresh failure. Each is separately identified by type/source/level and is not an application error. Application-console errors, runtime exceptions, page errors, and CSP/JavaScript errors are independently zero.
- A browser evidence target accepts only a fresh absent task-owned directory leaf under its own gate root. It rejects a symlink, a nonempty/reused leaf, another gate's evidence, or an arbitrary caller path, and it never recursively deletes a caller-provided directory. Required capture names, order, and count come from the gate-owned immutable manifest fixed before launch; every indexed file is reopened and rehashed immediately before the receipt, and missing, extra, renamed, byte-count-drifted, or digest-drifted files fail.
- Todo 6B computes the Git candidate manifest before any browser work, materializes exactly those entries into a separate read-only QA source snapshot, and runs Compose rendering, PostgreSQL build context, Python gate imports, and the isolated Lighthouse npm install from that snapshot. Candidate, snapshot-destination, and evidence traversal uses private directory descriptors plus no-follow/exclusive opens; regular snapshot leaves are written with `O_EXCL|O_NOFOLLOW`, modes are applied with `fchmod`, and a symlinked root, ancestor, index, review, or PNG fails instead of escaping its closed tree. The snapshot verifies the exact path set plus every path type, mode, and payload before removing regular-file write bits. No browser work reads live source after snapshot creation; a temporary live-source edit therefore cannot influence the run, while the snapshot's manifest implementation still rehashes the original source at gate completion and requires a byte-exact manifest match.
- Each independent visual-review receipt contains the exact `run-a` and `run-b` capture-index SHA-256 mapping, process-baseline SHA-256, and successful fresh-cleanup SHA-256. Fresh cleanup must itself be a strict `PASS` with trigger/final status 75 and every container/network/volume/listener/temp/image/process observation exactly zero; a failed fresh cleanup can never be upgraded by a later resume. The final verifier reopens every indexed PNG on every fresh or resumed invocation, requires the exact regular-file set below that run's `captures/` directory, and recomputes byte count and SHA-256 before accepting a review. A receipt from an earlier index, baseline, cleanup, or changed PNG cannot be reused.

### Browser runtime and packaged resources

- Automation is exact Python `playwright==1.61.0` using Playwright-managed Chromium revision 1228 from the absolute `PLAYWRIGHT_BROWSERS_PATH` when configured, otherwise the current operating user's standard `~/.cache/ms-playwright`. Relative overrides are rejected. It never uses an alternate browser index, channel, executable override, system fallback, or install/download during normal tests. A missing revision-1228 managed cache is a blocking failure.
- Each browser-QA invocation runs exactly two serialized, nonoverlapping phases in this fixed order: ordinary first, then native. The ordinary phase launches exactly one shared Playwright-managed browser process. Every viewport or task-flow scenario receives exactly one fresh nonpersistent `BrowserContext` and exactly one `Page`; maximum live ordinary contexts and pages are each one, and nested `finally` closes both before the next scenario. One pytest test may sequence several scenarios because context/page ownership is scenario-scoped rather than test-scoped.
- Native 200% is the sole extra browser-process phase. It may start only after the ordinary `Page`, `BrowserContext`, shared browser process, and Playwright driver have each been closed by nested-`finally` cleanup and independently observed zero. Native-first execution is forbidden. Native starts exactly one task-owned full `chromium-1228` via `launch_persistent_context`, with `headless=False`, sole browser argument `--headless=new`, and fixed 1280x900 outer window. It creates one fresh mode-0700 user-data directory containing only `Default/Preferences`, whose only preference is `partition.per_host_zoom_levels.x.127.0.0.1.zoom_level=3.8017840169239308`. The launch-created context owns exactly one launch-created page; shared-browser use, `new_context`, `new_page`, and popup creation are forbidden.
- Native never accepts `chromium_headless_shell-1228`, a system browser, channel/executable-path override, device-scale factor, post-launch viewport/window resize, CSS `zoom` or transform, keyboard zoom, or a mutating CDP emulation/page-scale command. Read-only CDP `Page.getLayoutMetrics` is allowed. Every action/navigation timeout remains bounded; sleep, retry, trace, HAR, raw network capture, and video are absent, and screenshot blackout follows the custody contract.
- Todo 4's native phase visits `/showcase` and completes the same deterministic fake-owner journey on that one page. Todo 6B repeats the complete low-vision owner journey on one native page against the exact Todo 6A candidate digest. A viewport set to 640px, device emulation, or equivalent scaling is never native evidence.
- Native PASS requires observed outer 1280x900, layout zoom 2, `innerWidth == 640`, `devicePixelRatio == 2`, `visualViewport.scale == 1`, and `(max-width: 767px)` true. At every stable state, `documentElement.scrollWidth <= documentElement.clientWidth`; after a maximum horizontal scroll attempt, `scrollX == 0`. The local vendored axe payload runs in the actual page, reports serious/critical zero, and causes zero axe network requests.
- Natural keyboard traversal visits every enabled control. For each focus stop, the union of computed border and outline rectangles is inside the visual viewport, is not intersected by a fixed/sticky nonancestor, and has clipped, hidden, and covered counts zero. Every stable state has equal before/after serialized-DOM SHA-256 around read-only observation. Secret-free full-page and focused-control screenshots are both indexed with `native_zoom:true`, observed byte count and SHA-256, and the manifest-defined capture ID.
- A native receipt is created only after nested-`finally` cleanup independently observes task-owned browser descendants, Playwright drivers, contexts, pages, persistent profiles, temporary paths, fake-server threads, port listeners, clipboard content, and capture-blackout state all zero. It records contexts/pages started exactly one, maximum live each one, `shared_browser_used:false`, the preference value and `Default/Preferences` file SHA-256 without a temporary path, executable/revision provenance, native metrics, axe/focus/overflow results, and capture IDs. One close failure is aggregated but cannot skip later cleanup. Cleanup values are observations, never hard-coded receipt literals or worker claims.
- The shell gate persists its initial browser, Playwright-driver, and Lighthouse `(PID, /proc start-time)` identities, and the review request plus both independent reviews bind the baseline receipt SHA-256. Cleanup and visual-review resume both re-observe task-labelled containers, networks, volumes, the loopback listener, task-labelled temporary PostgreSQL images, shell secret/client paths, browser/font/profile/Lighthouse temporary directories, and process identities absent from that baseline. Observer errors are distinct from an empty result and fail closed; resume never replaces observations with literal zeros. Fresh and resume cleanup receipts remain separately preserved while `cleanup.json` mirrors the latest observation. Each phase receipt is written through a noclobber temporary FD; the mirror must succeed first, and only then is the phase receipt renamed as the authoritative completion marker, so a post-cleanup write failure cannot later be promoted by resume.
- Two fresh task-owned runs must independently reproduce the required capture name/order/count, canonical stable-state DOM hashes, native metrics, preference hash, and zero cleanup observations before determinism is accepted. The DOM hash canonicalization may replace only runtime UUIDs, ISO timestamps, SHA fingerprints, long opaque hexadecimal IDs, and observed latency values; element structure, attributes, state labels, and all other text remain hash-significant. Each state also requires byte-exact raw DOM before/after its stability probes. Receipts are compared by an observer after both runs; a receipt's own assertion is insufficient.
- HTML, CSS, and JavaScript are only closed `importlib.resources` enum mappings included in the wheel. Browser resources are exactly `/admin`, `/showcase`, `/assets/admin.css`, `/assets/admin.js`, `/assets/showcase.css`, and `/assets/showcase.js`; browser routes cannot select another filename or filesystem path. The Showcase module owns only responsive disclosure state, current-section semantics, and deterministic section-heading focus; it makes no network request and reads no secret. Wheel-install QA runs outside repository CWD so a source-tree fallback cannot masquerade as packaged delivery.

### Deterministic focus return

- Closing an add or issue dialog returns focus to its invoker.
- Closing a row-action dialog returns focus to the same row action.
- If deletion removed that row, focus moves to the next row's first action, then the previous row, then the section heading when the table is empty.
- A successful login focuses the dashboard H1. Logout focuses the bearer field.
- A blocking error focuses its summary. Fixing the error returns focus to the triggering control.
- Refresh never steals focus. Replaced content preserves the focused control when it still exists.

## 6. Motion & Interaction

### Timing

| Type | Duration | Easing | Use |
| --- | --- | --- | --- |
| Press feedback | 80ms | ease-out | Button 1px transform |
| Dialog entry | 120ms | ease-out | Opacity and 4px transform |
| Dialog exit | 80ms | ease-in | Opacity only |
| State replacement | 0ms | none | Status, rows, errors, loading, and navigation |

### Motion rules

- Motion intensity remains 2. There is no automatic entrance sequence, scroll animation, parallax, marquee, chart animation, pulsing status, or decorative loop.
- Animate only transform and opacity. Do not animate layout, size, position, border width, or color.
- `prefers-reduced-motion: reduce` makes every transition immediate and removes the active transform. QA must observe `matchMedia('(prefers-reduced-motion: reduce)').matches === true`, zero computed transition duration and delay on every affected element, and computed active transform `none`; a stylesheet rule or self-reported flag is not proof.
- No interaction requires motion to communicate its result. Text, shape, and focus make every change explicit.
- No scroll listener, animation library, or requestAnimationFrame loop is permitted.
- Loading is static and announced semantically.

### Keyboard contract

- A visible skip link is the first focusable element.
- Tab order follows DOM and visual order. Positive `tabindex` is forbidden.
- Enter submits a valid single form. Space and Enter activate native buttons.
- Native checkboxes, selects, links, and `dialog` behavior are preserved.
- Escape closes a nonbusy dialog and returns focus. A busy mutation exposes an explicit safe cancel rule instead of trapping the operator.
- Initial-503 recovery follows natural order: Tab reaches the visible Retry control, Enter activates it, and successful recovery focuses the dashboard-title H1. The test does not call `.focus()` on Retry.
- Table actions are ordinary buttons. There is no spreadsheet keyboard model.
- Focus never enters disabled content, the hidden login form, a closed dialog, or stale replaced content.

### Pointer and touch

- Hover is enhancement only. Every action remains visible and usable by keyboard and touch.
- Hit areas are at least 44px in both dimensions and do not overlap.
- Active feedback begins on press but success language waits for the server result.
- Destructive actions require a dialog and never share the primary-action position.

## 7. Depth & Surface

### Strategy

The chosen strategy is mixed borders and tonal shifts. It uses no shadow. The canvas, rail, panel, and raised tokens create a shallow material ladder; borders explain structure; the 2px signal edge identifies only state or action.

| Level | Treatment | Use |
| --- | --- | --- |
| Canvas | `--surface-canvas` | Page background |
| Rail | `--surface-rail` plus one border | Navigation and login identity area |
| Panel | `--surface-panel` plus hairline separators | Tables, forms, summary band |
| Raised | `--surface-raised` plus default border | Dialog and open menu |
| Interactive | Hover or pressed graphite token | Pointer or press feedback |
| Signal | 2px role-colored edge | Selected route, primary action, status marker |

### Surface rules

- No generic cards. Routine metrics, filters, forms, and tables live in shared bands divided by hairlines or spacing.
- No box shadow, glass, blur, glow, noise, gradient, texture, or simulated lighting.
- All controls use 2px radius. Table internals and separators remain square.
- Green never creates depth. A green edge communicates signal only.
- Dialog depth comes from the scrim, tonal raised surface, and border.
- A section may have one top boundary and internal row separators. Do not box every row on all four sides.
- Empty, loading, and error variants keep the same outer surface and headings as populated content.
- Decorative imagery, brand screenshots, product renders, illustrations, and copied icons are absent.

## 8. Accessibility Constraints & Accepted Debt

### Constraints

- Target: WCAG 2.2 AA. Normal text contrast is at least 4.5:1, large text at least 3:1, meaningful non-text boundaries at least 3:1, and focus indication at least 3:1 against adjacent colors.
- Every task is operable with keyboard only. Focus is visible, ordered, never trapped outside an open modal, and returns deterministically.
- Status never depends on red versus green. Words, compact text markers, state relationships, and color work together.
- Semantic landmarks, headings, lists, forms, definition lists, tables, buttons, and dialogs are used before ARIA. ARIA supplements native semantics only.
- Errors are identified in text, linked to their source, and recoverable without losing safe input.
- Touch targets are at least 44px by 44px.
- `prefers-reduced-motion` is respected as defined in Section 6.
- At native 200% zoom, the full managed Chromium 1280x900 outer window produces the locked effective-640 metrics and content reflows without two-dimensional page scrolling, clipped controls, hidden focus, or covered messages. A 640px viewport or device emulation is only an ordinary narrow-layout check.
- At 375px, 768px, and 1280px widths, reading order and action priority remain the same.
- CJK and Korean labels wrap by semantic group. Glyphs, particles, machine IDs, and button labels are not clipped.
- Auth and credential values use password fields only during entry. Upstream plaintext is never rendered after creation. A one-time downstream token is removable and not recoverable.
- Token-bearing screenshots are forbidden until token dismissal, DOM removal, module clearing, and clipboard cleanup are all proven. If any cleanup proof fails, capture and visual-review progression remain blocked.
- Authenticated trace, HAR, and raw network capture remain disabled until logout or reload proves the in-memory admin bearer cleared. Allowlisted method/path/status telemetry is the only network evidence and has no header/body access. Video recording stays disabled for both complete browser-QA phases; only screenshot capture may resume, and only under the observable cleanup rule in the authenticated browser capture contract.
- No accepted accessibility issue may be hidden by a Lighthouse score, screenshot similarity, or visual-review approval.

### Inclusive persona pass criteria

| Persona | Required pass |
| --- | --- |
| Owner during an incident | Completes login, health diagnosis, key exclusion, and recovery by keyboard with stable rows, concise errors, and no focus loss |
| Low-vision operator at 200 percent zoom | Completes the same journey without horizontal page scrolling, clipped content, covered focus, or unreadable boundaries |
| Red-green color-vision deficiency | Distinguishes healthy, degraded, cooldown, disabled, error, stale, and revoked states without relying on hue |
| Motion-sensitive operator | Encounters no automatic motion and receives the full state model with reduced motion enabled |
| Distracted operator | Finds one primary action for the current page state, sees the target in every destructive confirmation, and encounters no critical hover-only information |

A persona failure is a blocking defect. It is not converted into minor debt without an explicit owner decision, affected-user record, exact remediation, and a new review.

### Adaptive preferences

- Reduced motion removes all transitions without changing layout or information.
- Browser text and page zoom control size; the UI does not override either.
- High contrast and forced-colors modes retain native control outlines, status words, and boundaries. Custom colors must not erase native focus.
- Screen-reader live regions announce only meaningful state changes and never repeat continuously.
- Locale changes do not change information hierarchy. English and Korean labels are tested with long content and mixed identifiers.
- Clipboard access is optional. Failure leaves copy as selectable text inside the one-time dialog and keeps capture blocked until safe cleanup is confirmed.

### Accepted Debt

No accepted design or accessibility debt.

The register is empty. Any future debt requires a located issue, severity, affected persona, exact fix, owner, expiry or exit condition, and explicit acceptance. Critical and major accessibility or persona blockers cannot be accepted as routine debt.

### Primitive Showcase Gate

Before composing the product dashboard, `GET /showcase` must render the exact tokens and primitives in this contract with no secret input:

- Button, Input, Status, Table, Dialog, Loading, Empty, and Error.
- Default, Hover, Focus, Active, Disabled, loading, empty, and error variants.
- Healthy, degraded, cooldown, disabled, stale, failed, no-data, and revoked status labels.
- Upstream-key, downstream-token, and event table anatomy with synthetic non-secret data.
- Visible non-affiliation notice and copyright boundary.
- Ordinary 375px, 768px, and unzoomed 1280px layouts plus the separate native-200% phase, Korean and English wrapping, keyboard order, and reduced motion.
- At unzoomed 1280, the exact 224px rail, fluid 12-column main grid, 24px gutters, 32px inset, hidden disclosure, six ordered rail links, zero operational Overview cells, full-grid sections, and internal 3/4/3 specimen/status/system-state grids.
- At native 200%, `/showcase` and the same fake-owner journey share the one persistent-context page and prove the locked metrics, overflow, axe, keyboard/focus-occlusion, DOM stability, secret blackout, capture, and cleanup receipts.

The showcase must pass manual keyboard traversal, contrast measurement, automated accessibility checks with zero serious or critical findings, and fresh visual review before product screens are composed.

### Visual QA Gate

The later implementation gate runs against the immutable candidate production image, not a source-only or development server:

1. Run the ordinary phase with exactly one shared managed browser and a fresh one-context/one-page scenario at each required 375px, 768px, and unzoomed 1280px state. Close each scenario before the next and fully close the ordinary browser and driver at the phase boundary.
2. In the sole extra process, repeat the complete low-vision owner journey against the exact candidate digest with the full-`chromium-1228`, one-page native persistent-profile contract. Prove the exact 1280x900/zoom-2/640/DPR-2/scale-1/narrow-media tuple; a 640 viewport or emulation is rejected.
3. Exercise login failure and success; natural-keyboard initial-503 recovery; upstream add, Cancel, Escape, probe, enable, disable, delete; downstream issue, copy, dismiss, revoke; offline, stale, empty, loading, reduced-motion, and error recovery.
4. At every stable native state, prove horizontal overflow and maximum-scroll results, local axe serious/critical and network zero, every-enabled-control traversal, unobscured in-viewport focus border+outline, and equal pre/post observation DOM hashes.
5. Inspect serialized DOM/attributes, form state, ephemeral JavaScript state, clipboard, and projected allowlisted telemetry for plaintext absence. Collectors retain only method/path/status or type/source/level, never queries, headers, bodies, or console arguments. Exactly three intentional UA diagnostics remain separate from zero application/runtime/page/CSP errors.
6. Keep trace, HAR, and raw network capture disabled throughout the authenticated tab and video recording disabled throughout both complete phases. Disable screenshot capture before every bearer/key entry and while a one-time token is present; only screenshots may resume after the applicable success/Cancel/Escape/dismiss/logout/reload path observably clears the live field, serialized DOM/attributes, every ephemeral JavaScript reference, and clipboard when used. Post-capture deletion or redaction cannot satisfy blackout.
7. Index each required secret-free full-page and focused-control native capture with `native_zoom:true`, byte count, SHA-256, and capture ID. Reopen and rehash the exact manifest-defined name/order/count immediately before receipt; missing, extra, or drifted files fail.
8. Run automated accessibility and real-browser performance checks on fresh cold profiles. Prove reduced-motion media true, computed transitions zero, and active transform none.
9. Use only fresh task-owned evidence leaves. Nested-finally cleanup must continue after individual close failures and observe every task browser descendant/driver/context/page/profile/temp/fake thread/listener, clipboard, and blackout state zero before receipt. Two fresh runs must reproduce the locked deterministic fields.
10. Submit the same rehashed artifacts to an objective visual pass and a separate design, accessibility, heuristic, and persona review. Both must have no blocker.

No UI is accepted because this document says it is correct. The built surface, interactions, and evidence must prove the contract.
