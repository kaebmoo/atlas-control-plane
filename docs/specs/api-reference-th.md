# Atlas API Reference

[English](api-reference-en.md) · **ภาษาไทย** · [OpenAPI 3.1](openapi.yaml)

สถานะ: **Current API specification v1.3**<br>
อ้างอิงระบบ: `atlas/app.py` ณ วันที่ 2026-07-01<br>
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

Atlas กำหนดให้ใช้ API token รายผู้ใช้เป็นค่าเริ่มต้น สร้าง administrator คนแรกด้วย:

```bash
python3 -m atlas.admin create-admin admin
```

ส่ง token ด้วย header:

```bash
curl -H 'Authorization: Bearer <token>' "$BASE_URL/api/workers"
```

หรือ query parameter:

```text
GET /api/jobs/{job_id}/events?token=<token>
```

Query token มีไว้รองรับ browser `EventSource` ที่ตั้ง Authorization header ไม่ได้ ไม่ควรใช้
กับ request ปกติเพราะ token อาจไปอยู่ใน URL log/history

ตั้ง `ATLAS_LOOPBACK_NO_AUTH=true` เฉพาะ development เพื่อให้ request จาก
`127.0.0.1` และ `::1` ไม่ต้องใช้ token; request แบบ loopback นี้จะถูกมองเป็น identity
**admin** ในตัว จึงข้ามการตรวจ role/permission (RBAC) ทั้งหมด — ห้ามเปิดใน deployment ที่ใช้
ร่วมกันหรือ production; ค่าปริยายที่ปลอดภัยคือ `false`
ส่วน `ATLAS_API_TOKEN` ยังใช้เป็น legacy admin token ได้

ข้อจำกัดปัจจุบัน:

- ไม่มี TLS ในตัว ควรวางหลัง HTTPS reverse proxy เมื่อใช้ข้ามเครื่อง
- CORS เป็น `Access-Control-Allow-Origin: *` และอนุญาต headers
  `authorization`, `content-type`, `x-filename`
- worker token ถูกเข้ารหัสแบบ authenticated ciphertext เมื่อกำหนด
  `ATLAS_SECRET_KEY`; หากไม่กำหนด Atlas จะเตือนและคง plaintext compatibility
  โดย API ไม่ส่งค่ากลับ มีเพียง `token_set`

Identity endpoints:

- `POST /api/auth/login` รับ `username` และ `password` แล้วคืน **dashboard session** แบบ raw
  เพียงครั้งเดียว พร้อม public user metadata และ `session.expires_at` โดย session หมดอายุ
  ปริยายใน 8 ชั่วโมง และจำกัด session ที่ใช้งานพร้อมกันต่อผู้ใช้ไว้ 5 รายการ; เมื่อเกินจะ
  revoke เฉพาะ session ที่เก่าที่สุด `GET /api/me` คืน metadata ของ session ซ้ำเพื่อให้ UI
  เตือนก่อนหมดอายุได้
- `POST /api/auth/logout` revoke per-user token ปัจจุบัน
- การล็อกอินที่ล้มเหลวถูกจำกัดในหน่วยความจำด้วย normalized username + direct peer IP ก่อน
  ตรวจรหัสผ่าน (ปริยาย 5 ครั้ง/นาที แล้ว cooldown 60 วินาที) เมื่อถูกจำกัดจะได้ `429` พร้อม
  `Retry-After`; Atlas ไม่เชื่อ `X-Forwarded-For` โดยเจตนา การ restart process จะล้างหน้าต่าง
  ป้องกันระยะสั้นนี้ ดังนั้น reverse proxy ใน production ต้องจำกัด rate ที่ public edge ด้วย
- API token ที่ admin ออกให้มี `purpose: "api"` ซึ่งแก้ไม่ได้; ส่ง `expires_at` UTC ในอนาคต
  แบบ optional ไปที่ `POST /api/tokens` หรือไม่ส่งเพื่อให้ integration token ไม่หมดอายุอย่างชัดเจน
  metadata ของ token แสดง `purpose`, `expires_at`, `revoked_at` แต่ไม่แสดง raw token หรือ hash
- CRUD สำหรับ admin เท่านั้น: `/api/users`, `/api/users/{id}`, `/api/tokens` และ
  `/api/tokens/{id}` พร้อม alias `POST /api/tokens/{id}/revoke`
- Roles: `viewer` อ่านข้อมูลปกติ, `operator` รัน jobs/workflows และตัดสิน approvals,
  `auditor` อ่าน audit และ usage เพิ่มเติม และ `admin` มีสิทธิ์ทั้งหมด

## 3. รูปแบบ request/response

### JSON

- JSON request body ต้องเป็น object ไม่รับ array เป็น root
- ใช้ `Content-Type: application/json`
- action ที่ไม่มี payload อาจส่ง body ว่างหรือ `{}`
- เวลาใน response เป็น ISO 8601 UTC เช่น `2026-06-29T10:00:00Z`
- ID สร้างโดยระบบพร้อม prefix เช่น `wrk_`, `wsp_`, `job_`, `wfd_`, `wfr_`,
  `art_`, `apr_`, `wtr_`, `usg_`

### Errors

Error ทุกประเภทเป็น JSON รูปแบบเดียว:

```json
{"error":"message"}
```

