#!/usr/bin/env python3
"""One-shot setup for the Booth Demo PoC against a RUNNING Atlas + two REAL thClaws workers.

Idempotent: registers/repoints two workers by their stable Atlas IDs (looked up by name), then creates or
updates two workflows by name:

  Booth News Desk    reporter --push files--> anchor -> approval (human gate) -> publish
  Booth Permit Desk  intake --push uploads--> examiner -> approval (human gate) -> notice

Both exercise the full T9a/T9b file path: `collect_files` snapshots what a worker WROTE
(thClaws Job Artifacts, v0.88+), an edge `push_files` places those files into the NEXT
worker's workspace via Bearer `POST /v1/inputs`, and the downstream prompt reads them
from `{files_dir}`. `policy.file_handoff` is the explicit opt-in.

NOTE (engine contract): `push_files` must sit on a worker->worker edge. Edges leaving a
human gate are taken by the approval decision path, which does not carry push intents —
so in both graphs the gate comes AFTER the push.

Env:
  ATLAS_BASE       Atlas base URL            (default http://127.0.0.1:8787)
  ATLAS_TOKEN      operator/admin API token  (blank ok with ATLAS_LOOPBACK_NO_AUTH=true)
  REPORTER_URL     thClaws worker #1         (default http://127.0.0.1:4317)
  REPORTER_TOKEN   its THCLAWS_API_TOKEN     (default dev-token-1)
  ANCHOR_URL       thClaws worker #2         (default http://127.0.0.1:4318)
  ANCHOR_TOKEN     its THCLAWS_API_TOKEN     (default dev-token-2)

Run:  python3 setup.py
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

ATLAS = os.environ.get("ATLAS_BASE", "http://127.0.0.1:8787").rstrip("/")
TOKEN = os.environ.get("ATLAS_TOKEN", "")

REPORTER_URL = os.environ.get("REPORTER_URL", "http://127.0.0.1:4317").rstrip("/")
REPORTER_TOKEN = os.environ.get("REPORTER_TOKEN", "dev-token-1")
ANCHOR_URL = os.environ.get("ANCHOR_URL", "http://127.0.0.1:4318").rstrip("/")
ANCHOR_TOKEN = os.environ.get("ANCHOR_TOKEN", "dev-token-2")

NEWS_WORKFLOW = "Booth News Desk"
PERMIT_WORKFLOW = "Booth Permit Desk"


def api(method: str, path: str, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(ATLAS + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode("utf-8", errors="replace")
            return json.loads(text or "{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Atlas {method} {path} -> HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Cannot reach Atlas at {ATLAS} ({e.reason}). Start Atlas first.")


def as_list(resp):
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for key in ("workers", "workflows", "items", "results", "data"):
            if isinstance(resp.get(key), list):
                return resp[key]
    return []


def rec_id(resp):
    if isinstance(resp, dict):
        for key in ("id", "workflow_definition_id", "worker_id"):
            if resp.get(key):
                return resp[key]
        for key in ("definition", "workflow", "worker"):
            inner = resp.get(key)
            if isinstance(inner, dict) and inner.get("id"):
                return inner["id"]
    raise SystemExit(f"Could not find an id in response: {json.dumps(resp)[:300]}")


def ensure_worker(name: str, url: str, token: str, role: str, tags: str) -> str:
    payload = {
        "name": name,
        "base_url": url,
        "token": token,
        "role": role,
        "tags": tags,
    }
    for existing in as_list(api("GET", "/api/workers")):
        if existing.get("name") == name and existing.get("id"):
            payload["id"] = existing["id"]
            break
    worker = api("POST", "/api/workers", payload)
    wid = rec_id(worker)
    poll = api("POST", f"/api/workers/{wid}/poll")
    status = "unknown"
    if isinstance(poll, dict):
        status = poll.get("status") or (poll.get("worker") or {}).get("status") or worker.get("status") or "unknown"
    print(f"worker {name} ({wid}) -> {url}  [status: {status}]")
    if status not in ("online", "healthy"):
        print(f"  ! not online — start thClaws at {url} with THCLAWS_API_TOKEN matching, "
              f"a model key (BYOK), and its OWN working directory; then re-run setup.py")
    return wid


# --------------------------------------------------------------------------- news desk

def news_graph(reporter_id: str, anchor_id: str) -> dict:
    return {
        "start": "reporter",
        "nodes": [
            {
                "id": "reporter",
                "type": "worker",
                "worker_id": reporter_id,
                "role": "reporter",
                "prompt": (
                    "คุณเป็นผู้สื่อข่าว หาข่าวเทคโนโลยีที่น่าสนใจ 1 เรื่องเกี่ยวกับ: {input.topic}\n"
                    "(ถ้าค้นเว็บไม่ได้ ให้ใช้ความรู้ล่าสุดที่มี ระบุด้วยว่าข้อมูล ณ ช่วงเวลาใด)\n\n"
                    "ให้ทำ 2 อย่าง:\n"
                    "1. เขียนบทความข่าวสั้น (หัวข้อ + ข้อเท็จจริง 4-6 ข้อ) บันทึกลงไฟล์ชื่อ article.md "
                    "และบันทึกรายการแหล่งอ้างอิง/ที่มา ลงไฟล์ชื่อ sources.md "
                    "ทั้งสองไฟล์อยู่ในไดเรกทอรีทำงานปัจจุบัน (current directory)\n"
                    "2. ตอบกลับด้วยสรุปสั้น ๆ 2-3 ประโยคว่าได้ข่าวเรื่องอะไร"
                ),
                "outputs": ["notes"],
                "collect_files": ["article.md", "sources.md"],
            },
            {
                "id": "anchor",
                "type": "worker",
                "worker_id": anchor_id,
                "role": "anchor",
                "prompt": (
                    "คุณเป็นผู้ประกาศข่าว ไฟล์บทความจากผู้สื่อข่าวถูกส่งมาไว้ที่โฟลเดอร์ {files_dir} "
                    "(path เทียบกับไดเรกทอรีทำงานของคุณ)\n\n"
                    "ให้ทำ 2 อย่าง:\n"
                    "1. อ่านไฟล์ทั้งหมดในโฟลเดอร์นั้น แล้วเขียนสคริปต์อ่านข่าวออกอากาศภาษาไทย "
                    "ความยาวประมาณ 30 วินาที ห้ามเพิ่มข้อเท็จจริงที่ไม่มีในบทความ "
                    "บันทึกลงไฟล์ชื่อ broadcast.md ในไดเรกทอรีทำงานปัจจุบัน\n"
                    "2. ตอบกลับด้วยสคริปต์ฉบับเต็ม"
                ),
                "outputs": ["script"],
                "collect_files": ["broadcast.md"],
            },
            {
                "id": "approval",
                "type": "human_gate",
                "label": "อนุมัติการออกอากาศ",
                "reason": "ตรวจสคริปต์ข่าวก่อนเผยแพร่ — อนุมัติเพื่อออกอากาศ หรือปฏิเสธเพื่อยกเลิก",
            },
            {
                "id": "publish",
                "type": "worker",
                "worker_id": reporter_id,
                "role": "reporter",
                "prompt": (
                    "สคริปต์ข่าวนี้ได้รับอนุมัติให้เผยแพร่แล้ว:\n{artifact.script}\n\n"
                    "เขียนประกาศเผยแพร่สั้น ๆ 2-3 ประโยค (หัวข้อข่าว + ช่องทาง On Air) ตอบกลับมาเท่านั้น"
                ),
                "outputs": ["bulletin"],
            },
        ],
        "edges": [
            # T9b: files the reporter WROTE (collected as files.reporter.*) land in the
            # anchor's workspace before its job starts. Push edges must be worker->worker.
            {"from": "reporter", "to": "anchor", "condition": {"type": "always"},
             "push_files": ["files.reporter.*"]},
            {"from": "anchor", "to": "approval", "condition": {"type": "always"}},
            {"from": "approval", "to": "publish", "condition": {"type": "always"}},
        ],
    }


# ------------------------------------------------------------------------- permit desk

def permit_graph(reporter_id: str, anchor_id: str) -> dict:
    return {
        "start": "upload_ready",
        "nodes": [
            {
                "id": "upload_ready",
                "type": "human_gate",
                "label": "กำลังอัปโหลดเอกสารแนบ",
                "reason": "เว็บฟอร์มจะเริ่มตรวจคำขอหลังอัปโหลดเอกสารแนบทั้งหมดสำเร็จ",
            },
            {
                "id": "intake",
                "type": "worker",
                "worker_id": reporter_id,
                "role": "permit",
                "prompt": (
                    "ตรวจความครบถ้วนของคำขออนุญาตต่อไปนี้ และระบุสิ่งที่ขาด (ตอบกลับเป็นข้อความ):\n"
                    "ผู้ขอ: {input.applicant_name}\n"
                    "ประเภทคำขอ: {input.permit_type}\n"
                    "รายละเอียด: {input.detail}\n"
                    "รายการเอกสารแนบที่ผู้ขอแจ้ง: {input.attachments}"
                ),
                "outputs": ["review"],
            },
            {
                "id": "examiner",
                "type": "worker",
                "worker_id": anchor_id,
                "role": "permit",
                "prompt": (
                    "ไฟล์เอกสารแนบจริงของผู้ขอถูกวางไว้ที่โฟลเดอร์ {files_dir} "
                    "(path เทียบกับไดเรกทอรีทำงานของคุณ; ถ้าโฟลเดอร์ว่างหรือไม่มี แปลว่าผู้ขอไม่ได้แนบไฟล์)\n\n"
                    "ให้ทำ 2 อย่าง:\n"
                    "1. อ่านไฟล์ในโฟลเดอร์นั้น เทียบกับผลตรวจเบื้องต้นนี้:\n{artifact.review}\n"
                    "   แล้วเขียนบันทึกสรุปเสนอผู้พิจารณา (ความครบถ้วนของเอกสารจริง + ข้อเสนอแนะ อนุมัติ/ไม่อนุมัติ) "
                    "บันทึกลงไฟล์ชื่อ brief.md ในไดเรกทอรีทำงานปัจจุบัน\n"
                    "2. ตอบกลับด้วยบันทึกสรุปฉบับเต็ม"
                ),
                "outputs": ["brief"],
                "collect_files": ["brief.md"],
            },
            {
                "id": "approval",
                "type": "human_gate",
                "label": "อนุมัติคำขออนุญาต",
                "reason": "ตรวจบันทึกสรุปของเจ้าหน้าที่ก่อนตัดสินใจอนุมัติหรือปฏิเสธ",
            },
            {
                "id": "notice",
                "type": "worker",
                "worker_id": anchor_id,
                "role": "permit",
                "prompt": (
                    "คำขอได้รับอนุมัติแล้ว ร่างหนังสือแจ้งผลการอนุมัติอย่างเป็นทางการตามบันทึกสรุปนี้:\n"
                    "{artifact.brief}\n\n"
                    "ให้ทำ 2 อย่าง:\n"
                    "1. บันทึกหนังสือแจ้งผลลงไฟล์ชื่อ notice.md ในไดเรกทอรีทำงานปัจจุบัน\n"
                    "2. ตอบกลับด้วยเนื้อหาหนังสือแจ้งผลฉบับเต็ม"
                ),
                "outputs": ["notice"],
                "collect_files": ["notice.md"],
            },
        ],
        "edges": [
            {"from": "upload_ready", "to": "intake", "condition": {"type": "always"}},
            # T9b: files the CITIZEN uploaded through the web page (artifacts keyed upload_*)
            # are pushed into the examiner's workspace. The initial human gate holds the run
            # until every upload completes, so intake->examiner cannot resolve an empty set.
            {"from": "intake", "to": "examiner", "condition": {"type": "always"},
             "push_files": ["upload_*"]},
            {"from": "examiner", "to": "approval", "condition": {"type": "always"}},
            {"from": "approval", "to": "notice", "condition": {"type": "always"}},
        ],
    }


def ensure_workflow(name: str, graph: dict, policy: dict) -> str:
    for wf in as_list(api("GET", "/api/workflows")):
        if wf.get("name") == name:
            wid = wf["id"]
            api("PUT", f"/api/workflows/{wid}", {"name": name, "graph": graph, "policy": policy})
            print(f"updated workflow {name!r} ({wid})")
            return wid
    created = api("POST", "/api/workflows", {"name": name, "graph": graph, "policy": policy})
    wid = rec_id(created)
    print(f"created workflow {name!r} ({wid})")
    return wid


def main() -> None:
    reporter_id = ensure_worker("booth-reporter", REPORTER_URL, REPORTER_TOKEN, "reporter", "booth,permit")
    anchor_id = ensure_worker("booth-anchor", ANCHOR_URL, ANCHOR_TOKEN, "anchor", "booth,permit")

    policy = {
        "max_jobs": 10,
        "max_iterations": 10,
        "max_attempts_per_node": 1,
        "max_minutes": 30,
        "max_budget_units": 20,
        "file_handoff": True,  # explicit opt-in for edge push_files (T9b)
    }
    news_id = ensure_workflow(NEWS_WORKFLOW, news_graph(reporter_id, anchor_id), policy)
    permit_id = ensure_workflow(PERMIT_WORKFLOW, permit_graph(reporter_id, anchor_id), policy)

    print("\nSetup complete.")
    print(f"  reporter worker = {reporter_id}")
    print(f"  anchor worker   = {anchor_id}")
    print(f"  news workflow   = {news_id}")
    print(f"  permit workflow = {permit_id}")
    print("\nNext: run  python3 app.py  and open http://127.0.0.1:8090")


if __name__ == "__main__":
    main()
