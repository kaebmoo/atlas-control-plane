# Atlas API Reference

[English](api-reference-en.md) · **ภาษาไทย** · [OpenAPI 3.1](openapi.yaml)

สถานะ: **Current API specification v1.0**<br>
อ้างอิงระบบ: `atlas/app.py` ณ วันที่ 2026-06-29<br>
Base URL ปริยาย: `http://127.0.0.1:8787`

เอกสารนี้อธิบาย HTTP API ที่มีอยู่จริงใน Atlas ปัจจุบัน ส่วน contract ของ workflow graph
และ trigger แบบ machine-readable อยู่ใน:

- [Workflow Definition JSON Schema](workflow-definition.schema.json)
- [Workflow Trigger JSON Schema](workflow-trigger.schema.json)
- [Visual Workflow Builder Specification](workflow-visual-builder-spec-th.md)

## 1. เริ่มใช้งาน

```bash
BASE_URL=http://127.0.0.1:8787
curl -sS "$BASE_URL/api/health"
```

ผลลัพธ์:

```json
{"ok":true,"service":"atlas-control-plane","db":"/path/data/atlas.sqlite","workers":2}
```

API ยังไม่มี prefix แบบ `/v1`; client ควร pin กับ commit/release ที่ใช้งาน และติดตาม
เอกสารนี้เมื่อ contract เปลี่ยน

## 2. Authentication, CORS และความปลอดภัย

เมื่อไม่ได้ตั้ง `ATLAS_API_TOKEN` ทุก request เข้า API ได้โดยไม่ใช้ token เมื่อกำหนด token:

```bash
ATLAS_API_TOKEN="atlas-secret" python3 -m atlas
```

ส่ง token ด้วย header:

```bash
curl -H 'Authorization: Bearer atlas-secret' "$BASE_URL/api/workers"
```

หรือ query parameter:

```text
GET /api/jobs/{job_id}/events?token=atlas-secret
```

Query token มีไว้รองรับ browser `EventSource` ที่ตั้ง Authorization header ไม่ได้ ไม่ควรใช้
กับ request ปกติเพราะ token อาจไปอยู่ใน URL log/history

ถ้า `ATLAS_LOOPBACK_NO_AUTH=true` ซึ่งเป็นค่าปริยาย request จาก `127.0.0.1` และ `::1`
ไม่ต้องใช้ token แม้ตั้ง `ATLAS_API_TOKEN`

ข้อจำกัดปัจจุบัน:

- ใช้ shared API token เดียว ยังไม่มี user identity, RBAC หรือสิทธิ์แยกตาม resource
- ไม่มี TLS ในตัว ควรวางหลัง HTTPS reverse proxy เมื่อใช้ข้ามเครื่อง
- CORS เป็น `Access-Control-Allow-Origin: *` และอนุญาต headers
  `authorization`, `content-type`, `x-filename`
- worker token ถูกเก็บใน SQLite แต่ API ไม่ส่งค่ากลับ มีเพียง `token_set`

## 3. รูปแบบ request/response

### JSON

- JSON request body ต้องเป็น object ไม่รับ array เป็น root
- ใช้ `Content-Type: application/json`
- action ที่ไม่มี payload อาจส่ง body ว่างหรือ `{}`
- เวลาใน response เป็น ISO 8601 UTC เช่น `2026-06-29T10:00:00Z`
- ID สร้างโดยระบบพร้อม prefix เช่น `wrk_`, `wsp_`, `job_`, `wfd_`, `wfr_`,
  `art_`, `apr_`, `wtr_`

### Errors

Error ทุกประเภทเป็น JSON รูปแบบเดียว:

```json
{"error":"message"}
```

| HTTP | ความหมาย |
| --- | --- |
| `400` | payload, state transition หรือ reference ไม่ถูกต้อง |
| `401` | token ไม่ถูกต้องหรือไม่มี token |
| `404` | resource/route ไม่มี |
| `500` | error ที่ handler ไม่ได้แปลงเป็น validation error |

