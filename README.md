# Atlas Control Plane

Atlas is a standalone control plane for coordinating many `thclaws --serve`
workers from one browser dashboard.

It is intentionally separate from thClaws. Atlas does not patch, fork, or depend
on private thClaws internals. Each machine runs thClaws as a worker runtime, and
Atlas talks to the HTTP APIs thClaws already exposes today:

- `GET /healthz`
- `GET /v1/agent/info`
- `POST /agent/run`

The goal is to make one operator-facing surface that can route work to the right
machine, stream results live, preserve job/session history, and chain workers
together into practical multi-agent workflows.

## Current Status

Atlas currently supports:

- Per-instance users, admin/operator/viewer/auditor RBAC, per-user API tokens,
  dashboard login/logout, and authenticated audit actors.
- Authenticated encryption for worker tokens when `ATLAS_SECRET_KEY` is set.
- Multiple thClaws workers, one per machine or runtime.
- Workspace mapping per worker.
- Worker polling and capability snapshots.
- Manual routing by worker or workspace.
- Auto routing by conversation binding, worker status, workspace key, company,
  tags, role, and prompt hints.
- Job creation, status tracking, streaming output, event replay, and audit log.
- SQLite persistence with no external database requirement.
- Dashboard UI for workers, workspaces, jobs, live streams, audit, and setup.
- Best-effort cancellation at the Atlas layer.
- Single-step handoff: when job A succeeds, Atlas can automatically start job B
  on another worker and pass job A's result into job B's prompt.
- Workflow definitions, workflow runs, condition edges, guarded loops,
  fan-out, and `all`/`any` joins without worker jobs for join nodes.
- Text/JSON artifacts, manual artifact APIs, webhook and internal event triggers,
  trigger dedupe, and trigger event history.
- Workflow run lifecycle events, pause/resume/cancel controls, and runtime
  worker/time policy enforcement. Resume skips completed nodes.
- Human approval gates, approval audit events, and iteration-based approval
  policy enforcement without creating worker jobs for gates.
- Bounded manager nodes that propose next actions as strict JSON while Atlas
  validates edges, artifacts, routes, workspaces, and execution guards.
- A validated workflow builder for draft, explain, repair, and trigger
  suggestions, with simple node/condition/trigger forms and raw JSON editing.
- Four built-in workflow templates that copy into the editor without saving.
- Policy form/JSON synchronization, non-saving Explain/Repair previews, trigger
  enable/disable controls, and validated worker suggestions.
- Integer budget-unit enforcement, configurable failure continuation, human
  branch choices, and quorum joins.
- Bounded binary file artifacts with SHA-256 metadata and secure downloads.
- Explicit restart recovery that never retries an interrupted worker job
  without operator authorization.

This is enough to run simple real workflows such as:

```text
Reporter worker -> Anchor worker
Research worker -> Writer worker
Coder worker    -> Reviewer worker
```

The workflow implementation checklist is in
[docs/plans/workflow-engine-coding-plan.md](docs/plans/workflow-engine-coding-plan.md).

## User Documentation

- [Documentation Index](docs/README.md)
- [คู่มือใช้งานผ่านเว็บ (ภาษาไทย)](docs/guides/web-user-guide-th.md)
- [Web User Guide (English)](docs/guides/web-user-guide-en.md)
- [API Reference](docs/specs/api-reference-en.md) ·
  [ภาษาไทย](docs/specs/api-reference-th.md) · [OpenAPI](docs/specs/openapi.yaml)
- [Visual Workflow Builder Specification](docs/specs/workflow-visual-builder-spec-en.md) ·
  [ภาษาไทย](docs/specs/workflow-visual-builder-spec-th.md)
- [Workflow Examples](docs/workflow-examples.md)
- [Demo Script](docs/demo-script.md)
- [Workflow Engine Coding Plan](docs/plans/workflow-engine-coding-plan.md)

## Core Concepts

### Worker

A worker is one running thClaws server:

```text
Local thClaws       -> http://127.0.0.1:4317
Local thClaws 2     -> http://127.0.0.1:4318
Company Mac         -> http://100.x.y.z:4317
```

Workers have:

- `name`: human-friendly label.
- `base_url`: thClaws serve URL.
- `token`: thClaws `THCLAWS_API_TOKEN`.
- `role`: what this worker is good at, such as `reporter`, `anchor`, `coder`.
- `tags`: routing hints, such as `personal`, `news`, `finance`, `company-a`.

### Workspace

A workspace is a project directory on a specific worker.

The same `workspace_key` may exist on multiple workers:

```text
Worker A -> workspace_key: thclaws -> /Users/seal/Documents/GitHub/thClaws
Worker B -> workspace_key: thclaws -> /home/user/thClaws
```

The `workspace_dir` is resolved on the worker machine, not on the Atlas machine.

Workers do not strictly require workspaces. If Atlas runs a job against a worker
without a workspace, thClaws uses the directory where `thclaws --serve` was
started. In practice, each worker should have at least one default workspace so
routing stays explicit and debuggable.

### Job

A job is one execution request routed to one worker. Atlas stores:

- prompt
- selected worker and workspace
- state
- thClaws session id when available
- streamed text
- event log
- errors
- parent/child handoff relationship

### Conversation

A conversation lets Atlas continue against the same thClaws session. Existing
conversation bindings are preferred before broad auto routing.

### Handoff

A handoff is a one-step chain:

```text
Job A succeeds -> Atlas creates Job B
```

The child prompt can include:

- `{result}`: assistant output from the source job.
- `{source_prompt}`: original prompt of the source job.
- `{source_job_id}`: source job id.

Example:

```text
You are a news anchor.
Read this report as a clear broadcast script.

{result}
```

## Requirements

- Python 3.11+ recommended. The current local environment uses Python 3.14.
- thClaws built or installed on each worker machine.
- Network reachability from Atlas to each worker URL.
- `THCLAWS_API_TOKEN` configured on each thClaws worker.

No Python package installation is required. Atlas uses only the Python standard
library and browser-native HTML/CSS/JavaScript.

## Run Atlas

```bash
cd /Users/seal/Documents/GitHub/atlas-control-plane
python3 -m atlas --host 127.0.0.1 --port 8787
```

Or:

```bash
cd /Users/seal/Documents/GitHub/atlas-control-plane
./scripts/run.sh
```

Open:

```text
http://127.0.0.1:8787
```

SQLite state is stored at:

```text
./data/atlas.sqlite
```

`data/` is ignored by Git because it may contain local worker URLs, cached
capabilities, job output, and audit history.

## Start thClaws Workers

On each machine, start thClaws with a token and a unique port.

Worker 1:

```bash
cd /Users/seal/Documents/GitHub/thClaws

THCLAWS_API_TOKEN="dev-token-1" \
thclaws --serve --bind 127.0.0.1 --port 4317
```

Worker 2:

```bash
cd /Users/seal/Documents/GitHub/thClaws

THCLAWS_API_TOKEN="dev-token-2" \
thclaws --serve --bind 127.0.0.1 --port 4318
```

If `thclaws` is not installed in `PATH`, run from the thClaws repository:

```bash
cd /Users/seal/Documents/GitHub/thClaws

THCLAWS_API_TOKEN="dev-token-1" \
cargo run --features gui --bin thclaws -- \
  --serve --bind 127.0.0.1 --port 4317
```

Health check:

```bash
curl http://127.0.0.1:4317/healthz
```

Expected response:

```text
ok
```

Capability check:

```bash
curl -H "Authorization: Bearer dev-token-1" \
  http://127.0.0.1:4317/v1/agent/info
```

## Add Workers In The Dashboard

Open Atlas and go to the **Fleet** view in the left navigation:

1. Click `Add worker` in the Workers card.
2. Fill:

```text
Name:     Local thClaws
Base URL: http://127.0.0.1:4317
Token:    dev-token-1
Role:     reporter
Tags:     local,news,reporter
```

3. Save.

Atlas automatically polls the worker after save. If the worker is reachable and
the token is correct, status becomes `online`.

Add another worker:

```text
Name:     Local thClaws 2
Base URL: http://127.0.0.1:4318
Token:    dev-token-2
Role:     anchor
Tags:     local,news,anchor
```

When editing an existing worker, leave the token blank to keep the stored token.

## Add Workspaces

In the **Fleet** view, click `Add workspace` in the Workspaces card.

Example:

```text
Worker:    Local thClaws
Key:       thclaws
Directory: /Users/seal/Documents/GitHub/thClaws
Company:   Personal
Tags:      local,rust,agents
```

