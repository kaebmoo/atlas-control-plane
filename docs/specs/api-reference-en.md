# Atlas API Reference

**English** · [ภาษาไทย](api-reference-th.md) · [OpenAPI 3.1](openapi.yaml)

Status: **Current API specification v1.3**<br>
System baseline: `atlas/app.py` as of 2026-07-01<br>
Default base URL: `http://127.0.0.1:8787`

This document describes the HTTP API implemented by the current Atlas server.
Machine-readable workflow and trigger contracts are available in:

- [Workflow Definition JSON Schema](workflow-definition.schema.json)
- [Workflow Trigger JSON Schema](workflow-trigger.schema.json)
- [Visual Workflow Builder Specification](workflow-visual-builder-spec-en.md)

## 1. Quick start

```bash
BASE_URL=http://127.0.0.1:8787
curl -sS "$BASE_URL/api/health"
```

Response:

```json
{"ok":true,"service":"atlas-control-plane","db":"/path/data/atlas.sqlite","workers":2}
```

The API currently has no `/v1` prefix. Clients should pin the deployed commit or
release and review this specification when contracts change.

## 2. Authentication, CORS, and security

Atlas requires a per-user API token by default. Create the first administrator:

```bash
python3 -m atlas.admin create-admin admin
```

Send the token in a header:

```bash
curl -H 'Authorization: Bearer <token>' "$BASE_URL/api/workers"
```

Or as a query parameter:

```text
GET /api/jobs/{job_id}/events?token=<token>
```

The query token primarily supports browser `EventSource`, which cannot set an
Authorization header. Do not use it for ordinary requests because URLs may be
recorded in logs and history.

Set `ATLAS_LOOPBACK_NO_AUTH=true` explicitly for local development to allow
requests seen as `127.0.0.1` or `::1` without a token. Such a loopback request is
treated as the built-in **admin** identity, so it bypasses every role/permission
(RBAC) check — keep it off in any shared or production deployment. The secure
default is `false`. A configured `ATLAS_API_TOKEN` remains accepted as a legacy
admin token.

Current limitations:

- No built-in TLS; use an HTTPS reverse proxy for remote access.
- CORS uses `Access-Control-Allow-Origin: *` and allows `authorization`,
  `content-type`, and `x-filename` headers.
- Worker tokens use authenticated ciphertext when `ATLAS_SECRET_KEY` is set;
  without it Atlas warns and preserves plaintext compatibility. Responses expose
  only `token_set`.

Identity endpoints:

- `POST /api/auth/login` with `username` and `password` returns a one-time raw
  token response plus public user metadata.
- `POST /api/auth/logout` revokes the current per-user token.
- `GET /api/me` returns the authenticated username and role.
- Admin-only CRUD: `/api/users`, `/api/users/{id}`, `/api/tokens`, and
  `/api/tokens/{id}`. `POST /api/tokens/{id}/revoke` is an additive revoke alias.
- Roles: `viewer` reads normal resources; `operator` runs jobs/workflows and
  decides approvals; `auditor` additionally reads audit and usage data; `admin` has all permissions.

## 3. Request and response conventions

### JSON

- A JSON request body must be an object; root arrays are rejected.
- Use `Content-Type: application/json`.
- Actions without payload may use an empty body or `{}`.
- Response timestamps use ISO 8601 UTC, for example `2026-06-29T10:00:00Z`.
- Server-generated IDs use prefixes such as `wrk_`, `wsp_`, `job_`, `wfd_`,
  `wfr_`, `art_`, `apr_`, `wtr_`, and `usg_`.

### Errors

Every error uses one JSON shape:

```json
{"error":"message"}
```

| HTTP | Meaning |
| --- | --- |
| `400` | Invalid payload, state transition, or reference |
| `401` | Missing or incorrect token |
| `403` | Authenticated role lacks the route permission |
| `404` | Resource or route not found |
| `500` | Exception not converted into a validation error |

### Lists and asynchronous operations

- Most lists accept `?limit=N`, default 100; cursor pagination is not implemented.
- Job/run creation and some approval/trigger actions return `202` before background work finishes.
- Continue with GET, workflow events, or job SSE to observe completion.
- There is no general idempotency key; trigger fire supports `dedupe_key`.

## 4. Endpoint catalog