### Lists และ async operations

- list ส่วนใหญ่ใช้ `?limit=N` ค่าเริ่มต้น 100; ยังไม่มี cursor pagination
- create job/run และบาง approval/trigger action ตอบก่อนงานเบื้องหลังเสร็จด้วย `202`
- ตรวจสถานะต่อด้วย GET, workflow events หรือ job SSE
- API ไม่มี idempotency key ทั่วไป ยกเว้น trigger fire รองรับ `dedupe_key`

## 4. Endpoint catalog

### System, Fleet และ Routing

| Method | Path | ผลลัพธ์ |
| --- | --- | --- |
| GET | `/api/health` | สถานะ Atlas |
| GET | `/api/workers` | รายการ worker |
| POST | `/api/workers` | create/upsert worker |
| POST | `/api/workers/poll` | poll worker ทุกตัว |
| GET | `/api/workers/{worker_id}` | worker หนึ่งตัว |
| DELETE | `/api/workers/{worker_id}` | ลบ worker และ workspace ที่ผูก |
| POST | `/api/workers/{worker_id}/poll` | poll worker หนึ่งตัว |
| GET | `/api/workspaces` | รายการ workspace |
| POST | `/api/workspaces` | create/upsert workspace |
| GET | `/api/workspaces/{workspace_id}` | workspace หนึ่งตัว |
| DELETE | `/api/workspaces/{workspace_id}` | ลบ workspace |
| GET | `/api/conversations` | 100 conversation ล่าสุด |
| POST | `/api/conversations` | สร้าง conversation |
| POST | `/api/routes/resolve` | preview route โดยไม่สร้าง job |

### Jobs

| Method | Path | ผลลัพธ์ |
| --- | --- | --- |
| GET | `/api/jobs?limit=100` | รายการ job |
| POST | `/api/jobs` | route และเริ่ม job (`202`) |
| GET | `/api/jobs/{job_id}` | job detail |
| POST | `/api/jobs/{job_id}/cancel` | best-effort cancel |
| GET | `/api/jobs/{job_id}/events?after=0` | replay/follow SSE |

### Workflow definitions และ AI builder

| Method | Path | ผลลัพธ์ |
| --- | --- | --- |
| GET | `/api/workflows` | definitions |
| POST | `/api/workflows` | validate และสร้าง definition |
| GET | `/api/workflow-templates` | built-in templates |
| POST | `/api/workflows/draft` | AI draft ที่ validate แล้ว |
| POST | `/api/workflows/suggest-workers` | worker suggestions |
| GET | `/api/workflows/{workflow_id}` | definition detail |
| PUT | `/api/workflows/{workflow_id}` | validate และอัปเดต |
| DELETE | `/api/workflows/{workflow_id}` | ลบ definition |
| POST | `/api/workflows/{workflow_id}/validate` | validate preview |
| POST | `/api/workflows/{workflow_id}/explain` | อธิบาย definition |
| POST | `/api/workflows/{workflow_id}/repair` | repair preview ไม่บันทึก |
| POST | `/api/workflows/{workflow_id}/suggest-triggers` | trigger suggestions |

### Runs, Artifacts และ Approvals

