# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

### Fixed

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
