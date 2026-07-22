# v0.2.0 — Sovereign Control Plane: thClaws Integration & GA Hardening

**English** · [ภาษาไทย](RELEASE_NOTES_v0.2.0-th.md)

Atlas is a standalone HTTP control plane (Python standard library only) that coordinates many
`thclaws --serve` workers from one browser dashboard. It owns routing, workflow state, jobs,
sessions, policy, audit, approvals, usage metering, and outbound delivery; thClaws remains the
worker runtime.

This is the first release since `v0.1.0`. It closes the run-to-completion GA effort and adopts
thClaws' native worker contract end to end. Every change is **additive** — there are no breaking
API changes, and new schema migrations run automatically on start.

**Full changelog:** https://github.com/kaebmoo/atlas-control-plane/compare/v0.1.0...v0.2.0

---

## Highlights

- **thClaws worker integration end to end** — token usage capture, structured event timelines,
  fire-and-forget async jobs via worker callbacks, advisory worker state, and file collection /
  handoff over thClaws' native Job Artifacts + `POST /v1/inputs` (the earlier sync-tar transport
  is retired).
- **Sovereign platform features** — Atlas Fleet (multi-instance registry + provisioning), CDR
  usage export, a government solution pack with pack signing, and a BYOK key-injection helper.
- **Identity, sessions, and delivery security** — per-instance RBAC, bounded dashboard sessions
  with login rate limiting, HTTP/1.1 keep-alive hardening, and signed outbound delivery.
- **Operator experience** — a full NT-design-system dashboard with complete API coverage, a
  headless API / static-UI split, and a Usage view with cost estimates and threshold alerts.
- **Hardening** — versioned migrations, production ops tooling, and many independent adversarial
  bug-hunt / security-review rounds. No open Atlas security findings.

---

## thClaws worker integration (T0–T9)

- **Worker contract spike (T0)** — documented the thClaws endpoint/auth matrix, SSE event
  contract, 409-busy semantics, and the persistent `sync_mode` gate against a pinned build.
- **Token usage capture (T1a/T1b)** — worker token counts flow into the metering ledger, with an
  additive non-billable cost estimate derived from pricing snapshots.
- **Structured event surfaces (T2)** — assistant text, thinking, tool/skill, usage, result, and
  error frames are parsed separately; tool/skill payloads are projected to structural metadata
  only (never persisted raw), with a per-job Timeline tab in the dashboard.
- **Async execution via worker callbacks (T3)** — fire-and-forget jobs (`execution: "callback"`)
  through a single documented pre-auth callback endpoint, secured by a per-dispatch HMAC token,
  with idempotent terminal convergence, a reaper, and restart-durable recovery.
- **Advisory worker state (T4)** — an operator-owned `sync_mode` with a pre-enable probe, used
  only as a routing tie-break, never as a hard blocker.
- **File collection & handoff (T5/T6 → T9a/T9b)** — jobs collect deliverables through thClaws Job
  Artifacts (manifest + immutable snapshots) with all-or-nothing, failure-isolated publication;
  workflow edges hand files to the next worker through Bearer-authenticated `POST /v1/inputs`
  with an exact `written[]` acknowledgment. Workspace sync, tar extraction, and the `sync_mode`
  gate are fully retired from the collect/handoff paths.

## Sovereign platform

- **Atlas Fleet (M4)** — a separate registry (its own SQLite, no shared tenant DB) with an
  `atlas-fleet` CLI for provision / list / health / usage-pull; admin tokens referenced by id
  with a `0600` secrets sidecar. Adds an unauthenticated `GET /healthz` returning only
  `{ok, service, version}` for probes.
- **CDR usage export (M5 / B3)** — deterministic per-tenant CDR CSV
  (`x-schema: atlas.cdr.v1-proposed`), export only, via `python3 -m fleet cdr`.
- **Government solution pack (M6)** — `atlas/packs.py` with additive `/api/packs`,
  `/api/packs/import`, `/api/packs/{id}/export`, and a `gov_complaint` pack (intake → triage →
  draft → human gate → publish) that runs end to end.
- **Pack signing (M8)** — HMAC-SHA256 signing/verification over a canonical pack bundle, with an
  optional `require_signature` import mode and a `python3 -m atlas.packs sign/verify` CLI.
- **BYOK & managed-inference readiness (B5 / M7 / B7)** — write-only BYOK key injection
  (`0600` env file, key never in DB / logs / responses); managed inference documented as a
  worker/gateway-layer design with no Atlas-core change.
- **Multi-tenancy decision (M9)** — an ADR recording the silo decision and the exact pooled
  change-list; no `tenant_id` in core, guarded by a check. *(Documentation / ADR only.)*

## Identity, access, and session security

- **Per-instance identity & RBAC** — admin / operator / viewer / auditor roles, per-user API
  tokens, dashboard login/logout, and authenticated audit actors.
- **Bounded dashboard sessions** — interactive login mints `purpose=session` tokens with an
  8-hour default TTL and a five-active-session cap; admin-minted API tokens stay independent.
