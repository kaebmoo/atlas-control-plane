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

## Workflow Builder Draft API

Requires a worker with role or tag `workflow_builder`.

```bash
curl -sS -X POST http://127.0.0.1:8787/api/workflows/draft \
  -H 'content-type: application/json' \
  -d '{
    "plain_language_prompt": "Build a reporter to fact checker to anchor workflow. If fact checker says needs_more_sources, send it back to reporter up to 2 times."
  }'
```