Then add the same key for another worker if needed:

```text
Worker:    Local thClaws 2
Key:       thclaws
Directory: /Users/seal/Documents/GitHub/thClaws
Company:   Personal
Tags:      local,anchor
```

The same workspace key can be reused across different workers. Atlas stores the
unique record as `(worker_id, workspace_key)`.

## Run A Simple Job

In the **Command** view:

1. Enter a prompt.
2. Optionally select a Worker or Workspace.
3. Click `Run`.

If both Worker and Workspace are left on Auto, Atlas resolves a route by:

1. explicit workspace, if selected
2. explicit worker, if selected
3. existing conversation/session binding
4. scored candidates using worker status, workspace key, company, tags, role,
   and prompt hints

For testing, select a concrete Workspace to avoid ambiguity.

## Reporter To Anchor Handoff

This is the first supported workflow pattern.

Example setup:

```text
Local thClaws    role=reporter
Local thClaws 2  role=anchor
```

In the **Command** view:

1. Select `Local thClaws` as Worker.
2. Prompt:

```text
Find one concise technology news item and summarize the facts.
```

3. Enable `Hand off after success`.
4. Select `Local thClaws 2` under `Send to worker`.
5. Keep or edit the Handoff prompt:

```text
You are a news anchor.
Turn the reporter's notes into a broadcast-ready script.
Do not add facts that are not in the source.

{result}
```

6. Click `Run`.

When the reporter job succeeds, Atlas creates a child job on the anchor worker.
The Jobs list shows:

```text
parent job: handoff -> job_xxx
child job:  child of job_yyy
```

## API Examples

### Add Worker

```bash
curl -sS -X POST http://127.0.0.1:8787/api/workers \
  -H 'content-type: application/json' \
  -d '{
    "name": "Local thClaws",
    "base_url": "http://127.0.0.1:4317",
    "token": "dev-token-1",
    "role": "reporter",
    "tags": "local,news,reporter"
  }'
```

### Poll Worker

```bash
curl -sS -X POST http://127.0.0.1:8787/api/workers/wrk_xxx/poll
```

### Add Workspace

```bash
curl -sS -X POST http://127.0.0.1:8787/api/workspaces \
  -H 'content-type: application/json' \
  -d '{
    "worker_id": "wrk_xxx",
    "workspace_key": "thclaws",
    "workspace_dir": "/Users/seal/Documents/GitHub/thClaws",
    "company": "Personal",
    "tags": "rust,agents"
  }'
```

### Submit A Handoff Job

```bash
curl -sS -X POST http://127.0.0.1:8787/api/jobs \
  -H 'content-type: application/json' \
  -d '{
    "prompt": "Find one concise technology news item.",
    "worker_id": "wrk_reporter",
    "handoff": {
      "enabled": true,
      "worker_id": "wrk_anchor",
      "prompt": "You are a news anchor. Read this as a broadcast script.\\n\\n{result}"
    }
  }'
```

### Stream Job Events

```bash
curl -N http://127.0.0.1:8787/api/jobs/job_xxx/events?after=0
```

### Create A Workflow

```bash
curl -sS -X POST http://127.0.0.1:8787/api/workflows \
  -H 'content-type: application/json' \
  -d '{
    "name": "Reporter to Anchor",
    "graph": {
      "start": "reporter",
      "nodes": [
        {
          "id": "reporter",
          "type": "worker",
          "worker_id": "wrk_reporter",
          "prompt": "Find facts about: {input.topic}",
          "outputs": ["notes"]
        },
        {
          "id": "anchor",
          "type": "worker",
          "worker_id": "wrk_anchor",
          "prompt": "Turn this into a script: {artifact.notes}",
          "outputs": ["script"]
        }
      ],
      "edges": [
        {"from": "reporter", "to": "anchor", "condition": {"type": "always"}}
      ]
    },
    "policy": {"max_jobs": 5}
  }'
```

### Run A Workflow

```bash
curl -sS -X POST http://127.0.0.1:8787/api/workflow-runs \
  -H 'content-type: application/json' \
  -d '{
    "workflow_definition_id": "wfd_xxx",
    "input": {"topic": "technology news"}
  }'
```

