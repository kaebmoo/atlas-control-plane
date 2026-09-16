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
- drop the viewer scope-gate on the deliveries fetch -> the Promise.resolve assertion fails.
- let auditors click Retry (remove the .delivery-retry role gate) -> the gate assertion fails.
- count something other than failed rows in the deliveries badge -> the badge assertion fails.
- drop #deliveryList from markLoadFailed -> the first-load-failure assertion fails.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "atlas" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")

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

# --- Overview recent jobs → Jobs detail cross-navigation -------------------------------
need('<button class="dash-job" type="button"' in JS,
     "Overview recent jobs must be keyboard-activatable buttons")
need('data-job-id="${escapeHtml(job.id)}"' in JS,
     "Overview recent jobs must carry the selected job id")
need('event.target.closest(".job-row, .dash-job")' in JS,
     "job click handler must include Overview recent jobs")
need('state.jobRunFilter = "all"' in JS and 'showView("jobs")' in JS,
     "Overview recent jobs must open the Jobs detail view without a stale run filter")

# --- accepted-but-inert definition settings are visible on the run ------------------------
# (Mutations: drop the #workflowDefinitionWarning render in the run detail, or stop sending
# `warnings` on GET /api/workflows, and these go red.)
need('id="workflowDefinitionWarning"' in HTML, "run detail missing the #workflowDefinitionWarning banner")
need('$("#workflowDefinitionWarning")' in JS, "run detail does not render #workflowDefinitionWarning")
need("?.warnings" in JS, "the banner must use the server-computed warnings, not a local rule")
need("definitionEl.hidden = !definitionWarnings.length" in JS, "the banner must stay hidden when there is nothing to say")
# Anti-drift: the rule lives in atlas/workflows.workflow_graph_warnings. A console that builds
# the message itself silently goes stale the day the rule grows a case.
need("is ignored because" not in JS, "the console must not re-derive warning text; render what the API returns")
# Each banner labels itself. The label used to sit on the shared .alert-warn class, so the
# second banner silently announced itself as "Recovery required".
# (Mutation: move the content: rule back onto .alert-warn -> red.)
need(".alert-warn::before" not in CSS, "the shared .alert-warn class must not hard-code one banner's label")
need("#workflowRecoveryWarning::before" in CSS, "the recovery banner lost its own label")
need("#workflowDefinitionWarning::before" in CSS, "the ignored-settings banner has no label of its own")

# --- Deliveries view: the outbound ledger (reminders + return path) is an ops surface -----
need('data-view="deliveries"' in HTML, "Deliveries nav button missing")
need('id="view-deliveries"' in HTML, "Deliveries section missing")
need('id="deliveryList"' in HTML, "Deliveries list container missing")
need('data-badge="deliveries"' in HTML, "Deliveries nav badge missing")
need('class="nav-badge attn" data-badge="deliveries"' in HTML,
     "the deliveries badge must use the attention (attn) treatment — a failed reminder means nobody was chased")
need('deliveries: ["admin", "operator", "auditor"]' in JS,
     "VIEW_ROLES must gate deliveries to the roles holding deliveries.read (viewer excluded)")
need('"/api/deliveries?limit=' in JS, "loadAll does not fetch the deliveries ledger")
need("Promise.resolve({ deliveries: [] })" in JS,
     "a viewer must not issue a 403-bound deliveries request (scope-gate the fetch)")
# Pin the badge's own filter expression — a bare `status === "failed"` also matches the
# per-row Retry condition in renderDeliveries, which let a count-everything badge slip once.
need('setNavBadge("deliveries", state.deliveries.filter' in JS
     and 'delivery.status === "failed").length)' in JS,
     "the deliveries badge must count failed (dead-lettered) rows")
need("/api/deliveries/${encodeURIComponent(" in JS, "retry must POST the deliveries retry route with an escaped id")
need('".delivery-retry"' in JS and "!operator" in JS,
     "auditors can read deliveries but must not retry (workflows.run) — gate the button")
# The selector-list fragment, not a bare "#deliveryList" — $("#deliveryList") in the render
# path also matches that, which let a dropped markLoadFailed entry slip once.
need('"#auditList", "#deliveryList"' in JS, "markLoadFailed must cover the deliveries list")
need('"deliveryId"' in JS, "preserveListFocus must restore focus onto a rebuilt Retry button")
need("payload?.event" in JS, "the KIND column must come from the server payload's event discriminator")
need(".status.delivered" in CSS and ".status.blocked" in CSS,
     "delivered/blocked need their own chip styling (delivered is not 'succeeded')")

# --- existing anchors must not regress (careless rewrite guard) ---------------------------
need('id="usageBudgetUnits"' in HTML, "existing Usage marker regressed")
need('id="workflowRecoveryWarning"' in HTML, "existing run-detail alert marker regressed")
need('id="auditList"' in HTML, "existing Audit list marker regressed")

if problems:
    print("check_dashboard_surfaces FAILED:")
    for problem in problems:
        print(f"  - {problem}")
    sys.exit(1)
print("check_dashboard_surfaces OK")