| HTTP | ความหมาย |
| --- | --- |
| `400` | payload, state transition หรือ reference ไม่ถูกต้อง |
| `401` | token ไม่ถูกต้องหรือไม่มี token |
| `403` | role ที่ยืนยันตัวตนแล้วไม่มีสิทธิ์สำหรับ route |
| `429` | ถึง login rate limit; ให้ทำตาม response header `Retry-After` |
| `405` | method ไม่รองรับ; ตรวจ response header `Allow` |
| `404` | resource/route ไม่มี |
| `500` | error ที่ handler ไม่ได้แปลงเป็น validation error |

### Lists และ async operations

- list ส่วนใหญ่ใช้ `?limit=N` ค่าเริ่มต้น 100; ยังไม่มี cursor pagination
- create job/run และบาง approval/trigger action ตอบก่อนงานเบื้องหลังเสร็จด้วย `202`
- ตรวจสถานะต่อด้วย GET, workflow events หรือ job SSE
- API ไม่มี idempotency key ทั่วไป ยกเว้น trigger fire รองรับ `dedupe_key`
- `HEAD` มี status และ header เหมือน `GET` ที่สำเร็จ แต่ไม่มี body; Atlas ไม่มีสัญญา
  partial update สำหรับ `PATCH` จึงตอบ `405` พร้อม `Allow`
- หาก Atlas ปฏิเสธ request ก่อนอ่าน declared request body (เช่น auth/RBAC หรือ method
  ที่ไม่รองรับ) จะปิด HTTP/1.1 connection นั้นแทนการเสี่ยงให้ bytes ของ body ถูกตีความเป็น
  keep-alive request ถัดไป client ควร retry บน connection ใหม่เท่านั้น

## 4. Endpoint catalog

### System, Fleet และ Routing

| Method | Path | ผลลัพธ์ |
| --- | --- | --- |
| GET | `/healthz` | liveness probe ไม่ต้อง auth (`{ok, service, version}`) |
| GET | `/api/health` | สถานะ Atlas (ต้อง auth; รวมจำนวน worker) |
| GET | `/api/workers` | รายการ worker |
| POST | `/api/workers` | create/upsert worker (admin) |
| POST | `/api/workers/poll` | poll worker ทุกตัว |
| GET | `/api/workers/{worker_id}` | worker หนึ่งตัว |
| DELETE | `/api/workers/{worker_id}` | ลบ worker และ workspace ที่ผูก (admin) |
| POST | `/api/workers/{worker_id}/poll` | poll worker หนึ่งตัว |
| POST | `/api/workers/{worker_id}/sync-mode` | ตั้งโหมด sync trust (admin); การเปิด `tunnel`/`forward_auth` จะ probe sync ก่อนบันทึก — ถ้า probe ล้มเหลวคืน 400 และคงโหมดเดิม; บันทึก audit (`worker.sync_mode_changed`) |
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
| GET | `/api/jobs/{job_id}/artifacts` | Frozen Job Artifacts ของ job (T9a `file_ref` artifacts) |
| POST | `/api/worker-callbacks/{job_id}` | ช่องส่งผลลัพธ์ terminal สำหรับ job แบบ `execution: "callback"` (เฉพาะ worker ใช้ signed callback token ไม่ใช่ user auth) |

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

### Solution packs

| Method | Path | หน้าที่ |
|---|---|---|
| GET | `/api/packs` | แสดงรายการ solution pack ที่มีให้ติดตั้ง |
| POST | `/api/packs/import` | validate bundle แล้วสร้าง definition + trigger |
| GET | `/api/packs/{workflow_id}/export` | export definition กลับเป็น bundle |

รูปแบบ bundle: [pack-format.md](pack-format.md). การ import ใช้ตัว validate ของ workflow
graph และ trigger เดิม (ไม่ bypass); bundle ที่ไม่ถูกต้องจะถูกปฏิเสธพร้อม error ที่ชัดเจน
bundle ที่มีลายเซ็นจะถูกตรวจด้วย `ATLAS_SECRET_KEY` ตอน import (pack ที่ถูกแก้ไขจะถูกปฏิเสธ)
ส่วน pack ที่ไม่เซ็นยังนำเข้าได้ `import` ต้องมีสิทธิ์ `workflows.manage`; ส่วนการอ่านต้องมี `read`
การ export และ import จงใจไม่รวม `default_reply`: callback URL ผูกกับ deployment จึงไม่ควรอยู่ใน
bundle ที่ portable — ตั้งค่าใหม่ต่อ instance หลัง import ผ่าน `PUT /api/workflows/{id}`

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
| POST | `/api/workflow-runs/{run_id}/deliver` | ส่งผลลัพธ์ที่เซ็นแล้วไปยัง `_meta.reply.callback_url` ด้วยตนเอง (`202`) |
| GET | `/api/workflow-runs/{run_id}/artifacts` | artifacts ของ run |
| POST | `/api/workflow-runs/{run_id}/files?key=...` | upload binary file artifact |
| GET | `/api/artifacts` | list ข้าม run/job ทั้งหมดแบบ window ใหม่สุดก่อน (`?limit&run_id&job_id&key&kind`; โดยปริยายแถวมี `content`; เพิ่ม `include_content=false` หรือ `view=metadata` เพื่อเอาเฉพาะ metadata; response มี `total`) |
| POST | `/api/artifacts` | สร้าง inline artifact |
| GET | `/api/artifacts/{artifact_id}` | artifact detail |
| GET | `/api/artifacts/{artifact_id}/content` | download `file_ref` |
| GET | `/api/approvals` | approvals พร้อม filter |
| POST | `/api/approvals/{approval_id}/approve` | approve gate (`202`) |
| POST | `/api/approvals/{approval_id}/reject` | reject และ fail run |
| POST | `/api/approvals/{approval_id}/choose` | เลือก branch (`202`) |