| Method | Path | ผลลัพธ์ |
| --- | --- | --- |
| GET | `/api/workflow-runs` | รายการ runs |
| POST | `/api/workflow-runs` | เริ่ม run (`202`) |
| GET | `/api/workflow-runs/{run_id}` | run + nodes + traversed edges + approvals |
| GET | `/api/workflow-runs/{run_id}/events` | lifecycle events |
| POST | `/api/workflow-runs/{run_id}/pause` | pause |
| POST | `/api/workflow-runs/{run_id}/resume` | resume/recovery retry (`202`) |
| POST | `/api/workflow-runs/{run_id}/cancel` | cancel |
| GET | `/api/workflow-runs/{run_id}/artifacts` | artifacts ของ run |
| POST | `/api/workflow-runs/{run_id}/files?key=...` | upload binary file artifact |
| POST | `/api/artifacts` | สร้าง inline artifact |
| GET | `/api/artifacts/{artifact_id}` | artifact detail |
| GET | `/api/artifacts/{artifact_id}/content` | download `file_ref` |
| GET | `/api/approvals` | approvals พร้อม filter |
| POST | `/api/approvals/{approval_id}/approve` | approve gate (`202`) |
| POST | `/api/approvals/{approval_id}/reject` | reject และ fail run |
| POST | `/api/approvals/{approval_id}/choose` | เลือก branch (`202`) |

### Triggers และ Audit

| Method | Path | ผลลัพธ์ |
| --- | --- | --- |
| GET | `/api/workflow-triggers` | trigger list |
| POST | `/api/workflow-triggers` | สร้าง trigger |
| GET | `/api/workflow-triggers/{trigger_id}` | trigger detail |
| PUT | `/api/workflow-triggers/{trigger_id}` | update/revalidate |
| DELETE | `/api/workflow-triggers/{trigger_id}` | ลบ trigger/events |
| POST | `/api/workflow-triggers/{trigger_id}/fire` | fire manual/schedule/webhook (`202`) |
| GET | `/api/workflow-triggers/{trigger_id}/events` | trigger event history |
| GET | `/api/audit?limit=100` | audit log |

## 5. Workers และ Workspaces

### สร้างหรือแก้ Worker

`POST /api/workers` เป็น upsert จาก `id` หรือ `base_url`:

```bash
curl -sS -X POST "$BASE_URL/api/workers" \
  -H 'content-type: application/json' \
  -d '{
    "name":"Reporter",
    "base_url":"http://127.0.0.1:4317",
    "token":"worker-secret",
    "role":"reporter",
    "tags":["local","news"]
  }'
```

`base_url` เป็น required เว้น `token` ว่างตอน upsert เพื่อเก็บ token เดิม Response ไม่คืน
token จริง:

```json
{"worker":{"id":"wrk_xxx","name":"Reporter","token_set":true,"status":"unknown"}}
```

การ Save ผ่าน API ไม่ poll อัตโนมัติ เรียกต่อ:

```bash
curl -sS -X POST "$BASE_URL/api/workers/wrk_xxx/poll"
```

Poll ตอบ 200 แม้ worker offline โดย worker จะมี `status: "offline"` และ `last_error`

### สร้างหรือแก้ Workspace

```bash
curl -sS -X POST "$BASE_URL/api/workspaces" \
  -H 'content-type: application/json' \
  -d '{
    "worker_id":"wrk_xxx",
    "workspace_key":"atlas",
    "workspace_dir":"/srv/atlas",
    "company":"Example",
    "tags":["backend"]
  }'
```

Required: `worker_id`, `workspace_key`, `workspace_dir` Path ถูกตีความบนเครื่อง worker
ไม่ใช่ Atlas host

## 6. Conversations, Routing และ Jobs

### Conversation

```bash
curl -sS -X POST "$BASE_URL/api/conversations" \
  -H 'content-type: application/json' \
  -d '{"title":"News research","workspace_key":"atlas"}'
```

ถ้าสร้าง job โดยไม่ส่ง `conversation_id`, Atlas สร้าง conversation ใหม่จาก prompt ให้อัตโนมัติ
Conversation เดิมอาจมี session binding ไปยัง thClaws session เดิม

### Preview routing

```bash
curl -sS -X POST "$BASE_URL/api/routes/resolve" \
  -H 'content-type: application/json' \
  -d '{"role":"reporter","workspace_key":"atlas","prompt":"Research AI news"}'
```

ลำดับ route: explicit `workspace_id` → explicit `worker_id` → conversation binding →
auto route จาก online status, workspace key, company, tags, role และ prompt hints

