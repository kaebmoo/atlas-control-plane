#!/usr/bin/env python3
"""Dashboard-surface gate for the T1/T3/T5/T6 features on the web UI.

Hermetic: reads the static frontend files directly (no server, no DB) and asserts the markers
+ wiring that surface, on the dashboard, features the earlier milestones added only to the API:

  T1a/T1b — the Usage view shows token totals and the estimated (non-billable) cost.
  T3      — the Start-a-Job form can opt a job into async (execution: "callback").
  T5      — the Start-a-Job form can request collect_files.
  T6      — the visual builder can set policy.file_handoff (default-OFF) and edge push_files,
            and the run timeline shows files_pushed detail (count/bytes/target).

Mutation targets (break the code -> this file goes red):
- drop the usageTokens/usageEstCost lines in loadUsage -> the render assertions fail.
- drop `payload.collect_files` / `payload.execution` in submitJob -> the wiring assertions fail.
- flip the file_handoff boolean_off read from `value === true` to `value !== false`
  (so a missing file_handoff would wrongly render CHECKED) -> the exact-string assertion fails.
- drop `edge.push_files` in addBuilderEdge -> the assertion fails.
- drop the files_pushed detail branch in the event render -> the assertion fails.
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

# --- T3/T5: Start-a-Job form gains execution + collect_files ------------------------------
need('id="collectFilesInput"' in HTML, "job form missing #collectFilesInput")
need('id="jobExecutionCallback"' in HTML, "job form missing #jobExecutionCallback toggle")
need("payload.collect_files = collectFiles" in JS, "submitJob does not send collect_files")
need('payload.execution = "callback"' in JS, "submitJob does not send execution: callback")
# collect_files + callback is a server 400 — guard it client-side with a clear message.
need("asyncCallback && collectFiles.length" in JS, "submitJob must guard collect_files + callback client-side")
# the async toggle must reset after submit, or the next job silently inherits callback mode.
need('$("#jobExecutionCallback").checked = false' in JS, "submitJob must reset the async toggle after submit")

# --- T6: builder policy.file_handoff (default-off) + edge push_files ----------------------
need('id="policyFileHandoffInput"' in HTML, "builder missing #policyFileHandoffInput toggle")
need('id="builderEdgePushFilesInput"' in HTML, "builder missing #builderEdgePushFilesInput")
need('["file_handoff", "#policyFileHandoffInput", "boolean_off"]' in JS, "file_handoff not a boolean_off policy field")
# default-OFF correctness: a missing file_handoff must render UNCHECKED. The exact expression
# is load-bearing — flipping it to `value !== false` (the stop_on_first_failure default-ON rule)
# would silently enable handoff on every workflow, so pin the exact string.
need("$(selector).checked = value === true" in JS, "boolean_off read must be `value === true` (default-off)")
need("else delete policy[key]" in JS, "boolean_off write must drop the key when unchecked")
need("edge.push_files = pushFiles" in JS, "addBuilderEdge does not attach push_files")

# --- T6: run timeline shows files_pushed detail ------------------------------------------
need('type === "files_pushed"' in JS, "run timeline does not surface files_pushed detail")
need("payload.count" in JS and "payload.bytes" in JS and "payload.target_worker_id" in JS,
     "files_pushed detail must show count/bytes/target")
# the timeline must window to the LATEST events (seq ASC), else a late files_pushed on a long
# run never shows (the first 14 are setup events). Pin slice(-14), reject the old slice(0, 14).
need("state.workflowEvents.slice(-14)" in JS, "run timeline must show the most recent events, not the first 14")
need("state.workflowEvents.slice(0, 14)" not in JS, "run timeline still slices the FIRST 14 events")

# --- T5 gap: a standalone job's collected files are downloadable in the Jobs view ---------
need('data-job-tab="files"' in HTML, "Jobs view missing the Files tab")
need('id="jobArtifactDownloads"' in HTML, "Jobs view missing #jobArtifactDownloads pane")
need("async function loadJobArtifacts(" in JS, "loadJobArtifacts not defined")
need("/api/jobs/${encodeURIComponent(jobId)}/artifacts" in JS, "loadJobArtifacts must fetch the per-job artifacts route")
need('artifact.kind === "file_ref"' in JS, "loadJobArtifacts must filter file_ref artifacts")
# fetched on stream open AND on close (collection resolves at terminal).
need(JS.count("loadJobArtifacts(jobId)") >= 2, "loadJobArtifacts must run on job open AND on stream close")
# NB: the backend GET /api/jobs/{id}/artifacts route is behaviour-tested end-to-end in
# scripts/check_file_collection.py (a static substring can't tell a working route from a broken one).

# --- existing anchors must not regress (careless rewrite guard) ---------------------------
need('id="usageBudgetUnits"' in HTML, "existing Usage marker regressed")
need('id="promptInput"' in HTML, "existing job-form marker regressed")
need("function submitJob(" in JS and "function addBuilderEdge(" in JS, "core job/builder functions regressed")

if problems:
    print("check_dashboard_surfaces FAILED:")
    for problem in problems:
        print(f"  - {problem}")
    sys.exit(1)
print("check_dashboard_surfaces OK")
