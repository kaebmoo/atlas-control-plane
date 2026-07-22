#!/usr/bin/env python3
"""Dashboard-surface gate for the minimal ops console.

Hermetic: reads the static frontend files directly (no server, no DB) and asserts the markers
+ wiring the embedded UI still owns. Job submission and the visual workflow builder moved to
the external frontend (flow-designer), so their markers are gone by design; what remains is
the ops surface:

  T1a/T1b — the Usage view shows token totals and the estimated (non-billable) cost.
  T6      — the run timeline shows files_pushed detail (count/bytes/target).
  T9a     — a job's collected files are downloadable from the Jobs view.

Mutation targets (break the code -> this file goes red):
- drop the usageTokens/usageEstCost lines in loadUsage -> the render assertions fail.
- drop the files_pushed detail branch in the event render -> the assertion fails.
- window the run timeline back to the FIRST 14 events -> the slice assertions fail.
- drop the stream-close artifact refresh in openJobStream -> the call-count assertion fails.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "atlas" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")

problems: list[str] = []


def need(cond: bool, msg: str) -> None:
    if not cond:
        problems.append(msg)


# --- T1a/T1b: Usage view tokens + estimated cost -----------------------------------------
need('id="usageTokens"' in HTML, "Usage view missing #usageTokens metric")
need('id="usageEstCost"' in HTML, "Usage view missing #usageEstCost metric")
need("estimate" in HTML.lower(), "cost metric must be labelled an estimate (not a bill)")
need('$("#usageTokens").textContent' in JS, "loadUsage does not render #usageTokens")
need("totals.tokens_prompt" in JS and "totals.tokens_output" in JS, "loadUsage ignores token totals")
need('$("#usageEstCost").textContent' in JS and "totals.estimated_cost_usd" in JS, "loadUsage ignores estimated_cost_usd")

# --- ops-console scope: no job submission / builder in the embedded UI ---------------------
need('id="promptInput"' not in HTML, "job composer must not return to the embedded UI (flow-designer owns it)")
need('id="builderEdgePushFilesInput"' not in HTML, "workflow builder must not return to the embedded UI")

# --- T6: run timeline shows files_pushed detail ------------------------------------------
need('type === "files_pushed"' in JS, "run timeline does not surface files_pushed detail")
need("payload.count" in JS and "payload.bytes" in JS and "payload.target_worker_id" in JS,
     "files_pushed detail must show count/bytes/target")
# the timeline must window to the LATEST events (seq ASC), else a late files_pushed on a long
# run never shows (the first 14 are setup events). Pin slice(-14), reject the old slice(0, 14).
need("state.workflowEvents.slice(-14)" in JS, "run timeline must show the most recent events, not the first 14")
need("state.workflowEvents.slice(0, 14)" not in JS, "run timeline still slices the FIRST 14 events")

# --- T9a gap: a standalone job's collected files are downloadable in the Jobs view ---------
need('data-job-tab="files"' in HTML, "Jobs view missing the Files tab")
need('id="jobArtifactDownloads"' in HTML, "Jobs view missing #jobArtifactDownloads pane")
need("async function loadJobArtifacts(" in JS, "loadJobArtifacts not defined")
need("/api/jobs/${encodeURIComponent(jobId)}/artifacts" in JS, "loadJobArtifacts must fetch the per-job artifacts route")
need('artifact.kind === "file_ref"' in JS, "loadJobArtifacts must filter file_ref artifacts")
# fetched on stream open AND on close (collection resolves at terminal).
# Count the CALL sites only (`.catch`), not the `async function loadJobArtifacts(jobId)`
# definition — otherwise def(1)+open-call(1) = 2 would pass even with the stream-close refresh
# removed, which is exactly the mutation this guards.
need(JS.count("loadJobArtifacts(jobId).catch") >= 2, "loadJobArtifacts must run on job open AND on stream close")
# NB: the backend GET /api/jobs/{id}/artifacts route is behaviour-tested end-to-end in
# scripts/check_job_artifacts.py (a static substring can't tell a working route from a broken one).

# --- existing anchors must not regress (careless rewrite guard) ---------------------------
need('id="usageBudgetUnits"' in HTML, "existing Usage marker regressed")

if problems:
    print("check_dashboard_surfaces FAILED:")
    for problem in problems:
        print(f"  - {problem}")
    sys.exit(1)
print("check_dashboard_surfaces OK")
