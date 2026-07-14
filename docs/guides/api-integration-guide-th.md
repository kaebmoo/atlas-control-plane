# คู่มือเชื่อมต่อ Atlas API

[English](api-integration-guide-en.md) · **ภาษาไทย**

คู่มือนี้เขียนสำหรับ developer ที่จะสร้าง web UI หรือ backend application ภายนอกมาเชื่อมต่อ
กับ Atlas โดยไม่ใช้ dashboard ในตัว เป็นเอกสารประกอบ
[API Reference](../specs/api-reference-th.md) และ [OpenAPI 3.1](../specs/openapi.yaml)
ซึ่งยังเป็น contract หลักสำหรับ endpoint path, payload และ status code — คู่มือนี้จะไม่ทวน
endpoint catalog ทั้งหมดซ้ำ ดูเพิ่มเติมที่
[docs/plans/headless-ui-split-plan.md](../plans/headless-ui-split-plan.md) สำหรับ design
เบื้องหลังการแยก headless ที่อธิบายในคู่มือนี้

## 1. ภาพรวมและ Base URL

Atlas รันแบบ **headless** ได้ (มีแค่ API ไม่มี dashboard ในตัว) โดยตั้ง `ATLAS_SERVE_UI=0`
เมื่อเปิดโหมดนี้:

- `GET /` และ `GET /static/*` ใด ๆ จะคืน `404` พร้อม JSON body (`{"error": "not found"}`)
- `GET /healthz` (liveness probe ที่ไม่ต้อง authenticate) ยังคืน `200` พร้อม
  `{"ok": true, "service": "atlas-control-plane", "version": "<version>"}` เหมือนเดิม
- ทุก route ใต้ `/api/*` ทำงานเหมือนเดิมทุกประการ — headless mode ไม่เปลี่ยนพฤติกรรม
  authentication, payload หรือ response ใด ๆ

ถ้าไม่ได้ตั้ง flag (หรือตั้ง `ATLAS_SERVE_UI=1`) Atlas จะ serve dashboard ในตัวที่ `/` เหมือนเดิม
— combined mode ยังเป็นค่าเริ่มต้นเหมือนก่อนหน้านี้ทุกอย่าง

Base URL ปริยายตอน dev: `http://127.0.0.1:8787` endpoint ที่ใช้งานได้ทั้งหมดอยู่ใต้ `/api/*`
request/response เป็น JSON (ยกเว้น SSE และ body สำหรับ upload/download ไฟล์ — ดู §4–5)
error ทุกแบบมีรูปแบบเดียวคือ `{"error": "<message>"}` พร้อม HTTP status มาตรฐาน:

| HTTP | ความหมาย |
| --- | --- |
| `400` | payload, state transition หรือ reference ไม่ถูกต้อง |
| `401` | ไม่มี token หรือ token ผิด |
| `403` | authenticate ผ่านแล้วแต่ role ไม่มีสิทธิ์ใน route นั้น |
| `404` | ไม่พบ resource หรือ route |
| `500` | exception ที่ไม่ถูกจัดการ |

การสร้าง job/run และ trigger/approval action บางตัวจะคืน `202` ก่อนงานเบื้องหลังจะเสร็จ —
ให้ poll resource นั้นหรืออ่าน event stream ของมัน

## 2. Authentication

Atlas ใช้ Bearer token รายผู้ใช้ (ตรวจสอบผ่าน `_is_authorized()` ใน `atlas/app.py`) มีสองวิธีในการ
ได้ token:

**Interactive (มนุษย์ล็อกอินผ่าน UI ของคุณ):**

```bash
curl -sS -X POST "$BASE_URL/api/auth/login" \
  -H 'content-type: application/json' \
  -d '{"username":"alice","password":"..."}'
# -> {"token":"<raw token, shown once>", "user": {"id":"...","username":"alice","role":"operator",...}}
```

เก็บ `token` ที่ได้ไว้ฝั่ง client (dashboard ใช้
`localStorage.setItem("atlasApiToken", token)`) แล้วส่งมันไปกับทุก request ถัดไป:

```text
Authorization: Bearer <token>
```

`POST /api/auth/logout` จะ revoke token ที่ใช้อยู่; `GET /api/me` คืน identity ที่ authenticate
แล้ว

**Machine-to-machine (backend service ที่ไม่มี login flow):** admin สร้าง user ที่มี role
ที่ต้องการก่อน แล้วค่อยออก token ให้ — ทั้งสอง endpoint นี้ต้องเป็น admin เท่านั้น:

```bash
curl -sS -X POST "$BASE_URL/api/users" -H 'content-type: application/json' \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"username":"reporting-bot","password":"...","role":"auditor"}'

curl -sS -X POST "$BASE_URL/api/tokens" -H 'content-type: application/json' \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"username":"reporting-bot","name":"reporting service"}'
# -> {"token": {...}, "api_token": "<raw secret, shown once — store it now>"}
```

token จะรับ role ตาม user เจ้าของมัน — ไม่มี scope แยกรายตัว token เลือก role ให้ตรงกับหน้าที่
ของ integration นั้น ๆ **ห้ามแชร์ admin token** ให้ client ที่ต้องการแค่อ่าน usage หรือรัน job:

| Role | ทำอะไรได้ |
| --- | --- |
| `viewer` | อ่าน resource ปกติเท่านั้น |
| `operator` | อ่าน + รัน job/workflow, ตัดสิน approval, จัดการ workflow/resource, poll worker (ลงทะเบียน/ลบ worker เป็นสิทธิ์ admin เท่านั้น) |
| `auditor` | อ่าน + อ่าน audit log และข้อมูล usage/billing |
| `admin` | ทำได้ทุกอย่าง รวมถึงจัดการ user/token (`/api/users`, `/api/tokens`) |

request ที่ **ไม่มี token หรือ token ผิด** จะได้ `401`; request จาก **identity ที่
authenticate แล้วแต่ role ไม่มีสิทธิ์ใน route นั้น** จะได้ `403` — แยกสองกรณีนี้ตอน handle error

**กฎการจัดการ token:**

- ส่ง token ผ่าน header `Authorization: Bearer <token>` เท่านั้น
- ข้อยกเว้นที่มีเอกสารรองรับมีแค่ **หนึ่งเดียว** คือ `GET .../events?token=<token>`
  (job SSE) เพราะ browser `EventSource` ตั้ง header เองไม่ได้ — ห้ามใช้รูปแบบ query-string
  กับ request อื่นเด็ดขาด และควรใช้วิธี `fetch()` streaming แบบ header ใน §5 แทน
- ห้าม log token เด็ดขาด structured request log ของ Atlas เอง (`ATLAS_REQUEST_LOG`) จง
  ใจ log แค่ **path** ของ request ไม่เคย log query string ด้วยเหตุผลนี้

## 3. CORS สำหรับ browser client

ค่าเริ่มต้น: `Access-Control-Allow-Origin: *` (เหมือนเดิม) — origin ใดก็เรียก API ได้ เพราะ auth
เป็น Bearer token ไม่ใช่ cookie จึงไม่มีความเสี่ยงจาก ambient credential แม้ policy origin จะเปิด
กว้าง

สำหรับ production ให้ตั้ง allowlist ชัดเจน:

```bash
ATLAS_CORS_ORIGINS=https://ui.example.com,https://admin.example.com
```

เมื่อตั้ง allowlist แล้ว: request ที่ header `Origin` ตรงกับรายการที่อนุญาตทุกตัวอักษรจะได้
origin นั้น echo กลับมา (`Access-Control-Allow-Origin: <origin>` + `Vary: Origin`); origin อื่น
ใดจะ **ไม่ได้** header `Access-Control-Allow-Origin` เลย (browser จะ block response) Atlas ไม่
เคยส่ง `Access-Control-Allow-Credentials` เพราะไม่ได้ใช้ cookie จึงไม่มีอะไรต้อง opt-in
same-origin request และ client ที่ไม่ใช่ browser (ซึ่งไม่ส่ง `Origin`) ไม่ได้รับผลกระทบไม่ว่ากรณี
ใด