### System, Fleet, and Routing

| Method | Path | Result |
| --- | --- | --- |
| GET | `/healthz` | Unauthenticated liveness probe (`{ok, service, version}`) |
| GET | `/api/health` | Atlas health (authenticated; includes worker count) |
| GET | `/api/workers` | List workers |
| POST | `/api/workers` | Create/upsert worker |
| POST | `/api/workers/poll` | Poll all workers |
| GET | `/api/workers/{worker_id}` | Get worker |
| DELETE | `/api/workers/{worker_id}` | Delete worker and its workspaces |
| POST | `/api/workers/{worker_id}/poll` | Poll one worker |
| POST | `/api/workers/{worker_id}/sync-mode` | Set sync trust mode (admin); enabling `tunnel`/`forward_auth` runs a pre-enable sync probe — a failed probe returns 400 and leaves the mode unchanged; audited (`worker.sync_mode_changed`) |
| GET | `/api/workspaces` | List workspaces |
| POST | `/api/workspaces` | Create/upsert workspace |
| GET | `/api/workspaces/{workspace_id}` | Get workspace |
| DELETE | `/api/workspaces/{workspace_id}` | Delete workspace |
| GET | `/api/conversations` | 100 most recent conversations |
| POST | `/api/conversations` | Create conversation |
| POST | `/api/routes/resolve` | Preview routing without creating a job |

### Jobs

| Method | Path | Result |
| --- | --- | --- |
| GET | `/api/jobs?limit=100` | List jobs |
| POST | `/api/jobs` | Route and start job (`202`) |
| GET | `/api/jobs/{job_id}` | Job detail |
| POST | `/api/jobs/{job_id}/cancel` | Best-effort cancellation |
| GET | `/api/jobs/{job_id}/events?after=0` | Replay/follow SSE |
| POST | `/api/worker-callbacks/{job_id}` | Worker-only terminal delivery for `execution: "callback"` jobs (signed callback token, not user auth) |

### Workflow definitions and AI builder

| Method | Path | Result |
| --- | --- | --- |
| GET | `/api/workflows` | Definitions |
| POST | `/api/workflows` | Validate and create definition |
| GET | `/api/workflow-templates` | Built-in templates |
| POST | `/api/workflows/draft` | Validated AI draft |
| POST | `/api/workflows/suggest-workers` | Worker suggestions |
| GET | `/api/workflows/{workflow_id}` | Definition detail |
| PUT | `/api/workflows/{workflow_id}` | Validate and update |
| DELETE | `/api/workflows/{workflow_id}` | Delete definition |
| POST | `/api/workflows/{workflow_id}/validate` | Validate preview |
| POST | `/api/workflows/{workflow_id}/explain` | Explain definition |
| POST | `/api/workflows/{workflow_id}/repair` | Unsaved repair preview |
| POST | `/api/workflows/{workflow_id}/suggest-triggers` | Trigger suggestions |

