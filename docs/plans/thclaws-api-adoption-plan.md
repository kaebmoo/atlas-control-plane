# thClaws API Adoption Plan

Implementation plan for adopting thClaws `--serve` HTTP capabilities that Atlas
does not use yet. Written for execution by a coding agent (Claude Code), one
milestone per PR, following the conventions in `AGENTS.md` and the milestone
format of `docs/plans/workflow-engine-coding-plan.md`.

Survey source: thClaws source at v0.85.0 commit `e481015` (2026-07-03),
`crates/core/src/api_v1/` + `crates/core/src/server.rs`. Revised after five
independent review rounds (2026-07-03); validated findings are folded in
below — see "Review deltas" at the end for what changed and why.

Execution scope: **T0 → T1a → T2 → T3 are approved for implementation now**
(plus T1b and T4 when their preconditions are met). **T5–T8 are deferred
milestones** — specified here so the design survives, but each requires its
stated unblock (sync auth contract for T5–T6; operational demand for T7; a
benchmark for T8) before implementation starts.

## Objectives

1. **Meter real token usage (T1a), then estimated cost (T1b).** Fill the
   existing-but-always-NULL `usage_events.tokens_prompt` / `tokens_output`
   columns from the `usage` SSE event `/agent/run` already emits, so
   `/api/usage` and the Fleet CDR export carry token-level observability.
   Separately (T1b), compute estimated cost from `GET /v1/models` pricing —
   with the pricing snapshot persisted on the usage event at record time,
   because rates change over time and recomputing from a current cache is
   wrong. Fix the now-stale claim in `usage-metering-billing-plan.md` that
   thClaws does not emit usage.
2. **Surface structured execution events.** thClaws streams named SSE events
   (`text`, `thinking`, `tool_use_start`, `tool_use_result`,
   `tool_use_denied`, `skill_invoked`, `skill_invoked_result`,
   `user_message_injected`, `usage`, `result`, `error`) — Atlas stores frames
   but renders only text. Show a tool/skill timeline and denials in the
   dashboard and audit surfaces, persisting **structural metadata only**
   (never tool `input`/`output` — truncation is not redaction).
3. **Make long runs survive disconnects.** Adopt `x_callback` (fire-and-forget
   `/agent/run`): the worker keeps running and POSTs the terminal payload to an
   Atlas callback endpoint with retries and an idempotency key, so an Atlas
   restart or a dropped SSE no longer strands a long job. Also fixes a latent
   client defect: `thclaws_client.py` types `x_callback` as `str`, but thClaws
   requires an object `{url, api_key, run_id, idempotency_key?}`.
4. **Route on live worker state — advisorily.** Use the `busy` flag from
   `GET /workspace/sync/stat` and `skills[]` / `external_access.ui_url` from
   `/v1/agent/info` as dashboard information and small advisory scoring
   signals. Operator tags/roles stay the routing contract: the info snapshot
   is daemon-scoped (skills discovered from the daemon environment, not per
   `workspace_dir`), model lists are a catalogue (not a credential check), and
   `busy` is a racy process-wide snapshot.
5. **Move real files between workers.** Use `POST /workspace/sync/export`
   (selective path list — NOT `GET /sync/pull`, which tars the whole
   workspace) to collect job outputs, and `POST /workspace/sync/push` (gated)
   to hand real files to the next worker. This removes the single biggest
   quality ceiling on Coder→Reviewer / Reporter→Anchor chains, which today
   pass only `assistant_text` into the next prompt.
6. **Provision workers centrally.** `POST /v1/deploy/manifest|files` +
   `POST /v1/restart` (Bearer-authed) so an admin pushes a `.thclaws/` bundle
   from Atlas instead of editing each machine — with real provenance, not
   just an Atlas-side signature over whatever was uploaded.
7. **Validated model pickers now; chat-completions only if a benchmark earns
   it.** `GET /v1/models` for model validation is cheap and certain. But
   `/v1/chat/completions` is NOT a light surface: upstream builds a fresh
   `Agent` with the built-in `ToolRegistry` and runs the agent loop per call
   (`api_v1/chat.rs`). Any "faster/cheaper" claim must be measured before
   Atlas builds on it.

## Benefits

| Objective | Benefit | Who feels it |
|---|---|---|
| Tokens (T1a) + cost estimate (T1b) | Billing-grade observability; CDR gains token columns already reserved in the schema; estimated cost per job/run under BYOK (visibility, not a bill) | Operators, NT billing |
| Structured events | Operators see what tools/skills ran, what was denied, where time went — per job and per run | Operators, auditors |
| Async x_callback | Long workflows survive Atlas restarts and dropped streams; fixes the `x_callback` type defect before any call site exists | Workflow authors, operators |
| Advisory state | Fewer jobs behind a busy worker; worker UI deep-link; explainable decisions ("busy", "skill hint") | Operators |
| File transfer | Multi-agent workflows exchange real deliverables (code trees, reports, datasets) with SHA-256 verification | Workflow authors, end users |
| Central deploy | One audited dashboard action replaces N× ssh; fleet-wide config consistency | Admins |
| Model validation | Model names validated against the worker instead of typed blind; chat-completions deferred pending benchmark | Workflow authors |

## Non-goals

- **Remote job cancel.** thClaws has no cancel endpoint (only an in-process
  `CancelToken`). Atlas keeps best-effort cancellation; the upstream request
  is tracked under "External confirmations".
- **Duplicating thClaws-native features.** Agent Teams, local schedules, KMS,
  LINE/Telegram channels, GUI shells, `/upload`, `/ws` are worker-internal or
  browser surfaces, not control-plane APIs. Atlas does not re-implement or
  proxy them.
- **Remote approval.** thClaws has no approval-callback protocol on
  `/agent/run`; rendering `tool_use_denied` is observability, not approval.
- **Pooled tenancy, new runtime dependencies, framework adoption.** Unchanged
  invariants (below).

## Invariants (unchanged, enforced by the gate)

- Atlas core stays **Python stdlib only**; dashboard stays no-build HTML/CSS/JS.
- All `/api/*` changes are **additive** — no path or response-shape changes.
- **Silo:** no `tenant_id` in `atlas/` core (`scripts/check_silo.py`).
- Never log/store/return worker tokens or model keys.
- Every non-trivial behavior gets one hermetic, mutation-tested check under
  `scripts/`, folded into `scripts/gate.sh`.
- Any `/api/*` change updates `docs/specs/openapi.yaml` +
  `api-reference-en.md` + `api-reference-th.md` (EN/TH parity).
- Dashboard gate-marker substrings preserved.

