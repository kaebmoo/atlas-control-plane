# Workflow Examples

Copy these into the Workflows panel.

## Reporter To Anchor

Graph:

```json
{
  "start": "reporter",
  "nodes": [
    {
      "id": "reporter",
      "type": "worker",
      "role": "reporter",
      "prompt": "Find concise facts about: {input.topic}",
      "outputs": ["notes"]
    },
    {
      "id": "anchor",
      "type": "worker",
      "role": "anchor",
      "prompt": "Write a short broadcast script from these notes: {artifact.notes}",
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

Run input:

```json
{
  "topic": "technology news"
}
```

## Fact Checker Approved Branch

The fact checker must return JSON.

Graph:

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
      "id": "fact_checker",
      "type": "worker",
      "role": "fact_checker",
      "output_format": "json",
      "prompt": "Check these notes and return only JSON like {\"verdict\":\"approved\",\"notes\":[]}: {artifact.notes}",
      "outputs": ["fact_check"]
    },
    {
      "id": "anchor",
      "type": "worker",
      "role": "anchor",
      "prompt": "Write the final script from approved notes: {artifact.notes}",
      "outputs": ["script"]
    }
  ],
  "edges": [
    {"from": "reporter", "to": "fact_checker", "condition": {"type": "always"}},
    {
      "from": "fact_checker",
      "to": "anchor",
      "condition": {
        "type": "artifact_equals",
        "artifact": "fact_check",
        "path": "verdict",
        "value": "approved"
      }
    }
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

## Needs More Sources Loop With Policy Guard

This sends work back to the reporter while `fact_check.verdict` is
`needs_more_sources`. `policy.max_iterations` is the hard guard if the workflow
never reaches `approved`.

Graph:

```json
{
  "start": "reporter",
  "nodes": [
    {
      "id": "reporter",
      "type": "worker",
      "role": "reporter",
      "prompt": "Find or improve facts about: {input.topic}",
      "outputs": ["notes"]
    },
    {
      "id": "fact_checker",
      "type": "worker",
      "role": "fact_checker",
      "output_format": "json",
      "prompt": "Return only JSON with verdict approved or needs_more_sources for: {artifact.notes}",
      "outputs": ["fact_check"]
    },
    {
      "id": "anchor",
      "type": "worker",
      "role": "anchor",
      "prompt": "Write script from: {artifact.notes}",
      "outputs": ["script"]
    }
  ],
  "edges": [
    {"from": "reporter", "to": "fact_checker", "condition": {"type": "always"}},
    {
      "from": "fact_checker",
      "to": "anchor",
      "condition": {
        "type": "artifact_equals",
        "artifact": "fact_check",
        "path": "verdict",
        "value": "approved"
      }
    },
    {
      "from": "fact_checker",
      "to": "reporter",
      "condition": {
        "type": "artifact_equals",
        "artifact": "fact_check",
        "path": "verdict",
        "value": "needs_more_sources"
      }
    }
  ]
}
```

Policy:

```json
{
  "max_jobs": 10,
  "max_iterations": 4,
  "max_attempts_per_node": 3
}
```

Note: current edge conditions are independent. Do not model `verdict ==
needs_more_sources AND reporter_count < 2` as two separate edges; that would be
two OR branches.

## Human Gate Before Publish

The gate pauses after the reporter finishes and creates no worker job. Approve
to run the anchor once, or reject to fail the run.

```json
{
  "start": "reporter",
  "nodes": [
    {
      "id": "reporter",
      "type": "worker",
      "role": "reporter",
      "prompt": "Find concise facts about: {input.topic}",
      "outputs": ["notes"]
    },
    {
      "id": "publish_approval",
      "type": "human_gate",
      "label": "Approve publication",
      "reason": "Review reporter notes before creating the final script"
    },
    {
      "id": "anchor",
      "type": "worker",
      "role": "anchor",
      "prompt": "Write a short broadcast script from: {artifact.notes}",
      "outputs": ["script"]
    }
  ],
  "edges": [
    {"from": "reporter", "to": "publish_approval", "condition": {"type": "always"}},
    {"from": "publish_approval", "to": "anchor", "condition": {"type": "always"}}
  ]
}
```

Policy:

```json
{"max_jobs": 5, "max_iterations": 5}
```

For guarded loops, add `"requires_human_after_iterations": 2`. Atlas pauses
once before the next worker job after two worker jobs complete; the normal
`max_iterations` guard still applies.

## Fan-Out With Join All

The fact checker and editor both run after the reporter. The anchor starts only
after both branches succeed. The join itself does not create a worker job.

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
      "id": "fact_checker",
      "type": "worker",
      "role": "fact_checker",
      "output_format": "json",
      "prompt": "Return JSON with verdict and corrections for: {artifact.notes}",
      "outputs": ["fact_check"]
    },
    {
      "id": "editor",
      "type": "worker",
      "role": "editor",
      "prompt": "Return concise editing notes for: {artifact.notes}",
      "outputs": ["edit_notes"]
    },
    {
      "id": "reviews_join",
      "type": "join",
      "mode": "all"
    },
    {
      "id": "anchor",
      "type": "worker",
      "role": "anchor",
      "prompt": "Write the final script from {artifact.notes}. Fact check: {artifact.fact_check}. Editing notes: {artifact.edit_notes}",
      "outputs": ["script"]
    }
  ],
  "edges": [
    {"from": "reporter", "to": "fact_checker", "condition": {"type": "always"}},
    {"from": "reporter", "to": "editor", "condition": {"type": "always"}},
    {"from": "fact_checker", "to": "reviews_join", "condition": {"type": "always"}},
    {"from": "editor", "to": "reviews_join", "condition": {"type": "always"}},
    {"from": "reviews_join", "to": "anchor", "condition": {"type": "always"}}
  ]
}
```

