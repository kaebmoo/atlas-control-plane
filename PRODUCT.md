# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary: NT (National Telecom) internal ops team — engineers and operators who
run the thClaws worker fleet day to day: registering workers, watching live
jobs, monitoring workflow runs, approving gated steps, checking audit and
usage. RBAC roles are admin / operator / viewer / auditor; the auditor role
exists for compliance review, not daily operation.

## Product Purpose

Atlas is a standalone control plane that coordinates many `thclaws --serve`
workers: route work to the right machine, stream results live, preserve
job/session history, and chain workers into multi-agent workflows. The
embedded browser ops console is the operator-facing surface for fleet, live
jobs, workflow-run monitoring, audit, usage, and account setup. Success means
an operator can see fleet health and intervene in a running workflow (pause /
resume / cancel / approve) without leaving the console or touching the API by
hand.

## Positioning

Deliberately sovereign and self-contained: pure Python stdlib server
(`http.server`) plus SQLite — no external database, no framework, no external
network dependencies, HMAC-signed usage export for air-gapped transfer. Atlas
never forks or patches thClaws; it only speaks thClaws's public HTTP API. The
embedded console is intentionally minimal; the full authoring frontend
(workflow building, approvals UX, triggers, deliveries) is the separate
flow-designer product built on Atlas's API.

## Operating Context

- One thClaws worker runtime per machine; Atlas polls health/capabilities and
  routes jobs manually or automatically (bindings, tags, roles, prompt hints).
- Console sections in `atlas/static/`: fleet, live job streams, workflow-run
  monitoring, audit, usage, accounts. Served by the same stdlib server.
- Ingress from LINE / email (n8n) / web forms via the Input Adapter envelope;
  results can return via signed outbound webhooks (Return Path).
- The repo also carries NT Shield proposal material in `docs/`; the console may
  be shown in demos, but the internal ops team is the design priority.

## Capabilities and Constraints

- **Static UI gate:** `scripts/check_workflow_api.py` asserts exact HTML/JS
  substrings in the console. Any console edit must preserve the asserted ids,
  classes, and markers, and the full gate (`scripts/gate.sh`) plus
  `scripts/lint.sh` must stay green — both are required CI checks on protected
  `main`.
- **No external assets:** air-gapped-friendly deployment means the console
  must not load CDN scripts, remote fonts, or any off-box resource. Fonts ship
  locally in `atlas/static/fonts/`.
- **Scope boundary:** do not grow console features that belong to
  flow-designer (workflow authoring, trigger management UX, delivery UX).
  Ad-hoc job submission with routing/handoff is API-only by design.
- **Language:** UI copy is English-primary; Thai is supplementary. Docs keep
  EN and TH in separate single-language files, never interleaved.

## Brand Commitments

NT brand is **not binding** for the console (confirmed 2026-08-07): visual
direction is free to serve the ops tool best. NT assets exist and may be used
when they help — `atlas/static/nt-logo.png`, `nt-mark.png`, and real NT font
files in `atlas/static/fonts/`.

## Evidence on Hand

- `design_handoff_ops_console/` — Claude Design handoff bundle: HTML/CSS
  prototypes of the ops console redesign, `.dc.html` view-model, NT design
  tokens and fonts, rendered screenshots. Prototypes are reference, not
  production code.
- Live product truth is the running console itself (`python -m atlas` serving
  `atlas/static/`), exercised end-to-end by the gate's Playwright checks.

## Product Principles

1. Operator speed over expression: scanability, dense-but-legible status, and
   safe intervention controls outrank visual flourish.
2. The gate is the contract: no UI change ships that breaks the asserted
   markers or the end-to-end gate.
3. Stay minimal on purpose: resist console feature growth that duplicates
   flow-designer; every screen must serve fleet operation, monitoring, or
   audit.
4. Self-contained everywhere: no external runtime, no external assets, works
   air-gapped.
5. Auditability is a UI feature: who did what (actors, approvals, usage) must
   stay visible and exportable, not buried.
