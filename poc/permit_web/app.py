#!/usr/bin/env python3
"""Permit-application web PoC for Atlas (stdlib only).

A thin web adapter that demonstrates the Input Adapter Contract + poll return path:

  browser form  --/api/submit-->  Atlas POST /api/workflow-runs (envelope w/ _meta)
  browser poll  --/api/status-->  Atlas GET  /api/workflow-runs/{id} (+ artifacts, approval)
  approver      --/api/decide-->  Atlas POST /api/approvals/{id}/approve|reject

The operator token stays server-side (never in the browser). No OB-1 webhook / allowlist
needed — the page polls the run it just created.

Env:
  ATLAS_BASE     Atlas base URL            (default http://127.0.0.1:8787)
  ATLAS_TOKEN    operator/admin API token  (blank ok on loopback-no-auth Atlas)
  WORKFLOW_NAME  workflow to run           (default "PoC Permit Application")
  WORKFLOW_ID    override auto-discovery    (optional)
  PORT           PoC web port              (default 8080)

Run:  python3 app.py   then open http://127.0.0.1:8080
"""
from __future__ import annotations

import json
import os
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import urllib.error
import urllib.request

ATLAS = os.environ.get("ATLAS_BASE", "http://127.0.0.1:8787").rstrip("/")
TOKEN = os.environ.get("ATLAS_TOKEN", "")
WORKFLOW_NAME = os.environ.get("WORKFLOW_NAME", "PoC Permit Application")
WORKFLOW_ID = os.environ.get("WORKFLOW_ID", "")
PORT = int(os.environ.get("PORT", "8080"))

_wf_cache = {"id": WORKFLOW_ID}


class AtlasError(RuntimeError):
    pass