### Triggers, Audit และ Usage

| Method | Path | ผลลัพธ์ |
| --- | --- | --- |
| GET | `/api/workflow-triggers` | trigger list |
| POST | `/api/workflow-triggers` | สร้าง trigger |
| GET | `/api/workflow-triggers/{trigger_id}` | trigger detail |
| PUT | `/api/workflow-triggers/{trigger_id}` | update/revalidate |
| DELETE | `/api/workflow-triggers/{trigger_id}` | ลบ trigger/events |
| POST | `/api/workflow-triggers/{trigger_id}/fire` | fire manual/schedule/webhook (`202`) |
| GET | `/api/workflow-triggers/{trigger_id}/events` | trigger event history |
| GET | `/api/audit?limit=100&from=&to=&format=json\|csv` | audit log / export CSV |
| GET | `/api/usage?from=&to=&format=json\|csv` | raw usage ledger (เฉพาะ admin/auditor) |
| GET | `/api/metrics` | ตัวเลขสรุปเชิงปฏิบัติการ (ทุก role ที่ authenticate แล้ว) |

### Deliveries

| Method | Path | ผลลัพธ์ |
| --- | --- | --- |
| GET | `/api/deliveries?run_id=&status=` | รายการ outbound delivery (operator/auditor) |
| POST | `/api/deliveries/{delivery_id}/retry` | ลองส่งใหม่แบบมีขอบเขต 1 ครั้ง (operator, `202`) |

## 5. Workers และ Workspaces

### สร้างหรือแก้ Worker

`POST /api/workers` เป็น upsert จาก `id` หรือ `base_url` เหมือนกับ `DELETE
/api/workers/{worker_id}` route นี้ต้องใช้สิทธิ์ `admin` — token ของ `operator`
จะได้ `403` เฉพาะ `admin` เท่านั้นที่ลงทะเบียนหรือลบ worker ได้:

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

### รันแบบ async (`execution: "callback"`)

งานที่ใช้เวลานานสามารถรันแบบ fire-and-forget ได้ โดยเพิ่ม `execution: "callback"`
ใน request (ค่า default คือ `"stream"` ซึ่งพฤติกรรมเหมือนเดิมทุกไบต์; worker node
และ manager node ใน workflow ก็รับ field `execution` แบบ optional เช่นกัน):

```json
{"prompt": "Summarize this repo", "worker_id": "wrk_reporter", "execution": "callback"}
```

Worker จะตอบ 202 แล้วรันต่อโดยไม่ผูกกับ connection ของ Atlas — job ค้างสถานะ
`running` พร้อม `callback_deadline_at` เมื่อรันเสร็จ worker จะ POST ผลลัพธ์ terminal
มาที่ `POST /api/worker-callbacks/{job_id}` โดยใช้ signed token เฉพาะ dispatch
นั้นที่ Atlas ใส่ไว้ใน callback envelope — **ไม่ใช่** user API token (เป็นข้อยกเว้น
pre-auth จุดเดียวที่มีเอกสารกำกับ ดู `docs/specs/threat-model.md`) Atlas
apply ผลลัพธ์แบบ idempotent: เขียนสถานะ terminal, `summary` → `assistant_text`,
token usage เข้า metering ledger และเก็บ event `callback_result` เชิงโครงสร้าง
(เก็บเฉพาะ**ชื่อ** tool กับตัวนับ — ไม่เก็บ tool input/output เด็ดขาด)
การส่งซ้ำหรือ callback ที่ชนกับ reaper จะลู่เข้าสถานะ terminal เดียว
(ฝั่งที่แพ้ได้ `200` พร้อม `applied: false`) audit บนเส้นทางนี้ใช้ actor
`system:worker-callback`

ข้อกำหนดและขอบเขต:

- ต้องตั้ง `ATLAS_PUBLIC_BASE_URL` (URL ที่ worker เข้าถึง Atlas ได้) และ
  `ATLAS_SECRET_KEY` ไม่งั้น request ถูกปฏิเสธด้วย `400` — การ start workflow run
  ที่ graph มี callback node ก็ถูกปฏิเสธแบบ synchronous เช่นกัน (ไม่มี run ถูกสร้าง)
- job ที่ไม่มี callback กลับมาจะถูก reaper ตัดเป็น failed หลัง
  `ATLAS_CALLBACK_TIMEOUT_SECONDS` (default 3600) โดย callback token ยังใช้ได้
  เลย deadline นานพอครอบ retry ของ worker (3 ครั้งที่ ~0/10/60 วินาที) บวก
  margin กัน clock skew
- body ของ callback ถูกจำกัดขนาด (4 MiB) ก่อนเริ่มอ่าน token ผิดหรือหมดอายุได้
  `401` โดย job ไม่ถูกแตะ (audit เป็น `job.callback_rejected` เฉพาะเมื่อ job id
  มีอยู่จริง และ rate-limit ต่อ job — request ขยะไม่ทำให้เกิดการเขียนถาวรใด ๆ)
  payload ที่ `run_id` หายไปหรือไม่ตรงกับ job id ใน URL เป๊ะ ๆ ถูกปฏิเสธด้วย `400`
