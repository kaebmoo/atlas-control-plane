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

## User guides

- [Atlas Web User Guide — Thai](guides/web-user-guide-th.md)
- [Atlas Web User Guide — English](guides/web-user-guide-en.md)
- [Workflow Examples](workflow-examples.md)
- [Demo Script](demo-script.md)

## เอกสารอ้างอิง / Reference

- [Concepts & Reference (EN)](concepts-en.md) · [นิยามและอ้างอิง (ไทย)](concepts-th.md)
  — นิยามทุกตัวที่ระบบใช้จริง: node type, join mode, condition, artifact kind, policy,
  trigger, state ฯลฯ
- [Architecture](architecture.md) — บทบาท runtime, routing, state และ workflow execution
- [thClaws Capability Matrix](thclaws-capability-matrix.md) — ความสามารถที่ใช้ได้ทันที,
  workaround และข้อจำกัดของ thClaws

## แผนงาน / Plans

ไฟล์ใน [`plans/`](plans/) เป็นเอกสารออกแบบหรือแผนงาน ไม่ใช่คู่มือผู้ใช้:

- [Workflow Engine Plan](plans/workflow-engine-plan.md) — data model, execution model,
  API และ dashboard design
- [Workflow Engine Coding Plan](plans/workflow-engine-coding-plan.md) — milestone และ
  implementation checklist
- [Sovereign Platform Plan (GA Gaps)](plans/sovereign-platform-plan.md) — สิ่งที่ยังขาด
  เพื่อไป GA: auth/RBAC, multi-tenant แบบ silo, metering/billing, fleet provisioning, hardening

## Prompt files

ไฟล์ใน [`prompts/`](prompts/) ใช้เป็น prompt สำหรับงานพัฒนา:

- [Workflow Engine Coding Spin Prompts](prompts/workflow-engine-spin-prompts.md)
- [Sovereign Platform Spin Prompts](prompts/sovereign-platform-spin-prompts.md) —
  prompts สำหรับ implement ตาม sovereign platform plan (M1–M3 พร้อมรัน)

## โครงสร้าง

```text
docs/
├── README.md
├── guides/
│   ├── web-user-guide-th.md
│   └── web-user-guide-en.md
├── plans/
│   ├── workflow-engine-plan.md
│   ├── workflow-engine-coding-plan.md
│   └── sovereign-platform-plan.md
├── prompts/
│   ├── workflow-engine-spin-prompts.md
│   └── sovereign-platform-spin-prompts.md
├── concepts-en.md
├── concepts-th.md
├── architecture.md
├── thclaws-capability-matrix.md
├── workflow-examples.md
└── demo-script.md
```

เมื่อเพิ่มเอกสารใหม่ ให้จัดไว้ตามกลุ่มข้างต้นและเพิ่มลิงก์ในไฟล์นี้
