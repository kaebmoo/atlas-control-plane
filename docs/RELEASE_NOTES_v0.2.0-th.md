# v0.2.0 — Sovereign Control Plane: thClaws Integration & GA Hardening

[English](RELEASE_NOTES_v0.2.0.md) · **ภาษาไทย**

Atlas เป็น HTTP control plane แบบ standalone (ใช้ Python standard library เท่านั้น) ที่ทำหน้าที่
ประสาน worker `thclaws --serve` หลายตัวจาก dashboard เดียวบน browser โดย Atlas เป็นเจ้าของ
routing, workflow state, jobs, sessions, policy, audit, approvals, usage metering และ outbound
delivery ส่วน thClaws ยังคงเป็น worker runtime

release นี้เป็นตัวแรกนับจาก `v0.1.0` เป็นการปิดงาน run-to-completion GA และ adopt worker contract
ของ thClaws แบบครบวงจร ทุกการเปลี่ยนแปลงเป็นแบบ **additive** — ไม่มี breaking API change และ
schema migration ใหม่จะรันอัตโนมัติตอน start

**Changelog ฉบับเต็ม:** https://github.com/kaebmoo/atlas-control-plane/compare/v0.1.0...v0.2.0

---

## ไฮไลต์

- **เชื่อม thClaws worker แบบครบวงจร** — จับ token usage, timeline ของ structured event, async
  job แบบ fire-and-forget ผ่าน worker callback, advisory worker state และ file collection /
  handoff ผ่าน Job Artifacts + `POST /v1/inputs` ของ thClaws (transport แบบ sync-tar เดิมถูกเลิกใช้)
- **ฟีเจอร์ sovereign platform** — Atlas Fleet (registry หลาย instance + provisioning), CDR usage
  export, government solution pack พร้อม pack signing และ BYOK key-injection helper
- **ความปลอดภัยของ identity / session / delivery** — RBAC ต่อ instance, bounded dashboard session
  พร้อม login rate limiting, การ harden HTTP/1.1 keep-alive และ signed outbound delivery
- **ประสบการณ์ฝั่ง operator** — dashboard ตาม NT design system ที่ครอบคลุม API ครบ, การแยก
  headless API / static UI และ Usage view พร้อม cost estimate และ threshold alert
- **การ harden** — versioned migration, เครื่องมือ ops สำหรับ production และรอบ adversarial
  bug-hunt / security review หลายรอบ ไม่มี security finding ของ Atlas ค้าง

---

## เชื่อม thClaws worker (T0–T9)

- **Worker contract spike (T0)** — จัดทำเอกสาร endpoint/auth matrix, SSE event contract, semantics
  ของ 409-busy และ persistent `sync_mode` gate เทียบกับ build ที่ pin ไว้
- **จับ token usage (T1a/T1b)** — จำนวน token ของ worker ไหลเข้าสู่ metering ledger พร้อม cost
  estimate แบบ non-billable ที่คำนวณจาก pricing snapshot
- **Structured event surfaces (T2)** — แยก parse frame ของ assistant text, thinking, tool/skill,
  usage, result และ error ออกจากกัน; payload ของ tool/skill ถูก project เหลือเฉพาะ structural
  metadata (ไม่เก็บ raw) พร้อม Timeline tab ต่อ job บน dashboard
- **Async execution ผ่าน worker callback (T3)** — job แบบ fire-and-forget (`execution: "callback"`)
  ผ่าน callback endpoint แบบ pre-auth ที่มีเอกสารกำกับหนึ่งจุด ป้องกันด้วย HMAC token ต่อ dispatch
  พร้อม idempotent terminal convergence, reaper และ recovery ที่ทนต่อการ restart
- **Advisory worker state (T4)** — `sync_mode` ที่ operator เป็นเจ้าของ พร้อม pre-enable probe ใช้
  เป็นเพียง tie-break ของ routing ไม่ใช่ตัวบล็อกแบบ hard
- **File collection & handoff (T5/T6 → T9a/T9b)** — job เก็บ deliverable ผ่าน Job Artifacts ของ
  thClaws (manifest + immutable snapshot) แบบ all-or-nothing และ failure-isolated; workflow edge
  ส่งไฟล์ให้ worker ตัวถัดไปผ่าน `POST /v1/inputs` ที่ auth ด้วย Bearer พร้อม acknowledgment
  `written[]` แบบตรงตัว — workspace sync, การแตก tar และ `sync_mode` gate ถูกเลิกใช้จาก path
  collect/handoff ทั้งหมด