- Atlas restart จะไม่ล้ม **job** ที่รอ callback — งานยังรันอยู่บน worker จริง
  ไม่ใช่งานที่ถูกขัดจังหวะ — callback ที่มาช้าหลัง restart ยังปิดงานได้ตามปกติ
  ส่วน **workflow run** ที่ node ของมัน dispatch job แบบ callback ไว้ ยังใช้กติกา
  explicit recovery เดิมหลัง restart: run จะพักที่ `recovery_required` โดย recovery
  entry ติดธง `callback_pending` และผลลัพธ์ terminal ของ job ที่รันอยู่จะมาลงที่
  job row — ให้ตรวจผลนั้นก่อน authorize retry เพราะ retry จะ submit job ใหม่เสมอ

### Frozen Job Artifacts (`collect_files`, T9a)

job (หรือ worker/manager node ใน workflow) สามารถเก็บไฟล์ผลลัพธ์จริงจาก worker
หลังงานเสร็จได้ แทนที่จะส่งต่อเฉพาะข้อความ assistant โดยระบุรายการ path แบบ
relative อย่างชัดเจน:

```json
{"prompt": "เขียนรายงาน", "worker_id": "wrk_reporter", "collect_files": ["reports/out.md", "data/summary.csv"]}
```

Atlas จะส่ง glob เหล่านี้ต่อไปยัง `/agent/run` ของ thClaws (ทั้ง stream และ
`execution:"callback"`) เมื่อ worker สำเร็จแล้วจะอ่าน manifest แบบ frozen จาก
`GET /v1/sessions/{sid}/artifacts?workspace_dir=...` และอ่าน bytes ของสมาชิกผ่าน
`GET /v1/sessions/{sid}/artifacts/{aid}?workspace_dir=...` ตรวจ id/path ที่ไม่ซ้ำหลังทำ
POSIX normalization, relative path ที่ปลอดภัย (รวมถึงปฏิเสธ control character), size ที่ไม่ติดลบ, SHA-256 ตัวพิมพ์เล็ก, เพดาน 256 ไฟล์/
300 MiB รวม และ `skipped[]` ก่อนดาวน์โหลด ทุกไฟล์ต้องมี header `x-sha256`, ความยาว
จริง และ SHA-256 ที่ Atlas คำนวณตรงกับ manifest จึงจะเก็บเป็น `file_ref`:

- **ไม่มี sync fallback** เส้นทางนี้ใช้ Bearer API ของ Job Artifacts เท่านั้น Atlas
  ไม่เรียก `/workspace/sync/export` และไม่อ่าน `sync_mode`; worker รุ่นเก่าหรือ
  ไม่ทำตาม contract จะบันทึก `files.collection_failed` แบบแยกความล้มเหลวและ job ยังสำเร็จ
- **Barrier ก่อน terminal** การเก็บไฟล์จะเสร็จ *ก่อน* เขียนสถานะ `succeeded`
  ดังนั้น handoff และ workflow node ปลายทางจะเห็นชุด artifact ที่นิ่งแล้วเสมอ
  โดย barrier มีเพดานเวลา `ATLAS_COLLECT_DEADLINE_SECONDS` (ค่าเริ่มต้น 120 วินาที)
- **แยกความล้มเหลวและ atomic** manifest ผิดรูป, `skipped[]`, cap, timeout หรือ
  hash/length/header ไม่ตรง จะบันทึก `files.collection_failed` (audit ไม่เก็บเนื้อไฟล์)
  และ job ยังไปถึง `succeeded`; rows และ blobs ถูกเผยแพร่ทั้งหมดหรือไม่เผยแพร่เลย
- manifest ว่างโดยไม่มี `skipped[]` ใช้ได้; ไม่ระบุ `collect_files` จะไม่เรียก Artifact API
  เลย; callback collection อยู่นอก terminal transaction และ lease แบบ durable จะ serialize
  continued session เพื่อกัน snapshot ถูกเขียนทับ

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
`handoff_error`, `message`, `close` นอกจากนี้ Atlas เองยังเติม
`session_lease_waiting`, `session_lease_acquired`, `callback_dispatched` และ
`callback_dispatch_unconfirmed` รอบๆ การจอง session lease และ dispatch ในโหมด
callback ส่วน worker ก็ส่ง structured event เพิ่มเติม — `thinking`,
`user_message_injected`, `usage`, `result` และ event ของ tool/skill คือ
`tool_use_start`, `tool_use_result`, `tool_use_denied`, `skill_invoked`,
`skill_invoked_result` ทั้ง Atlas และ worker อาจส่งชื่อ event อื่นที่ไม่ได้ระบุ
ไว้ที่นี่ได้ — ให้ถือว่าชื่อที่ไม่รู้จักปลอดภัยที่จะข้ามไป

