# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`notify/` — the Atlas Notify sidecar**, a deployable receiver for
  `approval_overdue` deliveries: raw-byte HMAC verification, durable SQLite
  dedup (a restart mid-retry no longer re-notifies), SMTP + Telegram channels
  fanning out independently per target, and routing from a JSON config file
  (`node_key` × `level`, walk-down + fallback). Secrets live only in the
  environment — the sidecar refuses a config containing password/token keys —
  and the composed email subject is newline-collapsed at the header boundary
  (header-injection guard). One artifact for both managed and BYO operation;
  `notify/check_notify.py` joins the gate with 9 declared mutation targets.
- The `approval_overdue` webhook body is now a **declared contract v1**:
  additive-only fields, and a breaking change ships as a new event name
  (`approval_overdue.v2`), never as a mutation. Declared in the API reference
  §14 (EN/TH), `input-adapter-contract.md` §7.1, and machine-readably as a
  `webhooks:` section + `ApprovalOverdueEvent` schema in `openapi.yaml`
  (bumped to 1.4.0); `scripts/check_docs.py` now fails if any copy of the
  declaration vanishes, and pins EN/TH heading-level parity.
- `docs/plans/atlas-notify-plan.md` — the plan for promoting the POC receiver
  into a deployable `notify/` sidecar (SQLite dedup + SMTP + Telegram), with
  the deliberately deferred items (LINE, calendars, routing UI) recorded.
- Approval SLA reminders. A pending human approval ages in the scheduler tick and
  emits a signed `approval_overdue` webhook once per configured threshold
  (`policy.approval_webhook_url` / `policy.approval_overdue_hours`, defaulting to
  `ATLAS_APPROVAL_WEBHOOK_URL` / `ATLAS_APPROVAL_OVERDUE_HOURS`). The position in
  the hours list is the escalation level. Migration 017 adds
  `approvals.overdue_level` and `deliveries.payload`.
- `POST /api/workflows/{id}/test-approval-webhook` sends one synthetic reminder to
  the URL the sweep would really use, writing no delivery row, so a wrong receiver
  surfaces in a second rather than at the first real threshold days later.
- `poc/approval_reminder_receiver.py`, a runnable stdlib reference receiver —
  Atlas only POSTs, so the half that turns a delivery into a message to a person
  has to be built, and nothing said so.
- The AI builder context gained a per-type trigger contract, a `dsl_boundary`
  block, and exported vocabulary constants locked to the validator by a
  set-equality check.

### Fixed

- **Time parked at a human gate no longer counts against `policy.max_minutes`.**
  It was wall clock from `started_at`, so an approval answered later than
  `max_minutes` (default 30 minutes) was recorded `approved` and then the run
  failed — the node that approval authorized never dispatched, and the approver
  saw their decision accepted and discarded.
- `SIGTERM` now runs the same shutdown as Ctrl+C. It previously terminated the
  process outright, skipping every `finally`, which is the signal
  `systemctl stop` sends — so the deployed shutdown path was the ungraceful one.
- A model reply wrapped in a Markdown fence is parsed instead of costing a paid
  retry, and non-object items in a draft's `triggers` are dropped with a warning
  quoting each rather than failing the whole draft.

- Workflow status is now execution policy, enforced at every start path by one
  shared guard (`ensure_workflow_runnable`): `draft` allows explicit test runs
  only, `active` allows test and production, `disabled` blocks every run.
  `POST /api/workflow-runs` accepts `execution_mode` (`test` | `production`,
  omitted = `production` so legacy callers fail closed against drafts). A status
  refusal is `409` with the stable body
  `{"error": "workflow_not_runnable", "reason": …, "status": …}` and creates no
  run; trigger fire always uses production mode and records the refusal as a
  `failed` trigger event. `status` is validated against the closed vocabulary
  `draft`/`active`/`disabled` on create and update (create still defaults to
  `draft`), status changes are audited as `workflow_definition.status_change`
  with the old/new pair, and migration 016 backfills pre-enforcement rows to
  `active` (preserving explicit `disabled`, audited as
  `workflow_definition.status_backfill`) so no existing workflow stops running.
  Hermetic coverage: `scripts/check_workflow_status.py` in the gate.
