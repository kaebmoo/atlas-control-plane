# Workflow Engine Coding Plan

This is the implementation checklist to finish everything in
`docs/plans/workflow-engine-plan.md` from the current workflow MVP.

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
- workflow lifecycle events, joins, event triggers, artifact APIs, human
  approvals, and bounded manager routing from Milestones 1–6 below

Milestones 1–15 are complete. Milestones 9–15 below were implemented in the
documented dependency order.

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

- [x] Add `manager` node type.
- [x] Define manager prompt context: graph, current node, artifacts, counters,
  policy.
- [x] Parse manager JSON contract.
- [x] Add `manager_selected` condition.
- [x] Validate manager proposals:
  - [x] target node exists
  - [x] edge from manager to target exists
  - [x] required artifacts exist
  - [x] worker/workspace allowed
  - [x] loop/policy guards pass
- [x] Record manager accepted/rejected events and audit entries.
- [x] Show manager decisions in dashboard.

Checks:

- [x] valid manager JSON selects an allowed node once
- [x] invalid JSON fails node with an auditable rejection
- [x] target without a manager edge is rejected
- [x] forbidden worker or workspace is rejected before target job creation
- [x] missing artifact is rejected
- [x] manager loop stops at policy guard
- [x] duplicate target proposals do not create duplicate execution

## Milestone 7: Builder Completion

Goal: make the design-time builder cover the full workflow surface.

Files:

- `atlas/app.py`
- `atlas/static/app.js`
- `scripts/check_workflow_api.py`

Work:

- [x] Add `POST /api/workflows/{id}/suggest-triggers`.
- [x] Add builder context for joins, manager nodes, human gates, artifacts, and
  policy defaults.
- [x] Make explain use `workflow_builder` when available, with deterministic local
  fallback.
- [x] Make repair validate returned triggers and policy limits.
- [x] Add simple condition/trigger forms that update JSON preview.

Checks:

- [x] draft validates graph and trigger schedule
- [x] missing builder gives clear error
- [x] explain returns local fallback without builder
- [x] repair rejects invalid returned JSON
- [x] suggest-triggers returns validated trigger drafts

## Milestone 8: Templates

Goal: ship the templates named in the plan.

Files:

- `atlas/workflow_templates.py`
- `atlas/app.py`
- `atlas/static/app.js`
- `scripts/check_workflow_api.py`

Work:

- [x] Add built-in templates:
  - [x] News Desk
  - [x] Researcher -> Writer -> Reviewer
  - [x] Coder -> Tester -> Reviewer
  - [x] Manager-directed loop with max 3 iterations
- [x] Add `GET /api/workflow-templates`.
- [x] Add dashboard template picker that copies JSON into the editor.

Checks:

- [x] every built-in template validates
- [x] create workflow from template
- [x] template picker fills editor JSON

## Follow-Up Decisions

These decisions keep the remaining work deterministic and implementable with
the current thClaws API:

- Budget is an integer `budget_units` allowance, not money or token usage.
  Worker/manager nodes default to one unit and may declare a larger positive
  `budget_units` cost. Atlas reserves units only when it creates a job.
- `stop_on_first_failure` defaults to `true` to preserve current behavior. When
  false, Atlas finishes already-ready independent branches, never follows edges
  from a failed node, and finishes the run as failed after the queue drains.
- Human branch choices use a small `human_selected` edge condition. No arbitrary
  expressions or free-form target ids are accepted.
- File upload sends the browser `File` as a bounded binary request. Do not add
  multipart or base64 dependencies.
- Restart recovery never automatically repeats an interrupted worker job. An
  operator must explicitly authorize retry because worker tools may have side
  effects and thClaws has no remote job-resume/status API.

## Milestone 9: Dashboard Builder And Trigger Controls

Goal: expose existing backend capabilities without changing workflow runtime
semantics.

Files:

- `atlas/static/index.html`
- `atlas/static/app.js`
- `scripts/check_workflow_api.py`

Work:

- [x] Add a policy form for the currently supported fields: `max_jobs`,
  `max_iterations`, `max_attempts_per_node`, `max_minutes`,
  `requires_human_after_iterations`, `allowed_worker_ids`, and
  `allowed_workspace_ids`.
- [x] Keep Policy JSON as the source of truth; form edits update its preview and
  raw JSON edits repopulate the form when valid.
- [x] Add Explain and Repair controls for a saved workflow using the existing
  endpoints.
- [x] Explain displays text only. Repair copies validated graph, policy, and
  trigger drafts into previews but never saves automatically.
- [x] Add enable/disable actions to existing trigger cards using the existing
  `PUT /api/workflow-triggers/{id}` endpoint.
- [x] Preserve raw JSON editing and unsaved-editor dirty state across refreshes.

Checks:

- [x] policy form changes Policy JSON and invalid raw JSON is not overwritten
- [x] Explain uses local fallback when no builder exists
- [x] Repair preview changes graph/policy without saving
- [x] trigger enable/disable updates the card and scheduler eligibility
- [x] rendered desktop/mobile QA has no console errors

## Milestone 10: Builder Worker Suggestions