Policy:

```json
{"max_jobs": 5, "max_iterations": 10}
```

Use `"mode":"any"` when the first successful review may continue downstream.
Other queued branches still run; Atlas prevents the join or its downstream node
from being scheduled twice.

## Bounded Manager-Directed Loop

The manager chooses only declared outgoing targets. After research, the manager
can select the writer with `input_artifacts: ["research"]`, or return
`{"stop":true,"reason":"...","next":[]}`. Atlas validates the proposal before
creating the selected target job.

```json
{
  "start": "manager",
  "nodes": [
    {
      "id": "manager",
      "type": "manager",
      "worker_id": "wrk_manager",
      "schema": "manager_decision_v1",
      "prompt": "Choose researcher, writer, or stop. Return manager_decision_v1 JSON only."
    },
    {
      "id": "researcher",
      "type": "worker",
      "worker_id": "wrk_researcher",
      "prompt": "Research: {input.topic}",
      "outputs": ["research"]
    },
    {
      "id": "writer",
      "type": "worker",
      "worker_id": "wrk_writer",
      "prompt": "Write from: {artifact.research}",
      "outputs": ["draft"]
    }
  ],
  "edges": [
    {
      "from": "manager",
      "to": "researcher",
      "condition": {"type": "manager_selected", "target": "researcher"}
    },
    {
      "from": "manager",
      "to": "writer",
      "condition": {"type": "manager_selected", "target": "writer"}
    },
    {"from": "researcher", "to": "manager", "condition": {"type": "always"}}
  ]
}
```

Policy:

```json
{
  "max_jobs": 5,
  "max_iterations": 5,
  "max_attempts_per_node": 3,
  "max_minutes": 30,
  "allowed_worker_ids": ["wrk_manager", "wrk_researcher", "wrk_writer"]
}
```

Manager response selecting the writer:

```json
{
  "stop": false,
  "reason": "Research artifact is ready.",
  "next": [
    {
      "node": "writer",
      "input_artifacts": ["research"],
      "instructions": "Produce one concise draft."
    }
  ]
}
```

## Human Choice And Quorum

A choice gate declares its options and each branch names one declared choice:

```json
{
  "id": "publish_decision",
  "type": "human_gate",
  "label": "Choose publication path",
  "choices": [
    {"id": "publish", "label": "Publish"},
    {"id": "revise", "label": "Revise"}
  ]
}
```

```json
{"from":"publish_decision","to":"publisher","condition":{"type":"human_selected","choice":"publish"}}
```

A 2-of-3 join is `{"id":"reviews","type":"join","mode":"quorum","quorum":2}`.
Incoming sources are counted once even if duplicate edges exist. With
`"stop_on_first_failure":false`, independent ready branches continue; failed
nodes never traverse outgoing edges and the run still finishes failed.

Budget policy example:

```json
{"max_budget_units":6,"stop_on_first_failure":false}
```

Worker/manager nodes default to one unit and may set `"budget_units":2`.

## File Upload And Recovery APIs

Upload a bounded direct binary body (not multipart or base64):

```bash
curl -sS -X POST 'http://127.0.0.1:8787/api/workflow-runs/wfr_xxx/files?key=evidence' \
  -H 'content-type: application/pdf' \
  -H 'x-filename: evidence.pdf' \
  --data-binary @evidence.pdf
```