- **Login defense** — an in-memory rate limiter before PBKDF2 (default 5/min + cooldown, `429` +
  `Retry-After`); a reverse-proxy/WAF layer is still required for durable rate limiting.
- **HTTP/1.1 keep-alive safety** — a rejected request that never consumed its body closes the
  connection so body bytes cannot desynchronize the next keep-alive request; chunked request
  bodies are rejected explicitly.

## Input & output adapters

- **Input Adapter ingress (IA-1)** — any source (LINE, email via n8n, a web form, another
  system) POSTs one JSON envelope with a reserved `_meta` (`source` / `reply`), validated and
  provenance-audited at a single choke point.
- **Signed outbound delivery (OB-1)** — a `deliveries` ledger and `OutboundService` that sends an
  HMAC-signed body to an allowlisted, DNS-rebind-pinned callback, with bounded retries,
  dead-lettering, restart-durable reconcile, and a structural URL guard that rejects any
  credential-carrying callback URL. Adds `/api/deliveries`, `/api/deliveries/{id}/retry`, and
  `/api/workflow-runs/{id}/deliver`.

## Usage metering, dashboard, and headless UI

- **Usage view (B2) + threshold alert (B4)** — a dashboard Usage view (run/job/budget totals,
  token totals, non-billable cost estimate, authenticated JSON/CSV export with no token in the
  URL) plus a read-only run-count threshold alert.
- **NT dashboard redesign + full API coverage** — an NT-design-system dashboard across all
  operator screens, surfacing workers, workspaces, jobs, live streams, workflows, artifacts,
  deliveries, usage, audit, and setup.
- **Headless API / static-UI split** — `ATLAS_SERVE_UI`, a client `API_BASE`, and an
  `ATLAS_CORS_ORIGINS` allowlist let the dashboard be hosted on any origin against a headless
  Atlas; a stdlib dev static server is included.
- **Workflow UX enablement** — cursor-paged run events, optimistic workflow saves with
  `expected_version` conflict detection, SSE `retry` + keepalive frames, and a workflow-level
  `default_reply` inherited by runs.

## Hardening & security

- **Versioned migrations + production ops (M3)** — an ordered, idempotent migration runner with a
  `schema_version`, plus `backup.sh`, `run-prod.sh`, an example systemd unit, and an optional
  JSON request log.
- **Adversarial review** — many independent bug-hunt and Codex/Claude review rounds with
  mutation-locked regression checks; observability, compliance, and cross-cutting items closed
  (metrics endpoint, audit export, artifact classification, purge).
- **No open Atlas security findings.** Two documented, intentional exceptions to per-user auth
  exist: `GET /healthz` (returns only `{ok, service, version}`) and
  `POST /api/worker-callbacks/{job_id}` (authorized by its own per-dispatch HMAC token).

---

## Upgrade notes

- **Additive, no breaking API changes.** Existing `/api/*` contracts are unchanged; new fields
  and endpoints are additive.
- **Migrations run automatically** on start via the versioned migration runner (adds the
  deliveries ledger, callback reaper index, worker `sync_mode`, `jobs.collect_files`, and
  `api_tokens.purpose` / `expires_at`). Take a backup first (`scripts/backup.sh`).
- **Session token reclassification:** identifiable legacy dashboard-login tokens are reclassified
  and revoked, so operators sign in again after upgrade. Generic admin-issued API tokens are
  unaffected.
- **Set `ATLAS_SECRET_KEY`** to enable worker-token encryption, usage/pack/delivery signing, and
  async callbacks. Async jobs and callback-node workflows also require `ATLAS_PUBLIC_BASE_URL`.
- **New environment variables include** `ATLAS_SERVE_UI`, `ATLAS_CORS_ORIGINS`,
  `ATLAS_PUBLIC_BASE_URL`, `ATLAS_OUTBOUND_ALLOWLIST`, and the request-log / timeout knobs — see
  `docs/ops/deployment.md`.

## Known limitations and external confirmations still outstanding

- **CDR schema is proposed** (`atlas.cdr.v1-proposed`) — fields/units pending confirmation with
  NT billing/mediation.
- **BYOK option-a (thClaws save-key) is a documented stub** — blocked on the upstream endpoint;
  option-b env injection works today.
- **No built-in SSO/OIDC** — local users today; OIDC is a documented extension point.
- **Provisioning target not finalized** — docker-compose / systemd on a VM by default; k8s/GDCC
  noted as alternates.
- **Single-node SQLite runtime** — Atlas is not yet horizontally scaled; managed inference (M7)
  and pooled tenancy (M9) are readiness docs / ADR, not shipped code.

## Verification

The canonical gate (`scripts/gate.sh`) is green from a clean tree, with per-feature hermetic
checks (`scripts/check_*.py`, `fleet/check_fleet.py`) and mutation-locked regression tests across
auth, jobs, workflows, usage, packs, fleet, outbound delivery, file collection/handoff, and the
headless split. Type-checking (mypy) and linting (ruff) pass.