Goal: help design-time drafts resolve missing roles without weakening save/run
validation.

Files:

- `atlas/app.py`
- `atlas/static/index.html`
- `atlas/static/app.js`
- `scripts/check_workflow_api.py`

Work:

- [x] Add `POST /api/workflows/suggest-workers` for unsaved graph JSON.
- [x] Structurally validate the graph before suggestion, but allow unresolved
  role references at this design-time endpoint only.
- [x] Return one item per unresolved node:
  `node_id`, `role`, optional `worker_id`, optional `workspace_id`, `reason`, and
  `state` (`matched`, `fallback`, or `unavailable`).
- [x] Use `workflow_builder` when available and a deterministic local fallback
  when absent.
- [x] Validate every suggested worker/workspace id against current DB records,
  workspace ownership, and policy allowlists; reject the complete builder
  response if any id is invented or forbidden.
- [x] Add a dashboard Suggest Workers control and explicit per-item Apply action.
  Applying only edits Graph JSON; normal workflow validation still gates save.
- [x] Do not create workers, workspaces, workflows, or jobs automatically from a
  suggestion.

Checks:

- [x] exact role/tag match returns a validated worker suggestion
- [x] missing builder returns deterministic unresolved-role diagnostics
- [x] invented worker/workspace ids are rejected
- [x] policy-forbidden suggestions are rejected
- [x] applying a suggestion updates JSON preview without saving

## Milestone 11: Budget And Failure Policy

Goal: complete policy behavior before adding quorum joins or recovery retries.

Files:

- `atlas/app.py`
- `atlas/workflows.py`
- `atlas/static/index.html`
- `atlas/static/app.js`
- `scripts/check_workflows.py`
- `scripts/check_workflow_api.py`

Work:

- [x] Add optional `policy.max_budget_units` and optional positive integer
  `node.budget_units` with default `1` for worker/manager nodes and `0` for
  control-plane nodes.
- [x] Validate budget values and expose them in builder context, templates, raw
  JSON, and the policy/node forms.
- [x] Track `counters.budget_units_spent`; increment after successful job
  creation even if that job later fails.
- [x] Enforce the budget in the shared pre-job guard so worker nodes, manager
  nodes, loops, resumes, and manager-selected targets use the same path.
- [x] Validate the total cost of all unique targets in a manager proposal before
  accepting any target.
- [x] Add `policy.stop_on_first_failure` with default `true`.
- [x] When false, record failed nodes, continue only already-ready independent
  nodes, do not traverse failed-node edges, and finish failed after the queue
  drains. Include a stable failure summary in run counters/events.
- [x] Ensure `join all` cannot succeed when an upstream fails; later quorum work
  may still succeed when enough other upstreams complete.

Checks:

- [x] budget boundary permits exact spend and rejects the next job
- [x] failed jobs still consume reserved budget units
- [x] manager multi-target proposal is rejected when combined cost exceeds cap
- [x] stop-on-first-failure true preserves current immediate failure behavior
- [x] false lets an independent ready branch finish, then marks the run failed
- [x] no outgoing edge is taken from a failed node

## Milestone 12: Human-Gate Branch Selection

Goal: let one human gate choose one declared branch while preserving current
approve/reject gates.

Files:

- `atlas/db.py`
- `atlas/workflows.py`
- `atlas/app.py`
- `atlas/static/index.html`
- `atlas/static/app.js`
- `scripts/check_workflow_db.py`
- `scripts/check_workflows.py`
- `scripts/check_workflow_api.py`

Work:

- [x] Extend `human_gate` with optional unique choices containing `id` and
  `label`.
- [x] Add `human_selected` edge condition whose `choice` must be declared by the
  source gate.
- [x] Add approval columns for encoded choices and selected choice with a
  backward-compatible SQLite migration.
- [x] Add `POST /api/approvals/{id}/choose` with body `{"choice":"..."}`.
- [x] Refactor gate continuation so outgoing edges are staged only after the
  decision. Existing gates without choices keep approve/reject behavior.
- [x] Validate the selected choice, evaluate only its declared outgoing edges,
  and retain duplicate-decision prevention.
- [x] Record choice in workflow events, audit, run detail, and dashboard.
- [x] Add choice inputs to Builder Lite and builder context.

Checks:

- [x] legacy approve/reject gate behavior remains unchanged
- [x] valid choice schedules only its matching branch once
- [x] unknown choice and `human_selected` on a non-gate are rejected
- [x] duplicate choice cannot schedule downstream work twice
- [x] choice survives API readback and appears in event/audit history

## Milestone 13: Quorum Joins

Goal: allow a join to continue after a declared number of successful upstreams.

Files:

- `atlas/workflows.py`
- `atlas/static/index.html`
- `atlas/static/app.js`
- `scripts/check_workflows.py`
- `scripts/check_workflow_api.py`

Work:

- [x] Add join mode `quorum` with required positive integer `quorum`.
- [x] Validate quorum does not exceed the number of distinct incoming upstream
  nodes.
- [x] Reuse join completion tracking and schedule downstream once when successful
  upstream count reaches quorum.
- [x] Track failed upstreams and fail the run when remaining possible successes
  can no longer satisfy quorum.