### เริ่ม Job

```bash
curl -sS -X POST "$BASE_URL/api/jobs" \
  -H 'content-type: application/json' \
  -d '{
    "prompt":"Research AI news",
    "role":"reporter",
    "workspace_key":"atlas",
    "model":"optional-model"
  }'
```

ตอบ `202` พร้อม job สถานะ `queued` Job states คือ `queued`, `running`,
`cancel_requested`, `succeeded`, `failed`, `cancelled`

### Handoff

```json
{
  "prompt": "Collect source facts",
  "worker_id": "wrk_reporter",
  "handoff": {
    "enabled": true,
    "worker_id": "wrk_writer",
    "prompt": "Write from this result:\n\n{result}"
  }
}
```

Handoff เริ่มเฉพาะเมื่อ source job สำเร็จและมี assistant text ตัวแปรรองรับคือ
`{result}`, `{source_prompt}`, `{source_job_id}` การ cancel เป็น best effort และ worker
อาจทำ side effect ไปแล้ว

### Job SSE

```bash
curl -N "$BASE_URL/api/jobs/job_xxx/events?after=0"
```

Frame:

```text
id: 4
event: text
data: {"text":"hello","seq":4,"created_at":"..."}
```

Event ที่พบบ่อย: `route`, `session`, `state`, `text`, `error`, `done`,
`cancel_requested`, `handoff_configured`, `handoff_started`, `handoff_skipped`,
`handoff_error`, `message`, `close`

ใช้ `after=<last_seq>` เพื่อ resume/replay เมื่อ job terminal และไม่มี event ค้าง server ส่ง
`close` แล้วปิด connection

## 7. Workflow Definitions และ AI Builder

### สร้าง definition

```bash
curl -sS -X POST "$BASE_URL/api/workflows" \
  -H 'content-type: application/json' \
  -d '{
    "name":"Research to writer",
    "graph":{
      "start":"researcher",
      "nodes":[
        {"id":"researcher","type":"worker","role":"researcher","prompt":"Research {input.topic}","outputs":["research"]},
        {"id":"writer","type":"worker","role":"writer","prompt":"Write from {artifact.research}"}
      ],
      "edges":[{"from":"researcher","to":"writer","condition":{"type":"always"}}]
    },
    "policy":{"max_jobs":3,"max_iterations":3}
  }'
```

Backend บังคับ `graph`; name/policy มี default แต่ client ควรส่ง canonical payload ตาม
[Workflow Definition Schema](workflow-definition.schema.json) Server ตรวจ graph,
policy, worker/workspace references และ allowlists ก่อนบันทึก

`PUT /api/workflows/{id}` เป็น partial update แต่ graph/policy ที่ได้หลัง merge ต้อง valid
`DELETE` ลบ definition และ trigger ที่ผูก; run เก่ายังคงอยู่แต่
`workflow_definition_id` อาจเป็น null ตาม foreign-key behavior

### Validate, Explain, Repair

```bash
curl -sS -X POST "$BASE_URL/api/workflows/wfd_xxx/validate" \
  -H 'content-type: application/json' \
  -d '{"graph":{...},"policy":{...}}'
```

Validate ต้องมี saved workflow ID ก่อน field ที่ไม่ส่งจะใช้ค่าที่บันทึกอยู่ Explain อ่าน
saved definition และใช้ workflow_builder ถ้ามี ไม่เช่นนั้นอธิบายแบบ local Repair รับ
graph/policy/triggers preview และคืน draft ที่ยังไม่บันทึก

### AI Draft

ต้องมี worker role/tag `workflow_builder`:

```bash
curl -sS -X POST "$BASE_URL/api/workflows/draft" \
  -H 'content-type: application/json' \
  -d '{"plain_language_prompt":"Create researcher to writer with max 3 jobs"}'
```

ผล AI ต้องเป็น JSON object เดียวและผ่าน deterministic validation ก่อน API ส่งกลับ
AI endpoint ไม่ Save/Run อัตโนมัติ