### Solution packs

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/packs` | List available solution packs |
| POST | `/api/packs/import` | Validate a bundle and create its definitions + triggers |
| GET | `/api/packs/{workflow_id}/export` | Export a definition back to a bundle |

Bundle format: [pack-format.md](pack-format.md). Import reuses the workflow graph and
trigger validators (no bypass); an invalid bundle is rejected with a clear error. A
signed bundle is verified with `ATLAS_SECRET_KEY` on import (a tampered signed pack is
rejected); unsigned packs are accepted. `import` requires `workflows.manage`; the reads
require `read`.

### Runs, Artifacts, and Approvals

| Method | Path | Result |
| --- | --- | --- |
| GET | `/api/workflow-runs` | List runs |
| POST | `/api/workflow-runs` | Start run (`202`) |
| GET | `/api/workflow-runs/{run_id}` | Run + nodes + traversed edges + approvals |
| GET | `/api/workflow-runs/{run_id}/events` | Lifecycle events |
| POST | `/api/workflow-runs/{run_id}/pause` | Pause |
| POST | `/api/workflow-runs/{run_id}/resume` | Resume/recovery retry (`202`) |
| POST | `/api/workflow-runs/{run_id}/cancel` | Cancel |
| POST | `/api/workflow-runs/{run_id}/deliver` | Manual (re)send the signed result to `_meta.reply.callback_url` (`202`) |
| GET | `/api/workflow-runs/{run_id}/artifacts` | Run artifacts |
| POST | `/api/workflow-runs/{run_id}/files?key=...` | Upload binary file artifact |
| POST | `/api/artifacts` | Create inline artifact |
| GET | `/api/artifacts/{artifact_id}` | Artifact detail |
| GET | `/api/artifacts/{artifact_id}/content` | Download `file_ref` |
| GET | `/api/approvals` | Approvals with filters |
| POST | `/api/approvals/{approval_id}/approve` | Approve gate (`202`) |
| POST | `/api/approvals/{approval_id}/reject` | Reject and fail run |
| POST | `/api/approvals/{approval_id}/choose` | Choose branch (`202`) |

### Triggers, Audit, and Usage

| Method | Path | Result |
| --- | --- | --- |
| GET | `/api/workflow-triggers` | Trigger list |
| POST | `/api/workflow-triggers` | Create trigger |
| GET | `/api/workflow-triggers/{trigger_id}` | Trigger detail |
| PUT | `/api/workflow-triggers/{trigger_id}` | Update/revalidate |
| DELETE | `/api/workflow-triggers/{trigger_id}` | Delete trigger/events |
| POST | `/api/workflow-triggers/{trigger_id}/fire` | Fire manual/schedule/webhook (`202`) |
| GET | `/api/workflow-triggers/{trigger_id}/events` | Trigger event history |
| GET | `/api/audit?limit=100&from=&to=&format=json\|csv` | Audit log / CSV export |
| GET | `/api/usage?from=&to=&format=json\|csv` | Raw usage ledger (admin/auditor only) |
| GET | `/api/metrics` | Aggregate operational counters (any authenticated role) |

### Deliveries

| Method | Path | Result |
| --- | --- | --- |
| GET | `/api/deliveries?run_id=&status=` | List outbound deliveries (operator/auditor) |
| POST | `/api/deliveries/{delivery_id}/retry` | One bounded manual (re)attempt (operator, `202`) |

## 5. Workers and Workspaces

### Create or update a Worker

`POST /api/workers` upserts by `id` or `base_url`:

```bash
curl -sS -X POST "$BASE_URL/api/workers" \
  -H 'content-type: application/json' \
  -d '{
    "name":"Reporter",
    "base_url":"http://127.0.0.1:4317",
    "token":"worker-secret",
    "role":"reporter",
    "tags":["local","news"]
  }'
```

`base_url` is required. Leave `token` blank during upsert to retain the stored
token. The response never returns the secret:

```json
{"worker":{"id":"wrk_xxx","name":"Reporter","token_set":true,"status":"unknown"}}
```

Saving through the API does not poll automatically. Follow with:

```bash
curl -sS -X POST "$BASE_URL/api/workers/wrk_xxx/poll"
```

Polling returns 200 even when the worker is offline; inspect `status: "offline"`
and `last_error`.

### Create or update a Workspace

```bash
curl -sS -X POST "$BASE_URL/api/workspaces" \
  -H 'content-type: application/json' \
  -d '{
    "worker_id":"wrk_xxx",
    "workspace_key":"atlas",
    "workspace_dir":"/srv/atlas",
    "company":"Example",
    "tags":["backend"]
  }'
```

Required fields are `worker_id`, `workspace_key`, and `workspace_dir`. The path
is interpreted on the worker machine, not the Atlas host.

## 6. Conversations, Routing, and Jobs

### Conversation

```bash
curl -sS -X POST "$BASE_URL/api/conversations" \
  -H 'content-type: application/json' \
  -d '{"title":"News research","workspace_key":"atlas"}'
```

If job creation omits `conversation_id`, Atlas creates a conversation from the
prompt. An existing conversation may bind to an existing thClaws session.

### Preview routing

```bash
curl -sS -X POST "$BASE_URL/api/routes/resolve" \
  -H 'content-type: application/json' \
  -d '{"role":"reporter","workspace_key":"atlas","prompt":"Research AI news"}'
```

Routing precedence is explicit `workspace_id` → explicit `worker_id` →
conversation binding → auto-route by online status, workspace key, company,
tags, role, and prompt hints.

### Start a Job

```bash
curl -sS -X POST "$BASE_URL/api/jobs" \
  -H 'content-type: application/json' \
  -d '{
    "prompt":"Research AI news",
    "role":"reporter",
    "workspace_key":"atlas",
    "model":"optional-model"
  }'