Event ของ tool/skill มีเฉพาะ **structural metadata** เท่านั้น — ไม่เคยมี payload
`input`/`output` ของ tool (ซึ่งอาจมี secret ที่ Atlas ตรวจไม่ได้) โดย `data` ถูก
project เป็น `{id, name, status, input_bytes, output_bytes, input_sha256,
output_sha256}` (ฟิลด์ byte/hash มีเมื่อฝั่งนั้นมีเนื้อหา) ส่วน `status` ปกติเป็น
`started`, `ok`, `error` หรือ `denied` แต่ worker อาจส่งค่าอื่นได้ — ให้ถือเป็น
open string การ project นี้ทำตอน read ด้วย ดังนั้น event ที่ replay จาก database
เก่าก็ไม่เผย raw payload:

```text
id: 7
event: tool_use_result
data: {"id":"t1","name":"Bash","status":"ok","output_bytes":20,"output_sha256":"…","seq":7,"created_at":"…"}
```

ใช้ `after=<last_seq>` เพื่อ resume/replay เมื่อ job terminal และไม่มี event ค้าง server ส่ง
`close` แล้วปิด connection ระหว่าง job ที่ยังทำงานแต่เงียบ Atlas ส่ง SSE comment
`: keepalive` ทุก 15 วินาที และส่ง reconnect hint แรก `retry: 3000`; comment ไม่มี sequence
จึงห้ามเก็บเป็น timeline event

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

`default_reply` เป็น optional และใช้รูปแบบเดียวกับ `input._meta.reply` เช่น
`{"mode":"webhook","callback_url":"https://relay.internal.nt.th/atlas/reply"}` Atlas
ตรวจ callback กับ `ATLAS_OUTBOUND_ALLOWLIST` ตอนสร้างหรืออัปเดต definition เมื่อ run ใหม่
ไม่มี `input._meta.reply` Atlas จะ copy default นี้ลง input ที่ persist ของ run; reply ระดับ
run มีลำดับสูงกว่า default ที่เก็บไว้จะถูกตรวจกับ allowlist ปัจจุบันอีกครั้ง ณ จังหวะที่ run
inherit มัน — ถ้า allowlist เปลี่ยนจน default ส่งไม่ได้ จะ reject เฉพาะ run ที่จะใช้ default
เท่านั้น ไม่กระทบ run ที่ส่ง reply มาเอง ใช้ `{"mode":"none"}` เป็น default สำหรับ poll หรือส่ง
`null` ใน `PUT` เพื่อล้าง default

`PUT /api/workflows/{id}` เป็น partial update แต่ graph/policy ที่ได้หลัง merge ต้อง valid
ส่ง `"policy": null` เพื่อล้าง policy (อ่านกลับจะเป็น `null` และระบบถือว่าเป็น `{}`)
`DELETE` ลบ definition และ trigger ที่ผูก; run เก่ายังคงอยู่แต่
`workflow_definition_id` อาจเป็น null ตาม foreign-key behavior

สำหรับ visual editor ให้ส่ง version ที่ client โหลดมาเป็น `expected_version` (และห้ามส่ง
`version` พร้อมกัน) save ที่ version ตรงกันจะเพิ่ม version ของ definition แบบ atomic;
save ที่มีคนแก้แทรกจะได้ `409` เพื่อให้ editor refresh, เสนอทางเลือก merge และไม่เขียนทับ
งานของผู้อื่นแบบเงียบ ๆ

### การส่งไฟล์ระหว่าง node (`push_files`, T9b)

edge สามารถส่งต่อไฟล์ที่เก็บไว้ก่อนหน้า (ดู `collect_files`, T9a) ให้ worker
**ตัวถัดไป** ก่อนที่ job ของ node ปลายทางจะเริ่ม — เพื่อให้สาย
Coder→Reviewer หรือ Reporter→Anchor ส่งต่อผลงานจริง ไม่ใช่แค่ข้อความ เปิดใช้แบบ
opt-in ต่อ workflow:

```json
{
  "policy": {"file_handoff": true},
  "graph": {
    "nodes": [
      {"id": "coder", "type": "worker", "role": "coder", "collect_files": ["src/app.py"]},
      {"id": "reviewer", "type": "worker", "role": "reviewer", "prompt": "ตรวจไฟล์ใน {files_dir}"}
    ],
    "edges": [{"from": "coder", "to": "reviewer", "condition": {"type": "always"}, "push_files": ["files.coder.*"]}]
  }
}
```

- **ต้องตั้ง `policy.file_handoff`** สำหรับ edge ที่มี `push_files` — ตรวจทั้งตอน
  save (validation error) และเป็น runtime guard ปิดเป็นค่าเริ่มต้น
- `push_files` เป็นรายการ glob ของ artifact key จับกับ `file_ref` artifact ที่เก็บไว้
  (คีย์ `files.<node_key>.<relpath>`)
- **เพิ่มอย่างเดียวและถูกจำกัดขอบเขต (additive + jailed)** Atlas ส่งไฟล์ผ่าน
  `POST /v1/inputs` ที่ยืนยันตัวด้วย Bearer ของ worker ไฟล์ลงใต้
  `inputs/incoming/<run_id>/<node_key>/…` — อยู่ใน jail ปลายทางค่าเริ่มต้น `inputs/`
  ของ thClaws — และ API นี้ไม่มีคำสั่ง delete/replace จึงทับไฟล์เดิมของปลายทางไม่ได้
  โทเคน `{files_dir}` ใน prompt ปลายทางจะถูกแทนด้วย prefix นั้น
