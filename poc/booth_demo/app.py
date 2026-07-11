#!/usr/bin/env python3
"""Booth Demo web PoC for Atlas (stdlib only) — two pages, real thClaws workers.

  /news    Booth News Desk    reporter --push files--> anchor -> human gate -> publish
  /permit  Booth Permit Desk  intake --push uploads--> examiner -> human gate -> notice

What it shows on top of poc/permit_web:
  * T9a `collect_files`  — files a worker actually WROTE are snapshotted (thClaws Job
    Artifacts) and become downloadable `file_ref` artifacts on the page.
  * T9b `push_files`     — those files (or citizen uploads) land in the NEXT worker's
    workspace via Bearer `POST /v1/inputs`; the page shows the `files_pushed` event.
  * Uploads              — the permit page uploads real attachment files into the run
    (`POST /api/workflow-runs/{id}/files`) while the intake node is still running.
  * Everything from permit_web: envelope + `_meta.source` provenance, routing, human
    approval gate, artifacts, audit, poll return path. Token stays server-side.

Env:
  ATLAS_BASE     Atlas base URL            (default http://127.0.0.1:8787)
  ATLAS_TOKEN    operator/admin API token  (blank ok on loopback-no-auth Atlas)
  PORT           PoC web port              (default 8090)

Run:  python3 app.py   then open http://127.0.0.1:8090
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse
import urllib.error
import urllib.request

ATLAS = os.environ.get("ATLAS_BASE", "http://127.0.0.1:8787").rstrip("/")
TOKEN = os.environ.get("ATLAS_TOKEN", "")
PORT = int(os.environ.get("PORT", "8090"))
CSRF_TOKEN = secrets.token_urlsafe(32)

MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # per file, PoC-side guard (Atlas has its own cap)
UPLOAD_ACTIVATION_TIMEOUT_SECONDS = 5

SCENARIOS = {
    "news": {
        "workflow_name": "Booth News Desk",
        "title": "News Desk — สายพานข่าว AI",
        "subtitle": "Reporter เขียนไฟล์บทความ → Atlas เก็บไฟล์ (collect) → ส่งไฟล์ให้ Anchor (push) → "
                    "Anchor เขียนสคริปต์ → คนอนุมัติก่อนออกอากาศ",
        "nodes": [
            {"id": "reporter", "label": "ผู้สื่อข่าว", "hint": "เขียน article.md + sources.md"},
            {"id": "anchor", "label": "ผู้ประกาศ", "hint": "อ่านไฟล์ที่ถูกส่งมา → broadcast.md"},
            {"id": "approval", "label": "อนุมัติออกอากาศ", "hint": "human gate"},
            {"id": "publish", "label": "เผยแพร่", "hint": "ประกาศ On Air"},
        ],
        "fields": [
            {"name": "topic", "label": "หัวข้อข่าวที่อยากได้", "type": "text",
             "value": "ข่าว AI หรือเทคโนโลยีที่น่าสนใจล่าสุด"},
        ],
        "submit_label": "เริ่มสายพานข่าว",
        "uploads": False,
    },
    "permit": {
        "workflow_name": "Booth Permit Desk",
        "title": "Permit Desk — ยื่นคำขออนุญาต",
        "subtitle": "ฟอร์ม + ไฟล์แนบจริง → Atlas ส่งไฟล์เข้า workspace ของ agent ผู้ตรวจ (push) → "
                    "บันทึกสรุป → เจ้าหน้าที่อนุมัติ → ได้หนังสือแจ้งผลเป็นไฟล์ (collect)",
        "nodes": [
            {"id": "intake", "label": "รับเรื่อง", "hint": "ตรวจข้อมูลในฟอร์ม"},
            {"id": "examiner", "label": "ตรวจเอกสาร", "hint": "อ่านไฟล์แนบที่ถูกส่งมา → brief.md"},
            {"id": "approval", "label": "เจ้าหน้าที่อนุมัติ", "hint": "human gate"},
            {"id": "notice", "label": "ออกหนังสือแจ้งผล", "hint": "เขียน notice.md"},
        ],
        "fields": [
            {"name": "applicant_name", "label": "ชื่อผู้ขอ", "type": "text", "value": "นายทดสอบ ระบบ"},
            {"name": "permit_type", "label": "ประเภทคำขออนุญาต", "type": "select",
             "options": ["ขออนุญาตก่อสร้าง", "ขออนุญาตประกอบกิจการ", "ขออนุญาตใช้พื้นที่"]},
            {"name": "detail", "label": "รายละเอียด / เหตุผล", "type": "textarea",
             "value": "ขออนุญาตก่อสร้างอาคารพาณิชย์ 2 ชั้น บนที่ดินของตนเอง"},
            {"name": "attachments", "label": "รายการเอกสารแนบ (พิมพ์)", "type": "text",
             "value": "สำเนาบัตรประชาชน, โฉนดที่ดิน, แบบแปลน"},
        ],
        "submit_label": "ยื่นคำขอ",
        "uploads": True,
    },
}

_wf_ids: dict[str, str] = {}


class AtlasError(RuntimeError):
    pass


def atlas(method: str, path: str, body=None, raw: bytes | None = None, headers_extra=None):
    if raw is not None:
        data = raw
        ctype = None
    else:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        ctype = "application/json" if data else None
    headers = {"Accept": "application/json"}
    if ctype:
        headers["Content-Type"] = ctype
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    for k, v in (headers_extra or {}).items():
        headers[k] = v
    req = urllib.request.Request(ATLAS + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            text = r.read().decode("utf-8", errors="replace")
            return json.loads(text or "{}")
    except urllib.error.HTTPError as e:
        raise AtlasError(f"Atlas {method} {path} -> HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
    except urllib.error.URLError as e:
        raise AtlasError(f"Cannot reach Atlas at {ATLAS} ({e.reason})")


def atlas_bytes(path: str) -> tuple[bytes, str]:
    headers = {}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(ATLAS + path, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read(), r.headers.get("Content-Type") or "application/octet-stream"
    except urllib.error.HTTPError as e:
        raise AtlasError(f"Atlas GET {path} -> HTTP {e.code}")
    except urllib.error.URLError as e:
        raise AtlasError(f"Cannot reach Atlas at {ATLAS} ({e.reason})")


def as_list(resp):
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for key in ("workflows", "workers", "runs", "artifacts", "approvals", "events",
                    "items", "results", "data"):
            if isinstance(resp.get(key), list):
                return resp[key]
    return []


def workflow_id(scenario: str) -> str:
    if scenario in _wf_ids:
        return _wf_ids[scenario]
    wanted = SCENARIOS[scenario]["workflow_name"]
    for wf in as_list(atlas("GET", "/api/workflows")):
        if wf.get("name") == wanted:
            _wf_ids[scenario] = wf["id"]
            return wf["id"]
    raise AtlasError(f"Workflow {wanted!r} not found. Run setup.py first.")


def start_run(scenario: str, fields: dict) -> dict:
    cfg = SCENARIOS[scenario]
    envelope = {f["name"]: str(fields.get(f["name"], "")) for f in cfg["fields"]}
    envelope["_meta"] = {
        "source": {
            "channel": "web_form",
            "adapter": "booth-demo-poc",
            "form": scenario,
            "external_id": "booth-" + uuid.uuid4().hex[:12],
        }
    }
    run = atlas("POST", "/api/workflow-runs", {
        "workflow_definition_id": workflow_id(scenario),
        "input": envelope,
    })
    rid = run.get("id") or (run.get("run") or {}).get("id")
    if not rid:
        raise AtlasError(f"No run id in response: {json.dumps(run)[:200]}")
    return {"run_id": rid}


_KEY_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def upload_key(filename: str) -> str:
    base = _KEY_SAFE.sub("_", os.path.basename(filename or "file")).strip("._-") or "file"
    return ("upload_" + base)[:127]


def _payload(evt) -> dict:
    payload = evt.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    return payload if isinstance(payload, dict) else {}


def run_status(run_id: str) -> dict:
    detail = atlas("GET", f"/api/workflow-runs/{run_id}")
    run = detail.get("run") or {}
    state = run.get("state") or "unknown"
    node_states = {n.get("node_key"): n.get("state") for n in detail.get("nodes") or []}

    approval = None
    for ap in detail.get("approvals") or []:
        if ap.get("state") == "pending":
            approval = {"id": ap.get("id"), "label": ap.get("label"), "reason": ap.get("reason")}
            break

    texts, files = [], []
    for a in as_list(atlas("GET", f"/api/workflow-runs/{run_id}/artifacts")):
        if a.get("kind") == "file_ref":
            meta = a.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            files.append({
                "id": a.get("id"),
                "key": a.get("key"),
                "name": meta.get("relpath") or meta.get("filename") or a.get("key"),
                "size": meta.get("size"),
                "sha256": meta.get("sha256"),
                "source": "uploaded" if meta.get("filename") else "collected",
            })
        else:
            content = a.get("content")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, indent=2)
            texts.append({"key": a.get("key"), "kind": a.get("kind"), "content": content})

    pushes = []
    for evt in as_list(atlas("GET", f"/api/workflow-runs/{run_id}/events")):
        if evt.get("event_type") == "files_pushed":
            p = _payload(evt)
            pushes.append({"to_node": p.get("to_node"), "count": p.get("count"),
                           "bytes": p.get("bytes"), "files_dir": p.get("files_dir")})
        elif evt.get("event_type") == "files_push_empty":
            pushes.append({"to_node": _payload(evt).get("to"), "count": 0, "bytes": 0, "files_dir": ""})

    return {"state": state, "error": run.get("error"), "node_states": node_states,
            "approval": approval, "texts": texts, "files": files, "pushes": pushes}


def decide(run_id: str, action: str) -> dict:
    detail = atlas("GET", f"/api/workflow-runs/{run_id}")
    target = None
    for ap in detail.get("approvals") or []:
        if ap.get("state") == "pending":
            target = ap.get("id")
            break
    if not target:
        raise AtlasError("No pending approval for this run")
    verb = "approve" if action == "approve" else "reject"
    atlas("POST", f"/api/approvals/{target}/{verb}")
    return {"ok": True, "action": verb}


def activate_uploads(run_id: str) -> dict:
    """Release the permit run only after every browser upload succeeds."""
    deadline = time.monotonic() + UPLOAD_ACTIVATION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        detail = atlas("GET", f"/api/workflow-runs/{run_id}")
        for approval in detail.get("approvals") or []:
            if approval.get("state") == "pending" and approval.get("node_key") == "upload_ready":
                atlas("POST", f"/api/approvals/{approval['id']}/approve")
                return {"ok": True}
        run = detail.get("run") or {}
        if run.get("state") in {"failed", "cancelled", "succeeded"}:
            raise AtlasError("run finished before uploads could be activated")
        time.sleep(0.05)
    raise AtlasError("upload gate did not become ready")


def cancel_run(run_id: str) -> dict:
    if not run_id:
        raise AtlasError("run_id is required")
    atlas("POST", f"/api/workflow-runs/{quote(run_id, safe='')}/cancel")
    return {"ok": True}


def overview() -> dict:
    workers = []
    for w in as_list(atlas("GET", "/api/workers")):
        workers.append({"name": w.get("name"), "role": w.get("role"),
                        "status": w.get("status"), "base_url": w.get("base_url")})
    names = {wf.get("name") for wf in as_list(atlas("GET", "/api/workflows"))}
    return {
        "atlas": ATLAS,
        "workers": workers,
        "workflows": {s: (SCENARIOS[s]["workflow_name"] in names) for s in SCENARIOS},
    }


# ------------------------------------------------------------------------------ pages

STYLE = """
  :root { color-scheme: light dark; }
  body { font-family: system-ui, -apple-system, "Noto Sans Thai", sans-serif; max-width: 860px;
         margin: 24px auto; padding: 0 16px; line-height: 1.55; }
  h1 { font-size: 21px; margin: 0 0 4px; }
  .sub { color: #777; font-size: 13px; margin: 0 0 18px; }
  a { color: inherit; }
  label { display:block; font-size: 13px; font-weight: 600; margin: 12px 0 4px; }
  input, textarea, select { width: 100%; padding: 8px; border: 1px solid #bbb; border-radius: 8px;
         font: inherit; box-sizing: border-box; background: transparent; color: inherit; }
  textarea { min-height: 60px; }
  button { font: inherit; padding: 9px 16px; border-radius: 8px; border: 1px solid #888;
           background: #2b6; color: #fff; cursor: pointer; margin-top: 14px; }
  button.reject { background: #c33; border-color:#c33; }
  button:disabled { opacity: .5; cursor: default; }
  .pipeline { display:flex; flex-wrap:wrap; align-items:center; gap:6px; margin: 18px 0 6px; }
  .chip { border: 1px solid #999; border-radius: 999px; padding: 4px 12px; font-size: 13px; }
  .chip small { display:block; font-size: 10px; color:#888; }
  .chip.running { border-color:#08c; color:#08c; animation: pulse 1.2s infinite; }
  .chip.succeeded { border-color:#2b6; color:#2b6; }
  .chip.failed { border-color:#c33; color:#c33; }
  .chip.waiting_for_human { border-color:#e0a800; color:#e0a800; }
  .arrow { color:#888; font-size: 15px; }
  .arrow.pushed { color:#08c; font-weight: 700; }
  @keyframes pulse { 50% { opacity:.45; } }
  .step { border: 1px solid #ccc; border-radius: 10px; padding: 12px; margin: 10px 0; }
  .step h3 { margin: 0 0 6px; font-size: 14px; }
  pre { white-space: pre-wrap; margin: 0; font: inherit; }
  .state { display:inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px;
           border: 1px solid #888; }
  .muted { color:#777; font-size:12px; }
  .gate { border-color:#e0a800; background: rgba(224,168,0,.08); }
  .filecard { display:flex; justify-content:space-between; gap:10px; align-items:center;
              border:1px dashed #999; border-radius:8px; padding:8px 12px; margin:6px 0; font-size:13px; }
  .push-banner { border:1px solid #08c; color:#08c; border-radius:8px; padding:8px 12px;
                 margin:10px 0; font-size:13px; }
  .cards { display:flex; gap:14px; flex-wrap:wrap; margin-top:18px; }
  .card { flex:1 1 300px; border:1px solid #ccc; border-radius:12px; padding:16px; text-decoration:none; }
  .card h2 { margin:0 0 6px; font-size:16px; }
  .ok { color:#2b6; } .bad { color:#c33; }
"""

INDEX_PAGE = """<!doctype html>
<html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Atlas Booth Demo</title><style>%%STYLE%%</style></head>
<body>
  <h1>Atlas Booth Demo — จัดทีม AI ให้ทำงานเป็นสายพาน</h1>
  <p class="sub">หลาย agent · ส่งไฟล์งานต่อกันได้จริง · มีคนคุมจุดอนุมัติ · budget-capped · ตรวจสอบย้อนหลังได้ทุกขั้น</p>
  <div id="status" class="muted">กำลังตรวจระบบ…</div>
  <div class="cards">
    <a class="card" href="/news"><h2>📺 News Desk</h2>
      <p class="muted">โชว์หลักสำหรับบูธ: reporter เขียนไฟล์บทความ → Atlas ส่งไฟล์ให้ anchor →
      สคริปต์ข่าว → คนกดอนุมัติก่อนออกอากาศ</p></a>
    <a class="card" href="/permit"><h2>📄 Permit Desk</h2>
      <p class="muted">โทนงานเอกสาร: แนบไฟล์จริงจากเครื่องคุณ → agent ผู้ตรวจได้รับไฟล์ →
      เจ้าหน้าที่อนุมัติ → ได้หนังสือแจ้งผลเป็นไฟล์ดาวน์โหลด</p></a>
  </div>
  <p class="muted" style="margin-top:20px">Dashboard เต็มของ Atlas (จ๊อบสตรีมสด / fleet / usage):
    <a href="%%ATLAS%%" target="_blank">%%ATLAS%%</a></p>
<script>
fetch("/api/overview").then(r=>r.json()).then(j=>{
  const esc = s => String(s==null?"":s).replace(/[&<>"']/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  let h = "workers: ";
  h += (j.workers||[]).map(w=>`<span class="${w.status==="online"?"ok":"bad"}">●</span> ${esc(w.name)} (${esc(w.role)}: ${esc(w.status)})`).join(" · ") || "<span class='bad'>ไม่พบ worker</span>";
  h += "<br>workflows: " + Object.entries(j.workflows||{}).map(([k,v])=>`${esc(k)} ${v?'<span class="ok">พร้อม</span>':'<span class="bad">ยังไม่ setup</span>'}`).join(" · ");
  document.getElementById("status").innerHTML = h;
}).catch(()=>{ document.getElementById("status").innerHTML = '<span class="bad">ต่อ Atlas ไม่ได้ — เปิด Atlas แล้วรีเฟรช</span>'; });
</script>
</body></html>
"""

SCENARIO_PAGE = """<!doctype html>
<html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>%%TITLE%%</title><style>%%STYLE%%</style></head>
<body>
  <h1>%%TITLE%%</h1>
  <p class="sub">%%SUBTITLE%% · <a href="/">← หน้าหลัก</a></p>
  <form id="f"></form>
  <div id="pipeline"></div>
  <div id="panel"></div>
<script>
const CFG = %%CONFIG%%;
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const csrfHeaders = {"X-CSRF-Token": CFG.csrf_token};
let timer = null, runId = null, uploadNote = "";

function buildForm() {
  let h = "";
  for (const f of CFG.fields) {
    h += `<label>${esc(f.label)}</label>`;
    if (f.type === "textarea") h += `<textarea name="${esc(f.name)}">${esc(f.value||"")}</textarea>`;
    else if (f.type === "select") h += `<select name="${esc(f.name)}">` + (f.options||[]).map(o=>`<option>${esc(o)}</option>`).join("") + `</select>`;
    else h += `<input name="${esc(f.name)}" value="${esc(f.value||"")}">`;
  }
  if (CFG.uploads) {
    h += `<label>ไฟล์เอกสารแนบ (เลือกได้หลายไฟล์ · ไฟล์ text/markdown/pdf ขนาดเล็ก)</label>`;
    h += `<input type="file" id="files" multiple>`;
  }
  h += `<button type="submit" id="go">${esc(CFG.submit_label)}</button>`;
  $("#f").innerHTML = h;
}
buildForm();

$("#f").addEventListener("submit", async (e) => {
  e.preventDefault();
  runId = null;
  $("#go").disabled = true;
  const fd = {};
  for (const f of CFG.fields) fd[f.name] = ($("#f").elements[f.name] || {}).value || "";
  $("#panel").innerHTML = '<p class="muted">กำลังส่งเข้า Atlas…</p>';
  try {
    const r = await fetch("/api/submit", {method:"POST", headers:{"content-type":"application/json", ...csrfHeaders},
      body: JSON.stringify({scenario: CFG.scenario, fields: fd})});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "submit failed");
    runId = j.run_id;
    if (CFG.uploads) {
      await uploadFiles();
      await activateUploads();
    }
    poll();
    timer = setInterval(poll, 2000);
  } catch (err) {
    const failedRunId = runId;
    $("#panel").innerHTML = '<p style="color:#c33">ผิดพลาด: '+esc(err.message)+'</p>';
    $("#go").disabled = false;
    if (CFG.uploads && failedRunId) await cancelFailedRun(failedRunId);
  }
});

async function uploadFiles() {
  const input = $("#files");
  if (!input || !input.files.length) { uploadNote = "ไม่ได้แนบไฟล์"; return; }
  let done = 0;
  for (const file of input.files) {
    const r = await fetch("/api/upload?run_id="+encodeURIComponent(runId)+"&name="+encodeURIComponent(file.name),
      {method:"POST", headers: csrfHeaders, body: file});
    if (!r.ok) { const j = await r.json().catch(()=>({})); throw new Error(j.error || ("upload failed: "+file.name)); }
    done++;
  }
  uploadNote = "อัปโหลดแล้ว "+done+" ไฟล์ — จะถูกส่งเข้า workspace ของ agent ผู้ตรวจโดยอัตโนมัติ";
}

async function activateUploads() {
  const r = await fetch("/api/activate", {method:"POST", headers:{"content-type":"application/json", ...csrfHeaders},
    body: JSON.stringify({run_id: runId})});
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || "could not activate uploaded files");
}

async function cancelFailedRun(failedRunId) {
  try {
    await fetch("/api/cancel", {method:"POST", headers:{"content-type":"application/json", ...csrfHeaders},
      body: JSON.stringify({run_id: failedRunId})});
  } catch (_err) {}
}

async function poll() {
  if (!runId) return;
  let j;
  try {
    const r = await fetch("/api/status?run_id="+encodeURIComponent(runId));
    j = await r.json();
    if (!r.ok) throw new Error(j.error || "status error");
  } catch (err) { $("#panel").innerHTML = '<p style="color:#c33">'+esc(err.message)+'</p>'; return; }
  render(j);
  if (["succeeded","failed","cancelled"].includes(j.state)) { clearInterval(timer); $("#go").disabled = false; }
}

function pipelineHTML(j) {
  const pushedTo = new Set((j.pushes||[]).filter(p=>p.count>0).map(p=>p.to_node));
  let h = '<div class="pipeline">';
  CFG.nodes.forEach((n, i) => {
    if (i > 0) h += `<span class="arrow ${pushedTo.has(n.id)?"pushed":""}" title="${pushedTo.has(n.id)?"files pushed":""}">${pushedTo.has(n.id)?"⇒📁":"→"}</span>`;
    const st = (j.node_states||{})[n.id] || "";
    h += `<span class="chip ${esc(st)}">${esc(n.label)}<small>${esc(st || n.hint)}</small></span>`;
  });
  return h + '</div>';
}

function render(j) {
  $("#pipeline").innerHTML = pipelineHTML(j);
  let h = '<p>สถานะ: <span class="state">'+esc(j.state)+'</span> <span class="muted">('+esc(runId)+')</span>'
        + (uploadNote ? ' <span class="muted">· '+esc(uploadNote)+'</span>' : '') + '</p>';
  for (const p of (j.pushes||[])) {
    if (p.count > 0)
      h += '<div class="push-banner">📁 Atlas ส่งไฟล์ '+p.count+' ไฟล์ ('+p.bytes+' bytes) เข้า workspace ของโหนด <b>'
         + esc(p.to_node)+'</b> ผ่าน POST /v1/inputs → '+esc(p.files_dir)+'</div>';
    else
      h += '<div class="push-banner" style="border-color:#999;color:#999">📭 edge ส่งไฟล์ไปยัง <b>'+esc(p.to_node)+'</b> ทำงานแล้ว แต่ไม่มีไฟล์ตรงเงื่อนไข</div>';
  }
  if (j.approval) {
    h += '<div class="step gate"><h3>⏸ '+esc(j.approval.label||"รออนุมัติ")+'</h3>'
       + '<p class="muted">'+esc(j.approval.reason||"")+'</p>'
       + '<button onclick="decide(\\'approve\\')">อนุมัติ</button> '
       + '<button class="reject" onclick="decide(\\'reject\\')">ปฏิเสธ</button></div>';
  }
  if ((j.files||[]).length) {
    h += '<div class="step"><h3>ไฟล์ในสายพาน (file_ref artifacts)</h3>';
    for (const f of j.files) {
      h += '<div class="filecard"><span>'+(f.source==="uploaded"?"⬆️":"📄")+' <b>'+esc(f.name)+'</b>'
         + ' <span class="muted">'+esc(f.key)+' · '+(f.size==null?"?":f.size)+' bytes · sha256 '
         + esc((f.sha256||"").slice(0,12))+'…</span></span>'
         + '<a href="/api/file?id='+encodeURIComponent(f.id)+'&name='+encodeURIComponent(f.name)+'" download="'+esc(f.name)+'">ดาวน์โหลด</a></div>';
    }
    h += '</div>';
  }
  for (const a of (j.texts||[])) {
    h += '<div class="step"><h3>'+esc(a.key)+' <span class="muted">('+esc(a.kind)+')</span></h3><pre>'+esc(a.content)+'</pre></div>';
  }
  if (j.state === "succeeded") h += '<p class="ok" style="color:#2b6">✔ เสร็จสมบูรณ์</p>';
  if (j.state === "failed") h += '<p style="color:#c33">✘ run ล้มเหลว'+(j.error?': '+esc(j.error):'')+'</p>';
  $("#panel").innerHTML = h;
}

async function decide(action) {
  const r = await fetch("/api/decide", {method:"POST", headers:{"content-type":"application/json", ...csrfHeaders},
    body: JSON.stringify({run_id: runId, action})});
  const j = await r.json();
  if (!r.ok) { alert(j.error || "decide failed"); return; }
  poll();
}
</script>
</body></html>
"""


def render_index() -> str:
    return INDEX_PAGE.replace("%%STYLE%%", STYLE).replace("%%ATLAS%%", ATLAS)


def render_scenario(scenario: str) -> str:
    cfg = SCENARIOS[scenario]
    config = dict(cfg)
    config["scenario"] = scenario
    config["csrf_token"] = CSRF_TOKEN
    config.pop("workflow_name", None)
    page = SCENARIO_PAGE.replace("%%STYLE%%", STYLE)
    page = page.replace("%%TITLE%%", cfg["title"]).replace("%%SUBTITLE%%", cfg["subtitle"])
    # </ must not appear inside the inline <script> block
    return page.replace("%%CONFIG%%", json.dumps(config, ensure_ascii=False).replace("</", "<\\/"))


# ------------------------------------------------------------------------------ server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        return

    def _send(self, status: int, body: bytes, ctype: str, extra=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_body(self, cap: int = MAX_UPLOAD_BYTES) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length < 0 or length > cap:
            raise AtlasError(f"body exceeds {cap} bytes")
        return self.rfile.read(length) if length else b""

    def _read_json(self) -> dict:
        try:
            return json.loads(self._read_body(1 << 20) or b"{}")
        except AtlasError:
            raise
        except Exception:
            return {}

    def _csrf_valid(self) -> bool:
        host = self.headers.get("Host", "").lower()
        origin = self.headers.get("Origin", "").rstrip("/")
        allowed_hosts = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        return (
            host in allowed_hosts
            and origin == f"http://{host}"
            and hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), CSRF_TOKEN)
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                self._send(200, render_index().encode("utf-8"), "text/html; charset=utf-8")
            elif parsed.path in ("/news", "/permit"):
                self._send(200, render_scenario(parsed.path[1:]).encode("utf-8"), "text/html; charset=utf-8")
            elif parsed.path == "/api/overview":
                self._json(overview())
            elif parsed.path == "/api/status":
                self._json(run_status((q.get("run_id") or [""])[0]))
            elif parsed.path == "/api/file":
                artifact_id = (q.get("id") or [""])[0]
                name = os.path.basename((q.get("name") or ["file"])[0]) or "file"
                data, ctype = atlas_bytes(f"/api/artifacts/{artifact_id}/content")
                self._send(200, data, ctype, {
                    "Content-Disposition": "attachment; filename*=UTF-8''" + quote(name)})
            else:
                self._json({"error": "not found"}, 404)
        except AtlasError as e:
            self._json({"error": str(e)}, 502)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        if not self._csrf_valid():
            self._json({"error": "same-origin CSRF token required"}, 403)
            return
        try:
            if parsed.path == "/api/submit":
                body = self._read_json()
                scenario = body.get("scenario")
                if scenario not in SCENARIOS:
                    self._json({"error": "unknown scenario"}, 400)
                    return
                self._json(start_run(scenario, body.get("fields") or {}))
            elif parsed.path == "/api/upload":
                run_id = (q.get("run_id") or [""])[0]
                name = (q.get("name") or ["file"])[0]
                data = self._read_body()
                if not run_id or not data:
                    self._json({"error": "run_id and a non-empty file body are required"}, 400)
                    return
                artifact = atlas(
                    "POST",
                    f"/api/workflow-runs/{run_id}/files?key={quote(upload_key(name))}",
                    raw=data,
                    headers_extra={
                        "X-Filename": upload_key(name),
                        "Content-Type": self.headers.get("Content-Type") or "application/octet-stream",
                    },
                )
                self._json({"ok": True, "artifact_key": (artifact.get("artifact") or {}).get("key")})
            elif parsed.path == "/api/activate":
                body = self._read_json()
                self._json(activate_uploads(body.get("run_id", "")))
            elif parsed.path == "/api/cancel":
                body = self._read_json()
                self._json(cancel_run(body.get("run_id", "")))
            elif parsed.path == "/api/decide":
                body = self._read_json()
                action = "approve" if body.get("action") == "approve" else "reject"
                self._json(decide(body.get("run_id", ""), action))
            else:
                self._json({"error": "not found"}, 404)
        except AtlasError as e:
            self._json({"error": str(e)}, 502)


def main() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Booth Demo on http://127.0.0.1:{PORT}   (Atlas: {ATLAS})")
    print("  /news    News Desk   — collect + push + human gate")
    print("  /permit  Permit Desk — uploads + push + human gate + collected notice")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