## Sovereign platform

- **Atlas Fleet (M4)** — registry แยกต่างหาก (SQLite ของตัวเอง ไม่มี tenant DB ร่วม) พร้อม CLI
  `atlas-fleet` สำหรับ provision / list / health / usage-pull; admin token อ้างด้วย id พร้อม
  secrets sidecar สิทธิ์ `0600` เพิ่ม `GET /healthz` แบบ unauthenticated ที่คืนเฉพาะ
  `{ok, service, version}` สำหรับ probe
- **CDR usage export (M5 / B3)** — CDR CSV ต่อ tenant แบบ deterministic
  (`x-schema: atlas.cdr.v1-proposed`) เป็น export อย่างเดียว ผ่าน `python3 -m fleet cdr`
- **Government solution pack (M6)** — `atlas/packs.py` พร้อม `/api/packs`, `/api/packs/import`,
  `/api/packs/{id}/export` แบบ additive และ pack `gov_complaint` (intake → triage → draft →
  human gate → publish) ที่รันได้ครบวงจร
- **Pack signing (M8)** — signing/verification แบบ HMAC-SHA256 บน canonical pack bundle พร้อม
  import mode `require_signature` และ CLI `python3 -m atlas.packs sign/verify`
- **BYOK & managed-inference readiness (B5 / M7 / B7)** — BYOK key injection แบบ write-only
  (env file `0600`, key ไม่เคยอยู่ใน DB / logs / responses); managed inference มีเอกสารเป็น design
  ระดับ worker/gateway โดยไม่แก้ Atlas core
- **การตัดสินใจเรื่อง multi-tenancy (M9)** — ADR บันทึกการตัดสินใจแบบ silo และ change-list ของ
  pooled แบบละเอียด; core ไม่มี `tenant_id` และมี check เฝ้าไว้ *(เป็นเอกสาร / ADR เท่านั้น)*

## Identity, access และ session security

- **Identity & RBAC ต่อ instance** — role admin / operator / viewer / auditor, API token ต่อ user,
  dashboard login/logout และ audit actor ที่ผ่านการ authenticate
- **Bounded dashboard session** — interactive login ออก token `purpose=session` ด้วย TTL ค่าเริ่มต้น
  8 ชั่วโมง และเพดาน 5 active session; API token ที่ admin ออกยังแยกอิสระ
- **การป้องกัน login** — rate limiter ใน memory ก่อน PBKDF2 (ค่าเริ่มต้น 5/นาที + cooldown, `429` +
  `Retry-After`); ยังต้องมี reverse-proxy/WAF อีกชั้นสำหรับ rate limiting แบบถาวร
- **HTTP/1.1 keep-alive safety** — request ที่ถูก reject โดยยังไม่ได้อ่าน body จะปิด connection เพื่อ
  ไม่ให้ byte ของ body ทำให้ request ถัดไปใน keep-alive เพี้ยน; chunked request body ถูก reject
  อย่างชัดเจน

## Input & output adapter

- **Input Adapter ingress (IA-1)** — source ใด ๆ (LINE, email ผ่าน n8n, web form, ระบบอื่น) POST
  envelope JSON เดียวพร้อม `_meta` ที่สงวนไว้ (`source` / `reply`) โดย validate และ audit provenance
  ที่ choke point จุดเดียว
- **Signed outbound delivery (OB-1)** — ledger `deliveries` และ `OutboundService` ที่ส่ง body ซึ่ง
  sign ด้วย HMAC ไปยัง callback ที่อยู่ใน allowlist และ pin กัน DNS-rebind พร้อม retry แบบมีขอบเขต,
  dead-lettering, reconcile ที่ทนการ restart และ structural URL guard ที่ reject callback URL ที่พก
  credential เพิ่ม `/api/deliveries`, `/api/deliveries/{id}/retry` และ `/api/workflow-runs/{id}/deliver`

## Usage metering, dashboard และ headless UI

- **Usage view (B2) + threshold alert (B4)** — Usage view บน dashboard (ยอด run/job/budget, ยอด
  token, cost estimate แบบ non-billable, export JSON/CSV ที่ authenticate โดยไม่มี token ใน URL)
  พร้อม read-only threshold alert ตามจำนวน run
- **NT dashboard redesign + API ครบ** — dashboard ตาม NT design system ครบทุกหน้าจอ operator ครอบคลุม
  workers, workspaces, jobs, live stream, workflows, artifacts, deliveries, usage, audit และ setup