- เพดาน = min(`ATLAS_SYNC_MAX_FILES`/`ATLAS_SYNC_MAX_BYTES`, ลิมิต input ของ thClaws
  100 ไฟล์ / 64 MiB) ตรวจครบก่อนส่ง ส่งเพียง **หนึ่ง** request ต่อ edge — API ฝั่ง
  upstream ไม่มี transaction/idempotency key — และ Atlas ต้องได้ ack `written[]`
  ที่ตรงทั้ง path/ขนาด/SHA-256 ของทุกไฟล์ก่อนบันทึก audit `files.pushed` และก่อนสร้าง
  job ปลายทาง ความล้มเหลวใดๆ (transport หรือ ack) จะทำให้ edge ล้มชัดเจน ไม่ใช้
  workspace sync, tar, หรือ `sync_mode` แล้ว: handoff ทำงานได้แม้ worker ปิด sync
- การส่งไฟล์ใช้ deadline เดียวกับการเก็บไฟล์ (`ATLAS_COLLECT_DEADLINE_SECONDS`
  ค่าเริ่มต้น 120 วินาที) batch ใกล้เพดาน (หลายสิบ MiB) บนลิงก์ช้าอาจไม่พอ —
  ปรับ env นี้ได้ deadline ครอบทั้ง request รวมถึงการอ่าน ack

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
events เป็น JSON cursor page ไม่ใช่ SSE ให้เก็บ `next_after` แล้วขอเฉพาะ sequence ที่ใหม่กว่า;
`has_more` บอกว่ายังมีหน้าเพิ่มใน run เดิมหรือไม่:

```text
GET /api/workflow-runs/wfr_xxx/events?limit=500&after=0
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

### Global listing

```bash
curl -sS "$BASE_URL/api/artifacts?limit=50&kind=file_ref"
curl -sS "$BASE_URL/api/artifacts?limit=50&kind=file_ref&include_content=false"
```

window แสดงผลแบบใหม่สุดก่อนข้ามทุก run/job พร้อม filter `run_id`, `job_id`,
`key`, `kind` (ใส่หรือไม่ใส่ก็ได้) โดยปริยายแถวยังรักษา shape เดิมของ `Artifact`
และมี `content` อยู่ สำหรับ dashboard list ที่ต้องการเฉพาะ metadata ให้เพิ่ม
`include_content=false` (หรือ `view=metadata`): แถวจะไม่มี top-level property
`content` และ server ไม่อ่าน/ไม่ serialize inline content สำหรับ list request นั้น
(content ไม่มี size cap การรวมไว้จะเปิดช่องให้ materialize payload ขนาดใหญ่ได้)
ต้องการ inline content แบบ on-demand ให้ใช้ `GET /api/artifacts/{artifact_id}`
และต้องการ bytes ของ `file_ref` ให้ใช้ `GET /api/artifacts/{artifact_id}/content`
response เป็น `{"artifacts": [...], "total": N, "limit": M}` — `total` นับ artifact
ทั้งหมดที่ตรง filter โดยคำนวณจาก database snapshot เดียวกับแถวที่คืน เพื่อให้ UI
บอกได้ตรง ๆ ว่า "แสดงล่าสุด M จาก N" ผู้ใช้งานที่ต้องเห็นครบทุกตัวแบบไม่ถูกตัด ให้ใช้
`/api/workflow-runs/{run_id}/artifacts` และ `/api/jobs/{job_id}/artifacts` เช่นเดิม

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

ฟิลด์ `classification` (ไม่บังคับ: `public`, `internal`, `confidential`, `secret`)
ใช้ติดป้ายชั้นความลับของข้อมูล ระบบ validate ตอนสร้างและเก็บเป็น
`metadata.classification` ค่าอื่นนอกเหนือจากนี้จะได้ `400`

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

แต่ละ entry มี `action`, `actor`, `resource_type`, `resource_id`, `details`, `created_at`
request ที่ authenticate แล้วใช้ username ส่วน explicit loopback dev/background work อาจใช้
`local` ไม่มี endpoint ลบ audit (append-only โดยตั้งใจ)

สำหรับส่งมอบงาน compliance ใช้ `from`/`to` (วันที่หรือ timestamp แบบ ISO 8601,
รวมค่าขอบช่วง) เพื่อจำกัดช่วงเวลา และ `format=csv` เพื่อ export เป็น CSV — กติกา
เดียวกับ `/api/usage` และใช้สิทธิ์ `audit.read` เหมือนกัน:

```bash
curl -sS -H 'Authorization: Bearer <token>' \
  "$BASE_URL/api/audit?from=2026-06-01&to=2026-06-30&format=csv"
```

`GET /api/metrics` คืนตัวเลขสรุปเชิงปฏิบัติการสำหรับ dashboard และการ scrape จาก
Fleet — จำนวนตาม state ของ workers/jobs/workflow runs, ยอดรวม definition/trigger/
approval/artifact/usage, `schema_version`, `version`, `time` เป็น aggregate ล้วน
จึงใช้แค่สิทธิ์ `read`:

```bash
curl -sS -H 'Authorization: Bearer <token>' "$BASE_URL/api/metrics"
```

Retention: ลบ artifact ของ run ที่จบแล้ว (terminal) ผ่าน CLI
(`python3 -m atlas.admin purge-artifacts --older-than-days N [--dry-run]`)
ไฟล์ `file_ref` บนดิสก์ถูกลบพร้อมกัน และการ purge ถูกบันทึก audit เป็น
`artifact.purge` ส่วนป้าย `classification` ของ artifact ดูที่หัวข้อ Artifacts

## 13. Usage Metering และ Export

`GET /api/usage` ใช้ได้เฉพาะ `admin` และ `auditor` พารามิเตอร์ `from`/`to`
รับวันที่หรือ timestamp แบบ ISO 8601 และรวมค่าที่ตรงขอบช่วง ส่วน `format`
มีค่าปริยาย `json` และเลือก `csv` ได้

```bash
curl -sS -H 'Authorization: Bearer <token>' \
  "$BASE_URL/api/usage?from=2026-06-01&to=2026-06-30&format=json"
