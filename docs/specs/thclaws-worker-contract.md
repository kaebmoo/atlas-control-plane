# thClaws Worker Protocol Contract

This document pins the HTTP behavior Atlas may rely on when talking to a
`thclaws --serve` worker. It is a compatibility contract for Atlas, not an
upstream thClaws specification.

## Tested build

- Runtime-reported version: `0.89.0` (includes the v0.88.0 Job Artifact API)
- Source commit: `bf1d6bb` (local `main`, 2026-07-10)
- Built and probed: 2026-07-10
- Earlier source survey used by the adoption plan: `e481015` (v0.85.0)

thClaws exposes no protocol/schema version, so Atlas must treat any other
reported engine version as unverified rather than assuming SemVer compatibility.

## Endpoint and authentication contract

The matrix below describes plain single-tenant `--serve`. In `--multiuser`
mode, thClaws wraps the entire router in its outer HMAC identity middleware:
every route below except `/healthz` requires that deployment identity **in
addition to** any `AuthOk` Bearer requirement. Atlas does not treat a worker
Bearer token alone as sufficient for a multiuser worker.

| Endpoint | Atlas use | Authentication Atlas may rely on |
|---|---|---|
| `GET /healthz` | Liveness | Public health surface; no Bearer requirement |
| `POST /agent/run` | Stream, synchronous run, or `x_callback` dispatch | `THCLAWS_API_TOKEN` Bearer (`AuthOk`) |
| `GET /v1/agent/info` | Daemon capability snapshot | Bearer (`AuthOk`) |
| `GET /v1/models` | Model catalogue and pricing | Bearer (`AuthOk`) |
| `POST /v1/chat/completions` | Benchmark-gated preview surface | Bearer (`AuthOk`) |
| `POST /v1/deploy`, `/v1/deploy/files`, `/v1/deploy/manifest` | Bundle deployment | Bearer (`AuthOk`) |
| `POST /v1/restart` | Explicit worker restart | Bearer (`AuthOk`) |
| `/workspace/sync/stat|pull|push|manifest|export|trash` | Advisory busy state and gated file transfer | **Not protected by `THCLAWS_API_TOKEN` in plain single-tenant `--serve`**; protection must come from an approved tunnel, hosted ForwardAuth, or the multiuser HMAC layer |

For Bearer-protected routes, an unset/empty `THCLAWS_API_TOKEN` disables the API
with `404`; a real token requires an exact `Authorization: Bearer ...` match and
returns `401` otherwise. `THCLAWS_API_TOKEN=disable-auth` is permitted only on a
loopback bind.

The sync routes share `/upload`'s outer deployment-auth surface. Sending the
worker Bearer token to a sync route does not authenticate it. A network-reachable
plain `--serve` listener therefore exposes sync to any caller that can reach the
socket.

## Live probe findings

The same freshly-built binary and a non-secret test token were used for both
listener configurations.

| Listener configuration | Request | Without Bearer | With Bearer | Finding |
|---|---|---:|---:|---|
| `127.0.0.1` | `GET /workspace/sync/stat` | `200` | `200` | Worker Bearer is ignored by sync |
| `127.0.0.1` | `POST /v1/deploy/manifest` | `401` | `200` | `/v1/deploy/*` enforces Bearer |
| `0.0.0.0` | `GET /workspace/sync/stat` | `200` | `200` | Same router behavior on a LAN-bound configuration |
| `0.0.0.0` | `POST /v1/deploy/manifest` | `401` | `200` | Bearer remains enforced on `/v1/*` |

The LAN-bound process was successfully probed through the host's local socket.
A connection through the physical LAN interface timed out before any HTTP
response, consistent with a host/network firewall; that timeout is not counted
as thClaws authentication evidence.

## Sync capability gate

Atlas must persist an operator-owned `workers.sync_mode` column. Allowed values:

- `disabled` — default; Atlas makes no sync request.
- `tunnel` — the worker sync socket is reachable only through an operator-approved
  private/SSH tunnel.
- `forward_auth` — a trusted ingress authenticates the request before it reaches
  thClaws.

Changing the mode is an authenticated Atlas admin action and emits
`worker.sync_mode_changed` with actor and old/new values. The setting must not be
stored in `agent_info`, because polling replaces that blob.

Enabling `tunnel` or `forward_auth` requires both an operator assertion of the
deployment shape and a successful `/workspace/sync/stat` probe through that same
Atlas client path. The admin transition probes first and persists the new mode
only after validating the response; failure leaves the old mode unchanged. The
probe verifies reachability and response shape; it does **not** prove that the
worker Bearer protects sync. Atlas never auto-detects or auto-enables a mode,
and never falls back to sync when the mode is `disabled`.