```

The API returns `202` with a `queued` job. Job states are `queued`, `running`,
`cancel_requested`, `succeeded`, `failed`, and `cancelled`.

### Async execution (`execution: "callback"`)

Long-running jobs can run fire-and-forget by adding `execution: "callback"` to
the job request (default is `"stream"`, byte-identical to today; workflow
worker and manager nodes accept the same optional `execution` field):

```json
{"prompt": "Summarize this repo", "worker_id": "wrk_reporter", "execution": "callback"}
```

The worker 202-ACKs and keeps running independently of Atlas's connection; the
job stays `running` with `callback_deadline_at` set. When the run finishes, the
worker POSTs the terminal payload to `POST /api/worker-callbacks/{job_id}`,
authorized by a per-dispatch signed token Atlas minted into the callback
envelope — **not** a user API token (this is the one documented pre-auth
exception; see `docs/specs/threat-model.md`). Atlas applies the result
idempotently: terminal state, `summary` → `assistant_text`, token usage into the
metering ledger, and a structural `callback_result` event (tool **names** and
counters only — tool input/output is never stored). Duplicate deliveries and a
delivery racing the reaper converge to one terminal state (`200` with
`applied: false` on the losing side). Audit rows on this path use the
`system:worker-callback` actor.

Requirements and bounds:

- `ATLAS_PUBLIC_BASE_URL` (the URL workers can reach Atlas on) and
  `ATLAS_SECRET_KEY` must be set, or the request is rejected with `400` — the
  same applies to starting a workflow run whose graph contains callback nodes
  (rejected synchronously, no run is created).
- A job that never calls back is failed by a reaper after
  `ATLAS_CALLBACK_TIMEOUT_SECONDS` (default 3600). The callback token stays
  valid past that deadline long enough to cover the worker's retry envelope
  (3 attempts at ~0/10/60 s) plus clock-skew margin.
- Callback bodies are capped (4 MiB) before reading; bad or expired tokens get
  `401` and leave the job untouched (audited as `job.callback_rejected` when
  the job id is real, rate-limited per job — junk requests write nothing
  durable). A payload whose `run_id` is missing or not exactly the URL's job id
  is rejected with `400`.
- Atlas restarts preserve callback-pending **jobs** — they are running
  remotely, not interrupted — and a late callback after the restart still
  completes them. A **workflow run** whose node dispatched a callback job still
  follows the standard explicit-recovery rule after a restart: the run parks as
  `recovery_required`, the recovery entry is flagged `callback_pending`, and
  the remote job's terminal result lands on the job row. Check that outcome
  before authorizing retry — retry always submits a new job.

### Handoff

```json
{
  "prompt": "Collect source facts",
  "worker_id": "wrk_reporter",
  "handoff": {
    "enabled": true,
    "worker_id": "wrk_writer",
    "prompt": "Write from this result:\n\n{result}"
  }
}
```

A handoff starts only after the source job succeeds with non-empty assistant
text. Supported variables are `{result}`, `{source_prompt}`, and
`{source_job_id}`. Cancellation is best effort; the worker may already have
performed side effects.

### Job SSE

```bash
curl -N "$BASE_URL/api/jobs/job_xxx/events?after=0"
```

Frame:

```text
id: 4
event: text
data: {"text":"hello","seq":4,"created_at":"..."}
```

Common events are `route`, `session`, `state`, `text`, `error`, `done`,
`cancel_requested`, `handoff_configured`, `handoff_started`, `handoff_skipped`,
`handoff_error`, `message`, and `close`. Workers also emit structured events —
`thinking`, `user_message_injected`, `usage`, `result`, and the tool/skill
events `tool_use_start`, `tool_use_result`, `tool_use_denied`, `skill_invoked`,
`skill_invoked_result`. Unknown event names may appear (worker-defined) and are
safe to ignore.

Tool and skill events carry **structural metadata only** — never the tool
`input`/`output` payload (which can hold secrets Atlas cannot detect). Their
`data` is projected to `{id, name, status, input_bytes, output_bytes,
input_sha256, output_sha256}` (byte/hash fields present only when that side had
content). `status` is commonly `started`, `ok`, `error`, or `denied`, but a
worker may report any other value — treat it as an open string. This projection
is applied on read too, so events replayed from older databases never expose a
raw payload:

```text
id: 7
event: tool_use_result
data: {"id":"t1","name":"Bash","status":"ok","output_bytes":20,"output_sha256":"…","seq":7,"created_at":"…"}
```

Use `after=<last_seq>` to resume/replay. When the job is terminal and no events
remain, the server sends `close` and closes the connection.

## 7. Workflow Definitions and AI Builder

### Create a definition

```bash
curl -sS -X POST "$BASE_URL/api/workflows" \
  -H 'content-type: application/json' \
  -d '{
    "name":"Research to writer",
    "graph":{
      "start":"researcher",
      "nodes":[
        {"id":"researcher","type":"worker","role":"researcher","prompt":"Research {input.topic}","outputs":["research"]},
        {"id":"writer","type":"worker","role":"writer","prompt":"Write from {artifact.research}"}
      ],
      "edges":[{"from":"researcher","to":"writer","condition":{"type":"always"}}]
    },
    "policy":{"max_jobs":3,"max_iterations":3}
  }'