- [x] Keep already-running/ready sibling branches bounded by normal policy; do
  not cancel them implicitly when quorum is reached.
- [x] Add quorum to builder context, Builder Lite, run detail, and templates only
  where it improves an existing example.

Checks:

- [x] quorum 2-of-3 continues after two successful upstreams
- [x] duplicate incoming edges count one upstream once
- [x] downstream node is scheduled once when later upstreams finish
- [x] impossible quorum fails with an explicit event
- [x] invalid zero/oversized quorum is rejected before save/run

## Milestone 14: File Upload Artifacts

Goal: upload bounded files as opaque `file_ref` artifacts without exposing
arbitrary server paths.

Files:

- `atlas/config.py`
- `atlas/db.py`
- `atlas/app.py`
- `atlas/static/index.html`
- `atlas/static/app.js`
- `scripts/check_workflow_api.py`

Work:

- [x] Add a configurable upload directory beside the SQLite DB and a conservative
  maximum upload size.
- [x] Add `POST /api/workflow-runs/{id}/files?key=...` accepting a direct binary
  body plus filename/content-type headers; reject missing length, oversized
  bodies, invalid keys, and unknown runs.
- [x] Store content under a generated opaque id using an atomic temporary-file
  rename. Never use the client filename as a path.
- [x] Create a `file_ref` artifact with relative opaque reference and metadata:
  original filename, media type, byte size, and SHA-256.
- [x] Add `GET /api/artifacts/{id}/content` for `file_ref` download with strict
  root containment and safe Content-Disposition.
- [x] Fire the existing `artifact_created` trigger after upload.
- [x] Add browser-native file/key controls and artifact download links.
- [x] Keep text/JSON artifact APIs unchanged; do not add a general file manager
  or multipart dependency.

Checks:

- [x] binary round-trip preserves bytes, size, and SHA-256
- [x] filename traversal cannot affect the stored path or response headers
- [x] oversized and incomplete uploads leave no artifact or temporary file
- [x] non-file artifact content download is rejected
- [x] upload fires one deduplicated artifact-created event

## Milestone 15: Restart Recovery

Goal: make persisted workflow state safe and explicit after Atlas restarts,
without pretending an interrupted thClaws stream can resume.

Files:

- `atlas/db.py`
- `atlas/app.py`
- `atlas/workflows.py`
- `atlas/static/app.js`
- `scripts/check_workflow_db.py`
- `scripts/check_workflows.py`
- `scripts/check_workflow_api.py`

Work:

- [x] On runtime startup, reconcile non-terminal runs that have no live Atlas
  runner thread.
- [x] Leave `paused` and `waiting_for_human` runs intact. Preserve pending
  approvals so they remain decidable after restart.
- [x] Mark runs with interrupted worker/manager nodes as `recovery_required` and
  append an auditable recovery event; never auto-create a replacement job.
- [x] Add recovery detail to run API/dashboard, including interrupted node/job
  ids and the duplicate-side-effect warning.
- [x] Add explicit retry action requiring `{"retry_interrupted":true}`. Requeue
  only interrupted/incomplete nodes and continue skipping completed nodes.
- [x] Reject ordinary resume from `recovery_required`; cancel remains available.
- [x] Recover queued control-plane-only work automatically only when it cannot
  repeat an external worker side effect.
- [x] Document that native active-stream recovery remains blocked on thClaws
  remote job status/resume support.

Checks:

- [x] reopening the DB creates no duplicate worker job
- [x] interrupted run becomes recovery-required with ordered event/audit history
- [x] explicit retry reruns only incomplete nodes
- [x] completed nodes and accepted approvals are not repeated
- [x] pending human approval can still be decided after restart
- [x] cancel from recovery-required reaches terminal state

## Cross-Cutting Rules For Milestones 9–15

- Reuse existing graph/reference/policy/trigger validators, routing, workflow
  events, audit helpers, and duplicate-scheduling guards.
- Builder output and human/manager choices remain proposals; Atlas validates and
  enforces every id, edge, artifact, policy, and limit.
- Keep loops and recovery retries bounded. Never create an autonomous retry or
  manager loop.
- Use Python stdlib and browser-native JavaScript/CSS only.
- Preserve raw JSON editors and existing API behavior unless a milestone states
  an explicit backward-compatible extension.
- Add the smallest runnable DB/engine/API check for each non-trivial behavior.
- Update README, user guide, workflow examples, and the main plan only after the
  corresponding behavior and checks pass. Do not mark checklist items early.

## Completion Gate

Before committing a workflow milestone:

```bash
python3 -m py_compile atlas/config.py atlas/db.py atlas/app.py atlas/jobs.py atlas/workflows.py atlas/router.py atlas/workflow_templates.py scripts/check_workflows.py scripts/check_workflow_api.py
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
- edit policy through form and raw JSON in both directions
- explain/repair without auto-save
- enable/disable an existing trigger
- review/apply a worker suggestion
- inspect budget/failure counters
- choose a human-gate branch
- inspect quorum progress/failure
- upload/download a file artifact
- inspect and explicitly retry a recovery-required run
