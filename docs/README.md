# Atlas Documentation

[ภาษาไทย](#คู่มือสำหรับผู้ใช้งาน) · [English](#user-guides)

เอกสารในโฟลเดอร์นี้แยกตามวัตถุประสงค์ เพื่อไม่ให้คู่มือใช้งานปะปนกับแผนงาน
หรือ prompt สำหรับการพัฒนา

## คู่มือสำหรับผู้ใช้งาน

- [คู่มือใช้งาน Atlas ผ่านเว็บ (ภาษาไทย)](guides/web-user-guide-th.md) — เริ่มระบบ,
  Fleet, Command, Jobs, Workflows, Monitor, Audit และการแก้ปัญหา
- [คู่มือใช้งานผ่านเว็บ (English)](guides/web-user-guide-en.md) — คู่มือฉบับภาษาอังกฤษ
- [ตัวอย่าง Workflow](workflow-examples.md) — graph, condition, join, gate, manager,
  trigger, artifact และตัวอย่าง API
- [สคริปต์ Demo](demo-script.md) — ลำดับสำหรับสาธิตระบบ
- [บทพูด Booth AI Party 2026 (ไทย)](booth-ai-party-2026-th.md) — บทพูดสาธิตหน้าบูธ,
  T9a/T9b file handoff ระหว่าง worker

## User guides

- [Atlas Web User Guide — Thai](guides/web-user-guide-th.md)
- [Atlas Web User Guide — English](guides/web-user-guide-en.md)
- [Workflow Examples](workflow-examples.md)
- [Demo Script](demo-script.md)
- [Booth AI Party 2026 Talk Script (English)](booth-ai-party-2026-en.md) — booth demo
  walkthrough, T9a/T9b file handoff between workers

## เอกสารอ้างอิง / Reference

- [Concepts & Reference (EN)](concepts-en.md) · [นิยามและอ้างอิง (ไทย)](concepts-th.md)
  — นิยามทุกตัวที่ระบบใช้จริง: node type, join mode, condition, artifact kind, policy,
  trigger, state ฯลฯ
- Artifact โดยเฉพาะ: [ความหมายและตัวอย่าง (ไทย)](concepts-th.md#9-ชนิด-artifact) ·
  [definition and examples (English)](concepts-en.md#9-artifact-kinds)
- [Architecture](architecture.md) — บทบาท runtime, routing, state และ workflow execution
- [Workflow Patterns](workflow-patterns.md) — 6 multi-agent pattern map กับ Atlas:
  อะไรทำได้ (fixed N), อะไรติด (dynamic fan-out / map), workaround และเงื่อนไขควรสร้างเพิ่ม
- [thClaws Capability Matrix](thclaws-capability-matrix.md) — ความสามารถที่ใช้ได้ทันที,
  workaround และข้อจำกัดของ thClaws
- [Upstream requests](upstream/) — archived "Ideas" posts filed against thClaws upstream:
  [Job/artifact API](upstream/thclaws-artifact-api-idea.md) (#178, superseded in part by the
  T9a/T9b Job Artifact API — see thClaws Worker Protocol Contract) ·
  [capabilities contract](upstream/thclaws-capabilities-contract-idea.md) (#179, still outstanding)

## การตัดสินใจเชิงสถาปัตยกรรม / ADRs

- [ADR 0001 — Multi-tenancy: silo vs pooled](adr/0001-multi-tenancy-silo-vs-pooled.md)
  — decision (silo / instance-per-tenant), the exact pooled change-list + migration path
  if ever approved, and the revisit trigger. Atlas core stays `tenant_id`-free (guarded
  by `scripts/check_silo.py`).

## ปฏิบัติการ / Operations

- [Deployment (Production)](ops/deployment.md) — secure launcher, systemd unit,
  reverse-proxy TLS/gzip/request-size, request logging, config reference
- [Backup & Restore](ops/backup-restore.md) — online `.backup`, restore runbook,
  single-writer caveat
- [Atlas Fleet](../fleet/README.md) — multi-instance registry + `atlas-fleet` CLI
  (provision / list / health / usage-pull); separate component, no tenant logic in core

## Specifications สำหรับ Programmer

- [API Reference (ไทย)](specs/api-reference-th.md) ·
  [English](specs/api-reference-en.md) · [OpenAPI 3.1](specs/openapi.yaml) — endpoints,
  authentication, payloads, SSE, files, errors และ client checklist
- [Visual Workflow Builder Specification (ไทย)](specs/workflow-visual-builder-spec-th.md) ·
  [English](specs/workflow-visual-builder-spec-en.md) — visual grammar, drag/drop rules,
  JSON mapping, validation, AI workflow และ QA criteria
- [Input Adapter Contract](specs/input-adapter-contract.md) — the ingress envelope
  (`_meta.source` / `_meta.reply`) any channel (LINE, email→n8n, web form) POSTs to
  `/fire` or `/workflow-runs`, provenance→audit, and the return-path addressing
- [Solution Pack Format](specs/pack-format.md) — pack bundle schema, validation rules,
  `/api/packs` endpoints, and the `gov_complaint` reference pack
- [CDR Record Schema (proposed)](specs/cdr-schema.md) — Fleet's per-tenant usage/charge
  detail record CSV (export only, pending NT billing confirmation)
- [BYOK Key Injection](specs/byok-key-injection.md) — write-only key-injection helper
  (`atlas.byok`); Atlas core stores no model key
- [Managed Inference Gateway (readiness)](specs/managed-inference.md) — multi-provider
  gateway-worker + token/GPU-hour metering design (worker/gateway layer, not core)
- [thClaws Worker Protocol Contract](specs/thclaws-worker-contract.md) — tested
  endpoint/auth matrix, SSE events, sync busy semantics, and per-worker sync gate
- [Threat Model & Deployment Assumptions](specs/threat-model.md) — trust boundaries, accepted
  residual risks (owner + re-open trigger), and the definition-of-done stop criterion
- [Low-findings backlog](specs/backlog.md) — Low items that do not block sign-off (DoD #7),
  each with an owner and a promote-to-work trigger
- [Workflow Definition JSON Schema](specs/workflow-definition.schema.json)
- [Workflow Trigger JSON Schema](specs/workflow-trigger.schema.json)
- [AI Workflow Draft JSON Schema](specs/workflow-ai-draft.schema.json)

## แผนงาน / Plans

ไฟล์ใน [`plans/`](plans/) เป็นเอกสารออกแบบหรือแผนงาน ไม่ใช่คู่มือผู้ใช้:

- [Workflow Engine Plan](plans/workflow-engine-plan.md) — data model, execution model,
  API และ dashboard design
- [Workflow Engine Coding Plan](plans/workflow-engine-coding-plan.md) — milestone และ
  implementation checklist
- [Sovereign Platform Plan (GA Gaps)](plans/sovereign-platform-plan.md) — สิ่งที่ยังขาด
  เพื่อไป GA: auth/RBAC, multi-tenant แบบ silo, metering/billing, fleet provisioning, hardening
- Usage Metering & Billing Plan (`plans/usage-metering-billing-plan.md`, internal — not
  committed) — BYOK, billable unit, CDR export, metering schema, B-milestones
- [GA Completion Plan (run-to-completion)](plans/ga-completion-plan.md) — ลำดับเดินจนจบ
  ทุกงานที่เหลือ (M3→M9 + B3–B7), definition-of-done ต่อ milestone, scope tiers และ commit policy
- [Input Adapter & Return Path Plan](plans/input-adapter-return-path-plan.md) — IA-1
  (ingress envelope + provenance→audit) และ OB-1 (signed outbound delivery / ขากลับ),
  DoD + hermetic check ต่อ milestone, additive และคง silo
- [thClaws API Adoption Plan](plans/thclaws-api-adoption-plan.md) — approved now:
  T0 worker contract, T1a token capture, T2 structured event UI, T3 async
  x_callback (แล้วจึง T1b cost estimate, T4 advisory routing); deferred พร้อม
  unblock ชัดเจน: T5–T6 file collect/push (sync-gated), T7 worker deploy,
  T8 chat-completions (benchmark-gated); risk register + review deltas + DoD
  ต่อ milestone

## Prompt files

ไฟล์ใน [`prompts/`](prompts/) ใช้เป็น prompt สำหรับงานพัฒนา:

- [Workflow Engine Coding Spin Prompts](prompts/workflow-engine-spin-prompts.md)
- [Sovereign Platform Spin Prompts](prompts/sovereign-platform-spin-prompts.md) —
  prompts สำหรับ implement ตาม sovereign platform plan (M1–M3 พร้อมรัน)
- [GA Completion — Autonomous Spin Prompts](prompts/ga-completion-spin-prompts.md) —
  driver ที่ไล่ทำ M3→M9 ต่อเนื่องจนจบ, commit เมื่อ gate เขียวจบแต่ละ milestone
- [thClaws API Adoption Spin Prompts](prompts/thclaws-api-adoption-spin-prompts.md) —
  driver สำหรับ T0→T1a→T2→T3 (milestone ที่ approved) พร้อม Claude review loop
  ต่อ milestone: implement → mutation-test → gate → lint → independent Claude
  review (feature-dev:code-reviewer subagent) → แก้ findings → commit → review HEAD
- [Input Adapter & Return Path — Spin Prompts](prompts/input-adapter-return-path-spin-prompts.md)
  — driver สำหรับ IA-1 → OB-1 (ทำต่อเนื่อง, commit เมื่อ check เขียว)

## โครงสร้าง

```text
docs/
├── README.md
├── adr/
│   └── 0001-multi-tenancy-silo-vs-pooled.md
├── guides/
│   ├── web-user-guide-th.md
│   └── web-user-guide-en.md
├── ops/
│   ├── deployment.md
│   ├── backup-restore.md
│   └── atlas.service
├── plans/
│   ├── workflow-engine-plan.md
│   ├── workflow-engine-coding-plan.md
│   ├── sovereign-platform-plan.md
│   ├── usage-metering-billing-plan.md   (internal, not committed)
│   ├── nt-aiaas-business-plan.md        (internal, not committed)
│   ├── ga-completion-plan.md
│   └── input-adapter-return-path-plan.md
├── prompts/
│   ├── workflow-engine-spin-prompts.md
│   ├── sovereign-platform-spin-prompts.md
│   ├── ga-completion-spin-prompts.md
│   └── input-adapter-return-path-spin-prompts.md
├── specs/
│   ├── api-reference-th.md
│   ├── api-reference-en.md
│   ├── openapi.yaml
│   ├── pack-format.md
│   ├── input-adapter-contract.md
│   ├── thclaws-worker-contract.md
│   ├── workflow-visual-builder-spec-th.md
│   ├── workflow-visual-builder-spec-en.md
│   ├── workflow-definition.schema.json
│   ├── workflow-trigger.schema.json
│   └── workflow-ai-draft.schema.json
├── upstream/
│   ├── thclaws-artifact-api-idea.md
│   └── thclaws-capabilities-contract-idea.md
├── concepts-en.md
├── concepts-th.md
├── architecture.md
├── workflow-patterns.md
├── thclaws-capability-matrix.md
├── workflow-examples.md
├── demo-script.md
├── booth-ai-party-2026-en.md
└── booth-ai-party-2026-th.md
```

เมื่อเพิ่มเอกสารใหม่ ให้จัดไว้ตามกลุ่มข้างต้นและเพิ่มลิงก์ในไฟล์นี้