```

JSON response มีรูปแบบ:

```json
{
  "usage": [{
    "id": "usg_xxx",
    "idempotency_key": "run:wfr_xxx",
    "kind": "workflow_run",
    "run_id": "wfr_xxx",
    "job_id": null,
    "node_key": null,
    "worker_id": null,
    "actor": "admin",
    "status": "succeeded",
    "units": 3,
    "seconds": 4.0,
    "started_at": "2026-06-29T10:00:00Z",
    "finished_at": "2026-06-29T10:00:04Z",
    "model": null,
    "tokens_prompt": null,
    "tokens_output": null,
    "created_at": "2026-06-29T10:00:04Z",
    "metadata": {"billing_unit":"workflow_run","billable":true}
  }],
  "totals": {
    "workflow_runs": 1,
    "successful_workflow_runs": 1,
    "jobs": 1,
    "budget_units": 3,
    "wall_seconds": 4.0,
    "job_wall_seconds": 3.0,
    "tokens_prompt": 0,
    "tokens_output": 0,
    "estimated_cost_usd": 0.0
  },
  "from": "2026-06-01T00:00:00Z",
  "to": "2026-06-30T23:59:59Z"
}
```

Atlas สร้าง event แบบ idempotent หนึ่งรายการต่อ terminal job (`units=1`) และ
หนึ่งรายการต่อ terminal workflow run (`units=budget_units_spent`) จำนวน run event
คือ headline workflow-run count ส่วน `metadata.billable` เป็น true เฉพาะ run สำเร็จ
model/token เป็นข้อมูลเพื่อ visibility เท่านั้นภายใต้ BYOK
(`byok_token_counts_billable` ยังเป็น false): `tokens_prompt`/`tokens_output`
เก็บจาก `usage` SSE event ของ worker (thClaws v0.85.0 ขึ้นไป) พร้อม payload เต็มใน
`metadata.measures` และจะเป็น null สำหรับ worker รุ่นเก่า `totals` มียอดรวม
เมื่อ effective model มีอยู่ใน catalogue `/v1/models` ของ worker ระบบจะ snapshot
`effective_model`, แหล่งที่มา (`worker` หรือ `requested`), อัตรา USD ใน
`pricing_snapshot` และ `estimated_cost_usd` ลงใน `metadata` ถ้าอัตราไม่ครบจะคิด
เฉพาะชนิด token ที่มีอัตราและตั้ง `pricing_partial: true`; model ที่เป็น tier-billed
หรือไม่ทราบราคาไม่มี estimate ข้อมูลนี้ใช้เพื่อ visibility เท่านั้น ไม่ใช่ยอดเรียกเก็บ
`totals` มียอดรวม token แบบ additive และ `estimated_cost_usd` ที่อ่านจาก snapshot
ของ event เท่านั้น ราคา worker ปัจจุบันจะไม่ย้อนกลับไป reprice ประวัติ ความผิดพลาด
ของ metering ถูก log แต่ไม่เปลี่ยนผลลัพธ์ job/run

CSV มีหนึ่งแถวต่อ raw event และมีคอลัมน์ `id`, `idempotency_key`, `kind`, `status`,
`units`, `seconds`, `run_id`, `job_id`, `node_key`, `worker_id`, `actor`,
`started_at`, `finished_at`, `model`, `tokens_prompt`, `tokens_output`, `created_at`
และ `metadata` ที่ encode เป็น JSON

ระบบ air-gapped สร้างและตรวจ HMAC-SHA256 envelope ด้วย `ATLAS_SECRET_KEY`:

```bash
ATLAS_SECRET_KEY='<secret>' python3 -m atlas.usage export usage.json \
  --from 2026-06-01 --to 2026-06-30
ATLAS_SECRET_KEY='<secret>' python3 -m atlas.usage verify usage.json
```

ใช้ `--db /path/to/atlas.sqlite` เพื่อแทน `ATLAS_DB` Atlas ส่งออก raw CDR source
เท่านั้น ส่วน Fleet/ระบบ NT ทำ aggregation, rating และ invoicing ภายหลัง

## 14. Deliveries และ Return Path

OB-1 ผูก outbound delivery sender เข้ากับ event `workflow_run_completed`
เดียวกับที่ engine ยิงภายในอยู่แล้ว (ดู
[Input Adapter Contract §7](input-adapter-contract.md#7-return-path-forward-reference)
และ
[Input Adapter & Return Path Plan](../plans/input-adapter-return-path-plan.md))
เมื่อ run ถึงสถานะ `succeeded` หรือ `failed` และ input มี
`_meta.reply.mode: "webhook"` พร้อม `callback_url` Atlas จะ POST ผลลัพธ์ที่เซ็นแล้ว
ไปยัง URL นั้น — เป็น side effect ที่ isolate ความล้มเหลว ไม่มีทางเปลี่ยนผลลัพธ์ของ run เอง

```bash
curl -sS -H 'Authorization: Bearer <operator-token>' \
  "$BASE_URL/api/deliveries?run_id=wfr_xxx"