- Added held runs: `POST /api/workflow-runs` accepts `"hold": true` to create a
  born-paused run so input files can be attached race-free before an explicit
  resume starts it (event `run_created_held`, audit `workflow.run_created_held`).
- Added the `document_brief` bundled solution pack — a file-in/file-out demo
  (held run → `upload_*` handoff → analyst writes `executive_brief.md` →
  human review), plus a packs check that validates and imports every bundled
  pack on a bare database.

- Added the node field `collect_required` (boolean, default `false`): a workflow
  worker/manager node that declares `collect_files` can now fail the NODE when
  the collection produced no artifact, instead of completing silently with no
  files. The job itself still succeeds — collection never changes a job outcome.
  Requires `collect_files`; the `document_brief` pack's analyst node uses it.
- Added a `warnings` array to the workflow create, update, and validate responses.
  It reports fields that are accepted but inert — currently `collect_files` on a
  node that is not a `worker`/`manager`, which older graphs may carry and which
  has no runtime effect. Such graphs still save, so no existing definition breaks.
  `POST /api/packs/import` returns the same array, named per workflow, so importing
  a bundle is not a quieter way to introduce the field. Workflow read responses
  carry it too (inside each definition), and the ops console shows it as a banner
  on the run detail so an operator sees why a declared setting had no effect.
- Added a windowed global artifact listing endpoint, `GET /api/artifacts`, with
  truthful totals and an opt-in metadata-only view with strict selectors.
- Added `workflow.interface` v1 as an additive input/output contract for
  workflow definitions, direct runs, trigger fires, run snapshots, and solution
  packs, with ADR 0002 documenting the contract.
- Added in-console approval handling and a self-hosted documentation page to the
  embedded ops console.

### Changed

- Slimmed the embedded dashboard into a minimal NT-styled ops console while
  clarifying that the full workflow-authoring frontend lives in `flow-designer`.
- Improved ops-console copy, mobile behavior, accessibility, error states,
  confirmation flows, and audit depth.
- Changed `POST /api/workflow-runs/{run_id}/files` to reject uploads onto a run
  that already finished (`succeeded`, `failed`, `cancelled`) with `409` instead
  of a silent `201`: such a file can never be pushed to a worker or read by a
  node, so it only became a stranded artifact that looked like a successful
  attachment. Every non-terminal run state still accepts uploads. The state is
  re-checked atomically with the artifact insert, so a run cancelled or finished
  while the body is still streaming in is also rejected.

### Fixed

- Fixed a workflow node's job being linked to its runtime node only after dispatch
  had already started: a fast worker could finish and be collected first, keying
  that node's files `files.<relpath>` with a null run id — detached from the run,
  unmatched by `push_files` globs, and read as "no artifacts" by
  `collect_required`. The link is now written before the job service dispatches.
- Fixed a workflow node's file collection failing invisibly at run level: the
  collection outcome is now mirrored onto the run timeline as `files.collected`
  (`count`/`requested`) or `files.collection_failed` (redacted `error`/
  `requested`) with the node key — counts only, never a file list. Standalone
  jobs are unchanged.
- Fixed `push_files` intents being dropped silently on edges taken by the
  human-gate decision path (and lost on restart mid-run): push intents now ride
  the run's persisted counters, so a gate may sit between a collector and the
  worker that receives its files.
- Closed a direct `run_workflow` bypass around workflow-interface validation and
  widened coverage for interface edge cases.
- Fixed finite-number validation to use `math.isfinite`.

## [0.2.0] - 2026-07-21

[Release notes](docs/RELEASE_NOTES_v0.2.0.md)

### Added

- Added per-instance identity and access control: users, admin/operator/viewer/
  auditor RBAC, per-user API tokens, dashboard login/logout, and authenticated
  audit actors.
- Added bounded dashboard sessions, login rate limiting, and HTTP/1.1
  keep-alive hardening for rejected requests with unread bodies.
- Added idempotent usage metering, token capture from workers, non-billable cost
  estimates, signed JSON/CSV usage exports, a dashboard Usage view, and
  run-count threshold alerts.