## Hard gate on the sync surface (applies to T5–T6)

`/workspace/sync/*` does **not** use the `/v1/*` Bearer auth (`AuthOk`). Per
`server.rs`, sync shares `/upload`'s auth surface: cloud-ingress ForwardAuth
for hosted runners, the multiuser layer for multiuser pods, and **loopback
trust for plain local `--serve`**. A worker bound to a network interface may
expose sync unauthenticated.

Consequence: **sync-based features are disabled per worker by default**,
recorded as a persistent `workers.sync_mode` column (migration) with values
`disabled` (default) | `tunnel` | `forward_auth` — an enum naming the
approved deployment shape, not a bare boolean, so the audit trail says WHY
sync was trusted. It must NOT live inside the `agent_info` JSON blob:
`update_worker_status` rewrites that blob wholesale on every poll, so any
operator setting stored there would be silently erased. Changing `sync_mode`
is an authenticated admin action and is audited. If upstream later adds
Bearer auth to sync (tracked under "External confirmations"), a new
`bearer` mode collapses the gate to a version check. Atlas never falls back
to "use anyway".

Also verified: `sync/export|pull|push|trash` return **409 Conflict while an
agent turn is active** (`workspace busy`). Collectors must treat 409 as
retryable-after-terminal, not an error.

`/v1/deploy*`, `/v1/restart`, `/v1/models`, `/v1/chat/completions`, and
`/agent/run` all use the same Bearer auth — no such gate needed.

## Dependency order

```
APPROVED NOW
T0  (worker contract spike; no core code)
T1a (token capture + doc fix)      — independent; do first
T2  (structured events UI)         — independent
T3  (async x_callback)             — independent; includes client-defect fix
T1b (cost estimate w/ pricing snapshot) — needs T1a
T4  (advisory state + info surface)— needs T0 (sync_mode) for the stat probe

DEFERRED (design recorded; each has an explicit unblock)
T5  (file collect via sync/export) — unblock: sync auth contract (T0 gate)
T6  (file push handoff)            — needs T5
T7  (worker bundle deploy)         — unblock: operational demand; Bearer /v1/*
T8  (chat-completions surface)     — unblock: benchmark proves value
```

Recommended execution: T0 → T1a → T2 → T3, then T1b and T4.
T1a–T3 are pure-value, low-risk, and independent of the sync auth question.

---

## Milestone T0: Worker contract spike (read-only, no core changes)

Goal: establish, against a real `thclaws --serve` build, (a) which auth
applies to `/workspace/sync/*` in the deployment shapes Atlas supports, and
(b) a written worker protocol contract Atlas can version against — thClaws
exposes no protocol/schema version beyond the build version string.

Files:

- `docs/specs/thclaws-worker-contract.md` (new)
- `docs/ops/` (deployment guidance update)
- this file (record findings under this section)

Work:

- [ ] Run `thclaws --serve` locally; probe `/workspace/sync/stat` with and
      without the Bearer token, on loopback and on a LAN bind. Record results.
- [ ] Probe `/v1/deploy/manifest` the same way (expected: 401 without token).
- [ ] Write the worker contract doc: endpoints Atlas may call, auth per
      endpoint, the sync 409-busy semantics, the SSE event names Atlas relies
      on (`text`, `thinking`, `usage`, `result`, `error`, `session`,
      tool events, `[DONE]`), and the thClaws version range tested.
- [ ] Define per-worker capability gating: how a worker gets marked
      `sync_mode` (operator assertion of an approved deployment shape +
      successful authenticated probe), stored where, and how T5/T6 read it.
- [ ] File the upstream asks: Bearer auth on sync routes; a
      `GET /v1/capabilities?workspace_dir=…` that scopes skills to a
      workspace; protocol/schema version field in `/v1/agent/info`.

Checks:

- [ ] `scripts/check_docs.py` green (links resolve).
- [ ] Findings table added here.

Definition of done: contract doc merged; T4–T6 have explicit gating semantics
to build against.

---

## Milestone T1a: Token usage capture (+ stale-doc fix)

Goal: record prompt/output token counts from the worker's `usage` SSE event
into the existing `usage_events.tokens_prompt` / `tokens_output` columns.
Smallest diff, immediate value, no pricing involved.

