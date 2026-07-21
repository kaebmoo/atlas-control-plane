# Atlas API Integration Guide

**English** · [ภาษาไทย](api-integration-guide-th.md)

This guide is for developers building an external web UI or backend application
against Atlas — without the built-in dashboard. It is a companion to the
[API Reference](../specs/api-reference-en.md) and [OpenAPI 3.1](../specs/openapi.yaml)
spec, which remain the authoritative contract for endpoint paths, payloads, and
status codes; this guide does not repeat the full endpoint catalog. See also
[docs/plans/headless-ui-split-plan.md](../plans/headless-ui-split-plan.md) for the
design behind the headless split described here.

## 1. Overview & base URL

Atlas can run **headless** — API only, no built-in dashboard — by setting
`ATLAS_SERVE_UI=0`. In that mode:

- `GET /` and any `GET /static/*` return `404` with a JSON body (`{"error": "not found"}`).
- `GET /healthz` (unauthenticated liveness probe) still returns `200` with
  `{"ok": true, "service": "atlas-control-plane", "version": "<version>"}`.
- Every `/api/*` route behaves exactly as it does today — headless mode changes
  nothing about authentication, payloads, or responses.

With the flag unset (or `ATLAS_SERVE_UI=1`), Atlas serves the built-in dashboard
at `/` as it always has — combined mode is the unchanged default.

Default base URL in dev: `http://127.0.0.1:8787`. All functional endpoints live
under `/api/*`; requests and responses are JSON (except SSE and file
upload/download bodies — see §4–5). Every error is
`{"error": "<message>"}` with a standard HTTP status:

| HTTP | Meaning |
| --- | --- |
| `400` | Invalid payload, state transition, or reference |
| `401` | Missing or incorrect token |
| `403` | Authenticated role lacks the route permission |
| `429` | Login rate limit; wait for the `Retry-After` duration |
| `404` | Resource or route not found |
| `500` | Unhandled exception |

Job/run creation and some trigger/approval actions return `202` before
background work finishes — poll the resource or read its event stream.

## 2. Authentication

Atlas uses per-user Bearer tokens (verified against `_is_authorized()` in
`atlas/app.py`). There are two ways to get one:

**Interactive (a human signs in through your UI):**

```bash
curl -sS -X POST "$BASE_URL/api/auth/login" \
  -H 'content-type: application/json' \
  -d '{"username":"alice","password":"..."}'
# -> {"token":"<raw token, shown once>", "user": {"id":"...","username":"alice","role":"operator",...}, "session":{"token_id":"...","expires_at":"...Z"}}
```

Store the returned `token` client-side (the dashboard uses
`localStorage.setItem("atlasApiToken", token)`) and send it on every subsequent
call:

```text
Authorization: Bearer <token>
```

This is a `purpose=session` token, not a general integration credential: it expires
after 8 hours by default and `GET /api/me` repeats `session.expires_at` so the UI
can warn or sign out cleanly. Each user has at most five unexpired sessions; a new
login revokes only the oldest excess session. `POST /api/auth/logout` revokes the
token currently in use.

Failed logins are limited before PBKDF2 by normalized username + direct peer IP
(default 5/min, then a 60-second cooldown). A limit response is `429` with
`Retry-After`; clients should wait rather than retrying. The limiter is a bounded
in-memory layer and resets on Atlas restart, so deploy a rate limit at the reverse
proxy/WAF as well. Atlas intentionally does not trust `X-Forwarded-For`.

**Machine-to-machine (a backend service, no login flow):** an admin creates a
user with the right role, then mints a token for it — both are admin-only:

```bash
curl -sS -X POST "$BASE_URL/api/users" -H 'content-type: application/json' \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"username":"reporting-bot","password":"...","role":"auditor"}'

curl -sS -X POST "$BASE_URL/api/tokens" -H 'content-type: application/json' \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"username":"reporting-bot","name":"reporting service","expires_at":"2026-12-31T00:00:00Z"}'
# -> {"token": {...}, "api_token": "<raw secret, shown once — store it now>"}
```