```

The backend requires `graph`; name and policy have defaults, but clients should
send the canonical [Workflow Definition Schema](workflow-definition.schema.json).
Before persistence, the server validates graph, policy, worker/workspace
references, and allowlists.

`PUT /api/workflows/{id}` is a partial update, but the merged graph/policy must
remain valid. `DELETE` removes the definition and its triggers. Historical runs
remain, while their `workflow_definition_id` may become null according to the
foreign-key behavior.

### Validate, Explain, and Repair

```bash
curl -sS -X POST "$BASE_URL/api/workflows/wfd_xxx/validate" \
  -H 'content-type: application/json' \
  -d '{"graph":{...},"policy":{...}}'
```

Validate requires a saved workflow ID; omitted fields fall back to saved
values. Explain reads the saved definition and uses a workflow_builder when
configured, otherwise a local explanation. Repair accepts graph/policy/trigger
previews and returns an unsaved draft.

### AI Draft

A worker with role/tag `workflow_builder` is required:

```bash
curl -sS -X POST "$BASE_URL/api/workflows/draft" \
  -H 'content-type: application/json' \
  -d '{"plain_language_prompt":"Create researcher to writer with max 3 jobs"}'
```

AI must return one JSON object, and deterministic validation runs before the API
returns it. The endpoint never automatically saves or runs the draft.

`POST /api/workflows/suggest-workers` works locally without an AI worker and
accepts `{"graph":...,"policy":...}`. Suggestions can reference only real
worker/workspace IDs.

## 8. Workflow Runs and Events

### Start a run

```bash
curl -sS -X POST "$BASE_URL/api/workflow-runs" \
  -H 'content-type: application/json' \
  -d '{"workflow_definition_id":"wfd_xxx","input":{"topic":"AI"}}'
```

The API returns `202`. Run states are `running`, `paused`, `waiting_for_human`,
`recovery_required`, `succeeded`, `failed`, and `cancelled`.

Filter the list:

```text
GET /api/workflow-runs?workflow_definition_id=wfd_xxx&limit=20
```

Run detail contains `run`, runtime `nodes`, traversed `edges`, and `approvals`.
Lifecycle events are a JSON list, not SSE:

```text
GET /api/workflow-runs/wfr_xxx/events?limit=500
```

### Pause, Resume, Recovery, and Cancel

```bash
curl -sS -X POST "$BASE_URL/api/workflow-runs/wfr_xxx/pause"
curl -sS -X POST "$BASE_URL/api/workflow-runs/wfr_xxx/resume" \
  -H 'content-type: application/json' -d '{}'
curl -sS -X POST "$BASE_URL/api/workflow-runs/wfr_xxx/cancel"
```

Ordinary Resume works only from `paused`. A `recovery_required` run requires
explicit acceptance of duplicate-side-effect risk:

```json
{"retry_interrupted":true}
```

## 9. Artifacts and Files

### Inline artifact

```bash
curl -sS -X POST "$BASE_URL/api/artifacts" \
  -H 'content-type: application/json' \
  -d '{
    "run_id":"wfr_xxx",
    "key":"fact_check",
    "kind":"json",
    "content":{"verdict":"approved"},
    "metadata":{"source":"manual"}
  }'