## 4. Call flow หลัก

แต่ละ flow ด้านล่างแสดง 3 แบบ: `curl`, browser `fetch` และ Python stdlib `urllib.request` — ไม่
ต้องใช้ HTTP client จากภายนอกในการคุยกับ Atlas เลย

### 4a. Login → list workers → submit job → poll status

<details><summary>curl</summary>

```bash
BASE_URL=http://127.0.0.1:8787
TOKEN=$(curl -sS -X POST "$BASE_URL/api/auth/login" -H 'content-type: application/json' \
  -d '{"username":"alice","password":"..."}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')

curl -sS -H "Authorization: Bearer $TOKEN" "$BASE_URL/api/workers"

JOB=$(curl -sS -X POST "$BASE_URL/api/jobs" -H 'content-type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"prompt":"Research AI news","role":"reporter"}')
JOB_ID=$(echo "$JOB" | python3 -c 'import json,sys;print(json.load(sys.stdin)["job"]["id"])')

curl -sS -H "Authorization: Bearer $TOKEN" "$BASE_URL/api/jobs/$JOB_ID"
```

</details>

<details><summary>Browser fetch</summary>

```js
const BASE_URL = "http://127.0.0.1:8787"; // or "" for same-origin
let token;

async function login(username, password) {
  const res = await fetch(`${BASE_URL}/api/auth/login`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  const data = await res.json();
  token = data.token;
  return data.user;
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", `Bearer ${token}`);
  if (options.body) headers.set("content-type", "application/json");
  const res = await fetch(`${BASE_URL}${path}`, { ...options, headers });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

await login("alice", "...");
const { workers } = await api("/api/workers");
const { job } = await api("/api/jobs", { method: "POST", body: JSON.stringify({ prompt: "Research AI news", role: "reporter" }) });
const status = await api(`/api/jobs/${job.id}`);
```

</details>

<details><summary>Python (urllib.request)</summary>

```python
import json
import urllib.request

BASE_URL = "http://127.0.0.1:8787"


def call(method, path, token=None, payload=None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"content-type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE_URL + path, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


login = call("POST", "/api/auth/login", payload={"username": "alice", "password": "..."})
token = login["token"]
workers = call("GET", "/api/workers", token=token)
job = call("POST", "/api/jobs", token=token, payload={"prompt": "Research AI news", "role": "reporter"})["job"]
status = call("GET", f"/api/jobs/{job['id']}", token=token)
```

</details>

### 4b. รัน workflow แล้วอ่าน artifact ของ run

```bash
RUN=$(curl -sS -X POST "$BASE_URL/api/workflow-runs" -H 'content-type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"workflow_definition_id":"wfd_xxx","input":{"topic":"AI"}}')
RUN_ID=$(echo "$RUN" | python3 -c 'import json,sys;print(json.load(sys.stdin)["run"]["id"])')

curl -sS -H "Authorization: Bearer $TOKEN" "$BASE_URL/api/workflow-runs/$RUN_ID"
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE_URL/api/workflow-runs/$RUN_ID/artifacts"
```

`POST /api/workflow-runs` คืน `202` ก่อน run จะเสร็จ; ให้ poll `GET /api/workflow-runs/{id}`
(state จะเปลี่ยนเป็น `succeeded`/`failed`/ฯลฯ) หรืออ่าน `GET /api/workflow-runs/{id}/events`
สำหรับ timeline ของ lifecycle รูปแบบเดียวกันนี้ใช้ผ่าน `fetch`/`urllib.request` ได้เหมือน §4a —
แค่เปลี่ยน path และ payload

### 4c. อัปโหลดไฟล์เข้า run แล้วดาวน์โหลด artifact

การอัปโหลดเป็น binary body ตรง ๆ ไม่ใช่ multipart หรือ base64 ต้องมี `Content-Length` และขนาด
ถูกจำกัดโดย `ATLAS_MAX_UPLOAD_BYTES` (ปริยาย 10 MiB)