A machine token has immutable `purpose: "api"`; omit `expires_at` only when a
deliberately non-expiring integration credential is required. List/get token
metadata exposes `purpose`, `expires_at`, and revocation state, never a raw token
or token hash. A token inherits its user's role — there is no separate per-token scope. Pick
the role that matches the integration's job, **never share the admin token**
with a client that only needs to read usage or run jobs:

| Role | Can do |
| --- | --- |
| `viewer` | Read normal resources only |
| `operator` | Read + run jobs/workflows, decide approvals, manage workflows/resources, poll workers (registering/deleting workers is admin-only) |
| `auditor` | Read + read audit log and usage/billing data |
| `admin` | Everything, including user/token management (`/api/users`, `/api/tokens`) |

A request with **no or a wrong token** gets `401`; a request from an
**authenticated identity whose role lacks the route's permission** gets `403`
— distinguish the two when handling errors.

**Token handling rules:**

- Send the token only via the `Authorization: Bearer <token>` header.
- The **one** documented exception is `GET .../events?token=<token>` (job SSE),
  which exists solely because browser `EventSource` cannot set headers — never
  use the query-string form for any other request, and prefer the
  header-based `fetch()` streaming approach in §5 instead.
- Never log a token. Atlas's own structured request log (`ATLAS_REQUEST_LOG`)
  intentionally logs the request **path only**, never the query string, for
  this reason.

## 3. CORS for browser clients

Default: `Access-Control-Allow-Origin: *` (unchanged from today) — any origin
may call the API, since auth is Bearer-token, not cookies, so there is no
ambient-credential risk from a wide-open origin policy.

For production, set an explicit allowlist:

```bash
ATLAS_CORS_ORIGINS=https://ui.example.com,https://admin.example.com
```

With an allowlist set: a request whose `Origin` header exactly matches an
allowed entry gets that origin echoed back (`Access-Control-Allow-Origin: <origin>`
+ `Vary: Origin`); any other origin gets **no** `Access-Control-Allow-Origin`
header at all (the browser blocks the response). Atlas never sends
`Access-Control-Allow-Credentials` — it doesn't use cookies, so there is
nothing to opt into. Same-origin requests and non-browser clients (which don't
send `Origin`) are unaffected either way.

## 4. Core call flows

Every flow below is shown three ways: `curl`, browser `fetch`, and Python
stdlib `urllib.request` — no third-party HTTP client is required to talk to
Atlas.

### 4a. Login → list workers → submit a job → poll status

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

### 4b. Run a workflow and read its artifacts

```bash
RUN=$(curl -sS -X POST "$BASE_URL/api/workflow-runs" -H 'content-type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"workflow_definition_id":"wfd_xxx","input":{"topic":"AI"}}')
RUN_ID=$(echo "$RUN" | python3 -c 'import json,sys;print(json.load(sys.stdin)["run"]["id"])')

curl -sS -H "Authorization: Bearer $TOKEN" "$BASE_URL/api/workflow-runs/$RUN_ID"
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE_URL/api/workflow-runs/$RUN_ID/artifacts"
```

`POST /api/workflow-runs` returns `202` before the run finishes; poll
`GET /api/workflow-runs/{id}` (state becomes `succeeded`/`failed`/etc.) or read
`GET /api/workflow-runs/{id}/events` for the lifecycle timeline. The same
pattern applies via `fetch`/`urllib.request` as in §4a — swap the path and
payload.

### 4c. Upload a file to a run, then download an artifact

Uploads are raw binary bodies, not multipart or base64. `Content-Length` is
required and the size is capped by `ATLAS_MAX_UPLOAD_BYTES` (default 10 MiB).

```bash
curl -sS -X POST "$BASE_URL/api/workflow-runs/$RUN_ID/files?key=contract" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/pdf' \
  -H 'x-filename: contract.pdf' \
  --data-binary @contract.pdf
```

Downloading a `file_ref` artifact **requires the Authorization header** — a
bare `<a href="/api/artifacts/art_xxx/content">` will get a `401` because the
browser sends no credentials with a plain navigation. Fetch it as a blob and
trigger the download yourself (this is exactly what
`downloadArtifact()` in `atlas/static/app.js` does):

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

## 5. Streaming job events (SSE)

Two ways to consume `GET /api/jobs/{id}/events?after=<seq>`:

**Preferred — `fetch()` streaming with the Authorization header** (what
`atlas/static/app.js`'s `openJobStream()` does, ~line 1571): the token never
touches the URL, so it never lands in a reverse-proxy access log.

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

**Fallback — `EventSource` with the `?token=` query fallback.** This is the
**only** endpoint family where the query-token form is valid (see §2), because
`EventSource` cannot set headers:

```js
const es = new EventSource(`${BASE_URL}/api/jobs/${jobId}/events?after=0&token=${encodeURIComponent(token)}`);
es.addEventListener("text", (e) => console.log(JSON.parse(e.data)));
es.addEventListener("close", () => es.close());
```

Both forms use `id: <seq>` / `event: <name>` / `data: <json>` frames. Use
`after=<last_seq>` to resume/replay after a reconnect. The server always sends
a `close` event before ending the stream — reaching EOF without one means the
connection dropped, not that the job finished; reconnect with
`after=<last seq you saw>`. See the [API Reference §6](../specs/api-reference-en.md#job-sse)
for the full event-name catalog.

## 6. Building a replacement web UI

The shipped dashboard (`atlas/static/`) is a working reference client — plain
HTML/CSS/JS, no framework or build step. Two things make it deployable
anywhere:

- **`config.js` / `window.ATLAS_API_BASE`** — `atlas/static/config.js` sets
  `window.ATLAS_API_BASE = ""` (same-origin) by default. `app.js` reads it once
  into `const API_BASE` and prefixes every API call with it. Point a copy of
  the dashboard at a different Atlas instance by shipping a `config.js` that
  sets `window.ATLAS_API_BASE = "https://atlas.example.com"` — no rebuild, it's
  a static file.
- **Static files can be hosted anywhere** — any static host, CDN, or the
  `atlas/static/` directory itself — because CORS (§3) and Bearer auth (§2)
  make the API origin-independent. The API server does not need to also serve
  the UI (`ATLAS_SERVE_UI=0`, §1).

For local development against a running headless API, use
`scripts/serve_ui.py` (§7) instead of hand-rolling a static server — it
already mirrors the production SPA-fallback routing and injects the dev
`API_BASE` for you.

## 7. Local dev quickstart

**Combined (today's default) — one process, one port:**

```bash
python3 -m atlas --host 127.0.0.1 --port 8787
# ATLAS_LOOPBACK_NO_AUTH=true python3 -m atlas ...  — skips login for local hacking
```

**Split dev — API headless on one port, dashboard served live on another:**

```bash
# terminal 1
ATLAS_SERVE_UI=0 python3 -m atlas --host 127.0.0.1 --port 8787

# terminal 2 — edits under atlas/static/ show up on refresh (no-store, no build step)
python3 scripts/serve_ui.py --port 8000 --api-base http://127.0.0.1:8787
```

Open `http://127.0.0.1:8000`. `ATLAS_LOOPBACK_NO_AUTH` still works in split
mode — the browser calls the API directly from `127.0.0.1`, and the default
CORS `*` makes the cross-port call work with zero extra configuration.

**Security warning:** `ATLAS_LOOPBACK_NO_AUTH` grants the built-in **admin**
identity to any request whose *source address* is `127.0.0.1`/`::1` — it does
not check who is actually behind that request. Running Atlas behind a
same-host reverse proxy (nginx/Caddy on the same machine, proxying to
`127.0.0.1:8787`) makes **every** proxied request appear to originate from
`127.0.0.1`, silently granting admin to anyone the proxy accepts a connection
from. Keep `ATLAS_LOOPBACK_NO_AUTH` off (`false`, the default) whenever a
reverse proxy sits in front — see
[docs/ops/deployment.md](../ops/deployment.md).

## 8. Versioning & compatibility

`/api/*` is **additive-only**: Atlas never changes an existing endpoint path
or response shape (see `AGENTS.md`). New fields may appear in responses over
time — clients should tolerate and ignore unknown JSON fields, and treat
unrecognized SSE event names as safe to ignore (§5). There is currently no
`/v1` prefix; pin the deployed commit or release and review the
[API Reference](../specs/api-reference-en.md) when upgrading.
