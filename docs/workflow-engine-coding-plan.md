# Workflow Engine Coding Plan

This is the implementation checklist to finish everything in
`docs/workflow-engine-plan.md` from the current workflow MVP.

## Current Base

Already implemented:

- workflow definition CRUD
- workflow runs and runtime nodes/edges
- text and JSON artifacts
- prompt rendering
- worker node execution through existing jobs
- `always`, `artifact_equals`, `artifact_in`, `max_iterations_below`
- manual and simple schedule triggers
- trigger event history
- workflow draft/explain/repair endpoints
- dashboard workflow editor, runs, artifacts, triggers
- workflow lifecycle events, joins, event triggers, artifact APIs, and human
  approvals from Milestones 1–5 below

Milestones 1–5 are complete. The next implementation target is Milestone 6;
Milestones 7 and 8 remain dependent follow-up work.

## Milestone 1: Hardening And Observability

Goal: make current deterministic workflows safe enough to build on.

Files:

- `atlas/db.py`
- `atlas/app.py`
- `atlas/workflows.py`
- `atlas/static/app.js`
- `scripts/check_workflows.py`
- `scripts/check_workflow_api.py`

Work:

- [x] Add `workflow_events` table with append/list helpers.
- [x] Record run lifecycle events: created, node_started, node_succeeded,
  node_failed, edge_taken, condition_skipped, guard_tripped, run_finished.
- [x] Add `GET /api/workflow-runs/{id}/events`.
- [x] Add run cancel/pause/resume state APIs.
- [x] Enforce `policy.max_minutes`.
- [x] Enforce `policy.allowed_worker_ids` at runtime, not only save time.
- [x] Show workflow event timeline and run controls in the dashboard.

Checks:

- [x] run events are append-only and ordered
- [x] cancel blocks new nodes from starting
- [x] pause stops before next node
- [x] max_minutes fails loudly
- [x] disallowed worker fails before job creation

## Milestone 2: Joins And Fan-Out Completion

Goal: support real graph branches, not only naive next-ready execution.

Files:

- `atlas/workflows.py`
- `atlas/db.py`
- `scripts/check_workflows.py`
- `atlas/static/app.js`

Work:

- [x] Track completed node keys per run.
- [x] Add `join` node type.
- [x] Support join modes `all` and `any`.
- [x] Prevent duplicate downstream scheduling when multiple upstream edges target
  the same node.
- [x] Show join state in run detail.

Checks:

- [x] fan-out starts independent branches
- [x] join `all` waits for all upstream nodes
- [x] join `any` continues after first successful upstream
- [x] duplicate incoming edges do not run the same node twice
- [x] resume does not run completed nodes again

## Milestone 3: Webhook And Event Triggers

Goal: complete trigger types from the plan without changing thClaws.

Files:

- `atlas/workflows.py`
- `atlas/app.py`
- `atlas/db.py`
- `atlas/jobs.py`
- `atlas/static/app.js`
- `scripts/check_workflow_api.py`

Work:

- [x] Treat `POST /api/workflow-triggers/{id}/fire` as the webhook endpoint for
  webhook triggers.
- [x] Add trigger types:
  - [x] `webhook`
  - [x] `workflow_run_completed`
  - [x] `artifact_created`
  - [x] `worker_status_changed`
- [x] Fire internal event triggers from existing DB/service points.
- [x] Keep `dedupe_key` behavior for all trigger types.
- [x] Show last event/error per trigger in UI.

Checks:

- [x] webhook fire creates a workflow run
- [x] duplicate webhook dedupe_key is ignored
- [x] workflow completion trigger starts dependent workflow
- [x] artifact_created trigger receives artifact payload
- [x] worker status change trigger fires once per transition

## Milestone 4: Artifact APIs

Goal: make artifacts first-class workflow objects.

Files:

- `atlas/db.py`
- `atlas/app.py`
- `atlas/static/app.js`
- `scripts/check_workflow_api.py`

Work:

- [x] Add `GET /api/artifacts/{id}`.
- [x] Add `POST /api/artifacts`.
- [x] Validate artifact kind: `text`, `json`, `markdown`, `file_ref`, `summary`,
  `decision`.
