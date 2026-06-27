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
- Workflow definitions, workflow runs, artifacts, JSON artifacts,
  condition edges, loop guards, manual triggers, and schedule triggers.
- Workflow run lifecycle events, pause/resume/cancel controls, and runtime
  worker/time policy enforcement.
- A workflow builder entry point that routes plain-language draft requests to a
  worker with role or tag `workflow_builder`.

This is enough to run simple real workflows such as:

```text
Reporter worker -> Anchor worker
Research worker -> Writer worker
Coder worker    -> Reviewer worker
```

For deeper graph workflows, loops, conditions, and manager-directed execution,
see [docs/workflow-engine-plan.md](docs/workflow-engine-plan.md).

## User Documentation

- [User Guide](docs/user-guide.md)
- [Workflow Examples](docs/workflow-examples.md)
- [Demo Script](docs/demo-script.md)
- [Workflow Engine Coding Plan](docs/workflow-engine-coding-plan.md)

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

Open Atlas and use the sidebar:

1. Click `Add` in the Workers section.
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

Click `Add` in the Workspaces section.

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

In the Command panel:

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

In the Command panel:

1. Select `Local thClaws` as Worker.
2. Prompt:

```text
Find one concise technology news item and summarize the facts.
```

3. Enable `After success`.
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

Artifacts:

```bash
curl -sS http://127.0.0.1:8787/api/workflow-runs/wfr_xxx/artifacts
```

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
- `GET /api/workflows/{id}`
- `PUT /api/workflows/{id}`
- `DELETE /api/workflows/{id}`
- `POST /api/workflows/{id}/validate`
- `POST /api/workflows/{id}/explain`
- `POST /api/workflows/{id}/repair`
- `GET /api/workflow-runs`
- `POST /api/workflow-runs`
- `GET /api/workflow-runs/{id}`
- `GET /api/workflow-runs/{id}/artifacts`
- `GET /api/workflow-runs/{id}/events`
- `POST /api/workflow-runs/{id}/pause`
- `POST /api/workflow-runs/{id}/resume`
- `POST /api/workflow-runs/{id}/cancel`
- `GET /api/workflow-triggers`
- `POST /api/workflow-triggers`
- `GET /api/workflow-triggers/{id}`
- `PUT /api/workflow-triggers/{id}`
- `DELETE /api/workflow-triggers/{id}`
- `POST /api/workflow-triggers/{id}/fire`
- `GET /api/workflow-triggers/{id}/events`
- `GET /api/audit`

## Configuration

Environment variables:

```text
ATLAS_HOST=127.0.0.1
ATLAS_PORT=8787
ATLAS_DB=./data/atlas.sqlite
ATLAS_API_TOKEN=
ATLAS_LOOPBACK_NO_AUTH=true
ATLAS_REQUEST_TIMEOUT=30
```

Optional API auth:

```bash
ATLAS_API_TOKEN="atlas-secret" python3 -m atlas
```

When `ATLAS_API_TOKEN` is set, non-loopback API calls must include:

```text
Authorization: Bearer atlas-secret
```

For remote access, put Atlas behind a VPN, Tailscale, SSH tunnel, or a real TLS
reverse proxy with authentication.

## Security Notes

- Treat Atlas as an operator console. Anyone with access can trigger agents on
  registered workers.
- Do not expose Atlas or thClaws workers directly to the public internet.
- Use real tokens for thClaws workers.
- `THCLAWS_API_TOKEN=disable-auth` is only safe on loopback binds.
- Worker tokens are stored in SQLite and never returned to the dashboard API.
  API responses expose only `token_set: true`.
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
scrollable panels. SQLite still stores the full event history.

## Current Limitations

Atlas features fall into three levels:

- **Works today without changing thClaws**: health, capability discovery,
  `/agent/run`, live SSE streaming, `x_callback`, session continuation,
  deploy/sync, restart, and the multi-machine dashboard built by Atlas.
- **Possible as workarounds**: cancel, central approval, live reconnect, team
  control, and central audit. These can be approximated by Atlas but are not
  fully native in thClaws yet.
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

The next major step is the workflow engine:

- predefined workflow graphs
- multiple downstream edges
- conditional transitions
- fan-out and join
- loop guards
- run-level budgets
- artifact blackboard
- human approval gates
- LLM manager-directed next-step proposals inside Atlas policy limits

See [docs/workflow-engine-plan.md](docs/workflow-engine-plan.md).