Current base (verified): `/agent/run` SSE emits a named `usage` event at
`Done` with `prompt_tokens`, `completion_tokens`, `cached_input_tokens`,
`cache_creation_input_tokens`, `reasoning_output_tokens`; the sync JSON
response carries the same block. `emit_usage_event` in `db.py` already binds
`tokens_prompt`/`tokens_output` from its payload; `jobs.py` simply never
passes them. `docs/plans/usage-metering-billing-plan.md` ("Not available
today" list) still claims thClaws emits no usage — now false.

Files:

- `atlas/thclaws_client.py` (add `extract_usage`)
- `atlas/jobs.py` (capture usage in the stream loop; pass into
  `_record_job_usage`)
- `atlas/usage.py` (additive token totals in `summarize_usage`)
- `scripts/check_usage.py`, `scripts/check_jobs.py`
- `docs/plans/usage-metering-billing-plan.md` (correct the stale gap entry)
- openapi + api-reference EN/TH (only if `/api/usage` gains additive keys)

Work:

- [x] `extract_usage(event) -> dict | None` (style of `extract_session_id`;
      tolerate missing keys / non-integer values — return `None`, never raise).
- [x] Stream loop captures last-seen usage; `_record_job_usage` passes
      `tokens_prompt`/`tokens_output` and puts the full payload (cached /
      creation / reasoning counts) under `metadata.measures`.
- [x] `summarize_usage` gains additive token totals.
- [x] `byok_token_counts_billable: False` semantics untouched — this is
      observability, not a billing rule change.
- [x] Update `usage-metering-billing-plan.md`: thClaws emits usage as of
      v0.85.0; the gap is (was) Atlas-side parsing.

Checks:

- [x] Mock worker emits `usage` → job's usage row has non-NULL matching
      tokens.
- [x] No usage event (old worker) → NULL tokens, job still succeeds.
- [x] Malformed usage payload (strings, negatives, missing keys) → tolerated,
      NULL tokens, no crash.
- [x] Mutation test: `extract_usage` returns `{}` always → token-match
      assertion goes red.

---

## Milestone T1b: Estimated cost from pricing snapshots

Goal: attach an estimated, explicitly non-billable USD cost to usage events,
computed from `GET /v1/models` pricing. Split from T1a because pricing adds
real complexity: rates change over time, cache/reasoning token types have
separate rates, some models are free or tier-billed, and the effective model
for a turn may differ from the requested one.

Design decisions (fixed up front):

- **Snapshot at record time, never recompute.** The pricing block used
  (all `*_per_mtok` rates present) and the effective model id are persisted
  INTO the usage event's `metadata` when the event is recorded. Reports read
  the snapshot; they never re-price old events from a current cache.
- Cost fields are `estimated_cost_usd` + `estimate: true` + the snapshot;
  absent whenever pricing for the effective model is unknown. Partial rate
  coverage (e.g. no reasoning rate) prices the covered token types only and
  marks `pricing_partial: true`.
- `/v1/models` is fetched by the worker poll loop (short timeout, failure
  tolerated) and cached per worker. The cache feeds NEW events' snapshots
  only.
- No billing semantics change: `byok_token_counts_billable` untouched;
  CDR export may carry the estimate as an additive, clearly-marked column.

Files:

- `atlas/thclaws_client.py` (`list_models`)
- `atlas/app.py` (poll loop fetch + per-worker cache)
- `atlas/jobs.py` (snapshot into `metadata` at `_record_job_usage` time)
- `atlas/usage.py` (additive `estimated_cost_usd` totals from snapshots)
- `scripts/check_usage.py`
- openapi + api-reference EN/TH (additive keys)

Work:

- [ ] `list_models()` client + poll cache (failure → no snapshots, metering
      unaffected).
- [ ] Snapshot writer: effective model resolution (model reported by the
      worker's usage/result payload if present, else the requested model,
      recorded which), rates copied verbatim.
- [ ] Summaries compute cost strictly from per-event snapshots.

Checks:

- [ ] Mock pricing → cost = tokens × snapshot rates; changing the mock's
      pricing AFTER the event does not change the event's reported cost.
- [ ] Unknown model / no pricing → tokens recorded, no cost fields.
- [ ] Partial rates → only covered types priced, `pricing_partial: true`.
- [ ] Mutation test: make summaries read the live cache instead of the
      snapshot → the price-change check goes red.

---

## Milestone T2: Structured event surfaces

Goal: render the structured SSE events Atlas already stores as `job_events`
into operator-usable views: a per-job tool timeline and denial visibility,
built from **structural metadata only** — tool `input`/`output` are never
persisted and there is no payload preview, persistent or otherwise.
Observability only — explicitly NOT remote approval (no upstream protocol
exists).

Current base (verified): `jobs.py` appends non-text frames via
`append_job_event(job_id, event.event or "message", payload)`. Verified
upstream event names (`api_v1/agent.rs`): `text`, `thinking`,
`tool_use_start`, `tool_use_result`, `tool_use_denied`,
`skill_invoked` / `skill_invoked_result` (a `Skill` tool call is renamed at
emit time — same payload shape as the tool events), `user_message_injected`,
`usage`, `result`, `error`, plus the `session` frame. The dashboard must
still tolerate unknown names.

**Parser blocker (verified, must fix first):** `extract_text()` in
`thclaws_client.py` matches the keys `text`/`content`/`delta` on ANY event's
dict payload — so `thinking` (`{"delta": …}`) and `user_message_injected`
(`{"text": …}`) are currently folded into `assistant_text` and never reach
`append_job_event` as structured events. The timeline below cannot be built,
and handoff prompts inherit thinking text, until extraction is restricted to
assistant-text events.

Files:

- `atlas/thclaws_client.py` (fix `extract_text` event-name scoping)
- `atlas/jobs.py` (structured-event storage: project tool/skill events to
  structural metadata before write — payloads never stored)
- `atlas/static/app.js`, `atlas/static/index.html`, `atlas/static/styles.css`
- `atlas/app.py` (only if an additive events-summary endpoint is warranted;
  prefer reusing the existing job-events API)
- `scripts/check_jobs.py` (parser regressions),
  `scripts/check_ui_ux.py` or new `scripts/check_event_views.py`
- api-reference EN/TH + openapi only if a new endpoint is added

Work:

- [x] **Fix `extract_text`:** return assistant text only for assistant-text
      events (named `text`, or the legacy unnamed/`message` frames with the
      existing dict shapes — preserved for older workers); named structured
      events (`thinking`, `user_message_injected`, `tool_*`, `skill_*`,
      `usage`, `result`, `error`) must fall through to
      `append_job_event`, never into `assistant_text`.
- [x] **Persist structural metadata ONLY for tool/skill events — no
      payloads, no previews.** Truncation is not redaction: a short token or
      a secret at the head of a payload survives any cap, and Atlas cannot
      reliably detect secrets it has never seen (BYOK keys live outside
      Atlas by design). To keep the "never store tokens or model keys"
      invariant, `tool_use_start`/`tool_use_result`/`skill_invoked*` events
      are projected to `{id, name, status, input_bytes, output_bytes,
      input_sha256, output_sha256}` before `append_job_event` — `input` /
      `output` are dropped entirely. NOTE this also fixes current behavior:
      today's `append_job_event(..., payload)` path would persist RAW tool
      payloads once the parser fix lands, so the projection is what makes
      the parser fix safe to ship. Hashes still allow correlation with T5's
      collected artifacts without storing content.
- [x] No persistent payload preview in the UI either — the timeline renders
      from structural metadata; a payload view is deferred until an
      upstream-safe projection with a defined schema exists (recorded under
      External confirmations).
- [x] Job view: tool/skill timeline (name, start→result duration,
      ok/error/denied; `skill_invoked*` rendered as skill entries), derived
      client-side from the existing events list.
- [x] UI shows the structural-metadata timeline ONLY (name, status,
      durations, byte sizes, hashes) — no payload preview of any kind; the
      escape-untrusted-fields discipline from `check_permit_poc.py` applies
      to the stored fields that ARE rendered (tool/skill names, error
      strings).
- [x] Denials (`tool_use_denied`) and errors surfaced with distinct styling;
      per-job counters (tools run / denied / failed).
- [ ] Run view: per-node counter rollup. **DEFERRED** — the monitor view's
      `renderNodeChips` builds from `run.counters` only; a per-node tool/skill
      rollup needs per-node event aggregation that no current API provides
      (the timeline is per selected job via the existing events SSE) and no
      mandatory T2 check covers it. Deferred to a follow-up with a bulk
      per-node events summary; the per-job counters above ship now.
- [x] Unknown event names render as generic entries — never crash the view.

Checks:

- [x] **Parser regressions:** `thinking` and `user_message_injected` events
      do NOT appear in `assistant_text`; each is stored as a `job_events`
      row with its own event name; plain `text` events still accumulate
      (mutation: revert the `extract_text` scoping → both assertions go red).
- [x] **Secret literal planted in a mocked tool input AND output → a
      byte-scan of the SQLite DB file finds zero occurrences** (same style
      as `check_byok_helper.py`); the stored event rows carry only the
      structural fields (mutation: persist the payload → check goes red).
- [x] Mock worker emits a scripted tool sequence → timeline renders in order
      with correct statuses from structural metadata alone (assert on
      gate-marker substrings + data attrs).
- [x] Hostile tool/skill NAME (script tags, huge string) → escaped and
      length-capped in the UI (names are worker-controlled and ARE stored).
- [x] Unknown event name → view intact.
- [x] Existing gate markers preserved.

---

## Milestone T3: Async execution via x_callback

Goal: long-running jobs can run fire-and-forget — the worker 202-ACKs, keeps
running independently of Atlas's connection, and POSTs the terminal payload to
an Atlas callback endpoint. Fixes the latent client defect first.

Current base (verified): thClaws `XCallback` is an **object**
`{url, api_key, run_id, idempotency_key?}` (idempotency defaults to
`run_id`); delivery is best-effort, 3 attempts with backoff, gives up on
non-429 4xx. `thclaws_client.py` declares `x_callback: str | None` — wrong
shape, currently dead code (no call site), so fixing it is non-breaking.

Design decisions (fixed up front):

- Callback endpoint: `POST /api/worker-callbacks/{job_id}` — additive, BUT
  it must be dispatched **before the generic `/api/*` auth gate**: `do_POST`
  calls `_is_authorized()` (user/API-token auth) before routing, and a
  worker callback carries the HMAC `api_key`, not a user token — without an
  explicit pre-auth carve-out the callback dies with 401 before its handler
  runs. The carve-out is a dedicated handler with: its own HMAC verification
  (below), a strict **body-size cap** before reading, and a **system audit
  actor** (`system:worker-callback`) since no user identity exists on this
  path. This is a deliberate, documented exception to "all `/api/*` behind
  `_is_authorized()`" — threat-model entry required.
- Auth: per-dispatch **short-lived signed token** carried as the `api_key`
  field (HMAC over job_id + expiry with `ATLAS_SECRET_KEY`, same primitive as
  usage export). Constant-time compare; single-use enforced by job-state
  transition idempotency, replay after terminal state is a no-op 200.
  **Token validity must cover the full remote window**: callback deadline +
  the worker's retry envelope (3 attempts with backoff) + clock skew margin —
  a token that expires at the deadline rejects legitimate final retries.
- `run_id` = Atlas job id = idempotency key. Callback applies result, usage
  (reuse T1a parsing), session id, and terminal state **idempotently** — a
  duplicate delivery, or a callback racing the **reaper** (the realistic
  race: stream and callback modes are mutually exclusive per job), must
  converge to one terminal state (same discipline as
  `check_audit_fixes.py` terminal races).
- Async mode is **opt-in per job/node** (`execution: "callback"`), default
  stream — zero behavior change otherwise.
- Reaper: async jobs that never call back are failed by a sweep after
  `ATLAS_CALLBACK_TIMEOUT_SECONDS`. **Restart reconciliation must preserve
  callback-pending jobs**: they are legitimately in-flight on a remote
  worker, NOT interrupted jobs — `reconcile_jobs` must exempt them from
  interrupted-job handling and leave them to the reaper's deadline.
- Requires Atlas to be reachable from the worker (`ATLAS_PUBLIC_BASE_URL`);
  when unset, async mode is rejected at validation time with a clear error.

Files:

- `atlas/thclaws_client.py` (fix `x_callback` to a dict; build the envelope)
- `atlas/jobs.py` (dispatch path, callback application, reaper)
- `atlas/app.py` (callback route; `ATLAS_PUBLIC_BASE_URL` config)
- `atlas/config.py`, `atlas/db.py` (job fields for async state — migration if
  needed)
- `atlas/workflows.py` (node `execution: callback` — optional)
- `scripts/check_async_jobs.py` (new, in gate)
- openapi + api-reference EN/TH; threat-model update (new inbound surface)

Work:

- [ ] Client: correct `XCallback` envelope; 202-ACK handling (returns
      `session_id`).
- [ ] Callback route dispatched before `_is_authorized()`: dedicated
      handler, body-size cap, HMAC verification, system audit actor.
- [ ] Reaper; `reconcile_jobs` exemption for callback-pending jobs.
- [ ] Token expiry = deadline + retry envelope + skew margin.
- [ ] Workflow node opt-in + validation (rejects when base URL unset).

Checks:

- [ ] Callback with a valid HMAC `api_key` and NO user token reaches the
      handler and succeeds (mutation: route it through the generic
      `_is_authorized()` gate → check goes red with 401).
- [ ] Oversized callback body → rejected by the cap before processing.
- [ ] Mock worker delivers callback → job terminal, usage recorded, audit
      rows carry the system actor; duplicate delivery → single terminal
      state, 200.
- [ ] Bad/expired signature → 401, job unaffected, audit row.
- [ ] Token minted at dispatch still validates at deadline + retry window
      (simulated clock); token past that envelope → 401.
- [ ] No callback → reaper fails the job at the deadline.
- [ ] **Callback racing the reaper** (simulated) → one terminal state,
      idempotent convergence.
- [ ] Atlas restart with a callback-pending job → job survives reconcile as
      pending (not failed/interrupted), then completes via late callback.
- [ ] Mutation test: skip signature verification → check goes red.

---

## Milestone T4: Advisory worker state + richer info surface

Goal: show live worker state and capability info; feed routing **small
advisory signals only**. Operator tags/roles remain the routing contract.

Current base (verified): worker poller stores `/v1/agent/info` in
`workers.agent_info`; router ranks by status/tags/role/prompt hints.
`GET /workspace/sync/stat` returns `{…, busy, …}` — a process-wide snapshot
with inherent races, and only reachable under T0's approved shapes.
`info.skills` is daemon-environment-scoped (SkillStore::discover()), NOT per
`workspace_dir`; `model_capabilities.available_models` is a catalogue, not a
credential check.

Files:

- `atlas/thclaws_client.py` (`sync_stat()`)
- `atlas/db.py` (migration: `workers.sync_mode` TEXT NOT NULL DEFAULT
  'disabled' — persistent column, NOT the `agent_info` blob, which the poll
  loop rewrites wholesale on every status update)
- `atlas/app.py` (admin API to set `sync_mode` — additive, audited; poll
  loop: busy probe only when `sync_mode != 'disabled'`)
- `atlas/router.py` (advisory tie-break scoring; reasons in `RouteDecision`)
- `atlas/static/` (busy badge with staleness timestamp; "Open worker UI" link
  from `external_access.ui_url` — rendered ONLY when the URL parses as
  `http`/`https` (reject `javascript:`, `file:`, anything else), HTML-escaped,
  target=_blank rel=noopener; skills + `when_to_use` shown in worker detail
  marked "daemon-scoped, advisory")
- `scripts/check_worker_state.py` (new, in gate)
- openapi + api-reference EN/TH (additive worker fields)

Work:

- [ ] Migration + `sync_mode` admin API (`worker.sync_mode_changed` audit
      event carrying old→new value and actor).
- [ ] `sync_stat()` — short timeout; any error (incl. mode-disabled) →
      `busy: null` ("unknown"), never a routing blocker.
- [ ] Poll records `busy` + `busy_checked_at` inside the `agent_info` blob
      (probe results are poll-owned data, so blob storage is correct HERE —
      unlike the operator-owned `sync_mode`).
- [ ] Router: `busy == false` beats `true` only as a **tie-break** among
      equal-scored candidates; `null` between. Reason string emitted.
- [ ] Skill-hint matching (`when_to_use` vs prompt hints): advisory bonus
      smaller than any tag/role weight; existing routing fixture outcomes
      MUST NOT flip (asserted).
- [ ] Dashboard surfaces as above; version-compatibility warning when a
      worker's reported version is outside the contract-tested range (T0).

Checks:

- [ ] Busy worker loses the tie-break; stat 404/409/disabled → routing
      byte-identical to today.
- [ ] Fixture-stability assertion: existing route decisions unchanged.
- [ ] `sync_mode` survives a worker poll cycle (mutation: store it in
      `agent_info` instead → check goes red because the poll erases it).
- [ ] `ui_url` with `javascript:` / `file:` scheme → link not rendered.
- [ ] Gate markers for new dashboard elements; existing markers intact.
- [ ] Mutation test: invert the busy tie-break → check goes red.

---

## Milestone T5 (DEFERRED): Selective file collection (sync/export, read-only)

Unblock condition: the worker's `sync_mode` gate (T0) — an approved
deployment shape or upstream Bearer auth on sync routes.

Goal: after a job succeeds on a worker whose `sync_mode` is not `disabled`, Atlas fetches an
explicit list of output files via `POST /workspace/sync/export` (JSON array of
paths → gzip tar of just those paths) and stores them as `file_ref` artifacts
(existing upload store, SHA-256, secure download). Read-only; Atlas never
writes to the worker here.

NOT `GET /workspace/sync/pull` — pull tars the entire workspace; export is
the selective surface. Export returns **409 while an agent turn is active**,
with bounded 409-retries after the worker stream terminates.

**Collection is a pre-terminal barrier, not a post-success hook.** The
terminal `succeeded` state is what triggers `_maybe_start_handoff` and
workflow-node progression — anything published after it races the downstream
consumer. So the job lifecycle becomes: worker stream `[DONE]` → collection
resolves (`collected` / `collection_failed` / `collection_skipped`) → THEN
`state=succeeded` is written and handoff/workflow proceed. Downstream nodes
(and T6 pushes) therefore always observe a settled artifact set. Guarantees:

- Collection outcome never changes the job outcome — `succeeded` is written
  regardless; only its *timing* waits for collection to resolve.
- Bounded: `ATLAS_COLLECT_DEADLINE_SECONDS` caps the barrier (deadline →
  `collection_failed`, job proceeds to `succeeded`); a hung export can never
  hold a job non-terminal indefinitely.
- Restart during the barrier: the job is an interrupted job — the existing
  explicit-recovery rule applies (never auto-retried without operator
  authorization); collection is not silently re-attempted.

Design decisions (fixed up front):

- **Explicit path list, not "pull everything".** Job/node opts in with
  `collect_files: ["reports/a.md", "out/data.csv"]`. Globs are resolved
  Atlas-side against a prior `GET /workspace/sync/manifest` listing, so the
  export request itself is always a concrete path list. No config → no sync
  calls (zero behavior change).
- **Bounds.** `ATLAS_SYNC_MAX_BYTES` (default 64 MiB) and
  `ATLAS_SYNC_MAX_FILES` (default 200), enforced while streaming; over-limit
  aborts collection, job outcome unaffected (failure-isolated — same
  discipline as usage metering — but resolved BEFORE the terminal state, per
  the barrier above).
- **Tar safety (class fix).** One shared stdlib `tarfile` extractor with a
  strict member filter: reject absolute paths, `..`, symlinks/hardlinks,
  devices; per-file and total caps; write only into the opaque-id upload
  store, never a path derived from member names.
- **Gating.** Worker with `sync_mode = disabled` → `files.collection_skipped` job
  event, not an error.

Files:

- `atlas/thclaws_client.py` (`sync_manifest()`, `sync_export()` — deadline +
  byte-cap on the response, `iter_sse`-style discipline; 409 retry policy)
- `atlas/jobs.py` (pre-terminal collection barrier in `_run_job`, between
  `[DONE]` and the `succeeded` write)
- `atlas/workflows.py` (node `collect_files`; validator; artifacts keyed
  `files.<node_key>.<relpath>`)
- `atlas/app.py` (job create gains optional `collect_files` — additive)
- `scripts/check_file_collection.py` (new, in gate)
- openapi + api-reference EN/TH; `docs/specs/threat-model.md` (new trust
  boundary: worker-supplied tar)

Work:

- [ ] Client methods incl. bounded 409 retry (small count, fixed delay —
      collection follows worker stream termination, so contention should be
      transient).
- [ ] Safe-extractor helper (shared; the single validator any future tar
      ingestion must use).
- [ ] Job + node `collect_files`; artifacts with metadata: relpath, sha256
      (verified on write), bytes, source job/worker.
- [ ] Audit `files.collected` / `files.collection_failed` /
      `files.collection_skipped` (counts, never contents).
- [ ] Dashboard: collected files on job/run views via existing artifact list
      + download (gate markers).

Checks:

- [ ] Mock worker export → artifacts with correct sha256; downloaded bytes
      byte-identical.
- [ ] **Barrier ordering:** with `collect_files` + a handoff configured, the
      downstream job is created only AFTER artifacts exist (mock records
      event order); slow export (short of the deadline) still precedes
      `succeeded`.
- [ ] Collection deadline exceeded → `collection_failed`, job still reaches
      `succeeded`, handoff proceeds.
- [ ] Hostile tar members (`../x`, absolute, symlink) rejected; nothing
      written outside the upload store.
- [ ] Caps abort collection; job stays `succeeded`; failure audited.
- [ ] 409 then success → collection completes; persistent 409 → bounded give-up.
- [ ] No `collect_files` → mock saw zero sync calls.
- [ ] `sync_mode = disabled` worker → skipped event, no network call.
- [ ] Mutation test: disable `..` rejection → hostile-tar check goes red.

Why this milestone matters most (and the direction of its impact): today
handoff passes `assistant_text` into the next prompt
(`jobs.py::_maybe_start_handoff`), so downstream agents see descriptions of
files, not files. T5+T6 replace that with real file movement — positive for
capability; the risk side (hostile archives, oversized transfers, the sync
auth surface) is contained by the safe extractor, the caps, and the hard gate.

---

## Milestone T6 (DEFERRED): File handoff (push to the next worker)

Goal: a workflow edge or job handoff pushes previously-collected artifacts
into the target worker's workspace before the downstream job starts.

Depends on T5 (collection produces the file set; bounds and safety helpers
reused). Both endpoints under the same sync gate.

Design decisions (fixed up front):

- **Opt-in per workflow, off by default.** `policy.file_handoff: true` plus
  per-edge `push_files: [artifact key patterns]`. No policy → no push, ever
  (validator AND runtime guard).
- **Additive on the target.** Atlas never calls `/workspace/sync/trash` and
  never uses any replace/delete option. Files land under
  `incoming/<run_id>/<node_key>/…` only — a push can never clobber the
  target's own files.
- **Same bounds** as T5 on the outgoing tar; deterministic member order and
  normalized mtimes → reproducible bytes for audit hashing.
- **Audit before dispatch.** `files.pushed` (run, edge, target worker, count,
  bytes, sha256 list) recorded before the downstream job is created; a failed
  push fails the edge loudly (existing failure-continuation semantics apply
  if configured). Push also handles 409-busy with bounded retries.
- Downstream prompt template gains `{files_dir}` substitution pointing at the
  incoming prefix.

Files:

- `atlas/thclaws_client.py` (`sync_push()`)
- `atlas/workflows.py` (edge `push_files`, `policy.file_handoff`, prompt
  substitution, validator + explain/repair awareness)
- `atlas/jobs.py` (single-job variant: `handoff_push_files`)
- `scripts/check_file_handoff.py` (new, in gate)
- openapi + api-reference EN/TH; threat-model update (Atlas writes to workers)

Work:

- [ ] Tar assembly from file_ref artifacts; push client with deadline/caps;
      no automatic retry of a failed push beyond the 409 policy (a re-run is
      the operator's decision — same philosophy as restart recovery).
- [ ] Engine: resolve `push_files` against run artifacts → push → create
      downstream job with `{files_dir}`.
- [ ] Validator: `push_files` requires `policy.file_handoff`; builder repair
      suggests enabling it.
- [ ] Dashboard: pushes (count/bytes/target) on the run timeline (markers).

Checks:

- [ ] Two mock workers end-to-end: A's artifacts → pushed → B's prompt has
      `{files_dir}`; B's mock received members byte-identical to A's.
- [ ] Push without policy → save-time validation error AND runtime guard.
- [ ] Push failure → edge fails loudly; continue-on-failure audited skip.
- [ ] Mock asserts trash/replace endpoints never called.
- [ ] Mutation test: drop the runtime policy guard → check goes red.

---

## Milestone T7 (DEFERRED): Worker bundle deploy (fleet provisioning)

Goal: admin pushes a `.thclaws/` bundle (skills, MCP config, AGENTS.md,
settings) to selected workers with diff-aware transfer, per-worker outcomes,
audited SSE progress, and optional restart — with named-key provenance,
dry-run, and a previous-bundle re-deploy reference (explicitly NOT a
rollback; see below). Deliberately near the end of the roadmap: highest blast
radius, admin-only.

Current base (verified): `/v1/deploy/manifest` returns `{missing:[…]}` for a
`{files:[{path,sha256}]}` claim; `/v1/deploy/files` accepts a tar restricted
to allowlisted `.thclaws/` top-level entries, swaps atomically, preserves
sessions/memory, responds with SSE `extracted → reloaded → done`;
`/v1/restart` restarts. All Bearer-authed.

Design decisions:

- **Admin-only**; new permission `workers.deploy`.
- **Provenance = named publisher keys, honestly scoped.** A bundle must be
  signed by its publisher **before** upload. A single shared
  `ATLAS_SECRET_KEY` HMAC cannot identify a signer (every holder signs
  identically), so: per-publisher named HMAC keys — the signature envelope
  carries a `key_id`, Atlas resolves it from `ATLAS_BUNDLE_PUBLISHER_KEYS`
  (0600 sidecar file, same pattern as the Fleet token sidecar; never in the
  DB) and records bundle sha256 + `key_id` + uploading actor. Documented
  limitation: symmetric HMAC provenance means "signed by a holder of
  `key_id`", not a person — non-repudiable publisher identity needs
  asymmetric signatures, which stdlib-only Atlas core cannot do; recorded as
  a future ops/upstream ask, not faked. Atlas signing its own uploads proves
  nothing about origin — that shortcut remains rejected.
- **Dry-run first.** A deploy action's first phase is manifest-diff preview
  (which files the worker is missing); the operator confirms before bytes
  ship. Restart is a separate explicit confirmation.
- **Re-deploy of a previous bundle is NOT a rollback.** Verified upstream
  (`deploy.rs`): the deploy scratch dir is seeded from the LIVE `.thclaws/`
  tree and the bundle diff is extracted on top — deploy is a **merge**, so
  files added by a newer bundle survive re-deploying an older one. Atlas
  therefore offers "re-deploy previous recorded bundle" (previous sha256 kept
  per worker in `worker_deploys`, bundles retained in the upload store) and
  labels it exactly that in the UI and audit — never "rollback". A true
  replace/prune deploy mode is an upstream ask (External confirmations).
- Multi-worker deploys run sequentially with per-worker outcome; no
  partial-batch rollback (workers are independent).

Files:

- `atlas/thclaws_client.py` (`deploy_manifest()`, `deploy_files()` SSE,
  `restart()`)
- `atlas/app.py` (additive `POST /api/workers/{id}/deploy` (two-phase),
  `GET /api/worker-deploys`, `POST /api/workers/{id}/restart`)
- `atlas/db.py` (migration: `worker_deploys` — bundle sha256, signer, actor,
  phases, previous-bundle ref)
- `atlas/auth.py` (permission wiring)
- `atlas/static/` (admin flow: upload → verify → diff preview → confirm →
  progress → optional restart confirm; markers)
- `scripts/check_worker_deploy.py` (new, in gate)
- openapi + api-reference EN/TH; threat-model update

Work:

- [ ] Client methods incl. SSE progress with deadline.
- [ ] Bundle validation: publisher signature verified on upload against the
      named key from the sidecar (`key_id` resolution); allowlisted
      top-level entries only (mirror thClaws's own rule); size caps.
- [ ] Two-phase API + RBAC + audit
      (`worker.deploy.previewed/started/succeeded/failed`, `worker.restarted`).
- [ ] Re-deploy-previous-bundle flow (labeled as such, never "rollback").

Checks:

- [ ] Mock worker: diff preview shown before any file bytes ship; only
      `missing` files shipped; SSE phases recorded; restart only on explicit
      request.
- [ ] Unsigned or tampered bundle → rejected at upload, before any worker
      call.
- [ ] Operator role → 403; audit rows present; no bundle bytes in DB/logs.
- [ ] Re-deploy-previous ships the recorded previous sha256 and is labeled
      "re-deploy" in UI markers and audit rows (mutation: label it
      "rollback" → check goes red).
- [ ] Signature envelope with unknown `key_id` → rejected; audit records the
      attempted key id, never key material.
- [ ] Mutation test: skip signature verification → check goes red.

---

## Milestone T8 (DEFERRED): Model picker now; chat-completions only after a benchmark

Unblock condition for the chat-completions half: a measured benchmark showing
a real latency/cost advantage over `/agent/run` for the preview use case.
Verified reality (`api_v1/chat.rs`): each `/v1/chat/completions` call builds a
fresh `Agent` with the built-in `ToolRegistry` and runs the agent loop — it is
an OpenAI-compatible *wire format*, not a lighter execution path. The earlier
"faster/cheaper" assumption is withdrawn.

Part A — model picker (may ship with T1b/T4, no benchmark needed):

- `GET /api/workers/{id}/models` proxy (briefly cached; RBAC: any
  authenticated reader; never leaks worker tokens) reusing T1b's
  `list_models`.
- Dashboard model picker: dropdown when a list is available, free-text
  fallback preserved (markers).
- Checks: proxy passes through without token leakage; picker fallback works;
  existing markers intact.

Part B — chat-completions for builder previews (benchmark-gated):

- [ ] Benchmark spike first: same prompt via `/agent/run` (stream=false) vs
      `/v1/chat/completions` on a real worker; record latency + tokens.
      Record results here. If no clear advantage → close this part as
      "won't do" and keep the agent path.
- [ ] Only if the benchmark passes: non-streaming `chat_completion()` with
      deadline/size discipline; opt-in `llm_surface: "chat"` for builder
      Explain/Repair previews ONLY; manager-node execution keeps the agent
      path (a manager-on-chat experiment would be its own plan).
- [ ] Checks: preview via a mock worker implementing only
      `/v1/chat/completions`; default path byte-identical on existing
      fixtures.

---

## Cross-cutting definition of done (every milestone)

- `./scripts/gate.sh` and `./scripts/lint.sh` green from a clean tree.
- New checks hermetic (own temp DB, ephemeral port, mock worker) and
  mutation-tested (break the code → gate goes red, recorded in the PR).
- openapi.yaml + api-reference EN + TH updated for any `/api/*` change.
- `docs/specs/threat-model.md` updated per new trust boundary
  (T3: inbound callback; T5: worker-supplied tar; T6: Atlas writes to
  workers; T7: bundle deploy).
- `PROGRESS.md` gains one close-out row per milestone.
- No worker token or model key in logs, DB values (beyond existing
  `workers.token`), or API responses.

## Risk register

| Risk | Milestone | Mitigation |
|---|---|---|
| Sync endpoints unauthenticated on network binds | T4–T6 | hard gate: persistent `workers.sync_mode` enum requires an approved deployment shape (T0); default `disabled`; upstream Bearer ask filed |
| Callback endpoint as new inbound attack surface | T3 | pre-auth carve-out is dedicated + minimal: body cap before read, HMAC verify, system actor, idempotent apply, replay = no-op, reaper, threat-model entry |
| Thinking/user text polluting handoff prompts | T2 | `extract_text` scoped to assistant-text events; regression checks |
| Tool payload secrets persisted to SQLite | T2 | structural metadata only (`id`,`name`,`status`,bytes,sha256) — no payloads or previews stored; DB byte-scan check; truncation-is-not-redaction acknowledged |
| Hostile/oversized tar from a compromised worker | T5 | strict member filter, byte+count caps, opaque-id store writes only |
| Destructive write to a target workspace | T6 | additive-only under `incoming/<run_id>/`, no trash/replace ever, per-workflow opt-in + runtime guard |
| Bundle deploy as a malware vector | T7 | named publisher keys (`key_id`, 0600 sidecar), admin-only RBAC, allowlisted entries, dry-run, full audit |
| "Rollback" implying file removal it can't do | T7 | verified merge semantics; feature named "re-deploy previous bundle"; replace/prune mode filed upstream |
| Collection racing downstream handoff | T5 | pre-terminal barrier: collection resolves before `succeeded` is written; deadline-bounded |
| Token metering misread as billing change | T1a/T1b | `byok_token_counts_billable` untouched; cost labeled `estimate: true` |
| Stale pricing silently repricing history | T1b | pricing snapshot persisted per event at record time; summaries never read the live cache |
| Routing regressions from advisory signals | T4 | tie-break-only weighting + fixture-stability assertions |
| Daemon-scoped info mistaken for workspace truth | T4 | dashboard labels "daemon-scoped, advisory"; contract doc; upstream capabilities-endpoint ask |
| Race: busy snapshot stale at dispatch | T4 | advisory only; job dispatch never blocked on `busy` |

## Review deltas

### Round 1 (2026-07-03), each verified against source

1. **T1 expanded**: pricing/context from `/v1/models` (verified: per-mtok
   `PricingBlock`, `context_window` extension fields) + the stale
   `usage-metering-billing-plan.md` gap entry fix.
2. **New T2 (structured events)** and **new T3 (x_callback)**. The
   `x_callback: str` type defect in `thclaws_client.py` is confirmed
   (upstream `XCallback` is `{url, api_key, run_id, idempotency_key?}`,
   3-attempt delivery) — dead code today, fixed within T3.
3. **Old T2 split and demoted to advisory (now T4)**: skills in
   `/v1/agent/info` are daemon-scoped (`SkillStore::discover()`), model list
   is a catalogue, `busy` is a racy snapshot → tie-break-only signals,
   operator tags/roles stay the contract; upstream `capabilities?workspace_dir`
   ask filed via T0.
4. **File collection switched from `sync/pull` to `sync/export`** (verified:
   export takes a JSON path array; pull tars the whole workspace) and the
   **409-busy semantics** (verified on all sync write/read handlers) added.
5. **Sync-based features hard-gated** per worker instead of soft "degrade"
   language.
6. **T7 provenance corrected**: publisher signs before upload; Atlas-side
   post-upload signing rejected as provenance theater. Dry-run diff preview +
   rollback reference added; deploy moved to the roadmap tail.
7. **T8 scoped down**: chat-completions only for builder previews; manager
   nodes keep the agent path.

### Round 2 (2026-07-03), each re-verified against source

1. **Correction of a round-1 error in this file.** Round 1 claimed the
   `skill_invoked` event "was not found" — that was wrong. `skill_invoked`
   and `skill_invoked_result` exist in `api_v1/agent.rs` (~L235–252): the
   event name is computed (`if name == "Skill"`) rather than appearing as a
   string literal inside `named_event("…")`, which is why a literal grep
   missed it. T2's event list now includes both; T0 still pins the list in
   the contract doc, by reading the emitter code rather than grepping
   literals.
2. **T1 split into T1a (tokens) and T1b (cost estimate)**: pricing brings
   time-varying rates, cache/reasoning rate types, free/tier-billed models,
   and effective-model resolution. T1b persists a pricing snapshot + the
   effective model on each usage event at record time; summaries never
   reprice from a live cache.
3. **`sync_mode` made a persistent enum column** (`disabled|tunnel|
   forward_auth`, migration, audited admin change) instead of a flag near
   `agent_info` — verified that `update_worker_status` rewrites the
   `agent_info` blob wholesale on every poll, so anything operator-owned
   stored there would be erased. `external_access.ui_url` is rendered only
   when it parses as http/https.
4. **T8's "light surface" premise withdrawn** — verified that
   `/v1/chat/completions` builds a fresh `Agent` + `ToolRegistry` and runs
   the agent loop per call (`api_v1/chat.rs`). Model picker split out as
   Part A (no benchmark needed); chat-completions is Part B, gated on a
   recorded benchmark, default answer "won't do".
5. **Execution scope narrowed**: T0 → T1a → T2 → T3 approved now (T1b/T4
   when preconditions met); T5–T8 marked DEFERRED with explicit unblock
   conditions.

### Round 3 (2026-07-03), each verified against source

1. **T5 collection moved to a pre-terminal barrier.** Round-2 text said
   collection runs "after the job's terminal state" — that races the
   downstream: `jobs.py::_run_job` writes `succeeded` and immediately calls
   `_maybe_start_handoff`, and the workflow runner advances on the terminal
   state. Collection now resolves between `[DONE]` and the `succeeded`
   write, deadline-bounded (`ATLAS_COLLECT_DEADLINE_SECONDS`), outcome
   still failure-isolated; barrier-ordering and deadline checks added.
2. **T7 "rollback" renamed and re-scoped.** Verified in upstream
   `deploy.rs`: the deploy scratch is seeded from the live `.thclaws/` tree
   and the bundle is extracted on top — deploy is a merge; re-deploying an
   older bundle does not remove files a newer bundle added. The feature is
   now "re-deploy previous bundle" (UI + audit labels asserted by check);
   a replace/prune deploy mode is filed as an upstream ask.
3. **T7 provenance made honest.** A single shared `ATLAS_SECRET_KEY` HMAC
   cannot distinguish signers. Switched to named per-publisher HMAC keys
   (`key_id` in the signature envelope, keys in a 0600 sidecar per the
   Fleet pattern), with the residual limitation stated: provenance is
   "holder of `key_id`", not a person; asymmetric signing is out of scope
   for stdlib-only core and recorded as a future ask.

### Round 4 (2026-07-03), each verified against source

1. **T2 parser blocker added.** Verified: `extract_text()`'s dict branch
   matches `text`/`content`/`delta` keys on ANY event, so `thinking.delta`
   and `user_message_injected.text` currently leak into `assistant_text`
   instead of landing in `job_events`. T2 now leads with restricting text
   extraction to assistant-text events (legacy unnamed frames preserved),
   plus — as proposed in this round — sanitized/bounded storage of tool
   payloads (SUPERSEDED by Round 5: no payload storage at all) and parser
   regression checks.
2. **T3 callback-route blocker added.** Verified: `do_POST` runs
   `_is_authorized()` before routing, so a worker callback carrying only
   the HMAC `api_key` would 401 before its handler. T3 now specifies a
   pre-auth carve-out (dedicated handler: body-size cap, HMAC verify,
   system audit actor, threat-model entry), token expiry covering deadline
   + the worker's 3-attempt retry envelope + skew, `reconcile_jobs`
   exemption preserving callback-pending jobs across restarts, and the
   race check corrected to callback-vs-reaper (stream and callback modes
   are mutually exclusive per job, so callback-vs-SSE was untestable).
3. **T5 wording** ("collection follows terminal state" → "follows worker
   stream termination") aligned with the round-3 pre-terminal barrier.

### Round 5 (2026-07-03)

1. **T2 payload storage removed entirely — previews rejected.** Round 4
   proposed truncated/sanitized previews of tool `input`/`output`; the
   reviewer is right that truncation is not redaction (a short token or a
   secret at the head of the payload survives any cap) and Atlas cannot
   detect secrets it never knows (BYOK). Tool/skill events are now
   projected to structural metadata only (`id`, `name`, `status`, byte
   lengths, SHA-256) before storage; `input`/`output` never reach SQLite;
   the UI renders the timeline from structural fields with no persistent
   payload view until an upstream-safe projection schema exists. The DB
   byte-scan check asserts zero occurrences of a planted secret literal.
   This also hardens EXISTING behavior: the raw `append_job_event(...,
   payload)` path would otherwise start persisting full tool payloads the
   moment the round-4 parser fix lands.

## External confirmations outstanding

- thClaws: Bearer auth on `/workspace/sync/*` (blocks enabling T5/T6 for
  network-reachable workers outside tunnel/ForwardAuth shapes).
- thClaws: `GET /v1/capabilities?workspace_dir=…` (workspace-scoped skills).
- thClaws: protocol/schema version field in `/v1/agent/info`.
- thClaws: remote cancel endpoint — not planned upstream; Atlas cancel stays
  best-effort.
- thClaws: a replace/prune deploy mode (current deploy merges live + bundle;
  files are never removed) — prerequisite for a true rollback in T7.
- thClaws: a secret-safe tool-payload projection (redacted-at-source event
  payloads with a defined schema) — prerequisite for any persistent payload
  preview in T2's timeline.