```

Kinds are `text`, `json`, `markdown`, `file_ref`, `summary`, and `decision`.
JSON artifact content is decoded back to an object/list in API responses. Do not
create an inline `file_ref` when download is required; use the file-upload
endpoint.

An optional `classification` field (`public`, `internal`, `confidential`,
`secret`) tags the artifact for data-classification policy; it is validated on
create and stored as `metadata.classification`. Any other value is a `400`.

### File upload

The body is direct binary, not multipart or base64:

```bash
curl -sS -X POST "$BASE_URL/api/workflow-runs/wfr_xxx/files?key=contract" \
  -H 'content-type: application/pdf' \
  -H 'x-filename: contract.pdf' \
  --data-binary @contract.pdf
```

- `key` must match `[A-Za-z_][A-Za-z0-9_.-]{0,127}`.
- `Content-Length` is required; curl provides it automatically.
- Default limit is 10 MiB, configurable through `ATLAS_MAX_UPLOAD_BYTES`.
- The response is a `file_ref` with filename, media_type, size, and SHA-256.
- Upload ties a file to the run; it does not place it in a worker workspace, and workers do not read it automatically.

Download:

```bash
curl -OJ "$BASE_URL/api/artifacts/art_xxx/content"
```

The content endpoint works only for `file_ref` artifacts.

## 10. Approvals

```text
GET /api/approvals?state=pending&run_id=wfr_xxx&limit=100
```

Normal gate:

```bash
curl -sS -X POST "$BASE_URL/api/approvals/apr_xxx/approve"
curl -sS -X POST "$BASE_URL/api/approvals/apr_xxx/reject"
```

A gate with choices requires choose and cannot be approved directly:

```bash
curl -sS -X POST "$BASE_URL/api/approvals/apr_xxx/choose" \
  -H 'content-type: application/json' \
  -d '{"choice":"publish"}'
```

An approval can be decided once, and its run must be `waiting_for_human`.
Reject fails the run.

## 11. Workflow Triggers

### Create a trigger

```bash
curl -sS -X POST "$BASE_URL/api/workflow-triggers" \
  -H 'content-type: application/json' \
  -d '{
    "workflow_definition_id":"wfd_xxx",
    "name":"Every 15 minutes",
    "type":"schedule",
    "config":{"interval_minutes":15},
    "enabled":true
  }'
```

Types/config:

- `manual`: `{}`
- `webhook`: `{}`
- `schedule`: `{"interval_minutes":15}` or `{"daily_time":"09:30"}` in the Atlas host's local timezone
- `workflow_run_completed`: filters `source_workflow_definition_id`, `state`
- `artifact_created`: filters `source_workflow_definition_id`, `key`, `kind`
- `worker_status_changed`: filters `worker_id`, `status`

### Fire and deduplication

```bash
curl -sS -X POST "$BASE_URL/api/workflow-triggers/wtr_xxx/fire" \
  -H 'content-type: application/json' \
  -d '{"payload":{"topic":"AI"},"dedupe_key":"event-001"}'
```

Only manual, schedule, and webhook triggers can be fired directly. Atlas emits
the three internal trigger types. Reusing the same `dedupe_key` returns an
`ignored` event instead of starting another run.

PUT is partial. When type/config changes, the server recalculates
`next_fire_at`. Common trigger-event states are `received`, `started`, `ignored`,
and `failed`.

## 12. Audit

```bash
curl -sS "$BASE_URL/api/audit?limit=100"
```

Each entry contains `action`, `actor`, `resource_type`, `resource_id`, `details`,
and `created_at`. Authenticated requests use the username; explicit loopback
development and background work may use `local`. There is no audit deletion
endpoint (append-only by design).

For compliance hand-off, `from`/`to` (inclusive ISO 8601 date or timestamp) bound
the entries and `format=csv` exports them as CSV — same conventions as
`/api/usage`, same `audit.read` RBAC:

```bash
curl -sS -H 'Authorization: Bearer <token>' \
  "$BASE_URL/api/audit?from=2026-06-01&to=2026-06-30&format=csv"