```bash
curl -sS -X POST "$BASE_URL/api/workflow-runs/$RUN_ID/files?key=contract" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/pdf' \
  -H 'x-filename: contract.pdf' \
  --data-binary @contract.pdf
```

การดาวน์โหลด `file_ref` artifact **ต้องส่ง Authorization header** — `<a href="/api/artifacts/art_xxx/content">`
เปล่า ๆ จะได้ `401` เพราะ browser ไม่ส่ง credential ใด ๆ ไปกับการ navigate ตรง ๆ ให้ fetch เป็น
blob แล้วสั่งดาวน์โหลดเอง (เหมือนที่ `downloadArtifact()` ใน `atlas/static/app.js` ทำอยู่):

```js
async function downloadArtifact(artifactId) {
  const res = await fetch(`${BASE_URL}/api/artifacts/${artifactId}/content`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const blob = await res.blob();
  const filename = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "")?.[1] || "download";
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}
```

## 5. Job event streaming (SSE)

มีสองวิธีในการอ่าน `GET /api/jobs/{id}/events?after=<seq>`:

**แนะนำ — `fetch()` streaming พร้อม Authorization header** (แบบที่ `openJobStream()` ใน
`atlas/static/app.js` ทำ ~บรรทัด 1571): token ไม่ไปโผล่ใน URL เลย จึงไม่มีทางไปอยู่ใน access log
ของ reverse proxy

```js
const controller = new AbortController();
const res = await fetch(`${BASE_URL}/api/jobs/${jobId}/events?after=0`, {
  headers: { Authorization: `Bearer ${token}`, Accept: "text/event-stream" },
  signal: controller.signal,
});
const reader = res.body.getReader();
const decoder = new TextDecoder();
let buffer = "", sawClose = false;
for (;;) {
  const { value, done } = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, { stream: true });
  let sep;
  while ((sep = buffer.indexOf("\n\n")) !== -1) {
    const frame = buffer.slice(0, sep);
    buffer = buffer.slice(sep + 2);
    let name = "message", data = [];
    for (const line of frame.split("\n")) {
      if (line.startsWith("event:")) name = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
    }
    if (name === "close") sawClose = true;
    // handle (name, data.join("\n"))
  }
}
// EOF without a "close" frame means the connection dropped mid-stream.
if (!sawClose) console.warn("event stream disconnected");
```

**สำรอง — `EventSource` พร้อม query fallback `?token=`** นี่คือ endpoint กลุ่ม
**เดียว** ที่รูปแบบ query-token ใช้ได้ (ดู §2) เพราะ `EventSource` ตั้ง header เองไม่ได้:

```js
const es = new EventSource(`${BASE_URL}/api/jobs/${jobId}/events?after=0&token=${encodeURIComponent(token)}`);
es.addEventListener("text", (e) => console.log(JSON.parse(e.data)));
es.addEventListener("close", () => es.close());
```