def atlas(method: str, path: str, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(ATLAS + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode("utf-8", errors="replace")
            return json.loads(text or "{}")
    except urllib.error.HTTPError as e:
        raise AtlasError(f"Atlas {method} {path} -> HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
    except urllib.error.URLError as e:
        raise AtlasError(f"Cannot reach Atlas at {ATLAS} ({e.reason})")


def as_list(resp):
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for key in ("workflows", "runs", "artifacts", "approvals", "items", "results", "data"):
            if isinstance(resp.get(key), list):
                return resp[key]
    return []


def workflow_id() -> str:
    if _wf_cache["id"]:
        return _wf_cache["id"]
    for wf in as_list(atlas("GET", "/api/workflows")):
        if wf.get("name") == WORKFLOW_NAME:
            _wf_cache["id"] = wf["id"]
            return wf["id"]
    raise AtlasError(f"Workflow {WORKFLOW_NAME!r} not found. Run setup.py first.")


def start_run(fields: dict):
    envelope = {
        "applicant_name": fields.get("applicant_name", ""),
        "national_id": fields.get("national_id", ""),
        "permit_type": fields.get("permit_type", ""),
        "detail": fields.get("detail", ""),
        "attachments": fields.get("attachments", ""),
        "_meta": {
            "source": {
                "channel": "web_form",
                "adapter": "permit-web-poc",
                "form": "permit_request",
                "external_id": "web-" + uuid.uuid4().hex[:12],
            }
        },
    }
    run = atlas("POST", "/api/workflow-runs", {
        "workflow_definition_id": workflow_id(),
        "input": envelope,
    })
    rid = run.get("id") or (run.get("run") or {}).get("id")
    if not rid:
        raise AtlasError(f"No run id in response: {json.dumps(run)[:200]}")
    return {"run_id": rid}


def run_status(run_id: str):
    run = atlas("GET", f"/api/workflow-runs/{run_id}")
    state = run.get("state") or (run.get("run") or {}).get("state") or "unknown"
    artifacts = []
    for a in as_list(atlas("GET", f"/api/workflow-runs/{run_id}/artifacts")):
        artifacts.append({"key": a.get("key"), "kind": a.get("kind"), "content": a.get("content")})
    approval = None
    if state == "waiting_for_human":
        for ap in as_list(atlas("GET", "/api/approvals?state=pending")):
            rid = ap.get("run_id") or ap.get("workflow_run_id") or ap.get("wfr_id")
            if rid == run_id:
                approval = {"id": ap.get("id"), "label": ap.get("label"), "reason": ap.get("reason")}
                break
    return {"state": state, "artifacts": artifacts, "approval": approval}


def decide(run_id: str, action: str):
    target = None
    for ap in as_list(atlas("GET", "/api/approvals?state=pending")):
        rid = ap.get("run_id") or ap.get("workflow_run_id") or ap.get("wfr_id")
        if rid == run_id:
            target = ap.get("id")
            break
    if not target:
        raise AtlasError("No pending approval for this run")
    verb = "approve" if action == "approve" else "reject"
    atlas("POST", f"/api/approvals/{target}/{verb}")
    return {"ok": True, "action": verb}


PAGE = """<!doctype html>
<html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PoC — แบบฟอร์มขออนุญาต (Atlas)</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, -apple-system, "Noto Sans Thai", sans-serif; max-width: 760px;
         margin: 24px auto; padding: 0 16px; line-height: 1.5; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: #666; font-size: 13px; margin: 0 0 20px; }
  label { display:block; font-size: 13px; font-weight: 600; margin: 12px 0 4px; }
  input, textarea, select { width: 100%; padding: 8px; border: 1px solid #bbb; border-radius: 8px;
         font: inherit; box-sizing: border-box; background: transparent; color: inherit; }
  textarea { min-height: 64px; }
  button { font: inherit; padding: 9px 16px; border-radius: 8px; border: 1px solid #888;
           background: #2b6; color: #fff; cursor: pointer; margin-top: 16px; }
  button.secondary { background: transparent; color: inherit; }
  button.reject { background: #c33; border-color:#c33; }
  #panel { margin-top: 24px; }
  .step { border: 1px solid #ccc; border-radius: 10px; padding: 12px; margin: 10px 0; }
  .step h3 { margin: 0 0 6px; font-size: 14px; }
  pre { white-space: pre-wrap; margin: 0; font: inherit; }
  .state { display:inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px;
           border: 1px solid #888; }
  .muted { color:#777; font-size:12px; }
  .gate { border-color:#e0a800; background: rgba(224,168,0,.08); }
</style></head>
<body>
  <h1>แบบฟอร์มขออนุญาต — PoC</h1>
  <p class="sub">เว็บฟอร์ม (adapter) → Atlas (routing · policy · human approval · audit) → worker.
     ผลลัพธ์ดึงกลับด้วยการ poll สถานะ run</p>

  <form id="f">
    <label>ชื่อผู้ขอ</label><input name="applicant_name" required value="นายทดสอบ ระบบ">
    <label>เลขบัตรประชาชน</label><input name="national_id" value="1234567890123">
    <label>ประเภทคำขออนุญาต</label>
    <select name="permit_type">
      <option>ขออนุญาตก่อสร้าง</option>
      <option>ขออนุญาตประกอบกิจการ</option>
      <option>ขออนุญาตใช้พื้นที่</option>
    </select>
    <label>รายละเอียด / เหตุผล</label><textarea name="detail">ขออนุญาตก่อสร้างอาคารพาณิชย์ 2 ชั้น บนที่ดินของตนเอง</textarea>
    <label>เอกสารแนบ (พิมพ์รายการ)</label><input name="attachments" value="สำเนาบัตรประชาชน, โฉนดที่ดิน, แบบแปลน">
    <button type="submit">ส่งคำขอ</button>
  </form>

  <div id="panel"></div>

<script>
const $ = s => document.querySelector(s);
let timer = null, runId = null;

$("#f").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = Object.fromEntries(new FormData($("#f")).entries());
  $("#panel").innerHTML = '<p class="muted">กำลังส่งคำขอเข้า Atlas…</p>';
  try {
    const r = await fetch("/api/submit", {method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify(fd)});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "submit failed");
    runId = j.run_id;
    poll();
    timer = setInterval(poll, 1500);
  } catch (err) { $("#panel").innerHTML = '<p style="color:#c33">ผิดพลาด: '+err.message+'</p>'; }
});

async function poll() {
  if (!runId) return;
  const r = await fetch("/api/status?run_id="+encodeURIComponent(runId));
  const j = await r.json();
  if (!r.ok) { $("#panel").innerHTML = '<p style="color:#c33">'+(j.error||"status error")+'</p>'; clearInterval(timer); return; }
  render(j);
  if (["succeeded","failed","cancelled"].includes(j.state)) clearInterval(timer);
}

function render(j) {
  let h = '<p>สถานะ run: <span class="state">'+j.state+'</span> <span class="muted">('+runId+')</span></p>';
  if (j.approval) {
    h += '<div class="step gate"><h3>รออนุมัติจากเจ้าหน้าที่</h3>'
       + '<p class="muted">'+(j.approval.reason||"")+'</p>'
       + '<button onclick="decide(\\'approve\\')">อนุมัติ</button> '
       + '<button class="reject" onclick="decide(\\'reject\\')">ปฏิเสธ</button></div>';
  }
  for (const a of (j.artifacts||[])) {
    h += '<div class="step"><h3>'+a.key+' <span class="muted">('+a.kind+')</span></h3><pre>'
       + (typeof a.content === "string" ? a.content : JSON.stringify(a.content, null, 2)) + '</pre></div>';
  }
  if (j.state === "succeeded") h += '<p style="color:#2b6">เสร็จสมบูรณ์ — ได้หนังสือแจ้งผลแล้ว</p>';
  if (j.state === "failed") h += '<p style="color:#c33">run ล้มเหลว (เช่น ถูกปฏิเสธที่ human gate)</p>';
  $("#panel").innerHTML = h;
}

async function decide(action) {
  const r = await fetch("/api/decide", {method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({run_id: runId, action})});
  const j = await r.json();
  if (!r.ok) { alert(j.error || "decide failed"); return; }
  poll();
}
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        return

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw or b"{}")
        except Exception:
            return {}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/status":
            run_id = (parse_qs(parsed.query).get("run_id") or [""])[0]
            try:
                self._json(run_status(run_id))
            except AtlasError as e:
                self._json({"error": str(e)}, 502)
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if self.path == "/api/submit":
            try:
                self._json(start_run(self._read_json()))
            except AtlasError as e:
                self._json({"error": str(e)}, 502)
            return
        if self.path == "/api/decide":
            body = self._read_json()
            action = "approve" if body.get("action") == "approve" else "reject"
            try:
                self._json(decide(body.get("run_id", ""), action))
            except AtlasError as e:
                self._json({"error": str(e)}, 502)
            return
        self._json({"error": "not found"}, 404)


def main() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Permit PoC on http://127.0.0.1:{PORT}   (Atlas: {ATLAS}, workflow: {WORKFLOW_NAME!r})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
