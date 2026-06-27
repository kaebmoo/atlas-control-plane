# Atlas User Guide

This guide covers the current local Atlas control plane and deterministic
workflow engine.

## 1. Start Atlas

```bash
cd /Users/seal/Documents/GitHub/atlas-control-plane
python3 -m atlas --host 127.0.0.1 --port 8787
```

If port `8787` is busy, use another port:

```bash
python3 -m atlas --host 127.0.0.1 --port 8788
```

Open the dashboard:

```text
http://127.0.0.1:8787
```

Stop Atlas with `Ctrl+C` in the terminal.

## 2. Start thClaws Workers

Start one or more thClaws servers on separate ports.

```bash
cd /Users/seal/Documents/GitHub/thClaws

THCLAWS_API_TOKEN="dev-token-1" \
thclaws --serve --bind 127.0.0.1 --port 4317
```

Optional second worker:

```bash
cd /Users/seal/Documents/GitHub/thClaws

THCLAWS_API_TOKEN="dev-token-2" \
thclaws --serve --bind 127.0.0.1 --port 4318
```

## 3. Add Workers

In the sidebar, click `Add` under Workers.

Example reporter worker:

```text
Name:     Reporter
Base URL: http://127.0.0.1:4317
Token:    dev-token-1
Role:     reporter
Tags:     local,news,reporter
```

Example anchor worker:

```text
Name:     Anchor
Base URL: http://127.0.0.1:4318
Token:    dev-token-2
Role:     anchor
Tags:     local,news,anchor
```

After saving, Atlas polls the worker. A reachable worker becomes `online`.

## 4. Add Workspaces

Click `Add` under Workspaces.

```text
Worker:    Reporter
Key:       thclaws
Directory: /Users/seal/Documents/GitHub/thClaws
Company:   Personal
Tags:      local,news
```

`Directory` is resolved on the worker machine, not the Atlas machine.

## 5. Run A Simple Job

In the Command panel:

1. Enter a prompt.
2. Choose a worker or leave routing on auto.
3. Click `Run`.

The Jobs list shows state, route reason, prompt, and stream output.

## 6. Run A Handoff

Use this when job B should start after job A succeeds.

1. Select the reporter worker.
2. Enter a reporter prompt.
3. Enable `After success`.
4. Select the anchor worker.
5. Keep or edit the handoff prompt.
6. Click `Run`.

Atlas creates a child job when the reporter job succeeds.

## 7. Create A Workflow

In the Workflows panel:

1. Click `New`.
2. Set a name.
3. Paste graph JSON.
4. Set policy JSON.
5. Click `Save`.
6. Click `Validate`.

Minimal graph:

```json
{
  "start": "reporter",
  "nodes": [
    {
      "id": "reporter",
      "type": "worker",
      "role": "reporter",
      "prompt": "Find facts about: {input.topic}",
      "outputs": ["notes"]
    },
    {
      "id": "anchor",
      "type": "worker",
      "role": "anchor",
      "prompt": "Turn these notes into a short script: {artifact.notes}",
      "outputs": ["script"]
    }
  ],
  "edges": [
    {"from": "reporter", "to": "anchor", "condition": {"type": "always"}}
  ]
}
```

Policy:

```json
{
  "max_jobs": 5,
  "max_iterations": 5
}
```

## 8. Run A Workflow

In `Run Input JSON`, enter:

```json
{
  "topic": "technology news"
}
```

Click `Run Workflow`.

Inspect:

- Runs list
- Run detail JSON, including `completed_nodes` and join state
- Artifacts box, including decoded JSON content and metadata
- Lifecycle timeline and pause/resume/cancel controls
- Related jobs in the Jobs panel

### Fan-Out And Joins

Multiple matching outgoing edges queue every independent branch. Point those
branches at a join node when downstream work must wait:

```json
{"id":"join_reviews","type":"join","mode":"all"}
```

- `all` waits for every upstream node that reaches the join.
- `any` continues after the first successful upstream.
- A join node does not create a worker job.
- Resume skips node keys already recorded as completed.

See [workflow-examples.md](workflow-examples.md) for a complete graph.

### Artifacts

Worker output remains automatic. You can also attach a typed artifact to a run:

```bash
curl -sS -X POST http://127.0.0.1:8787/api/artifacts \
  -H 'content-type: application/json' \
  -d '{
    "run_id":"wfr_xxx",
    "key":"invoice",
    "kind":"json",
    "content":{"total":3},
    "metadata":{"source":"manual"}
  }'
```

