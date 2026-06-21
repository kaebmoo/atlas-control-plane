# Atlas Workflow Engine Plan

This plan upgrades Atlas from single-step handoff into a real workflow engine
for multi-worker agent organizations.

The key rule is:

```text
Atlas owns state, policy, routing, limits, and audit.
Workers execute tasks.
An LLM manager may recommend next steps, but Atlas validates and enforces them.
```

This avoids unbounded autonomous loops while still allowing practical agent
coordination.

## Goals

- Let users define flows across many workers.
- Support linear chains, fan-out, joins, conditional edges, and guarded loops.
- Keep all workflow state observable from the dashboard.
- Store intermediate outputs as named artifacts instead of passing long logs
  directly between every worker.
- Support both human-defined workflows and manager-assisted workflows.
- Keep thClaws unchanged for now. Atlas continues to call existing thClaws APIs.
- Build incrementally on the existing Atlas worker/job/event model.

## Non-Goals For First Implementation

- No distributed worker-side queue inside thClaws.
- No native thClaws job cancellation beyond current best-effort Atlas cancel.
- No arbitrary code execution in workflow conditions.
- No fully autonomous unbounded manager loops.
- No visual drag-and-drop editor in the first pass.

## Workflow Types

### 1. Predefined Workflow

The user defines a graph ahead of time.

Example:

```text
reporter -> fact_checker
fact_checker -> editor
editor -> anchor
fact_checker -> reporter if needs_more_sources
editor -> reporter if rewrite_needed
anchor -> done
```

Atlas executes the graph deterministically.

### 2. Conditional Workflow

Edges have conditions evaluated from structured output or a manager/evaluator
step.

Example:

```json
{
  "from": "fact_checker",
  "to": "reporter",
  "condition": "artifact.fact_check.verdict == 'needs_more_sources'"
}
```

First implementation should avoid arbitrary expression engines. Use a small
condition DSL.

### 3. Manager-Directed Workflow

A manager worker receives workflow state and recommends next actions as JSON.
Atlas validates:

- target worker is allowed
- target workspace is allowed
- max iterations not exceeded
- budget not exceeded
- required artifacts exist
- no forbidden edge is used

The manager proposes; Atlas decides.

## Core Data Model

### workflow_definitions

Stores reusable workflow graphs.

