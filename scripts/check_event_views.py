#!/usr/bin/env python3
"""T2 structured-event surfaces — dashboard tool/skill timeline gate.

Hermetic: reads the static frontend directly and exercises the PURE timeline builder through
node (no DOM, no server), so a future edit cannot silently regress three behaviours:

  1. buildToolTimeline() folds structural-metadata events into an ordered tool/skill call list
     — pairs *_start/_invoked with their *_result/_denied by id, derives statuses/durations from
     the STORED structural fields alone, and ignores unknown event types without crashing.
  2. The rendered timeline escapes AND length-caps the worker-controlled tool/skill NAME.
  3. The stream dispatcher surfaces every frame (known structured + unknown future names) as a
     generic entry, and the Timeline tab/pane markers exist.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "atlas" / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")

problems: list[str] = []


def need(cond: bool, msg: str) -> None:
    if not cond:
        problems.append(msg)


def _slice(src: str, start_marker: str, end_marker: str) -> str:
    start = src.find(start_marker)
    end = src.find(end_marker, start + 1)
    if start == -1 or end == -1:
        return ""
    return src[start:end]


def _run_node(source: str) -> str:
    result = subprocess.run(["node", "-e", source], capture_output=True, text=True)
    if result.returncode != 0:
        problems.append(f"node harness failed: {result.stderr.strip()}")
        return ""
    return result.stdout


# --- 1. buildToolTimeline behaviour (pure, run through node) --------------------
# The contiguous block: the two module consts + buildToolTimeline() + toolCounters(), up to the
# first DOM-touching function. Evaluated standalone so the timeline logic is unit-tested.
BUILDER = _slice(JS, "const TOOL_START_KIND", "function toolDotClass")
need(bool(BUILDER), "buildToolTimeline()/toolCounters() block not found in app.js")

# Events arrive newest-first (like state.events) and out of seq order on purpose — the builder
# must sort by payload.seq. Secret-free: only structural metadata is ever present here.
SCENARIO = [
    {"type": "brand_new_event", "payload": {"seq": 99, "note": "worker from the future"}},
    {"type": "tool_use_result", "payload": {"seq": 12, "id": "t9", "name": "Orphan", "status": "ok", "output_bytes": 3, "output_sha256": "0" * 64, "created_at": "2026-07-04T00:00:09Z"}},
    {"type": "tool_use_result", "payload": {"seq": 11, "id": "t3", "name": "Grep", "status": "error", "output_bytes": 4, "created_at": "2026-07-04T00:00:08Z"}},
    {"type": "tool_use_start", "payload": {"seq": 10, "id": "t3", "name": "Grep", "status": "started", "input_bytes": 5, "created_at": "2026-07-04T00:00:07Z"}},
    {"type": "skill_invoked_result", "payload": {"seq": 8, "id": "s1", "name": "pdf", "status": "ok", "output_bytes": 12, "output_sha256": "a" * 64, "created_at": "2026-07-04T00:00:03Z"}},
    {"type": "skill_invoked", "payload": {"seq": 7, "id": "s1", "name": "pdf", "status": "started", "created_at": "2026-07-04T00:00:02Z"}},
    {"type": "tool_use_denied", "payload": {"seq": 6, "id": "t2", "name": "WebFetch", "status": "denied"}},
    {"type": "tool_use_result", "payload": {"seq": 5, "id": "t1", "name": "Bash", "status": "ok", "output_bytes": 20, "output_sha256": "b" * 64, "created_at": "2026-07-04T00:00:01Z"}},
    {"type": "tool_use_start", "payload": {"seq": 4, "id": "t1", "name": "Bash", "status": "started", "input_bytes": 10, "input_sha256": "c" * 64, "created_at": "2026-07-04T00:00:00Z"}},
    {"type": "text", "payload": {"seq": 1, "text": "hi"}},
]
harness = BUILDER + (
    f"\nconst events = {json.dumps(SCENARIO)};"
    "\nprocess.stdout.write(JSON.stringify({calls: buildToolTimeline(events), counts: toolCounters(buildToolTimeline(events))}));"
)
out = _run_node(harness)
if out:
    data = json.loads(out)
    calls, counts = data["calls"], data["counts"]
    names = [c["name"] for c in calls]
    # Ordered by start seq; unknown event dropped; denied/orphan stand alone.
    need(names == ["Bash", "WebFetch", "pdf", "Grep", "Orphan"], f"timeline order/derivation wrong: {names}")
    if names == ["Bash", "WebFetch", "pdf", "Grep", "Orphan"]:
        bash, webfetch, pdf, grep, orphan = calls
        need(bash["status"] == "ok" and bash["duration_ms"] == 1000, f"start/result not paired by id: {bash}")
        need(bash["input_bytes"] == 10 and bash["output_bytes"] == 20, f"structural bytes lost: {bash}")
        need(webfetch["status"] == "denied", f"denial status wrong: {webfetch}")
        need(pdf["kind"] == "skill" and pdf["status"] == "ok", f"skill entry wrong: {pdf}")
        need(grep["status"] == "error", f"error status wrong: {grep}")
        need(orphan["status"] == "ok", f"orphan result not surfaced: {orphan}")
    need(counts == {"run": 5, "denied": 1, "failed": 1}, f"per-job counters wrong: {counts}")

# --- 2. Rendered name is escaped AND length-capped -----------------------------
need("escapeHtml((call.name" in JS, "timeline must escape the worker-controlled tool/skill name")
need(".slice(0, 80)" in JS, "timeline must length-cap the tool/skill name (defence in depth)")
ESC = _slice(JS, "function escapeHtml(value)", "\n}") + "\n}"  # _slice drops the end marker; restore the close brace
need(bool(ESC.strip()) and "escapeHtml" in ESC, "escapeHtml() not found in app.js")
if ESC:
    esc_out = _run_node(ESC + "\nprocess.stdout.write(/[<>]/.test(escapeHtml('<img src=x onerror=alert(1)>'.repeat(9).slice(0,80))) ? 'LIVE' : 'SAFE');")
    need(esc_out.strip() == "SAFE", f"escapeHtml() left angle brackets in a hostile name: {esc_out!r}")

# --- 3. Dispatcher surfaces every frame + Timeline tab/pane markers -------------
need("appendEvent(name, safeJson(data))" in JS, "unknown/structured frames must be surfaced as generic entries (never crash)")
need("data-tool-status=" in JS, "timeline entries must carry a data-tool-status marker")
need('id="toolTimeline"' in HTML and 'data-job-tab="timeline"' in HTML, "Timeline tab/pane markers missing from index.html")
need(".tl-dot.denied" in CSS, "denied timeline styling missing from styles.css")

if problems:
    print("event views check FAILED:", file=sys.stderr)
    for item in problems:
        print("  - " + item, file=sys.stderr)
    sys.exit(1)
print("event views check ok")