Download the resulting `file_ref` with
`GET /api/artifacts/art_xxx/content`. After restart, retry a
`recovery_required` run only after reviewing possible duplicate side effects:

```bash
curl -sS -X POST http://127.0.0.1:8787/api/workflow-runs/wfr_xxx/resume \
  -H 'content-type: application/json' \
  -d '{"retry_interrupted":true}'
```

## Manual Trigger API

```bash
curl -sS -X POST http://127.0.0.1:8787/api/workflow-triggers \
  -H 'content-type: application/json' \
  -d '{
    "workflow_definition_id": "wfd_xxx",
    "name": "Manual news run",
    "type": "manual"
  }'
```

```bash
curl -sS -X POST http://127.0.0.1:8787/api/workflow-triggers/wtr_xxx/fire \
  -H 'content-type: application/json' \
  -d '{
    "payload": {"topic": "technology news"},
    "dedupe_key": "manual-news-001"
  }'
```

## Interval Schedule Trigger API

```bash
curl -sS -X POST http://127.0.0.1:8787/api/workflow-triggers \
  -H 'content-type: application/json' \
  -d '{
    "workflow_definition_id": "wfd_xxx",
    "name": "Every 15 minutes",
    "type": "schedule",
    "config": {"interval_minutes": 15}
  }'
```

## Daily Local-Time Trigger API

```bash
curl -sS -X POST http://127.0.0.1:8787/api/workflow-triggers \
  -H 'content-type: application/json' \
  -d '{
    "workflow_definition_id": "wfd_xxx",
    "name": "Morning run",
    "type": "schedule",
    "config": {"daily_time": "09:30"}
  }'
```

## Webhook Trigger API

```bash
curl -sS -X POST http://127.0.0.1:8787/api/workflow-triggers \
  -H 'content-type: application/json' \
  -d '{
    "workflow_definition_id":"wfd_target",
    "name":"CRM webhook",
    "type":"webhook"
  }'

curl -sS -X POST http://127.0.0.1:8787/api/workflow-triggers/wtr_xxx/fire \
  -H 'content-type: application/json' \
  -d '{"payload":{"lead_id":"lead_123"},"dedupe_key":"crm-lead-123"}'
```

## Internal Event Trigger API

`workflow_definition_id` is the workflow Atlas starts. The config identifies
the source event:

```bash
curl -sS -X POST http://127.0.0.1:8787/api/workflow-triggers \
  -H 'content-type: application/json' \
  -d '{
    "workflow_definition_id":"wfd_target",
    "name":"After reporter workflow",
    "type":"workflow_run_completed",
    "config":{"source_workflow_definition_id":"wfd_source","state":"succeeded"}
  }'
```

For `artifact_created`, filter with `source_workflow_definition_id`, `key`, or
`kind`. For `worker_status_changed`, filter with `worker_id` or `status`.
Internal event triggers are fired only by Atlas.

## Manual Artifact API

```bash
curl -sS -X POST http://127.0.0.1:8787/api/artifacts \
  -H 'content-type: application/json' \
  -d '{
    "run_id":"wfr_xxx",
    "key":"invoice_batch",
    "kind":"json",
    "content":{"invoice_ids":["inv_1","inv_2"]},
    "metadata":{"source":"manual"}
  }'
```

The response includes the artifact id. Read it later with
`GET /api/artifacts/{id}`.

## Workflow Builder Draft API

Requires a worker with role or tag `workflow_builder`.

```bash
curl -sS -X POST http://127.0.0.1:8787/api/workflows/draft \
  -H 'content-type: application/json' \
  -d '{
    "plain_language_prompt": "Build a reporter to fact checker to anchor workflow. If fact checker says needs_more_sources, send it back to reporter up to 2 times."
  }'
```

Builder output is accepted only after graph, worker/workspace reference, policy,
and trigger validation. Other builder endpoints for a saved workflow are:

```bash
curl -sS -X POST http://127.0.0.1:8787/api/workflows/wfd_xxx/explain
curl -sS -X POST http://127.0.0.1:8787/api/workflows/wfd_xxx/repair \
  -H 'content-type: application/json' \
  -d '{"graph":{},"policy":{},"triggers":[]}'
curl -sS -X POST http://127.0.0.1:8787/api/workflows/wfd_xxx/suggest-triggers \
  -H 'content-type: application/json' \
  -d '{"plain_language_prompt":"Run every morning at 09:30"}'
```

List built-in templates without saving them:

```bash
curl -sS http://127.0.0.1:8787/api/workflow-templates
```