curl -sS -X POST -H 'Authorization: Bearer <operator-token>' \
  "$BASE_URL/api/deliveries/dlv_xxx/retry"
curl -sS -X POST -H 'Authorization: Bearer <operator-token>' \
  "$BASE_URL/api/workflow-runs/wfr_xxx/deliver"
```

Body ที่เซ็นแล้ว:

```json
{
  "delivery_id": "dlv_xxx",
  "run_id": "wfr_xxx",
  "state": "succeeded",
  "correlation_id": "line:U1234:msg-4f2a",
  "artifacts": [{"key": "reply_letter", "kind": "text", "content": "…"}],
  "signed_at": "2026-07-01T09:13:11Z"
}
```

เซ็นด้วย header `X-Atlas-Signature: sha256=<hex>` — HMAC-SHA256 บน
`ATLAS_SECRET_KEY` primitive เดียวกับ signed usage export
([§13](#13-usage-metering-และ-export)) คำนวณจาก bytes ที่ POST จริง
`callback_url` ต้อง resolve ไปยัง host ที่อยู่ใน `ATLAS_OUTBOUND_ALLOWLIST`
(hostname หรือ CIDR คั่นด้วย comma; จับคู่แบบ exact hostname หรือทุก address
ที่ resolve ได้ต้องอยู่ใน CIDR ที่ allowlist ไว้) **allowlist ว่าง = ปิด
outbound delivery ทั้งหมด** (secure default โดยปริยาย) เป้าหมายที่ไม่ผ่าน
allowlist หรือเป็น private address จะถูกบันทึกเป็น `blocked` และไม่ส่งเลย
ถ้าไม่ตั้ง `ATLAS_SECRET_KEY` ก็ปฏิเสธการส่งเช่นกัน (ไม่ส่งแบบไม่เซ็นเด็ดขาด)

`status` มีค่า `pending`, `delivered`, `failed`, หรือ `blocked` ความพยายามที่ล้มเหลว
จะ retry แบบ backoff สั้นและมีขอบเขต จนถึง `ATLAS_OUTBOUND_MAX_ATTEMPTS`
(ปริยาย 5, `ATLAS_OUTBOUND_TIMEOUT` วินาทีต่อครั้ง ปริยาย 10) ก่อนจะกลายเป็น
`failed` (dead-letter) `delivery_id` คงที่ทุกครั้งที่ retry เพื่อให้ผู้รับ dedupe ได้
`POST /api/deliveries/{delivery_id}/retry` ให้ delivery ที่ `failed` หรือ
`blocked` ลองใหม่แบบมีขอบเขตอีก 1 ครั้ง (ตรวจ allowlist ปัจจุบันซ้ำ);
`POST /api/workflow-runs/{run_id}/deliver` ส่งซ้ำโดยใช้
`_meta.reply.callback_url` ของ run เอง ไม่ว่า `mode` เดิมจะเป็นอะไร ทั้งสอง
route ต้องการให้ run เสร็จสิ้นแล้วเท่านั้น

ถ้า `_meta.reply` ที่ persist แล้วไม่มีหรือ `mode: "none"` adapter จะ poll
`GET /api/workflow-runs/{run_id}` จนถึงสถานะ terminal แล้วอ่าน
`GET /api/workflow-runs/{run_id}/artifacts` แทน

## 15. OpenAPI 3.1

[openapi.yaml](openapi.yaml) ระบุ 62 paths และ 81 operations พร้อม security schemes,
parameters, request bodies, response wrappers และ schema references ใช้กับ Swagger UI,
Redoc, code generator หรือ contract tests ได้

Workflow/trigger schemas ใน OpenAPI ใช้ canonical client shape ซึ่งเข้มกว่า backend บางจุด
backend อาจเติม default ให้ field ที่ omit แต่ client ใหม่ควรส่งรูป canonical เพื่อให้
validation และ round-trip เสถียร

การใช้ OpenAPI ไม่แทน semantic validation ของ workflow เช่น duplicate node ID, cycle guard,
manager/human edge coupling, quorum และ live worker/workspace references ดู
[Visual Workflow Builder Specification](workflow-visual-builder-spec-th.md)

## 16. Checklist สำหรับ API client

- ตั้ง timeout สำหรับ JSON request แต่ไม่ใช้ timeout สั้นกับ SSE
- เก็บ `last_seq` ของ SSE และ reconnect ด้วย `after`
- ตรวจ HTTP status ก่อนอ่าน success shape
- อย่า log Authorization/query token หรือ worker token
- retry POST อย่างระวังเพราะไม่มี idempotency ทั่วไป
- ใช้ trigger `dedupe_key` เมื่อ event ภายนอกอาจ retry
- treat cancel/recovery เป็น side-effect-sensitive operation
- validate workflow/trigger ด้วย schema และให้ server validate ซ้ำ
- อย่าสมมติว่าการ upload file ทำให้ worker อ่านไฟล์ได้