- [x] Allow manual artifact creation for a run.
- [x] Show artifact detail in dashboard.

Checks:

- [x] get artifact by id
- [x] create JSON artifact and read decoded metadata/content
- [x] reject unsupported artifact kind

## Milestone 5: Human Gates And Approvals

Goal: pause workflows for explicit human decisions.

Files:

- `atlas/db.py`
- `atlas/workflows.py`
- `atlas/app.py`
- `atlas/static/app.js`
- `atlas/static/index.html`
- `scripts/check_workflow_db.py`
- `scripts/check_workflows.py`
- `scripts/check_workflow_api.py`

Work:

- [x] Add `approvals` table.
- [x] Add `human_gate` node type.
- [x] When reached, create approval and set run `waiting_for_human`.
- [x] Add:
  - [x] `GET /api/approvals`
  - [x] `POST /api/approvals/{id}/approve`
  - [x] `POST /api/approvals/{id}/reject`
- [x] Resume approved run from the gate's outgoing edges.
- [x] Fail the run on rejection without adding a reject-branch DSL.
- [x] Enforce `requires_human_after_iterations`.

Checks:

- [x] human_gate pauses run without creating a worker job
- [x] approve resumes run and does not execute downstream twice
- [x] reject fails the run
- [x] duplicate approvals and decisions do not create duplicate execution
- [x] loop requiring human approval pauses after configured count

## Milestone 6: Manager Worker

Goal: add bounded manager-directed routing.

Files:

- `atlas/workflows.py`
- `atlas/app.py`
- `atlas/db.py`
- `atlas/static/app.js`
- `scripts/check_workflows.py`

Work:

- Add `manager` node type.
- Define manager prompt context: graph, current node, artifacts, counters,
  policy.
- Parse manager JSON contract.
- Add `manager_selected` condition.
- Validate manager proposals:
  - target node exists
  - edge from manager to target exists
  - required artifacts exist
  - worker/workspace allowed
  - loop/policy guards pass
- Record manager accepted/rejected events.
- Show manager decisions in dashboard.

Checks:

- valid manager JSON selects an allowed node
- invalid JSON fails node
- forbidden worker is rejected
- missing artifact is rejected
- manager loop stops at policy guard

## Milestone 7: Builder Completion

Goal: make the design-time builder cover the full workflow surface.

Files:

- `atlas/app.py`
- `atlas/static/app.js`
- `scripts/check_workflow_api.py`

Work:

- Add `POST /api/workflows/{id}/suggest-triggers`.
- Add builder context for joins, manager nodes, human gates, artifacts, and
  policy defaults.
- Make explain use `workflow_builder` when available, with deterministic local
  fallback.
- Make repair validate returned triggers and policy limits.
- Add simple condition/trigger forms that update JSON preview.

Checks:

- draft validates graph and trigger schedule
- missing builder gives clear error
- explain returns local fallback without builder
- repair rejects invalid returned JSON
- suggest-triggers returns validated trigger drafts

## Milestone 8: Templates

Goal: ship the templates named in the plan.

Files:

- `atlas/workflow_templates.py`
- `atlas/app.py`
- `atlas/static/app.js`
- `scripts/check_workflow_api.py`

Work:

- Add built-in templates:
  - News Desk
  - Researcher -> Writer -> Reviewer
  - Coder -> Tester -> Reviewer
  - Manager-directed loop with max 3 iterations
- Add `GET /api/workflow-templates`.
- Add dashboard template picker that copies JSON into the editor.

Checks:

- every built-in template validates
- create workflow from template
- template picker fills editor JSON

## Completion Gate

Before committing a workflow milestone:

```bash
python3 -m py_compile atlas/db.py atlas/app.py atlas/jobs.py atlas/workflows.py atlas/router.py scripts/check_workflows.py scripts/check_workflow_api.py
node --check atlas/static/app.js
python3 scripts/check_workflow_db.py
python3 scripts/check_workflows.py
python3 scripts/check_workflow_api.py
```

Manual UI smoke:

- create workflow from JSON
- validate
- run
- inspect nodes, events, artifacts
- create trigger
- fire trigger
- inspect trigger last event/error
- create/read a manual JSON artifact
- pause/cancel/resume run