`POST /api/workflows/suggest-workers` ทำงานแบบ local ได้ถ้าไม่มี AI worker และรับ
`{"graph":...,"policy":...}` ข้อเสนออ้างได้เฉพาะ worker/workspace ID ที่มีจริง

## 8. Workflow Runs และ Events

### เริ่ม run

```bash
curl -sS -X POST "$BASE_URL/api/workflow-runs" \
  -H 'content-type: application/json' \
  -d '{"workflow_definition_id":"wfd_xxx","input":{"topic":"AI"}}'
```

ตอบ `202` Run states: `running`, `paused`, `waiting_for_human`,
`recovery_required`, `succeeded`, `failed`, `cancelled`

Filter list:

```text
GET /api/workflow-runs?workflow_definition_id=wfd_xxx&limit=20
```

Run detail มี `run`, runtime `nodes`, traversed `edges`, `approvals` ส่วน lifecycle
events เป็น JSON list ไม่ใช่ SSE:

```text
GET /api/workflow-runs/wfr_xxx/events?limit=500
```

### Pause, Resume, Recovery และ Cancel

```bash
curl -sS -X POST "$BASE_URL/api/workflow-runs/wfr_xxx/pause"
curl -sS -X POST "$BASE_URL/api/workflow-runs/wfr_xxx/resume" \
  -H 'content-type: application/json' -d '{}'
curl -sS -X POST "$BASE_URL/api/workflow-runs/wfr_xxx/cancel"
```

Resume ปกติใช้ได้จาก `paused` เท่านั้น ถ้าเป็น `recovery_required` ต้องยอมรับความเสี่ยง
side effect ซ้ำอย่างชัดเจน:

```json
{"retry_interrupted":true}
```

## 9. Artifacts และ Files

### Inline artifact

```bash
curl -sS -X POST "$BASE_URL/api/artifacts" \
  -H 'content-type: application/json' \
  -d '{
    "run_id":"wfr_xxx",
    "key":"fact_check",
    "kind":"json",
    "content":{"verdict":"approved"},
    "metadata":{"source":"manual"}
  }'
```

Kinds: `text`, `json`, `markdown`, `file_ref`, `summary`, `decision` สำหรับ JSON
response API decode content กลับเป็น object/list ห้ามสร้าง `file_ref` ด้วย inline API หาก
ต้องการ download จริง ให้ใช้ file upload endpoint

### File upload

เป็น direct binary body ไม่ใช่ multipart หรือ base64:

```bash
curl -sS -X POST "$BASE_URL/api/workflow-runs/wfr_xxx/files?key=contract" \
  -H 'content-type: application/pdf' \
  -H 'x-filename: contract.pdf' \
  --data-binary @contract.pdf
```

- `key` ต้อง match `[A-Za-z_][A-Za-z0-9_.-]{0,127}`
- `Content-Length` ต้องมี; curl ใส่ให้โดยปริยาย
- default limit 10 MiB ปรับด้วย `ATLAS_MAX_UPLOAD_BYTES`
- response เป็น `file_ref` พร้อม filename, media_type, size, SHA-256
- upload ผูกไฟล์กับ run แต่ไม่ส่งเข้า worker workspace และ worker ไม่อ่านอัตโนมัติ

Download:

```bash
curl -OJ "$BASE_URL/api/artifacts/art_xxx/content"
```

Content endpoint ใช้ได้เฉพาะ artifact `file_ref`

## 10. Approvals

```text
GET /api/approvals?state=pending&run_id=wfr_xxx&limit=100
```

Gate ปกติ:

```bash
curl -sS -X POST "$BASE_URL/api/approvals/apr_xxx/approve"
curl -sS -X POST "$BASE_URL/api/approvals/apr_xxx/reject"
```

Gate แบบมีตัวเลือกต้องใช้ choose ไม่สามารถ approve ตรง ๆ:

```bash
curl -sS -X POST "$BASE_URL/api/approvals/apr_xxx/choose" \
  -H 'content-type: application/json' \
  -d '{"choice":"publish"}'
```

ตัดสินใจได้ครั้งเดียว และ run ต้องอยู่ `waiting_for_human` Reject ทำให้ run fail

## 11. Workflow Triggers

### สร้าง trigger

```bash
curl -sS -X POST "$BASE_URL/api/workflow-triggers" \
  -H 'content-type: application/json' \
  -d '{
    "workflow_definition_id":"wfd_xxx",
    "name":"Every 15 minutes",
    "type":"schedule",
    "config":{"interval_minutes":15},
    "enabled":true
  }'
```

Type/config:

- `manual`: `{}`
- `webhook`: `{}`
- `schedule`: `{"interval_minutes":15}` หรือ `{"daily_time":"09:30"}` ตาม local
  timezone ของ Atlas host
- `workflow_run_completed`: filter `source_workflow_definition_id`, `state`
- `artifact_created`: filter `source_workflow_definition_id`, `key`, `kind`
- `worker_status_changed`: filter `worker_id`, `status`

### Fire และ dedupe

```bash
curl -sS -X POST "$BASE_URL/api/workflow-triggers/wtr_xxx/fire" \
  -H 'content-type: application/json' \
  -d '{"payload":{"topic":"AI"},"dedupe_key":"event-001"}'
```

ยิงเองได้เฉพาะ manual/schedule/webhook Internal trigger สามชนิดถูกยิงโดย Atlas เท่านั้น
ส่ง `dedupe_key` เดิมซ้ำจะได้ event state `ignored` แทนการสร้าง run ซ้ำ

PUT เป็น partial update; เมื่อ type/config เปลี่ยน server คำนวณ `next_fire_at` ใหม่
Trigger event states ที่พบบ่อยคือ `received`, `started`, `ignored`, `failed`

## 12. Audit

```bash
curl -sS "$BASE_URL/api/audit?limit=100"
```

แต่ละ entry มี `action`, `actor` (ปัจจุบันมักเป็น `local`), `resource_type`,
`resource_id`, `details`, `created_at` API ยังไม่มี filter/cursor และไม่มี endpoint ลบ audit

## 13. OpenAPI 3.1

[openapi.yaml](openapi.yaml) ระบุ 42 paths และ 55 operations พร้อม security schemes,
parameters, request bodies, response wrappers และ schema references ใช้กับ Swagger UI,
Redoc, code generator หรือ contract tests ได้

Workflow/trigger schemas ใน OpenAPI ใช้ canonical client shape ซึ่งเข้มกว่า backend บางจุด
backend อาจเติม default ให้ field ที่ omit แต่ client ใหม่ควรส่งรูป canonical เพื่อให้
validation และ round-trip เสถียร

การใช้ OpenAPI ไม่แทน semantic validation ของ workflow เช่น duplicate node ID, cycle guard,
manager/human edge coupling, quorum และ live worker/workspace references ดู
[Visual Workflow Builder Specification](workflow-visual-builder-spec-th.md)

## 14. Checklist สำหรับ API client

- ตั้ง timeout สำหรับ JSON request แต่ไม่ใช้ timeout สั้นกับ SSE
- เก็บ `last_seq` ของ SSE และ reconnect ด้วย `after`
- ตรวจ HTTP status ก่อนอ่าน success shape
- อย่า log Authorization/query token หรือ worker token
- retry POST อย่างระวังเพราะไม่มี idempotency ทั่วไป
- ใช้ trigger `dedupe_key` เมื่อ event ภายนอกอาจ retry
- treat cancel/recovery เป็น side-effect-sensitive operation
- validate workflow/trigger ด้วย schema และให้ server validate ซ้ำ
- อย่าสมมติว่าการ upload file ทำให้ worker อ่านไฟล์ได้