This admin pre-enable probe is the only sync request permitted while the
persisted mode is `disabled`. It is an explicit, authenticated transition
operation, not a poll or job execution fallback.

Consumers apply the gate as follows:

- T4's normal poll probes `stat` only when `sync_mode != disabled`; any error becomes
  `busy: null` and never blocks routing.
- T5/T6 check the persisted mode immediately before every sync network call.
  A disabled worker produces a skipped event and no request.
- If upstream adds Bearer auth to sync, Atlas may add a separate `bearer` mode
  after version/capability verification; existing modes do not silently change.

## Busy behavior

`GET /workspace/sync/stat` returns a process-wide `busy` snapshot. It is advisory
and may race dispatch. `pull`, `push`, `manifest`, `export`, and `trash` return
`409 Conflict` with `workspace busy (active turn)` while an agent turn is active.
This matters only to T4's advisory `stat` probe and operator-managed legacy sync
use: Atlas no longer calls any sync data route (T9a collects via the Bearer Job
Artifact routes, T9b hands off via `POST /v1/inputs` — neither is busy-gated,
and the client's sync export/push methods were deleted with their callers), so
Atlas retries no `409` anywhere on this surface.

## Job Artifacts (v0.88.0+)

`POST /agent/run` accepts `collect_files: [glob, ...]`. thClaws copies matching
regular files into a session-scoped snapshot when the turn completes and serves
that immutable snapshot through Bearer-authenticated routes:

| Endpoint | Response | Atlas rule |
|---|---|---|
| `GET /v1/sessions/{sid}/artifacts?workspace_dir=...` | JSON manifest with `session_id`, `patterns`, `artifacts[]`, and optional `skipped[]` | Validate ids, jailed relative paths, sizes, lowercase SHA-256 values, unique members, and the 256-file/300-MiB caps before downloading anything. Any `skipped[]` is an explicit failure-isolated partial result. |
| `GET /v1/sessions/{sid}/artifacts/{aid}?workspace_dir=...` | Frozen bytes with `x-sha256` | Require the header, exact manifest length, and an independently calculated SHA-256 to match before staging. |
| `POST /v1/inputs` — body `{workspace_dir?, files: [{path, content_base64}]}` | `{workspace_dir, written: [{path, size, sha256}]}` | T9b handoff. Caps: 100 files, 64 MiB decoded (96 MiB JSON body). Destinations must sit under an allowed prefix (default `inputs/`; `..`/absolute/`.git`/`.thclaws` always rejected). Files are written ONE AT A TIME with no transaction or idempotency key — Atlas pre-validates the whole batch, sends exactly ONE request per handoff edge, never retries (409s included), and requires `written[]` to cover the exact sent set with matching size and SHA-256 before any success audit or downstream dispatch. |

The session id is emitted in the initial SSE `session` event or the async 202
ACK. `workspace_dir` must be the same resolved workspace used for dispatch.
The snapshot is not a view of the mutable workspace, and an empty manifest with
no `skipped[]` is a valid zero-file result. Atlas never uses
`/workspace/sync/export` for Job Artifact collection and never consults
`sync_mode` for this path. Collection is failure-isolated and completes before
Atlas writes `succeeded`; no partial rows or blobs are published.

## `/agent/run` SSE contract

Atlas recognizes these named events from `api_v1/agent.rs`:

- `session`
- `text`
- `thinking`
- `tool_use_start`, `tool_use_result`, `tool_use_denied`
- `skill_invoked`, `skill_invoked_result` (computed when the tool name is
  `Skill`, not a separately hard-coded emitter path)
- `user_message_injected`
- `usage`
- `result`
- `error`

A successful stream ends with an unnamed `data: [DONE]` sentinel. Unknown event
names must remain forward-compatible: Atlas stores/renders them generically and
must not crash. Tool and skill `input`/`output` payloads are never persisted by
Atlas; only the structural projection defined in the threat model is stored.

### `usage` field semantics (pinned for cost estimation)

The api_v1 layer maps `prompt_tokens` from the canonical `Usage.input_tokens`
(`api_v1/agent.rs`, `chat.rs`, `callback.rs`), and every provider normalizes
that field to the **UNCACHED input portion** before it gets there:
`providers/openai_responses.rs` subtracts `cached_tokens` explicitly
("Subtract cached from total_input so the canonical `Usage.input_tokens` is
the UNCACHED new portion"), and Anthropic's native `input_tokens` already
excludes cache reads/writes. `completion_tokens` **INCLUDES** the
`reasoning_output_tokens` subset (OpenAI reports reasoning inside
`output_tokens`; the provider passes it through verbatim).

Consequences for any consumer pricing these fields (Atlas
`_estimate_cost_usd`):

- price `prompt_tokens` **as-is** — subtracting `cached_input_tokens` again
  double-discounts the cache;
- price `max(0, completion_tokens - reasoning_output_tokens)` at the output
  rate and `reasoning_output_tokens` at the reasoning rate (falling back to
  the output rate) — pricing the full completion **plus** reasoning counts
  those tokens twice.

NOTE: thClaws' own `compute_cost_usd` documents the opposite convention for
its internal `TokenUsage` ("prompt includes cached") and its ledger feeds
these same api_v1-shaped fields into it — a known upstream inconsistency.
Atlas deliberately prices by the wire semantics above, not by that port.

## `x_callback` async dispatch and callback contract

`POST /agent/run` accepts an optional `x_callback` object (`api_v1/agent.rs`,
`callback.rs`). When present, the worker runs fire-and-forget: it replies with a
`202` ACK immediately and later delivers the terminal result to the Atlas URL.

- **Request envelope** (Atlas → worker): `x_callback` is an **object**
  `{url, api_key, run_id}` — NOT a bare string. `idempotency_key` defaults to
  `run_id` upstream, so Atlas omits it and uses `run_id` (= the Atlas job id) as
  the idempotency key.
- **202 ACK** (worker → Atlas, synchronous): a JSON object
  `{run_id, session_id, status: "accepted", ...}`. Atlas treats **only** a `202`
  whose body echoes `status: "accepted"` and the same `run_id` as an accepted
  dispatch; any other 2xx, a mismatched echo, or an unreadable ACK is not a clean
  dispatch.
- **Terminal delivery** (worker → Atlas, later): `POST <url>` carrying
  `Authorization: Bearer <api_key>` and the `CallbackPayload` body. Delivery is
  best-effort: **3 attempts at ~0/10/60 s** backoff (30 s per-attempt request
  timeout), and the worker **gives up on any non-`429` 4xx** — so Atlas answers
  an unverifiable/duplicate delivery with `200`/`503` (retryable), never a 4xx
  that would strand a real result.
- **`CallbackPayload` wire shape** (pinned against `callback.rs`):

  | Field | Type | Atlas use |
  |---|---|---|
  | `run_id` | string | MUST equal the Atlas job id in the URL, else `400` |
  | `status` | enum **`succeeded` \| `failed` \| `cancelled`** | terminal job state |
  | `finish_reason` | string (`stop`/`length`/`tool_calls`/`error`) | structural event only |
  | `summary` | string (may be empty) | appended to `assistant_text` |
  | `usage` | object (`prompt_tokens`, `completion_tokens`, `cached_input_tokens`, `cache_creation_input_tokens`, `reasoning_output_tokens`, …) | metering ledger (same tolerant rules as the streamed `usage` event) |
  | `tool_calls`, `tool_denials` | string[] (NAMES only) | structural `callback_result` event |
  | `iterations` | integer | structural `callback_result` event |
  | `error` | object `{code, message}` \| null | failure message (on non-success) |

  **`status` is the load-bearing field.** Atlas recognizes exactly the enum
  above. `succeeded`/`cancelled` map straight to the terminal state; `failed`
  (or an `error.message`) fails the job. Any value **outside** the enum — worker
  drift or an additive upstream status — is mapped to `failed` (never silently
  `succeeded`) and surfaced verbatim as
  `worker reported unrecognized terminal status: <value>`, so a vocabulary
  mismatch is loud and diagnosable. If upstream adds or renames a terminal
  status, THIS is the line Atlas must be re-verified against and
  `_CALLBACK_TERMINAL_STATUSES` (`atlas/jobs.py`) updated.

## Outstanding upstream contract requests

- Bearer authentication for `/workspace/sync/*`.
- `GET /v1/capabilities?workspace_dir=...` for workspace-scoped skills.
- A protocol/schema version field in `/v1/agent/info`.

The sync/artifact request is recorded locally in
[`../upstream/thclaws-artifact-api-idea.md`](../upstream/thclaws-artifact-api-idea.md)
and filed as [thClaws discussion #178](https://github.com/thClaws/thClaws/discussions/178).
The capability/version request is recorded in
[`../upstream/thclaws-capabilities-contract-idea.md`](../upstream/thclaws-capabilities-contract-idea.md)
and filed as [discussion #179](https://github.com/thClaws/thClaws/discussions/179).
Atlas keeps sync disabled by default until an approved deployment shape or a
future authenticated upstream surface satisfies the gate.