```

`GET /api/metrics` returns aggregate operational counters for dashboards and
Fleet scraping — counts by state for workers/jobs/workflow runs, definition/
trigger/approval/artifact/usage totals, `schema_version`, `version`, and `time`.
Aggregates only, so it needs just the `read` permission:

```bash
curl -sS -H 'Authorization: Bearer <token>' "$BASE_URL/api/metrics"
```

Retention: artifacts of **terminal** runs can be purged from the CLI
(`python3 -m atlas.admin purge-artifacts --older-than-days N [--dry-run]`);
file_ref bytes are deleted with the rows and the purge itself is audited as
`artifact.purge`. Artifacts may carry an optional `classification` tag
(`public`/`internal`/`confidential`/`secret`) stored in their metadata — see the
Artifacts section.

## 13. Usage Metering and Export

`GET /api/usage` is restricted to `admin` and `auditor`. `from` and `to` accept
ISO 8601 dates or timestamps and are inclusive. `format` defaults to `json` and
also accepts `csv`.

```bash
curl -sS -H 'Authorization: Bearer <token>' \
  "$BASE_URL/api/usage?from=2026-06-01&to=2026-06-30&format=json"
```

The JSON response is:

```json
{
  "usage": [{
    "id": "usg_xxx",
    "idempotency_key": "run:wfr_xxx",
    "kind": "workflow_run",
    "run_id": "wfr_xxx",
    "job_id": null,
    "node_key": null,
    "worker_id": null,
    "actor": "admin",
    "status": "succeeded",
    "units": 3,
    "seconds": 4.0,
    "started_at": "2026-06-29T10:00:00Z",
    "finished_at": "2026-06-29T10:00:04Z",
    "model": null,
    "tokens_prompt": null,
    "tokens_output": null,
    "created_at": "2026-06-29T10:00:04Z",
    "metadata": {"billing_unit":"workflow_run","billable":true}
  }],
  "totals": {
    "workflow_runs": 1,
    "successful_workflow_runs": 1,
    "jobs": 1,
    "budget_units": 3,
    "wall_seconds": 4.0,
    "job_wall_seconds": 3.0,
    "tokens_prompt": 0,
    "tokens_output": 0,
    "estimated_cost_usd": 0.0
  },
  "from": "2026-06-01T00:00:00Z",
  "to": "2026-06-30T23:59:59Z"
}
```

Atlas emits one idempotent `job` event per terminal job (`units=1`) and one
`workflow_run` event per terminal run (`units=budget_units_spent`). The headline
workflow-run count is the number of run events; `metadata.billable` is true only
for successful runs. Model/token fields are visibility-only under BYOK
(`byok_token_counts_billable` stays false): `tokens_prompt`/`tokens_output` are
captured from the worker's `usage` SSE event (thClaws v0.85.0+) with the full
usage payload under `metadata.measures`, and stay null for older workers.
When the effective model is present in the worker's `/v1/models` catalogue,
`metadata` also freezes `effective_model`, its source (`worker` or `requested`),
the USD `pricing_snapshot`, and `estimated_cost_usd`. Partial pricing covers only
token types with a published rate and sets `pricing_partial: true`; tier-billed or
unknown models have no estimate. These values are visibility-only, never billing.
`totals` includes additive token sums and `estimated_cost_usd` read strictly from
the immutable event snapshots; current worker pricing never reprices history.
Metering failures are logged and never change job/run outcomes.

CSV uses one row per raw event with columns `id`, `idempotency_key`, `kind`,
`status`, `units`, `seconds`, `run_id`, `job_id`, `node_key`, `worker_id`,
`actor`, `started_at`, `finished_at`, `model`, `tokens_prompt`, `tokens_output`,
`created_at`, and JSON-encoded `metadata`.

Air-gapped instances can write and verify an HMAC-SHA256 envelope using
`ATLAS_SECRET_KEY`:

```bash
ATLAS_SECRET_KEY='<secret>' python3 -m atlas.usage export usage.json \
  --from 2026-06-01 --to 2026-06-30