- Added Atlas Fleet as a separate instance registry and provisioning component,
  with health checks, usage pull, CDR export, and token sidecar handling.
- Added government solution packs, additive `/api/packs` endpoints, pack import/
  export, pack signing, and local registry readiness.
- Added BYOK key-injection tooling and managed-inference readiness documentation.
- Added Input Adapter ingress for source envelopes and provenance audit, plus
  signed outbound delivery with retry, dead-lettering, and delivery APIs.
- Added thClaws worker integration milestones: tested worker contract, structured
  event parsing, job timelines, async callback execution, advisory worker state,
  file collection, and file handoff through thClaws Job Artifacts and
  `POST /v1/inputs`.
- Added an NT design-system dashboard refresh, full API coverage in the
  dashboard, and a headless API/static-UI split with CORS configuration.
- Added workflow UX support: cursor-paged run events, optimistic workflow saves,
  workflow-level `default_reply`, and SSE keepalive frames.
- Added observability and compliance closeout items: metrics endpoint, audit
  export, artifact classification, and purge support.
- Added versioned migrations, production deployment tooling, backup/restore
  support, request logging options, static analysis, stress checks, parser fuzz,
  mypy, and release-gate documentation.
- Added the silo multi-tenancy ADR and guard that keeps `tenant_id` out of Atlas
  core.

### Changed

- Kept `/api/*` changes additive; existing endpoint paths and response shapes
  were not intentionally changed.
- Replaced earlier sync-tar file collection and handoff assumptions with the
  shipped thClaws Job Artifacts plus `POST /v1/inputs` path.
- Clarified the threat model, accepted residual risks, release baseline, and
  bug-hunt workflow.

### Removed

- Removed the legacy workspace sync/tar extraction path from the shipped
  collect/handoff workflow paths.

### Fixed

- Closed multiple adversarial bug-hunt findings across workflow state
  transitions, callback convergence, delivery attempts, file handoff, artifact
  validation, worker routing, session binding, trigger dedupe, migrations, and
  restart recovery.
- Fixed stored-XSS exposure, authenticated artifact download handling, unsafe
  query-token authentication outside SSE streams, stream termination handling,
  worker health ranking, workflow trigger enablement, stale workflow saves, and
  dashboard visibility issues.
- Hardened atomic durable writes, guarded SQL/urlopen usage, CSV/env injection
  handling, pack workspace ownership, backup completeness, provision rollback,
  and sidecar writes.

### Security

- Added authenticated encryption support for worker tokens when
  `ATLAS_SECRET_KEY` is set.
- Added HMAC signing for usage exports, pack bundles, worker callbacks, and
  outbound deliveries.
- Added URL, DNS-rebind, credential-leak, callback-token, and artifact-path
  guards across outbound delivery, async callbacks, file collection, and file
  handoff.

## [0.1.0] - 2026-06-29

### Added

- Added the initial Atlas control plane for coordinating `thclaws --serve`
  workers with SQLite persistence and no external runtime dependencies.
- Added worker registration, health/capability polling, workspace mapping,
  manual and automatic routing, conversations, job submission, streaming output,
  event replay, cancellation, and audit logging.
- Added the baseline workflow engine: definitions, runs, condition edges,
  guarded loops, fan-out, `all`/`any` joins, workflow APIs, approval gates,
  manager workers, templates, prompt rendering, validation, and observability.
- Added text/JSON artifacts, binary file artifacts, upload/download APIs, and
  workflow-builder draft/explain/repair/suggestion APIs.
- Added the embedded browser dashboard with sidebar navigation and views for
  workers, workspaces, jobs, workflows, runs, artifacts, audit, and setup.
- Added the first documentation set: README, documentation index, bilingual web
  guides, concepts reference, API reference, OpenAPI spec, workflow examples,
  visual workflow builder specification, architecture/planning docs, and license.

[Unreleased]: https://github.com/kaebmoo/atlas-control-plane/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/kaebmoo/atlas-control-plane/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kaebmoo/atlas-control-plane/releases/tag/v0.1.0