```sql
CREATE TABLE workflow_definitions (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'draft',
  graph TEXT NOT NULL,
  policy TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

`graph` is JSON:

```json
{
  "nodes": [
    {
      "id": "reporter",
      "type": "worker",
      "label": "Reporter",
      "worker_id": "wrk_reporter",
      "workspace_id": "wsp_news",
      "prompt": "Find facts about: {input.topic}",
      "outputs": ["reporter_notes"]
    }
  ],
  "edges": [
    {
      "id": "reporter_to_fact_checker",
      "from": "reporter",
      "to": "fact_checker",
      "condition": {"type": "always"}
    }
  ],
  "start": "reporter"
}
```

`policy` is JSON:

```json
{
  "max_jobs": 20,
  "max_iterations": 5,
  "max_minutes": 30,
  "allowed_worker_ids": ["wrk_reporter", "wrk_anchor"],
  "requires_human_after_iterations": 3,
  "stop_on_first_failure": false
}
```

### workflow_runs

One execution of a workflow definition.

```sql
CREATE TABLE workflow_runs (
  id TEXT PRIMARY KEY,
  workflow_definition_id TEXT,
  name TEXT NOT NULL,
  state TEXT NOT NULL,
  input TEXT NOT NULL DEFAULT '{}',
  current_nodes TEXT NOT NULL DEFAULT '[]',
  counters TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  updated_at TEXT NOT NULL
);
```

States:

```text
queued
running
waiting_for_jobs
waiting_for_human
succeeded
failed
cancelled
paused
```

### workflow_nodes

Runtime state per node.

```sql
CREATE TABLE workflow_nodes (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  node_key TEXT NOT NULL,
  state TEXT NOT NULL,
  job_id TEXT,
  attempt INTEGER NOT NULL DEFAULT 0,
  input_artifacts TEXT NOT NULL DEFAULT '[]',
  output_artifacts TEXT NOT NULL DEFAULT '[]',
  error TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  updated_at TEXT NOT NULL
);
```

### workflow_edges

Runtime record of transitions taken.

```sql
CREATE TABLE workflow_edges (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  from_node TEXT NOT NULL,
  to_node TEXT NOT NULL,
  condition_result TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
```

### artifacts

Shared blackboard for workflow outputs.

```sql
CREATE TABLE artifacts (
  id TEXT PRIMARY KEY,
  run_id TEXT,
  job_id TEXT,
  key TEXT NOT NULL,
  kind TEXT NOT NULL,
  content TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

Kinds:

```text
text
json
markdown
file_ref
summary
decision
```

## Prompt Rendering

Node prompts can use templates:

```text
Topic: {input.topic}
Reporter notes: {artifact.reporter_notes}
Fact check: {artifact.fact_check}
Previous job: {job.previous.assistant_text}
```

Template variables:

- `{input.<key>}`
- `{artifact.<key>}`
- `{run.id}`
- `{node.id}`
- `{job.<field>}`

First implementation should support simple replacement only. Missing variables
should fail the node with a clear error unless marked optional later.

## Node Types

### worker

Dispatches a job to a thClaws worker.

```json
{
  "type": "worker",
  "worker_id": "wrk_anchor",
  "workspace_id": "wsp_news",
  "prompt": "...",
  "outputs": ["anchor_script"]
}
```

### manager

Calls an LLM worker that returns next-step JSON.

```json
{
  "type": "manager",
  "worker_id": "wrk_manager",
  "prompt": "Given this state, choose next actions as JSON.",
  "schema": "manager_decision_v1"
}
```

### human_gate

Pauses until a human approves, rejects, or chooses a branch.

```json
{
  "type": "human_gate",
  "label": "Approve anchor script"
}
```

### join

Waits for multiple upstream nodes.

```json
{
  "type": "join",
  "mode": "all"
}
```

Modes:

- `all`: all upstream nodes must succeed.
- `any`: first successful upstream continues.
- `quorum`: future.

## Edge Conditions

Use a small JSON condition DSL.

### always

```json
{"type": "always"}
```

### artifact_equals

```json
{
  "type": "artifact_equals",
  "artifact": "fact_check",
  "path": "verdict",
  "value": "approved"
}
```

### artifact_in

```json
{
  "type": "artifact_in",
  "artifact": "editor_decision",
  "path": "next",
  "values": ["anchor", "publish"]
}
```

### manager_selected

```json
{
  "type": "manager_selected",
  "target": "reporter"
}
```

### max_iterations_below

```json
{
  "type": "max_iterations_below",
  "node": "reporter",
  "max": 3
}
```

## Execution Loop

Workflow runner algorithm:

```text
create workflow_run
enqueue start node
while run is active:
  load ready nodes
  for each ready node:
    validate policy
    render prompt
    create Atlas job
    mark node waiting_for_job
  when job completes:
    extract artifacts
    evaluate outgoing edges
    enqueue next nodes
  if no ready nodes and no running jobs:
    mark run succeeded or failed
```

Important:

- Jobs remain the unit of worker execution.
- Workflow nodes wrap jobs.
- Existing `/api/jobs` can continue to work independently.
- Workflow events should reuse `job_events` style append-only logs or add
  `workflow_events`.

## Artifact Extraction

First version:

- Default: store full assistant text under the first declared output key.
- Optional JSON extraction:
  - If node declares `output_format: json`, parse assistant text as JSON.
  - If parsing fails, mark node failed.

Example node:

```json
{
  "id": "fact_checker",
  "type": "worker",
  "outputs": ["fact_check"],
  "output_format": "json",
  "prompt": "Return JSON: {\"verdict\":\"approved|needs_more_sources\", \"notes\":[]}"
}
```

## Loop Guards

Every workflow run must enforce:

- max jobs per run
- max attempts per node
- max total iterations
- max runtime minutes
- optional human gate after N loops

Suggested defaults:

```json
{
  "max_jobs": 20,
  "max_attempts_per_node": 3,
  "max_iterations": 5,
  "max_minutes": 30,
  "requires_human_after_iterations": 3
}
```

When a guard trips, Atlas should pause or fail loudly instead of continuing.

## Manager LLM Contract

Manager worker returns JSON only:

```json
{
  "stop": false,
  "reason": "Fact check needs more sources.",
  "next": [
    {
      "node": "reporter",
      "input_artifacts": ["fact_check"],
      "instructions": "Find one more independent source."
    }
  ]
}
```

Atlas validates:

- `node` exists in workflow graph
- manager is allowed to select that node
- loop guards pass
- required artifacts exist
- edge from manager to target is allowed

If JSON is invalid, Atlas records a manager error and either retries or asks for
human input.

## API Plan

### Workflow Definitions

- `GET /api/workflows`
- `POST /api/workflows`
- `GET /api/workflows/{id}`
- `PUT /api/workflows/{id}`
- `DELETE /api/workflows/{id}`
- `POST /api/workflows/{id}/validate`

### Workflow Runs

- `POST /api/workflow-runs`
- `GET /api/workflow-runs`
- `GET /api/workflow-runs/{id}`
- `POST /api/workflow-runs/{id}/pause`
- `POST /api/workflow-runs/{id}/resume`
- `POST /api/workflow-runs/{id}/cancel`
- `GET /api/workflow-runs/{id}/events`

### Artifacts

- `GET /api/workflow-runs/{id}/artifacts`
- `GET /api/artifacts/{id}`
- `POST /api/artifacts`

### Human Gates

- `GET /api/approvals`
- `POST /api/approvals/{id}/approve`
- `POST /api/approvals/{id}/reject`

## Dashboard Plan

First version should be form/table based, not drag-and-drop.

Views:

1. **Workflow Definitions**
   - list definitions
   - create/edit JSON graph
   - validate graph

2. **Workflow Run Detail**
   - status timeline
   - active nodes
   - jobs per node
   - artifacts
   - loop counters
   - pause/cancel controls

3. **Workflow Builder Lite**
   - add node form
   - add edge form
   - policy form
   - JSON preview

4. **Manager Decision Panel**
   - manager proposals
   - accepted/rejected reason
   - policy validation failures

## Implementation Phases

### Phase 1: Static Workflow Graph

Deliver:

- SQLite tables and migrations.
- Workflow definition CRUD.
- Workflow run creation.
- Execute linear graph and fan-out.
- Store artifacts.
- Dashboard list/detail.

No manager yet.

### Phase 2: Conditions And Joins

Deliver:

- condition DSL
- JSON artifact parsing
- joins
- loop guards
- pause on guard trip

### Phase 3: Manager Worker

Deliver:

- manager node type
- manager JSON schema
- policy validation
- manager decision event log
- dashboard review of decisions

### Phase 4: Human Gates And Approvals

Deliver:

- human gate node type
- approval API
- approval UI
- resume workflow after approval

### Phase 5: Workflow Templates

Deliver built-in templates:

- Reporter -> Fact Checker -> Editor -> Anchor
- Researcher -> Writer -> Reviewer
- Coder -> Tester -> Reviewer
- Manager-directed loop with max 3 iterations

## Suggested First Template: News Desk

```json
{
  "name": "News Desk",
  "graph": {
    "start": "reporter",
    "nodes": [
      {
        "id": "reporter",
        "type": "worker",
        "role": "reporter",
        "prompt": "Find facts for this topic: {input.topic}",
        "outputs": ["reporter_notes"]
      },
      {
        "id": "fact_checker",
        "type": "worker",
        "role": "fact_checker",
        "output_format": "json",
        "prompt": "Check these notes and return JSON verdict: {artifact.reporter_notes}",
        "outputs": ["fact_check"]
      },
      {
        "id": "anchor",
        "type": "worker",
        "role": "anchor",
        "prompt": "Read this as a broadcast script: {artifact.reporter_notes}",
        "outputs": ["anchor_script"]
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
  },
  "policy": {
    "max_jobs": 10,
    "max_attempts_per_node": 3,
    "max_iterations": 3,
    "requires_human_after_iterations": 2
  }
}
```

## Testing Plan

Unit-level:

- graph validation
- prompt rendering
- condition evaluation
- loop guard evaluation
- artifact extraction

Integration:

- fake thClaws worker success
- fake thClaws worker failure
- linear workflow
- fan-out workflow
- conditional loop stops at max iteration
- manager returns invalid JSON
- manager proposes forbidden worker

UI:

- create workflow definition
- validate workflow
- start run
- inspect run detail
- cancel/pause/resume
- approve human gate

## Open Questions

- Should workflow definitions be edited as JSON first, or should Atlas start
  with a simple form-only builder?
- Should manager worker be a normal worker role, or a reserved control-plane
  worker?
- Should artifacts support file attachments in phase 1, or only text/JSON?
- Should workflow runs be resumable after Atlas restart in phase 1? Recommended:
  yes for completed node/job state, but not for active thClaws streams until
  thClaws has native job resume.

## Recommendation

Build Phase 1 and Phase 2 before adding manager autonomy.

Reason:

```text
Without graph state, artifacts, and loop guards, an LLM manager has nowhere
safe to operate.
```

Once Atlas can execute a bounded graph, manager-directed routing becomes a
small extension instead of the core risk.
