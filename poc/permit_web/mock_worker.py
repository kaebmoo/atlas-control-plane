#!/usr/bin/env python3
"""Minimal thClaws-compatible mock worker for the Permit PoC.

Speaks only the subset Atlas needs:
  GET  /healthz         -> {"ok": true}
  GET  /v1/agent/info   -> capability JSON (any 200 marks the worker online)
  POST /agent/run       -> SSE: one `data: {"text": ...}` frame then `data: [DONE]`

It returns canned Thai text chosen by a `STEP=<name>` marker embedded in the node
prompt, so the whole PoC runs with NO real thClaws. This is a stub for local demos,
not a real agent runtime.

Run:  python3 mock_worker.py [PORT]     (default 4399)
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Canned responses per workflow step. json.dumps keeps each as a single SSE `data:`
# line even though the text has newlines (they become \n inside the JSON string).
CANNED = {
    "intake": (
        "ผลตรวจความครบถ้วนของคำขออนุญาต (mock)\n"
        "- ข้อมูลผู้ขอ: ครบ\n"
        "- ประเภทคำขอ: ครบ\n"
        "- รายละเอียด/เหตุผล: ครบ\n"
        "- เอกสารแนบ: ตรวจพบตามที่ระบุ — แนะนำให้เจ้าหน้าที่ยืนยันสำเนาบัตรประชาชนและ\n"
        "  หลักฐานกรรมสิทธิ์อีกครั้งก่อนพิจารณา\n"
        "สรุป: เอกสารเพียงพอต่อการพิจารณาเบื้องต้น"
    ),
    "summary": (
        "บันทึกสรุปเสนอผู้พิจารณา (mock)\n"
        "เรื่อง: คำขออนุญาตพร้อมพิจารณา\n"
        "สาระสำคัญ: คำขอมีข้อมูลและเอกสารครบตามผลตรวจเบื้องต้น ไม่พบเงื่อนไขต้องห้าม\n"
        "ข้อเสนอแนะ: เห็นควรอนุมัติ โดยแจ้งเงื่อนไขให้ผู้ขอปฏิบัติตามระเบียบที่เกี่ยวข้อง\n"
        "ความเสี่ยง: ต่ำ — รอการยืนยันสำเนาเอกสารจากเจ้าหน้าที่"
    ),
    "notice": (
        "หนังสือแจ้งผลการพิจารณา (ร่าง/mock)\n"
        "เรียน ผู้ยื่นคำขอ\n"
        "ตามที่ท่านได้ยื่นคำขออนุญาตนั้น หน่วยงานได้พิจารณาแล้วเห็นควร \"อนุมัติ\" ตามคำขอ\n"
        "โดยขอให้ท่านปฏิบัติตามเงื่อนไขที่แนบและติดต่อรับเอกสารภายใน 15 วันทำการ\n"
        "จึงเรียนมาเพื่อทราบและดำเนินการต่อไป"
    ),
}


def pick_response(prompt: str) -> str:
    for key, text in CANNED.items():
        if f"STEP={key}" in prompt:
            return text
    return "รับทราบคำขอแล้ว (mock worker: ไม่พบ STEP marker)"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # keep the console quiet
        return

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json({"ok": True, "service": "mock-thclaws"})
        else:  # /v1/agent/info and anything else
            self._json({"name": "permit-mock", "roles": ["permit"], "capabilities": {}})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            prompt = (json.loads(raw or b"{}").get("prompt") or "")
        except Exception:
            prompt = ""
        text = pick_response(prompt)
        frame = b"data: " + json.dumps({"text": text}).encode("utf-8") + b"\n\n"
        body = frame + b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    host = "127.0.0.1"
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4399
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"mock thClaws worker listening on http://{host}:{port}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