Artifacts are named workflow-run results. A worker node creates one when it
declares `outputs`; later nodes read it with `{artifact.KEY}`. They are distinct
from the raw job output shown in Jobs. See the
[artifact reference](docs/concepts-en.md#9-artifact-kinds) or
[คำอธิบายภาษาไทย](docs/concepts-th.md#9-ชนิด-artifact).

List artifacts for a run:

```bash
curl -sS http://127.0.0.1:8787/api/workflow-runs/wfr_xxx/artifacts
```

Create a manual JSON artifact:

```bash
curl -sS -X POST http://127.0.0.1:8787/api/artifacts \
  -H 'content-type: application/json' \
  -d '{"run_id":"wfr_xxx","key":"invoice","kind":"json","content":{"total":3}}'
```

### Approve Or Reject A Human Gate

Add a control-plane node to the workflow graph:

```json
{
  "id": "publish_approval",
  "type": "human_gate",
  "label": "Approve publication",
  "reason": "Review the final artifact before publishing"
}
```

When execution reaches the gate, Atlas creates no worker job. The run changes
to `waiting_for_human` and the dashboard shows `Approve` and `Reject` controls.
The same actions are available through the API:

```bash
curl -sS 'http://127.0.0.1:8787/api/approvals?state=pending'

curl -sS -X POST http://127.0.0.1:8787/api/approvals/apr_xxx/approve
curl -sS -X POST http://127.0.0.1:8787/api/approvals/apr_xxx/reject
```

Approval resumes the staged outgoing edges. Rejection fails the run. Repeated
decisions return an error and do not schedule downstream nodes again.

Choice gates declare `choices` and use `human_selected` edges. Decide them with
`POST /api/approvals/{id}/choose` and `{"choice":"publish"}`.

### Run A Manager Node

A `manager` node uses a normal worker job but must return only the
`manager_decision_v1` JSON contract:

```json
{
  "stop": false,
  "reason": "Research is ready for writing.",
  "next": [
    {
      "node": "writer",
      "input_artifacts": ["research"],
      "instructions": "Write a concise draft."
    }
  ]
}
```

Manager outgoing edges use `manager_selected`, with `target` equal to the edge
destination. Atlas rejects the whole proposal before downstream scheduling if
any target, edge, artifact, worker/workspace route, or execution guard is
invalid. Accepted and rejected decisions are visible in run events, audit, and
the **Monitor** view's Manager decisions card.

### Draft A Workflow

Configure a worker with role or tag `workflow_builder`, then:

```bash
curl -sS -X POST http://127.0.0.1:8787/api/workflows/draft \
  -H 'content-type: application/json' \
  -d '{"plain_language_prompt":"Build a reporter -> fact checker -> anchor workflow"}'
```

### Create And Fire A Trigger

```bash
curl -sS -X POST http://127.0.0.1:8787/api/workflow-triggers \
  -H 'content-type: application/json' \
  -d '{
    "workflow_definition_id": "wfd_xxx",
    "name": "Manual publish",
    "type": "manual"
  }'
```

```bash
curl -sS -X POST http://127.0.0.1:8787/api/workflow-triggers/wtr_xxx/fire \
  -H 'content-type: application/json' \
  -d '{"payload":{"topic":"technology news"},"dedupe_key":"manual-001"}'
```

Interval schedules use `{"interval_minutes": 15}`. Daily local schedules use
`{"daily_time": "09:30"}`.

Trigger types are `manual`, `schedule`, `webhook`,
`workflow_run_completed`, `artifact_created`, and `worker_status_changed`.
Webhook callers use the same `/fire` endpoint with a stable `dedupe_key`.
Internal event triggers are fired by Atlas and cannot be fired manually.

## API Surface

- `GET /api/health`
- `GET /api/workers`
- `POST /api/workers`
- `POST /api/workers/poll`
- `GET /api/workers/{id}`
- `POST /api/workers/{id}/poll`
- `DELETE /api/workers/{id}`
- `GET /api/workspaces`
- `POST /api/workspaces`
- `GET /api/workspaces/{id}`
- `DELETE /api/workspaces/{id}`
- `GET /api/conversations`
- `POST /api/conversations`
- `POST /api/routes/resolve`
- `GET /api/jobs`
- `POST /api/jobs`
- `GET /api/jobs/{id}`
- `GET /api/jobs/{id}/events`
- `POST /api/jobs/{id}/cancel`
- `GET /api/workflows`
- `POST /api/workflows`
- `POST /api/workflows/draft`
- `POST /api/workflows/suggest-workers`
- `GET /api/workflows/{id}`
- `PUT /api/workflows/{id}`
- `DELETE /api/workflows/{id}`
- `POST /api/workflows/{id}/validate`
- `POST /api/workflows/{id}/explain`
- `POST /api/workflows/{id}/repair`
- `POST /api/workflows/{id}/suggest-triggers`
- `GET /api/workflow-templates`
- `GET /api/workflow-runs`
- `POST /api/workflow-runs`
- `GET /api/workflow-runs/{id}`
- `GET /api/workflow-runs/{id}/artifacts`
- `POST /api/workflow-runs/{id}/files?key=...`
- `GET /api/workflow-runs/{id}/events`
- `POST /api/workflow-runs/{id}/pause`
- `POST /api/workflow-runs/{id}/resume`
- `POST /api/workflow-runs/{id}/cancel`
- `GET /api/approvals`
- `POST /api/approvals/{id}/approve`
- `POST /api/approvals/{id}/reject`
- `POST /api/approvals/{id}/choose`
- `GET /api/artifacts/{id}`
- `GET /api/artifacts/{id}/content`
- `POST /api/artifacts`
- `GET /api/workflow-triggers`
- `POST /api/workflow-triggers`
- `GET /api/workflow-triggers/{id}`
- `PUT /api/workflow-triggers/{id}`
- `DELETE /api/workflow-triggers/{id}`
- `POST /api/workflow-triggers/{id}/fire`
- `GET /api/workflow-triggers/{id}/events`
- `GET /api/audit`
- `POST /api/auth/login`
- `POST /api/auth/logout`
- `GET /api/me`
- admin-only CRUD under `/api/users` and `/api/tokens`

## Configuration

Environment variables:

```text
ATLAS_HOST=127.0.0.1
ATLAS_PORT=8787
ATLAS_DB=./data/atlas.sqlite
ATLAS_API_TOKEN=
ATLAS_LOOPBACK_NO_AUTH=false
ATLAS_SECRET_KEY=
ATLAS_REQUEST_TIMEOUT=30
ATLAS_UPLOAD_DIR=./data/uploads
ATLAS_MAX_UPLOAD_BYTES=10485760
```

Create the first administrator (the token is printed once):

```bash
python3 -m atlas.admin create-admin admin
```

Atlas stores only password and API-token hashes. Roles are `admin`, `operator`,
`viewer`, and `auditor`. Use `python3 -m atlas.admin --help` for user and token
management commands.

`ATLAS_API_TOKEN`, when set, remains accepted as a legacy bootstrap admin token.
For explicit local development without auth:

```bash
ATLAS_LOOPBACK_NO_AUTH=true python3 -m atlas
```

This bypass applies only to clients seen as `127.0.0.1` or `::1`. Production
defaults require a valid bearer token. Authenticated API calls include:

```text
Authorization: Bearer <per-user-api-token>
```

For remote access, put Atlas behind a VPN, Tailscale, SSH tunnel, or a real TLS
reverse proxy with authentication.

## Security Notes

- Treat Atlas as an operator console and assign least-privilege roles.
- Do not expose Atlas or thClaws workers directly to the public internet.
- Use real tokens for thClaws workers.
- `THCLAWS_API_TOKEN=disable-auth` is only safe on loopback binds.
- Set a high-entropy `ATLAS_SECRET_KEY` to store worker tokens as authenticated
  ciphertext. Without it, Atlas preserves plaintext compatibility and logs a warning.
- Worker tokens are never returned to the dashboard API. API responses expose
  only `token_set: true`.
- `data/` is intentionally ignored by Git.

## Troubleshooting

### Worker stays offline

Check thClaws health:

```bash
curl http://127.0.0.1:4317/healthz
```

Check thClaws API token:

```bash
curl -H "Authorization: Bearer dev-token-1" \
  http://127.0.0.1:4317/v1/agent/info
```

Common causes:

- thClaws is not running.
- wrong port.
- wrong token.
- Atlas cannot reach the worker host.
- thClaws was started without `THCLAWS_API_TOKEN`, so `/v1/*` is disabled.

### `/healthz` returns `ok`, not JSON

This is expected. Atlas accepts plain-text `ok` and JSON health responses.

### Dashboard still shows an old JavaScript error

Hard refresh the browser:

```text
Cmd+Shift+R
```

Atlas serves static assets with `Cache-Control: no-store`, and the HTML includes
cache-busting query strings, but old browser tabs may still hold the previous
module in memory.

### Handoff did not start

Check:

- source job state is `succeeded`
- source job produced assistant text
- handoff worker or workspace was selected
- target worker is reachable
- Jobs list does not show `handoff error`

### Logs are long

The dashboard constrains job text, event log, job list, and audit log into
scrollable lists within each view. SQLite still stores the full event history.

## Current Limitations

Atlas features fall into three levels:

- **Works today without changing thClaws**: health, capability discovery,
  `/agent/run`, live SSE streaming, `x_callback`, session continuation,
  deploy/sync, restart, workflow-level human approval gates, central workflow
  audit, and the multi-machine dashboard built by Atlas.
- **Possible as workarounds**: cancel, live reconnect, and team control. These
  can be approximated by Atlas but are not fully native in thClaws yet.
- **Not native without thClaws changes**: per-tool remote approval, list running
  jobs, job status, cancel by job id, stream resume cursor, structured remote
  Team API, and first-class cross-machine tool-decision audit.

The current APIs are enough for a control-plane MVP, but production-grade deep
orchestration would benefit from native thClaws additions. The highest-value
requests are `job_id + status + cancel` and a remote approval protocol for
`/agent/run`.

Current missing native surfaces include:

- No native thClaws remote job id.
- No native thClaws job status API.
- No native thClaws job cancellation API.
- No native remote approval protocol.
- No native thClaws stream resume cursor.
- No structured HTTP API for thClaws team graph operations.

After restart, Atlas marks interrupted workflow worker/manager nodes as
`recovery_required`. It cannot inspect or resume the old thClaws stream, so an
operator must verify possible side effects and explicitly authorize retry.

Atlas handles these at the control-plane layer where possible. See
[docs/thclaws-capability-matrix.md](docs/thclaws-capability-matrix.md).

## Project Layout

```text
atlas/
  app.py              HTTP API and static dashboard server
  config.py           environment configuration
  db.py               SQLite schema, migration, persistence methods
  jobs.py             job runner, streaming bridge, handoff, worker polling
  router.py           routing decisions
  thclaws_client.py   thClaws HTTP/SSE client
  static/             dashboard HTML/CSS/JS
docs/
  architecture.md
  demo-script.md
  thclaws-capability-matrix.md
  user-guide.md
  workflow-examples.md
  workflow-engine-coding-plan.md
  workflow-engine-plan.md
scripts/
  run.sh
```

## Development

Run syntax checks:

```bash
python3 -B - <<'PY'
from pathlib import Path
for path in sorted(Path("atlas").rglob("*.py")):
    compile(path.read_text(), str(path), "exec")
print("python syntax ok")
PY

node --check atlas/static/app.js
```

Run Atlas:

```bash
python3 -B -m atlas --host 127.0.0.1 --port 8787
```

## Roadmap

The deterministic workflow engine now includes graph execution, conditions,
fan-out, all/any/quorum joins, loop/time/job/budget guards, text/JSON/file
artifacts, lifecycle and restart-recovery controls, event triggers, human
approval/choice gates, bounded manager-directed routing, the validated builder
surface, worker suggestions, and built-in templates. Milestones 1–15 are
complete.

## License

Atlas is **source-available** under the [Atlas Source-Available License 1.0](LICENSE)
— © 2026 Pornthep Nivatyakul ([@kaebmoo](https://github.com/kaebmoo)).

- **Internal and personal use is free.** Individuals and organizations may use,
  modify, and self-host Atlas for their own internal purposes, with no fee and no
  obligation to publish their changes.
- **Offering Atlas to third parties as a service** (SaaS, hosted, managed,
  white-labeled, or embedded in a product for others) requires a **commercial
  license** from the author, which carries a license fee and a source-disclosure
  requirement.

This is not an OSI-approved open-source license. For commercial licensing,
contact [@kaebmoo](https://github.com/kaebmoo).