- **การแยก headless API / static UI** — `ATLAS_SERVE_UI`, `API_BASE` ฝั่ง client และ allowlist
  `ATLAS_CORS_ORIGINS` ทำให้ host dashboard บน origin ใดก็ได้เทียบกับ Atlas แบบ headless; มี dev
  static server แบบ stdlib ให้ด้วย
- **Workflow UX enablement** — run event แบบ cursor-paged, optimistic workflow save พร้อมตรวจ conflict
  ด้วย `expected_version`, SSE `retry` + keepalive frame และ workflow-level `default_reply` ที่ run สืบทอด

## การ harden และ security

- **Versioned migration + production ops (M3)** — migration runner แบบ ordered/idempotent พร้อม
  `schema_version` และ `backup.sh`, `run-prod.sh`, ตัวอย่าง systemd unit และ JSON request log แบบ optional
- **Adversarial review** — รอบ bug-hunt และ Codex/Claude review อิสระหลายรอบพร้อม regression check
  ที่ mutation-locked; ปิดงาน observability, compliance และ cross-cutting (metrics endpoint, audit
  export, artifact classification, purge)
- **ไม่มี security finding ของ Atlas ค้าง** มีข้อยกเว้นจาก per-user auth ที่ตั้งใจและมีเอกสาร 2 จุด:
  `GET /healthz` (คืนเฉพาะ `{ok, service, version}`) และ `POST /api/worker-callbacks/{job_id}`
  (authorize ด้วย HMAC token ต่อ dispatch ของตัวเอง)

---

## หมายเหตุการอัปเกรด

- **Additive ไม่มี breaking API change** contract `/api/*` เดิมไม่เปลี่ยน; field และ endpoint ใหม่
  เป็นแบบ additive
- **Migration รันอัตโนมัติ** ตอน start ผ่าน versioned migration runner (เพิ่ม deliveries ledger,
  index ของ callback reaper, `sync_mode` ของ worker, `jobs.collect_files` และ
  `api_tokens.purpose` / `expires_at`) ควร backup ก่อน (`scripts/backup.sh`)
- **การ reclassify session token:** dashboard-login token เดิมที่ระบุได้จะถูก reclassify และ revoke
  ดังนั้น operator ต้อง sign in ใหม่หลังอัปเกรด; API token ทั่วไปที่ admin ออกไม่ได้รับผลกระทบ
- **ตั้ง `ATLAS_SECRET_KEY`** เพื่อเปิด worker-token encryption, การ sign usage/pack/delivery และ
  async callback; async job และ workflow ที่มี callback node ยังต้องใช้ `ATLAS_PUBLIC_BASE_URL`
- **environment variable ใหม่** เช่น `ATLAS_SERVE_UI`, `ATLAS_CORS_ORIGINS`, `ATLAS_PUBLIC_BASE_URL`,
  `ATLAS_OUTBOUND_ALLOWLIST` และ knob เรื่อง request-log / timeout — ดู `docs/ops/deployment.md`

## ข้อจำกัดที่ทราบ และ external confirmation ที่ยังค้าง

- **CDR schema ยังเป็น proposed** (`atlas.cdr.v1-proposed`) — field/หน่วยรอยืนยันกับ NT
  billing/mediation
- **BYOK option-a (thClaws save-key) เป็น stub ที่มีเอกสาร** — รอ endpoint ฝั่ง upstream; option-b
  env injection ใช้งานได้แล้ววันนี้
- **ยังไม่มี SSO/OIDC ในตัว** — วันนี้เป็น local user; OIDC เป็น extension point ที่มีเอกสาร
- **ยังไม่ล็อก provisioning target** — ค่าเริ่มต้นเป็น docker-compose / systemd บน VM; k8s/GDCC เป็น
  ทางเลือก
- **runtime เป็น single-node SQLite** — Atlas ยังไม่ scale แนวนอน; managed inference (M7) และ pooled
  tenancy (M9) เป็นเอกสาร readiness / ADR ไม่ใช่ code ที่ ship แล้ว

## การตรวจสอบ (Verification)

gate หลัก (`scripts/gate.sh`) ผ่านสีเขียวจาก tree ที่สะอาด พร้อม hermetic check ต่อฟีเจอร์
(`scripts/check_*.py`, `fleet/check_fleet.py`) และ regression test แบบ mutation-locked ครอบคลุม
auth, jobs, workflows, usage, packs, fleet, outbound delivery, file collection/handoff และการแยก
headless; type-checking (mypy) และ linting (ruff) ผ่าน
