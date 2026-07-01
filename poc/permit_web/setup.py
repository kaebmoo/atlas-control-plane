#!/usr/bin/env python3
"""One-shot setup for the Permit PoC against a RUNNING Atlas.

Idempotent by name: registers the mock worker, then creates (or updates) the
"PoC Permit Application" workflow whose worker nodes point at that worker.

  intake (worker)  -> brief (worker) -> approval (human_gate) -> notice (worker)

Env:
  ATLAS_BASE          Atlas base URL           (default http://127.0.0.1:8787)
  ATLAS_TOKEN         operator/admin API token (blank ok if Atlas runs with
                      ATLAS_LOOPBACK_NO_AUTH=true on loopback)
  MOCK_WORKER_URL     where the mock worker listens (default http://127.0.0.1:4399)
  MOCK_WORKER_TOKEN   token Atlas sends to the worker (default mock-token)

Run:  python3 setup.py
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

ATLAS = os.environ.get("ATLAS_BASE", "http://127.0.0.1:8787").rstrip("/")
TOKEN = os.environ.get("ATLAS_TOKEN", "")
WORKER_URL = os.environ.get("MOCK_WORKER_URL", "http://127.0.0.1:4399").rstrip("/")
WORKER_TOKEN = os.environ.get("MOCK_WORKER_TOKEN", "mock-token")

WORKER_NAME = "permit-mock"
WORKFLOW_NAME = "PoC Permit Application"


def api(method: str, path: str, body=None):
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


def ensure_worker() -> str:
    # Atlas upserts a worker BY base_url, so re-POSTing repoints/creates the worker at
    # WORKER_URL. Switching MOCK_WORKER_URL (mock <-> real thClaws) and re-running setup
    # therefore updates the target, and the workflow below is rebuilt to point at it.
    worker = api("POST", "/api/workers", {
        "name": WORKER_NAME,
        "base_url": WORKER_URL,
        "token": WORKER_TOKEN,
        "role": "permit",
        "tags": "poc,permit",
    })
    wid = rec_id(worker)
    poll = api("POST", f"/api/workers/{wid}/poll")
    status = "unknown"
    if isinstance(poll, dict):
        status = poll.get("status") or (poll.get("worker") or {}).get("status") or worker.get("status") or "unknown"
    print(f"worker {WORKER_NAME} ({wid}) -> {WORKER_URL}  [status: {status}]")
    if status not in ("online", "healthy"):
        print(f"  ! not online yet — ensure the worker is running at {WORKER_URL} with the "
              f"right token (and, for real thClaws, a model key), then re-run setup.py")
    return wid


def build_graph(worker_id: str) -> dict:
    return {
        "start": "intake",
        "nodes": [
            {
                "id": "intake",
                "type": "worker",
                "worker_id": worker_id,
                "prompt": (
                    "STEP=intake\n"
                    "ตรวจความครบถ้วนของคำขออนุญาตต่อไปนี้ และระบุสิ่งที่ขาด:\n"
                    "ผู้ขอ: {input.applicant_name}\n"
                    "ประเภทคำขอ: {input.permit_type}\n"
                    "รายละเอียด: {input.detail}\n"
                    "เอกสารแนบ: {input.attachments}"
                ),
                "outputs": ["review"],
            },
            {
                "id": "brief",
                "type": "worker",
                "worker_id": worker_id,
                "prompt": (
                    "STEP=summary\n"
                    "จากผลตรวจต่อไปนี้ ให้เขียนบันทึกสรุปเสนอผู้พิจารณาพร้อมข้อเสนอแนะ:\n"
                    "{artifact.review}"
                ),
                "outputs": ["brief"],
            },
            {
                "id": "approval",
                "type": "human_gate",
                "label": "อนุมัติคำขออนุญาต",
                "reason": "ตรวจบันทึกสรุปก่อนตัดสินใจอนุมัติหรือปฏิเสธ",
            },
            {
                "id": "notice",
                "type": "worker",
                "worker_id": worker_id,
                "prompt": (
                    "STEP=notice\n"
                    "ร่างหนังสือแจ้งผลการอนุมัติตามบันทึกสรุปนี้:\n"
                    "{artifact.brief}"
                ),
                "outputs": ["notice"],
            },
        ],
        "edges": [
            {"from": "intake", "to": "brief", "condition": {"type": "always"}},
            {"from": "brief", "to": "approval", "condition": {"type": "always"}},
            {"from": "approval", "to": "notice", "condition": {"type": "always"}},
        ],
    }


def ensure_workflow(worker_id: str) -> str:
    graph = build_graph(worker_id)
    policy = {"max_jobs": 10}
    for wf in as_list(api("GET", "/api/workflows")):
        if wf.get("name") == WORKFLOW_NAME:
            wid = wf["id"]
            api("PUT", f"/api/workflows/{wid}", {"name": WORKFLOW_NAME, "graph": graph, "policy": policy})
            print(f"updated workflow {WORKFLOW_NAME!r} ({wid})")
            return wid
    created = api("POST", "/api/workflows", {"name": WORKFLOW_NAME, "graph": graph, "policy": policy})
    wid = rec_id(created)
    print(f"created workflow {WORKFLOW_NAME!r} ({wid})")
    return wid


def main() -> None:
    worker_id = ensure_worker()
    workflow_id = ensure_workflow(worker_id)
    print("\nSetup complete.")
    print(f"  worker_id       = {worker_id}")
    print(f"  workflow_def_id = {workflow_id}")
    print("\nNext: run  python3 app.py  and open the printed URL.")
    print("(app.py auto-discovers the workflow by name; no id copying needed.)")


if __name__ == "__main__":
    main()