ทั้งสองแบบใช้ frame รูปแบบ `id: <seq>` / `event: <name>` / `data: <json>` ใช้
`after=<last_seq>` เพื่อ resume/replay หลัง reconnect server จะส่ง event `close` เสมอก่อนปิด
stream — ถ้าถึง EOF โดยไม่มี `close` แปลว่า connection หลุด ไม่ใช่ job เสร็จ ให้ reconnect ด้วย
`after=<seq ล่าสุดที่เห็น>` ดู event name ทั้งหมดได้ที่
[API Reference §6](../specs/api-reference-th.md#job-sse)

## 6. สร้าง web UI ทดแทน

dashboard ในตัว (`atlas/static/`) เป็น reference client ที่ใช้งานได้จริง — HTML/CSS/JS ธรรมดา
ไม่มี framework หรือ build step มีสองอย่างที่ทำให้ deploy ที่ไหนก็ได้:

- **`config.js` / `window.ATLAS_API_BASE`** — `atlas/static/config.js` ตั้ง
  `window.ATLAS_API_BASE = ""` (same-origin) เป็นค่าเริ่มต้น `app.js` อ่านค่านี้ครั้งเดียวเก็บใน
  `const API_BASE` แล้ว prefix ทุกการเรียก API ด้วยค่านี้ ต้องการชี้ dashboard ชุดเดียวกันไปที่
  Atlas instance อื่น แค่ส่ง `config.js` ที่ตั้ง `window.ATLAS_API_BASE = "https://atlas.example.com"`
  — ไม่ต้อง build ใหม่ มันเป็นแค่ static file
- **Static file host ที่ไหนก็ได้** — static host, CDN หรือ directory `atlas/static/` เอง — เพราะ
  CORS (§3) และ Bearer auth (§2) ทำให้ API ไม่ผูกกับ origin ใด API server ไม่จำเป็นต้อง serve UI
  ด้วย (`ATLAS_SERVE_UI=0`, §1)

สำหรับ dev ในเครื่องกับ headless API ที่รันอยู่ ให้ใช้ `scripts/serve_ui.py` (§7) แทนการเขียน
static server เอง — มันจำลอง SPA-fallback routing แบบ production ให้แล้วและ inject ค่า
`API_BASE` สำหรับ dev ให้ทันที

## 7. Local dev quickstart

**Combined (ค่าเริ่มต้นปัจจุบัน) — process เดียว port เดียว:**

```bash
python3 -m atlas --host 127.0.0.1 --port 8787
# ATLAS_LOOPBACK_NO_AUTH=true python3 -m atlas ...  — skips login for local hacking
```

**Split dev — API แบบ headless port หนึ่ง dashboard แบบ live อีก port หนึ่ง:**

```bash
# terminal 1
ATLAS_SERVE_UI=0 python3 -m atlas --host 127.0.0.1 --port 8787

# terminal 2 — edits under atlas/static/ show up on refresh (no-store, no build step)
python3 scripts/serve_ui.py --port 8000 --api-base http://127.0.0.1:8787
```

เปิด `http://127.0.0.1:8000` `ATLAS_LOOPBACK_NO_AUTH` ยังใช้ได้ใน split mode — browser เรียก
API ตรงจาก `127.0.0.1` และ CORS ปริยาย `*` ทำให้เรียกข้าม port ได้โดยไม่ต้องตั้งค่าเพิ่ม

**คำเตือนด้านความปลอดภัย:** `ATLAS_LOOPBACK_NO_AUTH` ให้ identity **admin** ในตัวกับ request ใด
ก็ตามที่ *source address* เป็น `127.0.0.1`/`::1` — มันไม่ได้ตรวจว่าใครอยู่เบื้องหลัง request จริง
ๆ การรัน Atlas หลัง reverse proxy บนเครื่องเดียวกัน (nginx/Caddy บนเครื่องเดียวกัน proxy ไปที่
`127.0.0.1:8787`) จะทำให้ **ทุก** request ที่ผ่าน proxy ดูเหมือนมาจาก `127.0.0.1` และมอบสิทธิ์
admin ให้ใครก็ตามที่ proxy ยอมรับ connection ด้วยอย่างเงียบ ๆ ให้ปิด `ATLAS_LOOPBACK_NO_AUTH`
(`false` ค่าเริ่มต้น) ไว้เสมอเมื่อมี reverse proxy อยู่ด้านหน้า — ดู
[docs/ops/deployment.md](../ops/deployment.md)

## 8. Versioning และ compatibility

`/api/*` เป็นแบบ **additive-only**: Atlas จะไม่เปลี่ยน endpoint path หรือ response shape ที่มีอยู่
แล้วเด็ดขาด (ดู `AGENTS.md`) field ใหม่อาจโผล่มาใน response ได้เรื่อย ๆ — client ควรทนและ
ignore field JSON ที่ไม่รู้จัก และถือว่า SSE event name ที่ไม่รู้จักปลอดภัยที่จะ ignore เช่นกัน
(§5) ตอนนี้ยังไม่มี prefix `/v1`; ให้ pin commit หรือ release ที่ deploy อยู่ และติดตาม
[API Reference](../specs/api-reference-th.md) เมื่ออัปเกรด
