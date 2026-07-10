# thClaws API Adoption Plan

Implementation plan for adopting thClaws `--serve` HTTP capabilities that Atlas
does not use yet. Written for execution by a coding agent (Claude Code), one
milestone per PR, following the conventions in `AGENTS.md` and the milestone
format of `docs/plans/workflow-engine-coding-plan.md`.

Survey source: thClaws v0.85.0 commit `e481015` (the original survey), then
v0.88.0 tag `66a80bb` (Job Artifacts) and local `main` `bf1d6bb` (2026-07-10,
also containing v0.89.0). The implementation source is
`crates/core/src/api_v1/{agent,artifacts}.rs` and `api_v1/mod.rs`.

Execution scope: **T0 → T6 are done and merged.** T5/T6 deliberately used the
then-available `/workspace/sync/*` path behind `tunnel` / `forward_auth`.
thClaws v0.88.0 now ships the stronger Job Artifact contract, so the next work
is **T9a → T9b: replace Atlas's sync-based file path with Job Artifacts**.
T7 and T8's chat-completions half remain deferred — T7 on operational demand,
T8 Part B on a benchmark.

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
5. **Move real files between workers through Job Artifacts.** Declare output
   globs in `POST /agent/run` as `collect_files`; thClaws freezes matching
   files with SHA-256 at terminal completion. Atlas reads the Bearer-authenticated
   manifest and individual snapshots, then sends selected files to the next
   worker through Bearer-authenticated `POST /v1/inputs`. This removes the
   Coder→Reviewer / Reporter→Anchor ceiling without requiring a sync tunnel.
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
| Job Artifact transfer | Multi-agent workflows exchange immutable, per-job deliverables (code trees, reports, datasets) with SHA-256 verification and no tunnel prerequisite | Workflow authors, end users |
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

## Job Artifact contract (v0.88.0; required by T9)

Job Artifacts is the supported control-plane file contract:

| Operation | thClaws contract | Atlas use |
|---|---|---|
| Declare outputs | `POST /agent/run` with `collect_files: [glob, ...]` | Forward each job/node's collection patterns when dispatching, on both SSE and `x_callback` execution. |
| Read manifest | `GET /v1/sessions/{sid}/artifacts?workspace_dir=...` | Fetch the frozen manifest after successful completion; validate its schema, paths, counts, sizes, and SHA-256 values. |
| Read bytes | `GET /v1/sessions/{sid}/artifacts/{aid}?workspace_dir=...` | Download only a manifest member, under a byte/deadline cap; require exact length and SHA-256 match before storing it. |
| Place inputs | `POST /v1/inputs` with `{workspace_dir?, files:[{path,content_base64}]}` | Place selected file artifacts under a fresh `inputs/incoming/<run>/<node>/` prefix before dispatching the downstream worker. |

All four endpoints use the existing `THCLAWS_API_TOKEN` Bearer policy. The
artifact snapshot is copied and hashed at worker completion; it is not a view
of the mutable live workspace. This eliminates the old export race and means
Atlas must **not** implement another tar format or use `sync/export|push` for
ordinary orchestration.

Pinned upstream limits are: output snapshots at 256 files / 300 MiB per run;
input requests at 100 files / 64 MiB decoded (96 MiB JSON-body ceiling).
Atlas must enforce no larger limit before sending a request. `collect_files`
uses thClaws globset syntax, not an Atlas-side filename list. An empty manifest
or non-empty upstream `skipped[]` is a visible, failure-isolated collection
outcome — never silently treated as a complete requested set.

The session id returned by `/agent/run` is the Job Artifact id. On SSE it is
the initial `session` event; on `x_callback` it is in the 202 ACK. Atlas
already persists this as `jobs.thclaws_session_id`, so no new job-id column is
needed. The worker/workspace that created it remains the lookup scope; Atlas
must always pass the resolved `workspace_dir` where one was used for dispatch.

## Legacy sync gate (T4 only after T9)

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

Also verified: `sync/manifest|export|pull|push|trash` return **409 Conflict
while an agent turn is active** (`workspace busy`). This remains relevant only
to T4's advisory `stat` probe and any explicitly operator-managed legacy sync
use; T9 does not use these routes.

`/v1/deploy*`, `/v1/restart`, `/v1/models`, `/v1/chat/completions`, and
`/agent/run` all use the same Bearer auth — no such gate needed.

## Legacy sync deployment-shape strategy (historical T5/T6 path)

`/workspace/sync/{export,push,manifest,pull}` already exist in stock thClaws;
only their **auth** is open (see the hard gate above). T5/T6 are therefore gated
on a trust shape, not on a missing endpoint — and the project is never blocked,
because Tier 1 needs nothing from upstream. Three tiers, in priority order:

1. **Tunnel / ForwardAuth now (APPROVED, default path).** Enable
   `sync_mode = tunnel` (sync socket reachable only through an operator-approved
   private/SSH tunnel) or `forward_auth` (an ingress authenticates before sync).
   Full T5/T6 on stock thClaws, no upstream change. Atlas builds against this.
2. **Upstream Bearer on sync (preferred, opportunistic — NOT blocking).** If
   thClaws adds Bearer to `/workspace/sync/*` (discussion #178), add a `bearer`
   `sync_mode` after a version/capability check; it lets network-reachable
   workers skip the tunnel. A convenience over Tier 1, never a prerequisite.
3. **Self-patch fallback (only if a non-tunnel shape is genuinely needed AND
   upstream stalls).** Atlas maintains a minimal thClaws patch — the
   artifact-idea Tier-1 change (`THCLAWS_SYNC_REQUIRE_AUTH=1`, requiring the
   existing Bearer on sync) — on a tracked fork, ships it, and submits the PR
   upstream. Carries fork-maintenance cost (rebase until merged), so it is
   justified only when a per-worker tunnel is a real operational blocker.

**Division of labor — the operator provisions the pipe; Atlas verifies, gates,
and tracks. Atlas never becomes a tunnel manager:**

- Atlas does **not** run `ssh`, hold tunnel keys, or spawn tunnel processes —
  that is a new secret-storage + subprocess trust surface and breaks the
  stdlib-only / no-new-runtime-dep invariant. Tunnel credentials stay in the
  OS/SSH layer, outside Atlas. The data path is wired simply by pointing the
  worker's `base_url` at the tunnel endpoint.
- Atlas **does** make it low-friction and central:
  - **Verify at enable time** — setting `tunnel`/`forward_auth` runs the T0
    pre-enable `sync_stat()` probe through the same client path; the mode
    persists only on success, so a broken tunnel fails loud at toggle time, not
    silently at collection time. (The probe proves reachability, not privacy —
    the operator's shape assertion is load-bearing and cannot be auto-detected.)
  - **One audited toggle per worker** (`worker.sync_mode_changed`, old→new +
    actor); a fleet can be enabled in a batch action, each still probed.
  - **Fleet visibility** — the dashboard shows each worker's sync shape and live
    probe/tunnel health (reusing T4's `busy`/`busy_checked_at` staleness), so
    "which tunnels are up" is one view, not N SSH sessions.
  - **Copy-paste recipes, not automation** — `docs/ops/deployment.md` ships the
    exact forward `ssh -NL` / reverse `ssh -R` / autossh / WireGuard / systemd
    snippet parameterized by worker host:port (a T5 deliverable), turning
    "figure out tunneling" into "paste this per machine".
- **Transport-agnostic** — Atlas needs only `base_url` reachable + the probe to
  pass, so the operator picks whatever fits: forward `ssh -L`, worker-initiated
  reverse `ssh -R` to a bastion (no inbound firewall change on the worker),
  WireGuard/Tailscale mesh, or an existing ingress ForwardAuth (near-zero
  per-worker setup for hosted fleets). No lock-in.

Net: **not** manual-and-blind per machine. The tunnel pipe is the one genuinely
per-worker step (paste a recipe); enabling, verifying, auditing, and monitoring
are central Atlas actions.

## Dependency order

```
APPROVED NOW
T0  (worker contract spike; no core code)          — done (#24)
T1a (token capture + doc fix)                      — done (#16)
T2  (structured events UI)                         — done (#18)
T3  (async x_callback)                             — done (#20, #26)
T1b (cost estimate w/ pricing snapshot)            — done
T4  (advisory state + info surface)                — done; sync_mode remains for stat only
T5  (file collect via sync/export)                 — done; superseded by T9a
T6  (file push handoff via sync/push)              — done; superseded by T9b
T9a (collect frozen Job Artifacts)                 — done (requires thClaws >= 0.88.0)
T9b (handoff through /v1/inputs)                   — requires T9a

DEFERRED (design recorded; each has an explicit unblock)
T7  (worker bundle deploy)         — unblock: operational demand; Bearer /v1/*
T8  (chat-completions surface)     — unblock: benchmark proves value
```

Recommended execution: implement **T9a first**, then **T9b**. T9a replaces
the collection transport and establishes the validated local `file_ref` set;
T9b reuses that set for inputs. Do not carry compatibility fallback to
`sync/export|push` in the job/workflow path: it would preserve the tunnel
dependency and multiply test states. `sync_mode` stays only for T4's optional
busy signal.

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

- [x] Run `thclaws --serve` locally; probe `/workspace/sync/stat` with and
      without the Bearer token, on loopback and on a LAN bind. Record results.
- [x] Probe `/v1/deploy/manifest` the same way (expected: 401 without token).
- [x] Write the worker contract doc: endpoints Atlas may call, auth per
      endpoint, the sync 409-busy semantics, the SSE event names Atlas relies
      on (`text`, `thinking`, `usage`, `result`, `error`, `session`,
      tool events, `[DONE]`), and the thClaws version range tested.
- [x] Define per-worker capability gating: how a worker gets marked
      `sync_mode` (operator assertion of an approved deployment shape +
      successful authenticated probe), stored where, and how T5/T6 read it.
- [x] File the upstream asks: Bearer auth on sync routes; a
      `GET /v1/capabilities?workspace_dir=…` that scopes skills to a
      workspace; protocol/schema version field in `/v1/agent/info`.
      Filed as thClaws discussions
      [#178](https://github.com/thClaws/thClaws/discussions/178) and
      [#179](https://github.com/thClaws/thClaws/discussions/179); local copies
      remain under `docs/upstream/`.

Checks:

- [x] `scripts/check_docs.py` green (links resolve).
- [x] Findings table added here.

Findings (live probe on 2026-07-04; thClaws `0.85.0`, source `18e3aa2`):

| Bind | Endpoint | No Bearer | Bearer | Result |
|---|---|---:|---:|---|
| loopback | `GET /workspace/sync/stat` | 200 | 200 | sync ignores the worker Bearer |
| loopback | `POST /v1/deploy/manifest` | 401 | 200 | `/v1/deploy/*` enforces Bearer |
| `0.0.0.0` | `GET /workspace/sync/stat` | 200 | 200 | LAN-bound configuration has the same sync auth surface |
| `0.0.0.0` | `POST /v1/deploy/manifest` | 401 | 200 | LAN-bound `/v1/*` still enforces Bearer |

The LAN-bound process was reached through the host-local socket. A probe through
the physical LAN interface timed out before HTTP, so it was excluded as host
firewall/network behavior rather than treated as an auth result. The full
contract and gating semantics are recorded in
`docs/specs/thclaws-worker-contract.md`.

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

- [x] `list_models()` client + poll cache (failure → no snapshots, metering
      unaffected).
- [x] Snapshot writer: effective model resolution (model reported by the
      worker's usage/result payload if present, else the requested model,
      recorded which), rates copied verbatim.
- [x] Summaries compute cost strictly from per-event snapshots.

Checks:

- [x] Mock pricing → cost = tokens × snapshot rates; changing the mock's
      pricing AFTER the event does not change the event's reported cost.
- [x] Unknown model / no pricing → tokens recorded, no cost fields.
- [x] Partial rates → only covered types priced, `pricing_partial: true`.
- [x] Mutation test: make summaries read the live cache instead of the
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

- [x] Client: correct `XCallback` envelope; 202-ACK handling (returns
      `session_id`).
- [x] Callback route dispatched before `_is_authorized()`: dedicated
      handler, body-size cap, HMAC verification, system audit actor.
- [x] Reaper; `reconcile_jobs` exemption for callback-pending jobs.
- [x] Token expiry = deadline + retry envelope + skew margin.
- [x] Workflow node opt-in + validation (rejects when base URL unset).
      Workflow-run restart semantics stay on the standing explicit-recovery
      rule (no engine re-attach in T3): the recovery entry flags
      `callback_pending` node jobs and the operator warning says to check the
      job outcome first, since retry always submits a NEW job.

Checks:

- [x] Callback with a valid HMAC `api_key` and NO user token reaches the
      handler and succeeds (mutation: route it through the generic
      `_is_authorized()` gate → check goes red with 401).
- [x] Oversized callback body → rejected by the cap before processing.
- [x] Mock worker delivers callback → job terminal, usage recorded, audit
      rows carry the system actor; duplicate delivery → single terminal
      state, 200.
- [x] Bad/expired signature → 401, job unaffected, audit row.
- [x] Token minted at dispatch still validates at deadline + retry window
      (simulated clock); token past that envelope → 401.
- [x] No callback → reaper fails the job at the deadline.
- [x] **Callback racing the reaper** (simulated) → one terminal state,
      idempotent convergence.
- [x] Atlas restart with a callback-pending job → job survives reconcile as
      pending (not failed/interrupted), then completes via late callback.
- [x] Mutation test: skip signature verification → check goes red.
      (Review-round additions, all in `scripts/check_async_jobs.py`: token
      never stored — DB/WAL byte-scan incl. dispatch-error echo redaction;
      cancel racing the terminal write converges to `cancelled` atomically;
      ambiguous failures — ACK loss, 5xx, oversized ACK — keep the job
      callback-pending while a definitive 4xx/refused-connect rejection still
      fails it; the whole apply (terminal state + text + events + audit +
      usage) is ONE transaction, so a mid-apply crash preserves the worker's
      retry as recovery; rejected-callback audit rows only for real job ids
      (unauthenticated DoS bound); workflow runs with callback nodes are
      rejected at start when async is unconfigured; falsey/unhashable/null
      `execution` values → clean 400; only a conforming 202 ACK counts as a
      dispatch (non-202 fails fast, mismatched ACK stays pending); delivered
      `run_id` must match the URL's job; rejected-callback audits are
      rate-limited per job (lock-serialized, race-proof, fail-closed at the
      cache cap); the inbound body read is time-bounded (per-recv timeout +
      wall-clock deadline) and slot-bounded across connections (503 overflow,
      retryable); usage
      attribution is derived inside the terminal transaction AND repaired at
      the node→job link (every interleaving covered); the reaper sweep uses a
      partial index over PENDING callbacks only (migration 007 — deliberately
      not in the base SCHEMA, which re-runs against legacy tables that lack
      the columns); the runner wait extends to a callback
      job's own deadline; workflow-run recovery flags `callback_pending`
      node jobs.)

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
  loop: busy probe only when `sync_mode != 'disabled'`; enabling a mode probes
  through the same client path before persisting it)
- `atlas/router.py` (advisory tie-break scoring; reasons in `RouteDecision`)
- `atlas/static/` (busy badge with staleness timestamp; "Open worker UI" link
  from `external_access.ui_url` — rendered ONLY when the URL parses as
  `http`/`https` (reject `javascript:`, `file:`, anything else), HTML-escaped,
  target=_blank rel=noopener; skills + `when_to_use` shown in worker detail
  marked "daemon-scoped, advisory")
- `scripts/check_worker_state.py` (new, in gate)
- openapi + api-reference EN/TH (additive worker fields)

Work:

- [x] Migration + `sync_mode` admin API. A transition from `disabled` to
      `tunnel`/`forward_auth` MUST run a short-timeout `sync_stat()` probe
      through the same worker URL/client path and validate the response before
      persisting; failure leaves the old mode unchanged. Successful changes emit
      `worker.sync_mode_changed` carrying old→new value and actor. This
      authenticated pre-enable transition is the ONLY caller allowed to probe
      while the persisted mode is still `disabled`.
- [x] `sync_stat()` — short-timeout client method. The normal poll calls it only
      when mode is enabled; disabled or any probe error produces `busy: null`
      ("unknown") without a network call/fallback, never a routing blocker.
- [x] Poll records `busy` + `busy_checked_at` inside the `agent_info` blob
      (probe results are poll-owned data, so blob storage is correct HERE —
      unlike the operator-owned `sync_mode`).
- [x] Router: `busy == false` beats `true` only as a **tie-break** among
      equal-scored candidates; `null` between. Reason string emitted.
- [x] Skill-hint matching (`when_to_use` vs prompt hints): advisory bonus
      smaller than any tag/role weight; existing routing fixture outcomes
      MUST NOT flip (asserted).
- [x] Dashboard surfaces as above; version-compatibility warning when a
      worker's reported version is outside the contract-tested range (T0).

Checks:

- [x] Busy worker loses the tie-break; stat 404/409/disabled → routing
      byte-identical to today.
- [x] Fixture-stability assertion: existing route decisions unchanged.
- [x] `sync_mode` survives a worker poll cycle (mutation: store it in
      `agent_info` instead → check goes red because the poll erases it).
- [x] Enabling `tunnel`/`forward_auth` with an unreachable or malformed stat
      response is rejected and leaves `sync_mode = disabled`; a valid probe
      enables it (mutation: skip the enable-time probe → check goes red).
- [x] `ui_url` with `javascript:` / `file:` scheme → link not rendered.
- [x] Gate markers for new dashboard elements; existing markers intact.
- [x] Mutation test: invert the busy tie-break → check goes red.

---

## Milestone T5 (APPROVED — tunnel/forward_auth): Selective file collection (sync/export, read-only)

Status: **unblocked for the tunnel/ForwardAuth shape.** The T0 `sync_mode` gate
is delivered and the export endpoint exists in stock thClaws, so T5 runs now on
any worker whose operator has asserted + probed a `tunnel` or `forward_auth`
shape (Tier 1 of the sync deployment-shape strategy above) — no upstream change.
Workers reachable only as plain network `--serve` stay `disabled` until upstream
Bearer (Tier 2) or a self-patch (Tier 3).

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

- [x] Client methods incl. bounded 409 retry (small count, fixed delay —
      collection follows worker stream termination, so contention should be
      transient). `sync_export()` in `thclaws_client.py`. NOTE `sync_manifest()`
      + Atlas-side glob resolution DEFERRED (no mandatory check covers it; the
      export request is always an explicit concrete path list) — add when a
      glob use-case appears.
- [x] Safe-extractor helper (shared; the single validator any future tar
      ingestion must use). `atlas/sync_files.py::safe_extract_tar` + `store_bytes`.
- [x] Job + node `collect_files`; artifacts with metadata: relpath, sha256
      (verified on write), bytes, source job/worker.
- [x] Audit `files.collected` / `files.collection_failed` /
      `files.collection_skipped` (counts, never contents).
- [x] Dashboard: collected files on job/run views via existing artifact list
      + download — reused as-is: collected files are `file_ref` artifacts keyed
      to the run, so they render in the existing run-artifacts list/download with
      no new markers.

Checks:

- [x] Mock worker export → artifacts with correct sha256; downloaded bytes
      byte-identical.
- [x] **Barrier ordering:** with `collect_files` + a handoff configured, the
      downstream job is created only AFTER artifacts exist (mock records
      event order); slow export (short of the deadline) still precedes
      `succeeded`.
- [x] Collection deadline exceeded → `collection_failed`, job still reaches
      `succeeded`, handoff proceeds.
- [x] Hostile tar members (`../x`, absolute, symlink) rejected; nothing
      written outside the upload store.
- [x] Caps abort collection; job stays `succeeded`; failure audited.
- [x] 409 then success → collection completes; persistent 409 → bounded give-up.
- [x] No `collect_files` → mock saw zero sync calls.
- [x] `sync_mode = disabled` worker → skipped event, no network call.
- [x] Mutation test: disable `..` rejection → hostile-tar check goes red.

Why this milestone matters most (and the direction of its impact): today
handoff passes `assistant_text` into the next prompt
(`jobs.py::_maybe_start_handoff`), so downstream agents see descriptions of
files, not files. T5+T6 replace that with real file movement — positive for
capability; the risk side (hostile archives, oversized transfers, the sync
auth surface) is contained by the safe extractor, the caps, and the hard gate.

---

## Milestone T6 (APPROVED — tunnel/forward_auth): File handoff (push to the next worker)

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

- [x] Tar assembly from file_ref artifacts (`sync_files.build_push_tar`,
      reproducible); push client `sync_push()` with deadline/caps + bounded
      409-retry; no automatic retry of a failed push beyond the 409 policy.
- [x] Engine: resolve `push_files` against run artifacts → push → create
      downstream job with `{files_dir}` (`_push_files_to_worker`, taken-edge
      intent stashed per target node, pushed to the RESOLVED worker).
- [x] Validator: `push_files` requires `policy.file_handoff` (save-time in
      `validate_workflow_graph` + runtime guard in the `_execute_run` node loop).
      NOTE: builder explain/repair "suggests enabling it" DEFERRED — an
      AI-builder nicety, no mandatory check; the hard validator ships.
- [x] Dashboard: pushes on the run timeline — reused: `files_pushed` workflow
      events (count/bytes/target) render via the existing workflow-event
      timeline; no new marker code needed.
- [ ] **Single-job `handoff_push_files` variant (jobs.py) — DEFERRED.** No
      mandatory check covers it; standalone-job handoff currently passes
      assistant_text and file push there needs its own opt-in + job column.
      Workflow edges are the tested primary path (add when a single-job
      file-handoff use-case appears).

Checks:

- [x] Two mock workers end-to-end: A's artifacts → pushed → B's prompt has
      `{files_dir}`; B's mock received members byte-identical to A's.
- [x] Push without policy → save-time validation error AND runtime guard.
- [x] Push failure → edge fails loudly; continue-on-failure audited skip.
- [x] Mock asserts trash/replace endpoints never called.
- [x] Mutation test: drop the runtime policy guard → check goes red.

---

## Milestone T9a (DONE): Collect frozen Job Artifacts

Goal: replace T5's sync-export/tar collection with thClaws Job Artifacts on
workers running v0.88.0 or newer. Each existing collect_files value becomes a
thClaws glob declaration on agent/run. After terminal completion but before
Atlas writes succeeded, Atlas uses the job's persisted thclaws_session_id to
read the immutable manifest and each selected snapshot.

This preserves the pre-terminal barrier and existing file_ref artifact shape,
so workflow ordering, downloads, dashboard rendering, and the later handoff
selection model remain stable. It removes the sync_mode collection gate,
409-busy retry, tar extraction, and tunnel prerequisite.

Design decisions:

- No fallback: a worker below 0.88.0 or without a conforming Artifact API
  records files.collection_failed with a compatibility reason; never fall back
  to workspace sync. A job without collect_files makes no artifact request.
- Forward collect_files on both stream and x_callback calls. The stream's
  session event and the async 202 ACK already populate thclaws_session_id; use
  that exact id plus the resolved workspace_dir for all artifact requests.
- Treat the manifest as untrusted metadata. Before download, validate unique
  nonempty ids/paths, the existing relative-path jail, integer size, lowercase
  64-hex SHA-256, aggregate Atlas and upstream limits, and skipped[] — which is
  OPTIONAL on the wire: thClaws serde-omits the key when nothing was skipped
  (the normal case), so absent means empty while a present non-list is still
  rejected. A malformed or truncated manifest publishes no partial file_ref set.
- Download a manifest member only under byte and wall-clock caps. Require its
  x-sha256 header, actual length, and locally calculated SHA-256 to match the
  manifest before opaque-store staging and the existing all-or-nothing DB write.
- ThClaws owns glob matching. Atlas validates only bounded, obviously safe
  pattern strings; it does not reimplement glob expansion. Empty non-skipped
  manifests are valid zero-file collections; nonempty skipped[] is an explicit,
  failure-isolated partial collection outcome.
- Callback success needs a pre-terminal collection phase. Do not put a blocking
  worker request inside T3's terminal DB transaction. Claim, collect outside
  the transaction, then atomically terminalize; duplicate callback and reaper
  races must retain exactly one terminal state and one published file set.
- **Serialize continued sessions through collection.** thClaws calls the
  session id a job id, but Atlas can intentionally reuse that id through
  session_bindings. A later turn on the same worker/workspace/session can
  overwrite that session's upstream artifact snapshot before Atlas has copied
  it. Preserve conversation continuity and the freeze guarantee by taking a
  durable, crash-recoverable lease for a bound session from dispatch until
  collection/terminalization completes; a second job using that binding waits
  rather than interleaving. Do not solve this by silently dropping session
  continuation or by using an in-memory-only lock.

Files:

- atlas/thclaws_client.py: artifact manifest/file methods and collect_files in
  both agent-run payloads; remove sync-export use.
- atlas/jobs.py: pattern validation/forwarding and one collector shared by
  stream and callback success.
- atlas/sync_files.py (or a narrowly renamed module): retain the shared
  relative-path and opaque-store helpers; remove tar handling after its callers
  are gone.
- atlas/workflows.py, scripts/check_job_artifacts.py, scripts/gate.sh, worker
  contract, threat model, OpenAPI, EN/TH reference docs, and PROGRESS.md.

Checks:

- Mock v0.88 worker proves forwarding, correct session/workspace lookup, and
  frozen bytes even when the live workspace changes after completion.
- Reject unsafe or duplicate manifest entries, caps, skipped[], bad header/body
  hashes, size mismatches, and short/oversized reads without rows or blobs.
- Stream and callback collections publish before succeeded; callback duplicate
  and reaper races retain T3's one-terminal-state guarantee.
- Two jobs sharing a continued thClaws session cannot interleave a later run
  with the first job's artifact collection; cancellation, worker failure, and
  restart release/recover the durable lease without admitting a second turn
  early.
- No collection means no Artifact API request; an old worker never triggers a
  sync fallback.
- Mutations: remove forwarding, skip local SHA validation, publish one good
  member from a bad set, and terminalize callback success before collection.

Close-out: implemented against thClaws `bf1d6bb` (v0.89.0 tree, including the
v0.88.0 Job Artifact API); hermetic stream/callback/lease checks are in
`scripts/check_job_artifacts.py` and run from `scripts/gate.sh`. T9b input
handoff remains deliberately unimplemented. A post-close-out bug hunt
(2026-07-10, Round 8 below) fixed a wire-shape rejection every real worker
would have hit (`skipped` absent) plus two lease/inflight enforcement gaps;
the reaper-vs-collector race is now mutation-locked in the same check
(`lease_loser_cleanup`).

---

## Milestone T9b (PLANNED): Handoff through Bearer-authenticated inputs

Goal: replace T6's sync-push tar handoff with POST /v1/inputs. Before creating
a downstream workflow-node job, Atlas converts selected, already-verified
file_ref artifacts to base64 entries and writes them below the fresh target
prefix inputs/incoming/<run_id>/<node_key>/. The downstream {files_dir}
substitution points to that prefix.

Design decisions:

- Keep policy.file_handoff plus edge push_files as save-time and runtime guards.
- Do not use sync_mode, workspace sync, tar, or 409 retries. The normal worker
  Bearer token authenticates the input call.
- Revalidate upload-store containment and source relpaths, then prefix every
  destination under inputs/incoming/<run>/<node>/. Never accept a caller-supplied
  destination or a .git/.thclaws path.
- Respect thClaws's smaller input limit in one request: at most 100 files and
  64 MiB decoded (96 MiB JSON body). Do not batch: the upstream API offers no
  transaction/idempotency key. Any failure or ambiguous POST prevents downstream
  dispatch; any residue is confined to the unique, undispatched prefix.
- Require a written[] acknowledgment for every path with matching path, size,
  and SHA-256 before auditing success or creating the downstream job. Do not
  blindly retry an ambiguous input write.

Files:

- atlas/thclaws_client.py: bounded post_inputs method and response parsing.
- atlas/workflows.py: replace tar build/sync push with input preparation,
  acknowledgment validation, and the existing policy/runtime guards.
- atlas/sync_files.py: delete build_push_tar after its final caller is gone.
- scripts/check_file_handoff.py, scripts/gate.sh, worker contract, threat
  model, EN/TH API docs, and PROGRESS.md.

Checks:

- Two mock workers prove A's frozen bytes arrive at B's exact
  inputs/incoming/... files and {files_dir}; no sync request occurs.
- Policy guards, unsafe/out-of-store sources, >100 files, >64 MiB, malformed
  acknowledgments, hash/size/path mismatch, and transport failure all prevent
  downstream dispatch and false-success audits.
- sync_mode=disabled no longer blocks an Artifact-capable handoff; mocks assert
  sync push/trash and tar content types are never used.
- Mutations: remove runtime policy guard, omit acknowledgment hash validation,
  drop the inputs prefix, and permit a second input batch.

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
  (T3: inbound callback; T9a: worker manifest/snapshot bytes; T9b: Atlas
  writes jailed inputs to workers; T7: bundle deploy).
- `PROGRESS.md` gains one close-out row per milestone.
- No worker token or model key in logs, DB values (beyond existing
  `workers.token`), or API responses.

## Risk register

| Risk | Milestone | Mitigation |
|---|---|---|
| Sync endpoints unauthenticated on network binds | T4 / legacy only | hard gate: persistent `workers.sync_mode` enum requires an approved deployment shape; T9 file paths do not call sync |
| Callback endpoint as new inbound attack surface | T3 | pre-auth carve-out is dedicated + minimal: body cap before read, HMAC verify, system actor, idempotent apply, replay = no-op, reaper, threat-model entry |
| Thinking/user text polluting handoff prompts | T2 | `extract_text` scoped to assistant-text events; regression checks |
| Tool payload secrets persisted to SQLite | T2 | structural metadata only (`id`,`name`,`status`,bytes,sha256) — no payloads or previews stored; DB byte-scan check; truncation-is-not-redaction acknowledged |
| Malformed manifest or changed artifact bytes | T9a | validate manifest schema/path/caps; require header, size, and local SHA-256 match before all-or-nothing opaque-store publication |
| Continued session overwrites a prior snapshot | T9a | durable per worker/workspace/session lease covers dispatch through collection/terminalization; recover/release on cancel, failure, and restart |
| Partial or destructive target input write | T9b | one ≤100-file/64-MiB request, unique `inputs/incoming/<run>/<node>/` prefix, verified acknowledgment, no downstream dispatch on failure |
| Bundle deploy as a malware vector | T7 | named publisher keys (`key_id`, 0600 sidecar), admin-only RBAC, allowlisted entries, dry-run, full audit |
| "Rollback" implying file removal it can't do | T7 | verified merge semantics; feature named "re-deploy previous bundle"; replace/prune mode filed upstream |
| Collection racing downstream handoff | T9a | pre-terminal barrier: collection resolves before `succeeded` is written; deadline-bounded in stream and callback modes |
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

### Round 6 (2026-07-05), execution-strategy update

1. **T5/T6 promoted from DEFERRED to APPROVED (tunnel/forward_auth).** The T0
   gate delivered `sync_mode` and the sync endpoints exist in stock thClaws, so
   the tunnel/ForwardAuth path was never actually blocked — only the
   network-reachable-without-tunnel case is. Added the "Sync deployment-shape
   strategy" section (Tier 1 tunnel now / Tier 2 upstream Bearer #178,
   opportunistic / Tier 3 self-patch fallback on a tracked fork, off the
   critical path) and the operator-provisions vs Atlas-verifies-gates-tracks
   division of labor, so Atlas never becomes a tunnel manager and the
   stdlib-only invariant holds.

### Round 7 (2026-07-10), upstream Job Artifact adoption

1. **T5/T6 are superseded as file transports, not erased as history.**
   thClaws v0.88.0 adds Bearer-authenticated, session-scoped output snapshots
   and input placement. T9a/T9b replace the already-shipped sync/tar path
   without changing Atlas's opt-in workflow model or file_ref presentation.
2. **The new contract has two correctness edges that sync did not solve.**
   T9a validates the manifest and independently verifies downloaded snapshot
   bytes; T9b is limited to one upstream input request because inputs have no
   transaction or idempotency key. Both rules are mutation-test requirements.
3. **Callback collection is deliberately explicit.** The existing T3 callback
   terminal transaction cannot contain a network fetch. T9a therefore plans a
   pre-terminal collection phase with its own duplicate/reaper race checks,
   rather than silently keeping callback jobs unsupported.

### Round 8 (2026-07-10), post-T9a hardening, verified against source

1. **The validator rejected every real manifest.** thClaws's `ArtifactManifest`
   declares `skipped` with serde `default, skip_serializing_if = "Vec::is_empty"`
   (`crates/core/src/api_v1/artifacts.rs`), so the key is ABSENT whenever nothing
   was skipped — the normal case. Atlas read `manifest.get("skipped")` and
   required a list, so a real worker's happy path always raised
   `files.collection_failed`; every gate mock sent `"skipped": []`, which is why
   the gate stayed green. Fixed with `manifest.get("skipped", [])` (a present
   non-list still fails); the happy-path mocks in `check_job_artifacts.py` and
   `check_file_handoff.py` now OMIT the key so the real wire shape is
   regression-locked, and a `skipped-non-list` case pins the type gate.
2. **The migration-012 lease invariant was not actually enforced.**
   `claim_session_lease`'s terminal-owner backstop deleted leases without
   checking `collection_inflight`, bypassing within one poll tick the guard
   `apply_job_terminal_result` deliberately leaves in place while a collector is
   mid-download; and the LOSING side of a terminal race (apply returning None)
   cleared neither its inflight flag nor its lease — the unguarded backstop was
   silently doing that job. Fixed as a pair plus a crash backstop: the backstop
   now requires `collection_inflight = 0`; the losing collector clears the flag
   and releases the lease itself (callback and stream paths); an apply WRITE
   failure drops only the flag, keeping the lease so the worker's retry
   re-collects the un-mutated snapshot; and startup
   (`clear_stale_collection_inflight` in `reconcile_jobs`) clears every stale
   flag — no collector thread survives a restart — so the stronger guard cannot
   wedge a session's waiters after a crash mid-collection. New
   `lease_loser_cleanup` check: the guard holds mid-download (a waiter must not
   dispatch) and the loser's handshake un-blocks a real continuation; both
   halves and the skipped fix are mutation-proven red.
3. **Minor hardening.** The lease wait backs off 50 ms → 1 s (each claim runs
   the global terminal-lease DELETE, and a callback job can hold its lease for
   hours); the 5-minute manifest clock-skew tolerance is now
   `ATLAS_ARTIFACT_CLOCK_SKEW_SECONDS` (default unchanged).

## External confirmations outstanding

- thClaws: Job Artifacts and Bearer-gated sync shipped in v0.88.0. T9 adopts
  Job Artifacts; it does not require THCLAWS_SYNC_REQUIRE_AUTH or a tunnel.
  The optional sync flag remains useful only to deployments that independently
  choose whole-workspace mirroring.
- thClaws: `GET /v1/capabilities?workspace_dir=…` (workspace-scoped skills;
  [discussion #179](https://github.com/thClaws/thClaws/discussions/179)).
- thClaws: protocol/schema version field in `/v1/agent/info`
  ([discussion #179](https://github.com/thClaws/thClaws/discussions/179)).
- thClaws: remote cancel endpoint — not planned upstream; Atlas cancel stays
  best-effort.
- thClaws: a replace/prune deploy mode (current deploy merges live + bundle;
  files are never removed) — prerequisite for a true rollback in T7.
- thClaws: a secret-safe tool-payload projection (redacted-at-source event
  payloads with a defined schema) — prerequisite for any persistent payload
  preview in T2's timeline.