Read it with `GET /api/artifacts/{id}`. Supported kinds are `text`, `json`,
`markdown`, `file_ref`, `summary`, and `decision`.

### Human Gates And Approvals

Place a `human_gate` between worker nodes when a person must decide whether the
workflow may continue:

```json
{
  "id": "publish_approval",
  "type": "human_gate",
  "label": "Approve publication",
  "reason": "Review the final artifact before publishing"
}
```

The gate creates no worker job. The run changes to `waiting_for_human`, and its
pending approval appears beside the run detail. `Approve` resumes from the
gate's outgoing edges; `Reject` fails the run. A second decision is rejected and
does not execute downstream nodes again.

API equivalents:

```bash
curl -sS 'http://127.0.0.1:8787/api/approvals?state=pending&run_id=wfr_xxx'
curl -sS -X POST http://127.0.0.1:8787/api/approvals/apr_xxx/approve
curl -sS -X POST http://127.0.0.1:8787/api/approvals/apr_xxx/reject
```

Set `policy.requires_human_after_iterations` to pause once before the next
worker job after that many worker jobs have completed. Approval clears this
policy gate for the rest of the run; `max_iterations` remains the hard limit.

### Manager Nodes

Use a `manager` node when a worker should propose one or more allowed next
nodes. Its outgoing edges must use `manager_selected`:

```json
{
  "id": "manager",
  "type": "manager",
  "worker_id": "wrk_manager",
  "schema": "manager_decision_v1",
  "prompt": "Choose the next bounded workflow action."
}
```

```json
{
  "from": "manager",
  "to": "writer",
  "condition": {"type": "manager_selected", "target": "writer"}
}
```

The manager receives only the graph, current node, artifacts, counters, and
policy. It must return one JSON object with `stop`, `reason`, and `next`; every
next item requires `node`, `input_artifacts`, and `instructions`. Atlas prepends
accepted instructions to the selected worker prompt, then enforces the target
edge, artifact presence, worker/workspace allowlists, and job/attempt/time/loop
guards before creating the target job. One invalid item rejects the whole
proposal and fails the manager node. Duplicate targets run once.

The run detail shows the proposal and accepted/rejected reason under **Manager
decisions**. The same decision is retained in workflow events and audit.

## 9. Draft A Workflow With A Builder Worker

Add a worker with role or tag `workflow_builder`.

Then use the Draft box:

```text
Create a reporter -> fact_checker -> anchor workflow. If fact check says
needs_more_sources, send it back to reporter up to 2 times.
```

Atlas sends available workers, workspaces, condition types, trigger types, and
templates to the builder worker. The returned JSON is validated before display.

## 10. Create And Fire A Trigger

Select a saved workflow, then fill Triggers:

Manual trigger:

```json
{}
```

Schedule trigger every 15 minutes:

```json
{
  "interval_minutes": 15
}
```

Daily local-time schedule:

```json
{
  "daily_time": "09:30"
}
```

Click `Create Trigger`. Use `Fire` to start the workflow manually from that
trigger. Click a trigger to inspect recent trigger events.

Other trigger types:

- `webhook`: call `POST /api/workflow-triggers/{id}/fire` with `payload` and a
  stable `dedupe_key`.
- `workflow_run_completed`: optional config filters are
  `source_workflow_definition_id` and `state`.
- `artifact_created`: optional filters are `source_workflow_definition_id`,
  `key`, and `kind`.
- `worker_status_changed`: optional filters are `worker_id` and `status`.

The three internal event types are fired by Atlas, so the dashboard does not
show a `Fire` button for them. Trigger cards show the last state and error;
click a card for the full `received`, `started`, `ignored`, or `failed` history.
Atlas blocks an internal trigger from starting its own source workflow to avoid
an unbounded self-trigger loop.

## Troubleshooting

- `No workers registered`: add and poll at least one worker.
- `role has no matching worker`: add a worker with that role or tag.
- `unknown worker_id`: the workflow references a deleted or wrong worker id.
- `missing prompt variable`: the prompt uses an input/artifact path that does
  not exist.
- `output_format=json` fails: the worker did not return valid JSON.
- `manager node ... returned invalid JSON`: return the exact
  `manager_decision_v1` object without Markdown fences or surrounding text.
- `manager proposal rejected`: inspect Manager decisions for the target, edge,
  artifact, route-policy, or guard reason.
