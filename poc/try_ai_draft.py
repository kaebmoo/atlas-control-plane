#!/usr/bin/env python3
"""Step-0 test for Atlas's existing AI-draft API (no code changes to Atlas needed).

What it does, in order:
  1. GET /api/workers and report whether a `workflow_builder` worker exists
     (Atlas picks the first worker, in name order, whose role == workflow_builder
     or whose tags contain workflow_builder — this script mirrors that rule).
  2. Optionally (--tag-worker) tags an existing worker as workflow_builder via the
     upsert endpoint, preserving its name/role/existing tags/token.
  3. Polls the builder worker so you see online/offline before spending a model call.
  4. POST /api/workflows/draft with a plain-language prompt (Thai is fine) and
     prints a readable summary; the raw JSON is saved to --out.
  5. Optionally (--create) saves the draft via POST /api/workflows. Atlas defaults
     the new workflow to status=draft (test-only), so nothing can run in
     production from this script. Triggers in the draft are printed, never created.

The draft endpoint itself never saves or runs anything — it only returns a
validated proposal. See docs/specs/api-reference-en.md section "AI Draft".

Examples (Atlas local, loopback no-auth):
  python3 poc/try_ai_draft.py                         # status check + draft with the default prompt
  python3 poc/try_ai_draft.py --tag-worker wrk_xxx    # first run: tag your worker, then draft
  python3 poc/try_ai_draft.py --prompt "..." --create # custom prompt, save result for flow-designer

Auth: pass --token or set ATLAS_API_TOKEN. Not needed when Atlas runs with
ATLAS_LOOPBACK_NO_AUTH=true and you call 127.0.0.1.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_PROMPT = (
    "สร้าง workflow รับเรื่องร้องเรียนของประชาชน: โหนดแรกให้ AI สรุปเรื่องร้องเรียนจาก {input.complaint} "
    "และจัดหมวดหมู่ เก็บเป็น text artifact ชื่อ summary จากนั้นโหนดที่สองให้ AI ร่างหนังสือตอบกลับอย่างเป็นทางการ "
    "จาก {artifact.summary} แล้วส่งเข้า human gate ให้เจ้าหน้าที่เลือก approve หรือ revise "
    "ถ้า revise ให้วนกลับไปแก้ร่างใหม่ ไม่เกิน 3 รอบ ตั้ง policy ให้เหมาะกับงานทดสอบ"
)

BUILDER_MARK = "workflow_builder"


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def api(base_url: str, token: str | None, method: str, path: str, payload: dict | None = None, timeout: float = 30.0) -> dict:
    url = base_url.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(body).get("error") or body
        except (json.JSONDecodeError, ValueError):
            message = body
        raise ApiError(exc.code, str(message)) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ApiError(0, f"timed out after {timeout:.0f}s waiting for the response") from None
        raise ApiError(0, f"cannot reach Atlas at {base_url}: {exc.reason}") from None


def as_tags(value) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(v) for v in parsed]
        except json.JSONDecodeError:
            pass
        return [t.strip() for t in value.split(",") if t.strip()]
    return []


def is_builder(worker: dict) -> bool:
    return worker.get("role") == BUILDER_MARK or BUILDER_MARK in as_tags(worker.get("tags"))


def find_builder(workers: list[dict]) -> dict | None:
    # Atlas iterates db.list_workers() (ORDER BY name) and takes the first match.
    for worker in sorted(workers, key=lambda w: str(w.get("name") or "").casefold()):
        if is_builder(worker):
            return worker
    return None


def print_workers(workers: list[dict]) -> None:
    print(f"Workers ({len(workers)}):")
    for w in workers:
        mark = " [workflow_builder]" if is_builder(w) else ""
        print(
            f"  - {w.get('id')}  name={w.get('name')!r}  role={w.get('role') or '-'}  "
            f"tags={as_tags(w.get('tags'))}  status={w.get('status')}{mark}"
        )


def tag_worker(base_url: str, token: str | None, workers: list[dict], selector: str) -> dict:
    matches = [w for w in workers if selector in (w.get("id"), w.get("name"))]
    if not matches:
        raise SystemExit(f"No worker with id or name {selector!r}. Run without --tag-worker to list workers.")
    if len(matches) > 1:
        raise SystemExit(f"Selector {selector!r} matches more than one worker; use the wrk_ id.")
    w = matches[0]
    if is_builder(w):
        print(f"Worker {w.get('id')} is already a workflow_builder; nothing to change.")
        return w
    payload = {
        # Upsert REPLACES name/role/tags with what we send, so send everything back
        # unchanged except tags. Token is omitted on purpose: blank/omitted keeps the
        # stored token (documented upsert behavior).
        "id": w["id"],
        "base_url": w["base_url"],
        "name": w.get("name") or w["base_url"],
        "role": w.get("role") or "",
        "tags": as_tags(w.get("tags")) + [BUILDER_MARK],
    }
    result = api(base_url, token, "POST", "/api/workers", payload)
    updated = result.get("worker") or {}
    print(f"Tagged {updated.get('id')} ({updated.get('name')!r}) with '{BUILDER_MARK}'. tags={as_tags(updated.get('tags'))}")
    return updated


def poll_worker(base_url: str, token: str | None, worker: dict) -> None:
    try:
        result = api(base_url, token, "POST", f"/api/workers/{worker['id']}/poll")
    except ApiError as exc:
        print(f"Poll failed ({exc.status}): {exc.message} — continuing anyway.")
        return
    polled = result.get("worker") or {}
    status = polled.get("status")
    line = f"Builder worker {polled.get('id')} ({polled.get('name')!r}) status: {status}"
    if polled.get("last_error"):
        line += f"  last_error: {polled['last_error']}"
    print(line)
    if status not in {"online", "healthy"}:
        print("WARNING: builder worker is not online — the draft job will very likely fail. "
              "Check that `thclaws --serve` is running and the base_url/token are right.")


def summarize_draft(draft: dict) -> None:
    graph = draft.get("graph") or {}
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    print("\n===== DRAFT SUMMARY =====")
    print(f"name       : {draft.get('name')}")
    print(f"description: {draft.get('description')}")
    print(f"start node : {graph.get('start')}")
    print(f"\nNodes ({len(nodes)}):")
    for n in nodes:
        head = f"  - {n.get('id')} [{n.get('type')}]"
        extra = []
        for key in ("role", "worker_id", "workspace_id", "mode", "quorum", "label", "schema", "outputs", "output_format", "choices"):
            if n.get(key) not in (None, "", []):
                extra.append(f"{key}={n[key]}")
        if extra:
            head += "  " + "  ".join(str(e) for e in extra)
        print(head)
        prompt = str(n.get("prompt") or "").strip().replace("\n", " ")
        if prompt:
            print(f"      prompt: {prompt[:160]}{'…' if len(prompt) > 160 else ''}")
    print(f"\nEdges ({len(edges)}):")
    for e in edges:
        cond = e.get("condition") or {}
        cond_type = cond.get("type") or "always"
        detail = {k: v for k, v in cond.items() if k != "type"}
        print(f"  - {e.get('from')} -> {e.get('to')}  [{cond_type}{' ' + json.dumps(detail, ensure_ascii=False) if detail else ''}]")
    policy = draft.get("policy") or {}
    if policy:
        print("\nPolicy:")
        for k, v in policy.items():
            print(f"  {k} = {v}")
    triggers = draft.get("triggers") or []
    if triggers:
        print(f"\nTriggers proposed ({len(triggers)}) — NOT created by this script:")
        for t in triggers:
            print(f"  - {json.dumps(t, ensure_ascii=False)}")
    warnings = draft.get("warnings") or []
    if warnings:
        print("\nWarnings from Atlas/builder:")
        for w in warnings:
            print(f"  - {w}")
    explanation = str(draft.get("explanation") or "").strip()
    if explanation:
        print(f"\nExplanation:\n{explanation}")
    print("=========================")


def explain_failure(exc: ApiError) -> None:
    msg = exc.message
    print(f"\nDRAFT FAILED (HTTP {exc.status}): {msg}\n")
    if exc.status == 401:
        print("Auth: pass --token/-t or export ATLAS_API_TOKEN, or run Atlas with ATLAS_LOOPBACK_NO_AUTH=true for local testing.")
    elif "No workflow_builder worker configured" in msg:
        print("No builder yet: re-run with  --tag-worker <wrk_id or name>  to tag an existing worker.")
    elif "must be one JSON object" in msg:
        print("The model replied with something that isn't a single raw JSON object (often a ```json fence or chatty text).")
        print("Options: simply retry (occasional slip), strengthen the builder worker's instructions on the thClaws side")
        print("(AGENTS.md: 'when asked for JSON, output raw JSON only, no code fences'), or use a stronger model.")
    elif "workflow_builder job failed" in msg:
        print("The job itself failed on the worker. Check thClaws is running, the worker token is right, and the model key works.")
        print("Inspect: GET /api/jobs (latest job) and the worker's last_error via POST /api/workers/<id>/poll.")
    elif "references unknown" in msg or "has no matching worker" in msg or "not allowed by policy" in msg:
        print("The model invented or mismatched an id/role and Atlas's deterministic validation rejected it (by design).")
        print("Retry, or make the request more explicit about which of your real workers/roles to use.")
    else:
        print("See the message above; the raw error is exactly what Atlas returned after deterministic validation.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Test Atlas's existing POST /api/workflows/draft end to end.")
    ap.add_argument("--base-url", default=os.environ.get("ATLAS_BASE_URL", "http://127.0.0.1:8787"))
    ap.add_argument("--token", "-t", default=os.environ.get("ATLAS_API_TOKEN") or None)
    ap.add_argument("--prompt", "-p", default=DEFAULT_PROMPT, help="Plain-language description of the flow (Thai OK)")
    ap.add_argument("--tag-worker", metavar="ID_OR_NAME", help="Tag this existing worker as workflow_builder first (admin token required)")
    ap.add_argument("--create", action="store_true", help="After a successful draft, save it via POST /api/workflows (status stays 'draft' = test-only)")
    ap.add_argument("--out", default="ai_draft_result.json", help="Where to write the raw draft JSON (default: ./ai_draft_result.json)")
    ap.add_argument("--timeout", type=float, default=300.0, help="Seconds to wait for the draft call (the model runs synchronously)")
    ap.add_argument("--no-poll", action="store_true", help="Skip the pre-draft worker poll")
    args = ap.parse_args()

    try:
        workers = api(args.base_url, args.token, "GET", "/api/workers").get("workers") or []
    except ApiError as exc:
        print(f"Cannot list workers (HTTP {exc.status}): {exc.message}")
        if exc.status == 401:
            print("Auth: pass --token/-t or export ATLAS_API_TOKEN, or run Atlas with ATLAS_LOOPBACK_NO_AUTH=true.")
        return 1

    print_workers(workers)
    if args.tag_worker:
        try:
            tag_worker(args.base_url, args.token, workers, args.tag_worker)
        except ApiError as exc:
            print(f"Tagging failed (HTTP {exc.status}): {exc.message}")
            if exc.status == 403:
                print("Worker upsert needs an ADMIN token (operator is not enough).")
            return 1
        workers = api(args.base_url, args.token, "GET", "/api/workers").get("workers") or []

    builder = find_builder(workers)
    if not builder:
        print("\nNo workflow_builder worker configured yet.")
        print("Pick a worker from the list above and re-run with:  --tag-worker <wrk_id or name>")
        print("(Tagging keeps everything else about the worker unchanged; it only adds the tag.)")
        return 2
    if not args.tag_worker:
        print(f"\nBuilder Atlas will use: {builder.get('id')} ({builder.get('name')!r})")
    if not args.no_poll:
        poll_worker(args.base_url, args.token, builder)

    print(f"\nRequesting draft (this blocks while the model works, up to {int(args.timeout)}s)...")
    print(f"Prompt: {args.prompt}\n")
    try:
        result = api(args.base_url, args.token, "POST", "/api/workflows/draft",
                     {"plain_language_prompt": args.prompt}, timeout=args.timeout)
    except ApiError as exc:
        explain_failure(exc)
        return 3
    except TimeoutError:
        print(f"Timed out after {args.timeout}s. Slow model? Retry with a larger --timeout.")
        return 3

    draft = result.get("draft") or {}
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(draft, fh, ensure_ascii=False, indent=2)
    summarize_draft(draft)
    print(f"\nRaw draft JSON saved to: {args.out}")
    print("Note: the draft endpoint validated this deterministically but saved NOTHING.")

    if args.create:
        payload = {
            "name": draft.get("name"),
            "description": draft.get("description") or "",
            "graph": draft.get("graph"),
            "policy": draft.get("policy") or {},
            # No "status": Atlas defaults to 'draft' (test-only) — exactly what we want here.
            # Triggers are intentionally NOT created by this script.
        }
        try:
            created = api(args.base_url, args.token, "POST", "/api/workflows", payload)
        except ApiError as exc:
            print(f"\nSaving the workflow failed (HTTP {exc.status}): {exc.message}")
            return 4
        wf = created.get("workflow") or {}
        print(f"\nSaved as workflow {wf.get('id')} (status={wf.get('status')}).")
        print("Open it in flow-designer to review the graph on the canvas; it can only run in test mode until you activate it.")
    else:
        print("If the draft looks good, re-run with --create to save it and inspect it on the flow-designer canvas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