ATLAS_SECRET_KEY='<secret>' python3 -m atlas.usage verify usage.json
```

Use `--db /path/to/atlas.sqlite` to override `ATLAS_DB`. Atlas exports raw CDR
source data only; Fleet/NT systems perform later aggregation, rating, and
invoicing.

## 14. Deliveries and the Return Path

OB-1 subscribes an outbound delivery sender to the same `workflow_run_completed`
event the engine already emits internally (see
[Input Adapter Contract §7](input-adapter-contract.md#7-return-path-forward-reference)
and the
[Input Adapter & Return Path Plan](../plans/input-adapter-return-path-plan.md)).
When a run reaches `succeeded` or `failed` and its input carries
`_meta.reply.mode: "webhook"` with a `callback_url`, Atlas POSTs a signed result
to that URL — a failure-isolated side effect that never changes the run's own
outcome.

```bash
curl -sS -H 'Authorization: Bearer <operator-token>' \
  "$BASE_URL/api/deliveries?run_id=wfr_xxx"
curl -sS -X POST -H 'Authorization: Bearer <operator-token>' \
  "$BASE_URL/api/deliveries/dlv_xxx/retry"
curl -sS -X POST -H 'Authorization: Bearer <operator-token>' \
  "$BASE_URL/api/workflow-runs/wfr_xxx/deliver"
```

The signed body:

```json
{
  "delivery_id": "dlv_xxx",
  "run_id": "wfr_xxx",
  "state": "succeeded",
  "correlation_id": "line:U1234:msg-4f2a",
  "artifacts": [{"key": "reply_letter", "kind": "text", "content": "…"}],
  "signed_at": "2026-07-01T09:13:11Z"
}
```

signed with header `X-Atlas-Signature: sha256=<hex>` — HMAC-SHA256 over
`ATLAS_SECRET_KEY`, the same primitive as the signed usage export
([§13](#13-usage-metering-and-export)), computed over the exact bytes POSTed.
A `callback_url` must resolve to a host covered by `ATLAS_OUTBOUND_ALLOWLIST`
(comma-separated hostnames or CIDRs; matched by exact hostname, or by every
resolved address falling inside an allowlisted CIDR). **An empty allowlist
disables outbound delivery entirely** (secure default); a disallowed or
private target is recorded `blocked` and never sent. A missing
`ATLAS_SECRET_KEY` also refuses to send (never unsigned).

`status` is one of `pending`, `delivered`, `failed`, or `blocked`. A failed
attempt is retried with a short bounded backoff up to
`ATLAS_OUTBOUND_MAX_ATTEMPTS` (default 5, `ATLAS_OUTBOUND_TIMEOUT` seconds per
attempt, default 10) before being dead-lettered as `failed`. `delivery_id` is
stable across every retry of the same delivery so a receiver can dedupe.
`POST /api/deliveries/{delivery_id}/retry` gives a `failed` or `blocked`
delivery one more bounded attempt (re-validating the callback against the
current allowlist); `POST /api/workflow-runs/{run_id}/deliver` (re)sends using
the run's own `_meta.reply.callback_url` regardless of the original `mode`.
Both routes require the run to have already completed.

If `_meta.reply` is absent or `mode: "none"`, the adapter instead polls
`GET /api/workflow-runs/{run_id}` until terminal, then reads
`GET /api/workflow-runs/{run_id}/artifacts`.

## 15. OpenAPI 3.1

[openapi.yaml](openapi.yaml) defines 58 paths and 77 operations, including
security schemes, parameters, request bodies, response wrappers, and schema
references. It can drive Swagger UI, Redoc, code generation, or contract tests.

Workflow and trigger schemas in OpenAPI use the canonical client shape, which is
stricter than the backend in a few places. The backend may default omitted
fields, but new clients should emit canonical form for stable validation and
round trips.

OpenAPI does not replace workflow semantic validation such as duplicate node
IDs, cycle guards, manager/human edge coupling, quorum, or live worker/workspace
references. See the
[Visual Workflow Builder Specification](workflow-visual-builder-spec-en.md).

## 16. API client checklist

- Set timeouts for JSON requests, but do not use a short timeout for SSE.
- Persist the latest SSE `seq` and reconnect with `after`.
- Check HTTP status before reading a success shape.
- Never log Authorization/query tokens or worker tokens.
- Retry POST carefully because there is no general idempotency support.
- Use trigger `dedupe_key` when an external event may be retried.
- Treat cancellation/recovery as side-effect-sensitive operations.
- Validate workflow/trigger schemas and let the server validate again.
- Never assume file upload makes a file readable by a worker.
