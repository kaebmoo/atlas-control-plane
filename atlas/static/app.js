const state = {
  currentUser: null,
  workers: [],
  workspaces: [],
  jobs: [],
  audit: [],
  workflows: [],
  workflowRuns: [],
  approvals: [],
  selectedJobId: null,
  selectedWorkflowRunId: null,
  workflowRunDetail: null,
  workflowArtifacts: [],
  jobArtifacts: [],
  workflowEvents: [],
  users: [],
  apiTokens: [],
  eventSource: null,
  auditFilter: "all",
  auditFilterText: "",
  auditLimit: 30,
  jobRunFilter: "all",
  streamText: "",
  events: [],
};

const API_BASE = (window.ATLAS_API_BASE || "").replace(/\/+$/, "");
const $ = (selector) => document.querySelector(selector);
const AUTO_POLL_MS = 60000;
async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (!headers.has("Content-Type") && options.body) headers.set("Content-Type", "application/json");
  const token = localStorage.getItem("atlasApiToken");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  let response;
  try {
    response = await fetch(API_BASE + path, { ...options, headers });
  } catch (error) {
    setHealth(false);  // transport failure — the control plane is actually unreachable
    throw error;
  }
  setHealth(true);  // any HTTP response (even 4xx/5xx) means the server is up
  const text = await response.text();
  let data = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { error: text };
    }
  }
  if (!response.ok) {
    // A non-JSON error body (e.g. a proxy's HTML error page) must not become the operator-facing
    // message — fall back to the status line instead of dumping markup into toasts/banners.
    const raw = data?.error || data?.message || "";
    const usable = typeof raw === "string" && raw && raw.length <= 200 && !raw.trimStart().startsWith("<");
    const message = usable ? raw : `HTTP ${response.status}${response.statusText ? ` ${response.statusText}` : ""}`;
    if (response.status === 401 && path !== "/api/auth/login") {
      localStorage.removeItem("atlasApiToken");
      showLogin("Session expired — please sign in again · เซสชันหมดอายุ กรุณาเข้าสู่ระบบใหม่");
    }
    throw new Error(message);
  }
  return data;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

// T4: only ever surface an "Open worker UI" link for an http(s) URL. A worker-reported
// external_access.ui_url is untrusted — a `javascript:`/`file:`/`data:` scheme in an href is an
// XSS/exfil vector, so anything that does not parse as http/https yields "" (link not rendered).
function safeHttpUrl(value) {
  if (typeof value !== "string") return "";
  try {
    const url = new URL(value.trim());
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : "";
  } catch {
    return "";
  }
}

// T4 worker-contract version range Atlas has actually tested (docs/specs/thclaws-worker-contract.md).
// A worker reporting anything else gets an advisory version-mismatch warning in its card.
const CONTRACT_TESTED_VERSIONS = new Set(["0.85.0"]);
const CONTRACT_TESTED_RANGE = "0.85.0";

function versionOutsideContract(version) {
  return Boolean(version) && !CONTRACT_TESTED_VERSIONS.has(version);
}

function statusClass(value) {
  // Whitelist to a safe CSS-class token: the result flows into class="status ${...}", so any
  // character outside [A-Za-z0-9-] (e.g. a quote) could break out of the attribute and inject
  // markup. An operator can set an arbitrary workflow status via PUT, so sanitize at the sink.
  return String(value || "unknown").replaceAll("_", "-").replace(/[^A-Za-z0-9-]/g, "") || "unknown";
}

// Operator-facing labels for machine states; statusClass() keeps the raw token for CSS.
const STATUS_LABELS = {
  waiting_for_human: "waiting for human",
  recovery_required: "needs recovery",
  cancel_requested: "cancelling…",
};
function statusLabel(value) {
  const raw = String(value || "unknown");
  return STATUS_LABELS[raw] || raw.replaceAll("_", " ");
}

function shortId(value) {
  return String(value || "").split("_").at(-1)?.slice(0, 8) || "";
}

function formatTime(value) {
  if (!value) return "";
  return new Date(value).toLocaleString([], { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

let toastTimer = null;
function toast(message) {
  const node = $("#toast");
  node.textContent = message || "Done";
  node.classList.add("visible");
  clearTimeout(toastTimer); // a still-pending timer from a prior toast would hide this one early
  toastTimer = setTimeout(() => node.classList.remove("visible"), 2600);
}

const VIEWS = ["overview", "monitor", "jobs", "audit", "usage", "fleet", "accounts"];
// Page subtitles are English-only: the title row already pairs EN + Thai, and the subtitle
// must survive narrow topbars. Thai supplements live in the body copy with lang="th".
const VIEW_META = {
  overview:  ["ภาพรวม", "Overview", "Fleet, jobs and workflow runs at a glance"],
  monitor:   ["ติดตาม", "Monitor", "Status and timeline of each workflow run"],
  jobs:      ["งาน", "Jobs", "Live output stream and job history"],
  fleet:     ["ฟลีต", "Fleet", "Manage workers and workspaces"],
  audit:     ["ตรวจสอบ", "Audit", "Last 30 actions on the control plane"],
  usage:     ["การใช้งาน", "Usage", "Workflow runs, jobs and budget units over time"],
  accounts:  ["บัญชี", "Accounts", "Manage users, roles and API tokens"],
};

// Role-gated views: one table drives showView's navigation guard AND applyRoleGate's
// nav visibility/kick-out, so a hash deep-link can never reach a view the nav hides.
const VIEW_ROLES = {
  audit: ["admin", "auditor"],
  usage: ["admin", "auditor"],
  accounts: ["admin"],
};

function showView(view) {
  if (!VIEWS.includes(view)) view = "overview";
  const allowedRoles = VIEW_ROLES[view];
  if (allowedRoles && state.currentUser && !allowedRoles.includes(state.currentUser.role)) view = "overview";
  for (const section of document.querySelectorAll(".view")) {
    section.classList.toggle("is-active", section.dataset.view === view);
  }
  for (const button of document.querySelectorAll(".nav-item")) {
    const active = button.dataset.view === view;
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  const meta = VIEW_META[view] || VIEW_META.overview;
  $("#pageTitleTh").textContent = meta[0];
  $("#pageTitleEn").textContent = meta[1];
  $("#pageSub").textContent = meta[2];
  // Per-view data loaders live here — the single navigation entry point — not in each
  // nav/hash/link handler. Skipped pre-auth; the boot .then re-runs them once signed in.
  if (state.currentUser) {
    if (view === "usage") loadUsage().catch(() => undefined);
    if (view === "accounts") loadAccounts().catch((error) => toast(error.message));
  }
  try { localStorage.setItem("atlasView", view); } catch { /* private mode */ }
  // Deep-linkable views: keep the URL hash in sync so refresh/back/share land on the same view.
  if (location.hash.slice(1) !== view) {
    if (location.hash) location.hash = view;
    else history.replaceState(null, "", `#${view}`);  // boot: no phantom history entry for Back
  }
}

const NAV_BADGE_LABELS = { approvals: "pending approvals", jobs: "active jobs", workers: "workers" };

function setNavBadge(name, count) {
  const badge = document.querySelector(`.nav-badge[data-badge="${name}"]`);
  if (!badge) return;
  badge.textContent = count > 99 ? "99+" : String(count);
  badge.setAttribute("aria-label", `${count} ${NAV_BADGE_LABELS[name] || name}`);
  badge.hidden = !count;
}

function renderNavBadges() {
  setNavBadge("approvals", state.approvals.length);
  setNavBadge("jobs", state.jobs.filter((job) => ["queued", "running", "cancel_requested"].includes(job.state)).length);
  setNavBadge("workers", state.workers.length);
}

function tagsToText(tags) {
  if (Array.isArray(tags)) return tags.join(", ");
  return String(tags || "");
}

function prettyJson(value) {
  return JSON.stringify(value ?? {}, null, 2);
}

function parseJsonField(selector, fallback = {}) {
  const raw = $(selector).value.trim();
  if (!raw) return fallback;
  return JSON.parse(raw);
}

async function loadAll() {
  const me = await api("/api/me");
  state.currentUser = me.user;
  hideLogin();
  const canReadAudit = ["admin", "auditor"].includes(state.currentUser?.role);
  const [workers, workspaces, jobs, workflows, workflowRuns, approvals, audit] = await Promise.all([
    api("/api/workers"),
    api("/api/workspaces"),
    api("/api/jobs"),
    api("/api/workflows"),
    api("/api/workflow-runs"),
    api("/api/approvals?state=pending"),
    canReadAudit ? api(`/api/audit?limit=${state.auditLimit}`) : Promise.resolve({ audit: [] }),
  ]);
  state.workers = workers.workers || [];
  state.workspaces = workspaces.workspaces || [];
  state.jobs = jobs.jobs || [];
  state.workflows = workflows.workflows || [];
  state.workflowRuns = workflowRuns.runs || [];
  state.approvals = approvals.approvals || [];
  state.audit = audit.audit || [];
  if (state.selectedWorkflowRunId && state.workflowRuns.some((run) => run.id === state.selectedWorkflowRunId)) {
    await loadWorkflowRunDetail(state.selectedWorkflowRunId);
  }
  preserveListFocus(render);
  setStale(null);
  announceApprovals();
}

// The 5s poll rewrites list innerHTML, which ejects keyboard focus to <body>. Remember the
// focused element's identifying data-* attribute (and class, to pick the same button among
// row siblings) and restore focus onto the rebuilt node.
function preserveListFocus(renderFn) {
  const active = document.activeElement;
  let marker = null;
  if (active && active.dataset) {
    for (const key of ["jobId", "runId", "workerId", "workspaceId", "userId", "tokenId", "approvalId", "navJob", "navRun"]) {
      if (active.dataset[key]) { marker = { key, value: active.dataset[key], className: active.className }; break; }
    }
  }
  renderFn();
  if (!marker) return;
  const attr = "data-" + marker.key.replace(/[A-Z]/g, (c) => "-" + c.toLowerCase());
  const candidates = [...document.querySelectorAll(`[${attr}="${CSS.escape(marker.value)}"]`)];
  const target = candidates.find((node) => node.className === marker.className) || candidates[0];
  target?.focus?.();
}

// A user's in-progress text selection (e.g. dragging to copy a job id) lives in the DOM; the
// poll's innerHTML rewrite would silently discard it. Skip that one list's redraw for this
// cycle when the selection anchors inside it — it catches up on the next poll once released.
function selectionInside(node) {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || !selection.rangeCount) return false;
  return node.contains(selection.getRangeAt(0).commonAncestorContainer);
}

// Persistent staleness signal: a failed poll shows a banner (with retry) until the next
// successful load — a 2.6s toast is not enough warning that every list may be out of date.
function setStale(error) {
  const banner = document.getElementById("staleBanner");
  if (!banner) return;
  if (!error) { banner.hidden = true; return; }
  const wasHidden = banner.hidden;
  banner.hidden = false;
  banner.innerHTML = `<span>⚠ Last update failed (${escapeHtml(error.message || "error")}) — data may be stale · <span lang="th">ข้อมูลอาจไม่เป็นปัจจุบัน</span></span><button id="staleRetryBtn" class="secondary-btn btn-sm" type="button">Retry</button>`;
  if (wasHidden) announce("Update failed — data may be stale");
}

// First-load failure: lists otherwise sit on "Loading…" forever, indistinguishable from slow.
// Only replaces containers still showing an .empty placeholder — stale real data is left alone.
function markLoadFailed() {
  for (const selector of ["#overviewJobs", "#jobList", "#workerList", "#workspaceList", "#auditList", "#workflowRunList", "#userList", "#tokenList"]) {
    const node = document.querySelector(selector);
    if (node && node.querySelector(".empty")) {
      node.innerHTML = '<div class="empty">Could not load — check the connection, then Retry above · <span lang="th">โหลดไม่สำเร็จ</span></div>';
    }
  }
}

// Polite screen-reader announcements for events that otherwise change silently.
function announce(message) {
  const node = document.getElementById("ariaStatus");
  if (node) node.textContent = message;
}

let lastApprovalCount = null;
function announceApprovals() {
  const count = state.approvals.length;
  if (lastApprovalCount !== null && count > lastApprovalCount) {
    announce(`${count} approval${count === 1 ? "" : "s"} waiting for a decision`);
  }
  lastApprovalCount = count;
}

async function refreshAll({ poll = false, notice = false } = {}) {
  const didPoll = poll && ["admin", "operator"].includes(state.currentUser?.role);
  if (didPoll) {
    await api("/api/workers/poll", { method: "POST" });
  }
  await loadAll();
  if (notice) toast(didPoll ? "Refreshed and polled workers" : "Refreshed");
}

function render() {
  renderMetrics();
  renderNavBadges();
  renderWorkers();
  renderWorkspaces();
  renderSelects();
  renderJobs();
  renderWorkflowRuns();
  renderAudit();
  renderIdentity();
  applyRoleGate();
  // Last: keep the selected job's header/cancel/cursor in sync on every poll, after
  // applyRoleGate (which would otherwise re-enable Cancel for operators on finished jobs).
  updateStreamHeader();
}

function showLogin(message = "Use the admin account of this Atlas instance") {
  state.currentUser = null;
  if (state.eventSource) state.eventSource.close();
  $("#loginMessage").textContent = message;
  $("#loginScreen").hidden = false;
  document.querySelector(".app-shell").inert = true;
  document.body.classList.remove("is-loading");
  $("#loginForm").elements.username.focus();
}

function hideLogin() {
  $("#loginScreen").hidden = true;
  document.querySelector(".app-shell").inert = false;
  $("#loginMessage").textContent = "Use the admin account of this Atlas instance";
}

function renderIdentity() {
  const panel = $("#signedInPanel");
  panel.hidden = !state.currentUser;
  if (!state.currentUser) return;
  const name = state.currentUser.username || "";
  $("#userName").textContent = name;
  $("#userRole").textContent = state.currentUser.role || "";
  $("#userAvatar").textContent = (name.trim()[0] || "–").toUpperCase();
}

function applyRoleGate() {
  const role = state.currentUser?.role;
  const operator = ["admin", "operator"].includes(role);
  const admin = role === "admin";
  for (const selector of [
    "#cancelJobBtn", "#pauseWorkflowRunBtn", "#resumeWorkflowRunBtn",
    "#retryInterruptedRunBtn", "#cancelWorkflowRunBtn", "#pollAllBtn",
  ]) {
    const node = $(selector);
    if (node) node.disabled = !operator;
  }
  for (const selector of ["#addWorkerBtn", "#addWorkspaceBtn"]) {
    const node = $(selector);
    if (node) node.disabled = !admin;
  }
  for (const [view, roles] of Object.entries(VIEW_ROLES)) {
    const navItem = document.querySelector(`.nav-item[data-view="${view}"]`);
    if (!navItem) continue;
    navItem.hidden = !roles.includes(role);
    if (navItem.hidden && document.querySelector(`#view-${view}.is-active`)) showView("overview");
  }
  for (const node of document.querySelectorAll(".edit-worker, .delete-worker, .edit-workspace, .delete-workspace, .sync-mode-select")) node.disabled = !admin;
  for (const node of document.querySelectorAll(".poll-worker")) node.disabled = !operator;
}

async function login(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submitBtn = form.querySelector('button[type="submit"]');
  submitBtn.disabled = true;
  try {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username: form.elements.username.value, password: form.elements.password.value }),
    });
    localStorage.setItem("atlasApiToken", data.token);
    form.elements.password.value = "";
    await loadAll();
  } finally {
    submitBtn.disabled = false;
  }
}

async function signOut() {
  try { await api("/api/auth/logout", { method: "POST" }); } catch { /* token may already be invalid */ }
  localStorage.removeItem("atlasApiToken");
  state.currentUser = null;
  showLogin("Signed out · ออกจากระบบแล้ว");
}

function renderRunSummary() {
  const summary = $("#workflowRunSummary");
  const run = state.workflowRunDetail?.run;
  if (!run) {
    summary.innerHTML = "";
    return;
  }
  const counters = run.counters || {};
  const chips = [`<span class="chip">state <b>${escapeHtml(statusLabel(run.state))}</b></span>`];
  if (typeof counters.jobs_started === "number") chips.push(`<span class="chip">jobs <b>${counters.jobs_started}</b></span>`);
  if (typeof counters.budget_units_spent === "number") chips.push(`<span class="chip">budget <b>${counters.budget_units_spent}</b></span>`);
  const completed = (counters.completed_nodes || []).length;
  if (completed) chips.push(`<span class="chip good">nodes done <b>${completed}</b></span>`);
  const failed = (counters.failed_nodes || []).length;
  if (failed) chips.push(`<span class="chip bad">nodes failed <b>${failed}</b></span>`);
  for (const [joinId, join] of Object.entries(counters.join_states || {})) {
    const have = (join.completed_upstreams || []).length;
    const need = join.mode === "quorum" ? (join.quorum ?? "?") : (join.upstream_nodes || []).length;
    const cls = join.state === "succeeded" ? "good" : join.state === "failed" ? "bad" : "warn";
    chips.push(`<span class="chip ${cls}">join ${escapeHtml(joinId)} · ${escapeHtml(join.mode)} <b>${have}/${need}</b></span>`);
  }
  summary.innerHTML = chips.join("");
}

function renderMetrics() {
  const set = (id, value) => { const node = document.getElementById(id); if (node) node.textContent = value; };
  const total = state.workers.length;
  const online = state.workers.filter((w) => w.status === "online").length;
  const offline = state.workers.filter((w) => w.status === "offline").length;
  const unknown = Math.max(0, total - online - offline);
  const queued = state.jobs.filter((j) => j.state === "queued").length;
  const running = state.jobs.filter((j) => j.state === "running").length;
  const active = state.jobs.filter((j) => ["queued", "running", "cancel_requested"].includes(j.state)).length;
  const activeRuns = state.workflowRuns.filter((r) => ["running", "waiting_for_human", "paused"].includes(r.state)).length;
  set("metricWorkers", online);
  set("metricWorkersUnit", `/ ${total}`);
  set("metricWorkersDelta", total ? `online ${online} · offline ${offline}` : "no workers yet");
  set("metricRunning", active);
  set("metricRunningDelta", active ? `queued ${queued} · running ${running}` : "no active jobs");
  const midnight = new Date(); midnight.setHours(0, 0, 0, 0);
  const runsToday = state.workflowRuns.filter((run) => run.created_at && new Date(run.created_at) >= midnight).length;
  set("metricRuns", runsToday);
  set("metricRunsDelta", !state.workflowRuns.length ? "no runs yet"
    : activeRuns ? `active ${activeRuns} · total ${state.workflowRuns.length}` : `of ${state.workflowRuns.length} total`);
  set("metricApprovals", state.approvals.length);
  set("metricApprovalsDelta", state.approvals.length ? "needs a decision" : "none pending");
  // Fleet health donut: yellow arc = online / total (r=40 → circumference 251.33)
  const arc = document.getElementById("fleetArc");
  if (arc) {
    const circumference = 2 * Math.PI * 40;
    const fraction = total ? online / total : 0;
    arc.setAttribute("stroke-dasharray", circumference.toFixed(1));
    arc.setAttribute("stroke-dashoffset", (circumference * (1 - fraction)).toFixed(1));
  }
  set("fleetFraction", `${online}/${total}`);
  set("fleetOnline", online);
  set("fleetOffline", offline);
  set("fleetUnknown", unknown);
  renderOverviewLists();
}

function renderOverviewLists() {
  const jobsBox = document.getElementById("overviewJobs");
  if (jobsBox) {
    const recent = state.jobs.slice(0, 5);
    jobsBox.innerHTML = recent.length
      ? recent.map((job) => `
        <button class="dash-job" type="button" data-job-id="${escapeHtml(job.id)}">
          <span class="status ${statusClass(job.state)} dot-only" aria-hidden="true"></span>
          <div class="grow">
            <div class="title">${escapeHtml(job.prompt || job.id)}</div>
            <div class="meta">${escapeHtml(job.worker_name || shortId(job.worker_id) || "—")} · ${escapeHtml(shortId(job.id))}</div>
          </div>
          <span class="status ${statusClass(job.state)}">${escapeHtml(statusLabel(job.state))}</span>
          <span class="ago">${escapeHtml(formatTime(job.created_at))}</span>
        </button>`).join("")
      : '<div class="empty">No jobs yet · <span lang="th">ยังไม่มีงาน</span></div>';
  }
  const apBox = document.getElementById("overviewApproval");
  if (apBox) {
    const a = state.approvals[0];
    const more = state.approvals.length - 1;
    apBox.innerHTML = a
      ? `<div class="approve-row">
          <span class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg></span>
          <div class="grow"><div class="title">${escapeHtml(a.label || a.reason || "Waiting for approval")}</div><div class="meta">run ${escapeHtml(shortId(a.run_id))} · ${escapeHtml(formatTime(a.created_at))}</div></div>
          <button class="primary-btn btn-sm" type="button" data-view-link="monitor" data-run-id="${escapeHtml(a.run_id)}">Review</button>
        </div>${more > 0 ? `<div class="approve-more">+ ${more} more waiting · <span lang="th">รออีก ${more} รายการ</span></div>` : ""}`
      : '<div class="approve-empty">Nothing waiting for approval · <span lang="th">ไม่มีรายการรออนุมัติ</span></div>';
  }
}

function renderWorkers() {
  const list = $("#workerList");
  if (selectionInside(list)) return;
  if (!state.workers.length) {
    list.innerHTML = '<div class="empty">No workers yet — press “+ Add worker” to connect one · <span lang="th">ยังไม่มี worker</span></div>';
    return;
  }
  list.innerHTML = state.workers.map((worker) => {
    const tags = (worker.tags || []).map((tag) => `<span class="wc-tag">${escapeHtml(tag)}</span>`).join("");
    const seen = worker.last_seen_at ? `last seen ${formatTime(worker.last_seen_at)}` : "never seen";
    // T4 advisory surfaces. All worker-reported values escaped/scheme-checked before render.
    const info = worker.agent_info || {};
    const agent = info.agent || {};
    const syncMode = worker.sync_mode || "disabled";
    const busyState = info.busy === true ? "busy" : info.busy === false ? "free" : "unknown";
    const busyBadge = syncMode !== "disabled"
      ? `<span class="wc-busy" data-busy="${busyState}">busy: ${busyState}${info.busy_checked_at ? ` · ${escapeHtml(formatTime(info.busy_checked_at))}` : ""}</span>`
      : "";
    const uiUrl = safeHttpUrl((agent.external_access || {}).ui_url);
    const uiLink = uiUrl ? `<a class="wc-ui-link" href="${escapeHtml(uiUrl)}" target="_blank" rel="noopener">Open worker UI ↗</a>` : "";
    const skills = Array.isArray(agent.skills) ? agent.skills : [];
    const skillChips = skills.slice(0, 20).map((skill) => `<span class="wc-skill" title="${escapeHtml(String((skill && skill.when_to_use) || ""))}">${escapeHtml(String((skill && skill.name) || "skill"))}</span>`).join("");
    const skillsBlock = skillChips ? `<div class="wc-skills" title="daemon-scoped, advisory">skills (daemon-scoped, advisory): ${skillChips}</div>` : "";
    const workerVersion = String(agent.version || "");
    const versionWarn = versionOutsideContract(workerVersion)
      ? `<div class="wc-version-warn" data-version-warn="1">⚠ worker version ${escapeHtml(workerVersion)} outside contract-tested range (${escapeHtml(CONTRACT_TESTED_RANGE)})</div>`
      : "";
    const syncOptions = ["disabled", "tunnel", "forward_auth"].map((mode) => `<option value="${mode}"${mode === syncMode ? " selected" : ""}>${mode}</option>`).join("");
    const syncControl = `<label class="wc-sync-mode" data-sync-mode="${escapeHtml(syncMode)}">sync <select class="sync-mode-select" data-worker-id="${escapeHtml(worker.id)}">${syncOptions}</select></label>`;
    return `
    <article class="worker-card">
      <div class="wc-head">
        <span class="status ${statusClass(worker.status)} dot-only" aria-hidden="true"></span>
        <span class="name">${escapeHtml(worker.name)}</span>
        <span class="status ${statusClass(worker.status)}">${escapeHtml(statusLabel(worker.status))}</span>
      </div>
      <div class="wc-url">${escapeHtml(worker.base_url)}</div>
      <div class="wc-url" title="Worker ID">${escapeHtml(worker.id)}<button class="copy-btn" type="button" data-copy="${escapeHtml(worker.id)}" title="Copy worker ID" aria-label="Copy id of ${escapeHtml(worker.name)}">⧉</button></div>
      <div class="wc-role">
        <span class="role-chip">${escapeHtml(worker.role || "unassigned")}<button class="copy-btn" type="button" data-copy="${escapeHtml(worker.role || "unassigned")}" title="Copy role" aria-label="Copy role of ${escapeHtml(worker.name)}">⧉</button></span>
        <span class="latency">${escapeHtml(seen)}</span>
      </div>
      <div class="wc-tags">${tags}</div>
      ${versionWarn}
      <div class="wc-advisory">${busyBadge}${syncControl}${uiLink}</div>
      ${skillsBlock}
      <div class="wc-actions">
        <button class="secondary-btn poll-worker" data-worker-id="${escapeHtml(worker.id)}" aria-label="Poll ${escapeHtml(worker.name)}">Poll</button>
        <button class="secondary-btn edit-worker" data-worker-id="${escapeHtml(worker.id)}" aria-label="Edit ${escapeHtml(worker.name)}">Edit</button>
        <button class="danger-btn delete-worker" data-worker-id="${escapeHtml(worker.id)}" aria-label="Delete ${escapeHtml(worker.name)}">Delete</button>
      </div>
    </article>`;
  }).join("");
}

function renderWorkspaces() {
  const list = $("#workspaceList");
  if (!state.workspaces.length) {
    list.innerHTML = '<div class="empty">No workspaces yet · <span lang="th">ยังไม่มี workspace</span></div>';
    return;
  }
  list.innerHTML = state.workspaces.map((workspace) => `
    <div class="ws-row">
      <span class="key">${escapeHtml(workspace.workspace_key)}</span>
      <span>${escapeHtml(workspace.worker_name || shortId(workspace.worker_id))}</span>
      <span class="dir">${escapeHtml(workspace.workspace_dir)}</span>
      <span>${escapeHtml(workspace.company || "—")}</span>
      <span class="ws-actions">
        <button class="ws-mini edit-workspace" data-workspace-id="${escapeHtml(workspace.id)}" aria-label="Edit workspace ${escapeHtml(workspace.workspace_key)}">Edit</button>
        <button class="ws-mini danger delete-workspace" data-workspace-id="${escapeHtml(workspace.id)}" aria-label="Delete workspace ${escapeHtml(workspace.workspace_key)}">Delete</button>
      </span>
    </div>
  `).join("");
}

function renderSelects() {
  // Only the workspace modal picks a worker now — the job composer lives in flow-designer.
  document.querySelector('#workspaceForm select[name="worker_id"]').innerHTML = state.workers.map((worker) => (
    `<option value="${escapeHtml(worker.id)}">${escapeHtml(worker.name)}</option>`
  )).join("");
}

// Run filter over the job list: one option per workflow run that produced jobs (+ Manual for
// non-workflow jobs). Runs are labelled from state.workflowRuns; run_id comes from the API.
// A <select> keeps this row one line tall however many runs exist — the chip row it replaced
// grew with every run until it squeezed the job list out of the card entirely. Runs of the
// same workflow share a name, so the workflow is the <optgroup> and each option carries what
// actually tells two runs apart: when it started, how it ended, and its id.
function renderJobFilters() {
  const box = document.getElementById("jobFilters");
  const select = document.getElementById("jobRunFilter");
  if (!box || !select) return;
  const runIds = [];
  const seen = new Set();
  for (const job of state.jobs) {
    if (job.run_id && !seen.has(job.run_id)) { seen.add(job.run_id); runIds.push(job.run_id); }
  }
  box.hidden = runIds.length === 0;  // no workflow jobs → no filter row at all
  if (box.hidden) { select.innerHTML = ""; return; }
  // Rebuilding the options under an open dropdown would close it mid-choice — same reasoning
  // as selectionInside() below: this one control skips the poll and catches up on the next.
  if (document.activeElement === select && select.options.length) return;
  const groups = [];  // [{ name, options }] — groups and options both stay newest-run-first
  for (const id of runIds) {
    const run = state.workflowRuns.find((item) => item.id === id);
    const name = run?.name || "Workflow run";
    let group = groups.find((entry) => entry.name === name);
    if (!group) { group = { name, options: [] }; groups.push(group); }
    const label = `${formatTime(run?.created_at)} · ${run?.state || "unknown"} · ${shortId(id)}`;
    group.options.push(`<option value="${escapeHtml(id)}">${escapeHtml(label)}</option>`);
  }
  const options = ['<option value="all">All jobs · ทั้งหมด</option>'];
  if (state.jobs.some((job) => !job.run_id)) options.push('<option value="manual">Manual — no workflow · สั่งตรง</option>');
  for (const group of groups) options.push(`<optgroup label="${escapeHtml(group.name)}">${group.options.join("")}</optgroup>`);
  select.innerHTML = options.join("");
  select.value = state.jobRunFilter || "all";
  if (!select.value) select.value = "all";  // filter's run dropped out of the list mid-poll
}

function renderJobs() {
  const list = $("#jobList");
  const count = document.getElementById("jobCount");
  let filter = state.jobRunFilter || "all";
  // A stale run filter (its run no longer has jobs) falls back to "all" so nothing is hidden.
  if (filter !== "all" && filter !== "manual" && !state.jobs.some((job) => job.run_id === filter)) {
    filter = state.jobRunFilter = "all";
  }
  renderJobFilters();
  const jobs = filter === "all" ? state.jobs
    : filter === "manual" ? state.jobs.filter((job) => !job.run_id)
    : state.jobs.filter((job) => job.run_id === filter);
  if (count) count.textContent = `${jobs.length} jobs`;
  if (selectionInside(list)) return;
  if (!jobs.length) {
    list.innerHTML = '<div class="empty">No jobs yet · <span lang="th">ยังไม่มีงาน</span></div>';
    return;
  }
  list.innerHTML = jobs.map((job) => {
    const handoff = job.handoff_error
      ? { label: "handoff error", cls: "err" }
      : job.handoff_job_id
        ? { label: "child", cls: "child" }
        : (job.handoff_worker_id ? { label: "handoff armed", cls: "armed" } : null);
    return `
    <button class="job-row ${job.id === state.selectedJobId ? "selected" : ""}" type="button" data-job-id="${escapeHtml(job.id)}">
      <div class="job-row-top">
        <span class="status ${statusClass(job.state)} dot-only" aria-hidden="true"></span>
        <span class="status ${statusClass(job.state)}">${escapeHtml(statusLabel(job.state))}</span>
        <span class="job-row-ago">${escapeHtml(formatTime(job.created_at))}</span>
      </div>
      <div class="job-row-prompt">${escapeHtml(job.prompt)}</div>
      <div class="job-row-meta">
        <span>${escapeHtml(job.worker_name || shortId(job.worker_id) || "—")}</span><span>·</span><span class="mono">${escapeHtml(shortId(job.id))}</span>
        ${handoff ? `<span class="handoff-badge ${handoff.cls}">${escapeHtml(handoff.label)}</span>` : ""}
      </div>
    </button>`;
  }).join("");
}

function wfRunNodeTotal(run) {
  const definition = state.workflows.find((item) => item.id === run?.workflow_definition_id);
  const nodes = definition?.graph?.nodes;
  return Array.isArray(nodes) ? nodes.length : 0;
}
function wfRunProgress(run) {
  const done = (run?.counters?.completed_nodes || []).length;
  const total = wfRunNodeTotal(run) || done || 1;
  return { done, total, pct: Math.round((done / total) * 100) };
}
function wfElapsed(run) {
  if (!run?.created_at) return "—";
  const start = new Date(run.created_at).getTime();
  const end = new Date(run.updated_at || run.created_at).getTime();
  const minutes = Math.max(0, Math.round((end - start) / 60000));
  if (minutes < 1) return "under 1 min";
  if (minutes < 60) return `${minutes} min`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}
function wfBarColor(runState) {
  const cls = statusClass(runState);
  if (["failed", "recovery-required"].includes(cls)) return "var(--nt-danger)";
  if (cls === "succeeded") return "var(--nt-success)";
  return "var(--nt-yellow-500)";
}
// A node with a resolved job renders as a button (click → open that job in Jobs); a node
// without one stays a plain span. jobId/runId are escaped and only fed to openJobStream by id.
function nodeChip(stateName, label, jobId, runId) {
  const key = String(label);
  if (jobId) {
    return `<button class="nchip ${stateName} is-nav" type="button" data-node-job="${escapeHtml(jobId)}" data-node-run="${escapeHtml(runId || "")}" title="Open this node's job"><span class="d"></span>${escapeHtml(key)}<span class="njob">${escapeHtml(shortId(jobId))}</span></button>`;
  }
  return `<span class="nchip ${stateName}"><span class="d"></span>${escapeHtml(key)}</span>`;
}
function renderNodeChips(run) {
  const definition = state.workflows.find((item) => item.id === run.workflow_definition_id);
  const nodes = definition?.graph?.nodes;
  const counters = run.counters || {};
  const done = new Set(counters.completed_nodes || []);
  const failed = new Set(counters.failed_nodes || []);
  const waiting = new Set(state.approvals.filter((approval) => approval.run_id === run.id).map((approval) => approval.node_key));
  // node_key → job_id from the loaded run detail (nodes[] is the authoritative linkage).
  const jobByNode = {};
  if (state.workflowRunDetail?.run?.id === run.id) {
    for (const runtimeNode of state.workflowRunDetail.nodes || []) {
      if (runtimeNode.job_id) jobByNode[runtimeNode.node_key] = runtimeNode.job_id;
    }
  }
  if (!Array.isArray(nodes) || !nodes.length) {
    const chips = [...done].map((id) => nodeChip("done", id, jobByNode[id], run.id))
      .concat([...failed].map((id) => nodeChip("err", id, jobByNode[id], run.id)));
    return chips.length ? chips.join("") : '<span class="hint">No node data · <span lang="th">ไม่มีข้อมูลโหนด</span></span>';
  }
  let markedRun = false;
  return nodes.map((node) => {
    let stateName;
    if (done.has(node.id)) stateName = "done";
    else if (failed.has(node.id)) stateName = "err";
    else if (waiting.has(node.id)) stateName = "wait";
    else if (run.state === "running" && !markedRun) { stateName = "run"; markedRun = true; }
    else stateName = "pend";
    return nodeChip(stateName, node.id, jobByNode[node.id], run.id);
  }).join("");
}

function renderWorkflowRuns() {
  const runs = state.workflowRuns;
  const list = $("#workflowRunList");
  const legend = document.getElementById("monRunsLegend");
  if (legend) {
    const running = runs.filter((run) => run.state === "running").length;
    const waiting = runs.filter((run) => run.state === "waiting_for_human").length;
    legend.innerHTML = `<span><span class="d" style="background:var(--nt-yellow-500)"></span>${running} running</span><span><span class="d" style="background:var(--nt-warning)"></span>${waiting} waiting</span>`;
  }
  if (!selectionInside(list)) {
    if (!runs.length) {
      list.innerHTML = '<div class="empty">No workflow runs yet · <span lang="th">ยังไม่มี run</span></div>';
    } else {
      list.innerHTML = runs.slice(0, 20).map((run) => {
        const progress = wfRunProgress(run);
        return `
      <button class="run-row ${run.id === state.selectedWorkflowRunId ? "selected" : ""}" type="button" data-run-id="${escapeHtml(run.id)}">
        <div class="run-row-top">
          <span class="status ${statusClass(run.state)} dot-only" aria-hidden="true"></span>
          <span class="name">${escapeHtml(run.name)}</span>
          <span class="status ${statusClass(run.state)}">${escapeHtml(statusLabel(run.state))}</span>
        </div>
        <div class="run-bar"><div class="track"><div class="fill" style="width:${progress.pct}%;background:${wfBarColor(run.state)}"></div></div><span class="pct">${progress.pct}%</span></div>
        <div class="run-row-meta"><span class="mono">${escapeHtml(shortId(run.id))}</span><span>·</span><span>${escapeHtml(wfElapsed(run))}</span></div>
      </button>`;
      }).join("");
    }
  }

  const run = state.workflowRunDetail?.run;
  // No selection → hide the skeleton progress/stat scaffolding instead of showing empty bars.
  document.querySelector(".mon-detail")?.classList.toggle("is-empty", !run);
  const set = (id, fn) => { const node = document.getElementById(id); if (node) fn(node); };
  set("monRunName", (node) => { node.textContent = run ? run.name : "Select a run · เลือก run จากรายการ"; });
  set("monRunChip", (node) => { node.hidden = !run; if (run) { node.className = `status ${statusClass(run.state)}`; node.textContent = statusLabel(run.state); } });
  set("monRunId", (node) => { node.textContent = run ? run.id : ""; });
  // Show only the controls valid for the current state (prototype behaviour). Role gating
  // (applyRoleGate) still disables them for non-operators; this just hides the inapplicable ones.
  const showCtl = (id, show) => { const node = document.getElementById(id); if (node) node.hidden = !show; };
  const canControl = run && ["running", "paused"].includes(run.state);
  showCtl("pauseWorkflowRunBtn", run?.state === "running");
  showCtl("resumeWorkflowRunBtn", run?.state === "paused");
  showCtl("cancelWorkflowRunBtn", canControl);
  showCtl("retryInterruptedRunBtn", run?.state === "recovery_required");
  const recovery = run?.counters?.recovery;
  const recoveryEl = $("#workflowRecoveryWarning");
  recoveryEl.textContent = recovery ? `${recovery.warning} Interrupted: ${(recovery.interrupted || []).map((item) => `${item.node_key}${item.job_id ? ` (${item.job_id})` : ""}`).join(", ")}` : "";
  recoveryEl.hidden = !recovery;
  // Fields this run's definition declares that Atlas accepts but never acts on (e.g. a
  // collect_files on a node that runs no job). The server computes them — never re-derive the
  // rule here, or the console goes stale the day the rule grows. Shown on the run because that
  // is where the confusion lands: "the graph says collect these files, so where are they?"
  const definitionWarnings = state.workflows.find((item) => item.id === run?.workflow_definition_id)?.warnings || [];
  const definitionEl = $("#workflowDefinitionWarning");
  definitionEl.textContent = definitionWarnings.length
    ? `This workflow declares settings Atlas never acts on · ตั้งค่าที่ระบบไม่ได้ใช้: ${definitionWarnings.join(" · ")}`
    : "";
  definitionEl.hidden = !definitionWarnings.length;
  renderRunApprovals(run);

  const progress = run ? wfRunProgress(run) : { done: 0, total: 0, pct: 0 };
  set("monProgressBar", (node) => { node.style.transform = `scaleX(${progress.pct / 100})`; node.style.background = run ? wfBarColor(run.state) : "var(--nt-yellow-500)"; });
  set("monStats", (node) => {
    if (!run) { node.innerHTML = ""; return; }
    const counters = run.counters || {};
    const stats = [
      ["Nodes done", `${progress.done} / ${progress.total}`],
      ["Jobs", String(counters.jobs_started ?? 0)],
      ["Budget units", String(counters.budget_units_spent ?? 0)],
      ["Iteration", counters.iteration != null ? String(counters.iteration) : "—"],
      ["Elapsed", wfElapsed(run)],
    ];
    node.innerHTML = stats.map(([label, value]) => `<div class="mon-stat"><div class="l">${escapeHtml(label)}</div><div class="v">${escapeHtml(value)}</div></div>`).join("");
  });
  set("monNodeChips", (node) => { node.innerHTML = run ? renderNodeChips(run) : '<span class="hint">Select a run to see node progress · <span lang="th">เลือก run เพื่อดูโหนด</span></span>'; });
  set("workflowEventList", (node) => {
    // Events arrive seq ASC (oldest→newest); show the most RECENT 14 so mid/late-run events
    // (e.g. files_pushed) surface instead of only the run's first 14 setup events.
    node.innerHTML = state.workflowEvents.slice(-14).map((event) => {
      const type = event.event_type || "";
      const dot = /completed|succeeded|granted/.test(type) ? "ok" : /running|started/.test(type) ? "run" : /failed|error|rejected/.test(type) ? "err" : "";
      // T6: surface the file-handoff push details (count/bytes/target) inline rather than only
      // in the raw JSON. Numbers are coerced and the worker id escaped, so the detail is safe.
      const payload = event.payload || {};
      const detail = type === "files_pushed"
        ? ` · ${Number(payload.count ?? 0)} files · ${Number(payload.bytes ?? 0)} bytes → ${escapeHtml(String(payload.target_worker_id || ""))}`
        : "";
      return `<div class="tl-item"><span class="tl-dot ${dot}"></span><div><div class="ttl">${escapeHtml(type)}${event.node_key ? ` · ${escapeHtml(event.node_key)}` : ""}</div><div class="sub">${escapeHtml(formatTime(event.created_at))}${detail}</div></div></div>`;
    }).join("");
  });

  const counters = run?.counters || {};
  $("#workflowRunDetail").textContent = state.workflowRunDetail ? prettyJson({ completed_nodes: counters.completed_nodes || [], joins: counters.join_states || {}, ...state.workflowRunDetail }) : "";
  $("#workflowArtifactList").textContent = state.workflowArtifacts.length ? prettyJson(state.workflowArtifacts) : "";
  const files = state.workflowArtifacts.filter((artifact) => artifact.kind === "file_ref");
  $("#workflowArtifactDownloads").innerHTML = files.map((artifact) => `
    <button type="button" class="workflow-run-item download-artifact" data-artifact-id="${escapeHtml(artifact.id)}" data-filename="${escapeHtml(artifact.metadata?.filename || artifact.key)}">
      <span>${escapeHtml(artifact.metadata?.filename || artifact.key)}</span>
      <span class="item-sub">${escapeHtml(artifact.metadata?.size ?? 0)} bytes · ${escapeHtml(artifact.metadata?.sha256 || "")}</span>
    </button>`).join("");
  renderRunSummary();
}

// Pending human gates for the selected run. Approve continues the run; a gate that declares
// branch choices renders one button per choice (the API rejects a plain approve for those);
// Reject fails the whole run, so it goes through the two-step arm like Cancel.
function renderRunApprovals(run) {
  const box = document.getElementById("monApprovals");
  if (!box) return;
  const pending = run ? state.approvals.filter((approval) => approval.run_id === run.id) : [];
  const operator = ["admin", "operator"].includes(state.currentUser?.role);
  const dis = operator ? "" : " disabled";
  box.innerHTML = pending.map((approval) => {
    const choices = Array.isArray(approval.choices) ? approval.choices : [];
    const gateName = escapeHtml(approval.label || approval.node_key || "approval");
    const decide = choices.length
      ? choices.map((choice) => `<button class="primary-btn btn-sm approval-choose" type="button" data-approval-id="${escapeHtml(approval.id)}" data-choice="${escapeHtml(choice.id)}" aria-label="Choose ${escapeHtml(choice.label || choice.id)} for ${gateName}"${dis}>${escapeHtml(choice.label || choice.id)}</button>`).join("")
      : `<button class="primary-btn btn-sm approval-approve" type="button" data-approval-id="${escapeHtml(approval.id)}" aria-label="Approve ${gateName}"${dis}>Approve</button>`;
    const reason = approval.reason && approval.reason !== approval.label ? ` · ${escapeHtml(approval.reason)}` : "";
    return `
    <div class="approval-gate">
      <div class="grow">
        <div class="title">${escapeHtml(approval.label || approval.reason || "Waiting for approval")}</div>
        <div class="meta">${escapeHtml(approval.node_key || "")} · ${escapeHtml(formatTime(approval.created_at))}${reason}</div>
      </div>
      <div class="approval-actions">${decide}<button class="danger-btn btn-sm approval-reject" type="button" data-approval-id="${escapeHtml(approval.id)}" aria-label="Reject ${gateName}"${dis}>Reject</button></div>
    </div>`;
  }).join("");
}

function renderAudit() {
  const list = $("#auditList");
  const filter = state.auditFilter || "all";
  const text = (state.auditFilterText || "").trim().toLowerCase();
  let rows = state.audit;
  if (filter !== "all") {
    rows = rows.filter((entry) => (entry.resource_type || "").includes(filter) || (entry.action || "").includes(filter));
  }
  if (text) {
    rows = rows.filter((entry) =>
      [entry.action, entry.resource_id, entry.resource_type, entry.actor, JSON.stringify(entry.details ?? "")]
        .join(" ").toLowerCase().includes(text));
  }
  // A full page from the server means older entries likely exist — offer to widen the window.
  const more = document.getElementById("auditMoreBtn");
  if (more) more.hidden = state.audit.length < state.auditLimit;
  if (selectionInside(list)) return;
  if (!rows.length) {
    list.innerHTML = '<div class="empty">No matching entries · <span lang="th">ไม่มีรายการ</span></div>';
    return;
  }
  list.innerHTML = rows.map((entry) => {
    const action = entry.action || "";
    let tone = "grey";
    if (/fail|error|cancel|reject|refused|denied|delete/.test(action)) tone = "danger";
    else if (/grant|approv/.test(action)) tone = "brand";
    else if (/succeed|complete|online|bind/.test(action)) tone = "success";
    const details = entry.details && typeof entry.details === "object"
      ? Object.entries(entry.details).map(([key, value]) => `${key}=${value}`).join(" · ")
      : String(entry.details || "");
    // Cross-nav: route a row to its target view by the resource id/type. job_* → Jobs (open its
    // stream), wfr_* → Monitor (select that run), any worker resource → Fleet. Non-routable rows
    // stay plain divs. The id is escaped and only fed to openJobStream/selectWorkflowRun (which
    // fetch by id), so a bad/stale id degrades to a 404 handled below — never an injection.
    const rid = entry.resource_id || "";
    const rtype = entry.resource_type || "";
    let nav = "";
    if (/^job_/.test(rid)) nav = ` data-nav-view="jobs" data-nav-job="${escapeHtml(rid)}"`;
    else if (/^wfr_/.test(rid)) nav = ` data-nav-view="monitor" data-nav-run="${escapeHtml(rid)}"`;
    else if (rtype.includes("worker")) nav = ` data-nav-view="fleet"`;
    const tag = nav ? `button type="button"` : "div";
    return `
    <${tag} class="audit-row"${nav}>
      <span class="audit-time">${escapeHtml(formatTime(entry.created_at))}</span>
      <span class="audit-action ${tone}">${escapeHtml(action)}</span>
      <span class="audit-target">${escapeHtml(entry.resource_id || entry.resource_type || "")}</span>
      <span class="audit-detail">${escapeHtml(details)}</span>
      <span class="audit-actor"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>${escapeHtml(entry.actor || "system")}</span>
    </${nav ? "button" : "div"}>`;
  }).join("");
}

// Authenticated download: fetch with the Bearer header (never a token in the URL, which
// would leak into browser history and proxy logs), then save the blob locally.
async function downloadUsage(format) {
  const params = new URLSearchParams();
  const from = $("#usageFromInput").value.trim();
  const to = $("#usageToInput").value.trim();
  if (from) params.set("from", from);
  if (to) params.set("to", to);
  if (format) params.set("format", format);
  const headers = new Headers();
  const token = localStorage.getItem("atlasApiToken");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`${API_BASE}/api/usage?${params.toString()}`, { headers });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = format === "csv" ? "usage.csv" : "usage.json";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

// Audit CSV export mirrors downloadUsage: authenticated fetch (Bearer header, never a token
// in the URL), saved as a local blob. Exports the currently loaded window (auditLimit rows).
async function downloadAudit() {
  const headers = new Headers();
  const token = localStorage.getItem("atlasApiToken");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`${API_BASE}/api/audit?format=csv&limit=${state.auditLimit}`, { headers });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "audit.csv";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

// Authenticated artifact download: a plain <a href> can't attach the Bearer token, so an
// auth-enabled instance would 401. Fetch with the header (like downloadUsage) and save the blob.
async function downloadArtifact(artifactId, filename) {
  const headers = new Headers();
  const token = localStorage.getItem("atlasApiToken");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`${API_BASE}/api/artifacts/${encodeURIComponent(artifactId)}/content`, { headers });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename || "artifact";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

// T9a: a job's frozen Job Artifacts (file_ref artifacts keyed to the job). Standalone jobs have
// run_id NULL so they never show in the Monitor run-artifact list — surface them per job here.
// downloadArtifact uses the passed filename for the saved name, so pass the artifact's relpath.
async function loadJobArtifacts(jobId) {
  let files = [];
  try {
    const data = await api(`/api/jobs/${encodeURIComponent(jobId)}/artifacts`);
    files = (data.artifacts || []).filter((artifact) => artifact.kind === "file_ref");
  } catch {
    files = [];
  }
  if (state.selectedJobId !== jobId) return;  // the user switched jobs mid-fetch
  state.jobArtifacts = files;
  const box = $("#jobArtifactDownloads");
  if (!box) return;
  box.innerHTML = files.length
    ? files.map((artifact) => {
        const name = artifact.metadata?.relpath || artifact.metadata?.filename || artifact.key;
        return `<button type="button" class="workflow-run-item download-artifact" data-artifact-id="${escapeHtml(artifact.id)}" data-filename="${escapeHtml(name)}">
      <span>${escapeHtml(name)}</span>
      <span class="item-sub">${escapeHtml(artifact.metadata?.size ?? 0)} bytes · ${escapeHtml(artifact.metadata?.sha256 || "")}</span>
    </button>`;
      }).join("")
    : '<div class="empty">No files collected from this job yet · <span lang="th">ยังไม่มีไฟล์จากงานนี้</span></div>';
}

// Read-only run-count threshold alert. Derived purely from usage_events; never touches
// budget_units (which stays the per-run cost guard).
function renderUsageAlert(usedRuns) {
  const box = $("#usageAlert");
  const expected = Number($("#usageExpectedInput").value) || 0;
  const thresholdPct = Number($("#usageThresholdInput").value) || 80;
  if (expected <= 0) { box.hidden = true; return; }
  const ratioPct = Math.round((usedRuns / expected) * 1000) / 10;
  const tripped = ratioPct >= thresholdPct;
  box.hidden = false;
  box.classList.toggle("is-tripped", tripped);
  box.textContent = `${usedRuns} / ${expected} expected runs used (${ratioPct}%)`
    + (tripped ? ` — over ${thresholdPct}% threshold` : "");
}

async function loadUsage() {
  const from = $("#usageFromInput").value.trim();
  const to = $("#usageToInput").value.trim();
  const params = new URLSearchParams();
  if (from) params.set("from", from);
  if (to) params.set("to", to);
  const qs = params.toString();
  const data = await api(`/api/usage${qs ? "?" + qs : ""}`);
  const totals = data.totals || {};
  $("#usageRuns").textContent = totals.workflow_runs ?? 0;
  $("#usageJobs").textContent = totals.jobs ?? 0;
  $("#usageBudgetUnits").textContent = totals.budget_units ?? 0;
  // T1a token totals + T1b estimated cost. The estimate is deliberately labelled non-billable
  // (byok_token_counts_billable stays false server-side); show a plain "$0" when absent.
  const tokensPrompt = Number(totals.tokens_prompt ?? 0);
  const tokensOutput = Number(totals.tokens_output ?? 0);
  $("#usageTokens").textContent = `${tokensPrompt.toLocaleString()} · ${tokensOutput.toLocaleString()}`;
  const estCost = Number(totals.estimated_cost_usd ?? 0);
  // Sub-cent estimates would round to "$0.0000" under toFixed(4) though they're non-zero;
  // fall back to 2 significant figures below $0.0001 so a real tiny cost stays visible.
  $("#usageEstCost").textContent = !estCost
    ? "$0"
    : `$${estCost >= 0.0001 ? estCost.toFixed(4) : estCost.toPrecision(2)}`;
  $("#usageMeta").textContent = (data.from || data.to)
    ? `range ${data.from || "…"} → ${data.to || "…"}`
    : "all time";
  renderUsageBars(data.usage || []);
  renderUsageQuota(totals.workflow_runs ?? 0);
  renderUsageEvents(data.usage || []);
  renderUsageAlert(totals.workflow_runs ?? 0);
}

const USAGE_DOW = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];

function renderUsageBars(events) {
  const box = document.getElementById("usageBars");
  if (!box) return;
  const counts = {};
  for (const event of events) {
    if (event.kind !== "workflow_run") continue;
    const day = String(event.created_at || "").slice(0, 10);
    if (day) counts[day] = (counts[day] || 0) + 1;
  }
  const days = Object.keys(counts).sort().slice(-7);
  if (!days.length) { box.innerHTML = '<div class="hint">No runs in this range · <span lang="th">ไม่มี run ในช่วงนี้</span></div>'; return; }
  const max = Math.max(1, ...days.map((day) => counts[day]));
  box.innerHTML = days.map((day) => {
    const value = counts[day];
    const pct = Math.round((value / max) * 100);
    const label = USAGE_DOW[new Date(day).getDay()] || day.slice(5);
    return `<div class="usage-bar"><span class="n">${value}</span><div class="track"><div class="fill${value === max ? " peak" : ""}" style="height:${pct}%"></div></div><span class="d">${escapeHtml(label)}</span></div>`;
  }).join("");
}

function renderUsageQuota(usedRuns) {
  const box = document.getElementById("usageQuota");
  if (!box) return;
  const expected = Number($("#usageExpectedInput").value) || 0;
  const threshold = Number($("#usageThresholdInput").value) || 80;
  if (expected <= 0) {
    box.innerHTML = '<p class="hint" style="margin-top:14px">Enter Expected runs to see quota and alerts · <span lang="th">ใส่ Expected runs เพื่อดูโควตา</span></p>';
    return;
  }
  const pct = Math.round((usedRuns / expected) * 100);
  const over = pct >= threshold;
  box.innerHTML = `
    <div class="usage-quota-val"><b>${usedRuns}</b><span>/ ${expected} expected runs</span></div>
    <div class="usage-quota-track"><div class="usage-quota-fill ${over ? "over" : ""}" style="width:${Math.min(100, pct)}%"></div></div>
    <div class="usage-quota-note ${over ? "over" : ""}"><span class="d"></span><span><b>${pct}%</b> of target · ${over ? `over the ${threshold}% alert threshold` : `under the ${threshold}% alert threshold`}</span></div>`;
}

function renderUsageEvents(events) {
  const box = document.getElementById("usageEvents");
  if (!box) return;
  if (!events.length) { box.innerHTML = '<div class="empty">No events in this range · <span lang="th">ไม่มีเหตุการณ์</span></div>'; return; }
  box.innerHTML = events.slice(0, 30).map((event) => `
    <div class="ue-row">
      <span>${escapeHtml(event.kind || "—")}</span>
      <span class="ref">${escapeHtml(event.reference || event.resource_id || event.idempotency_key || "—")}</span>
      <span>${escapeHtml(event.units ?? 0)}</span>
      <span class="secs">${escapeHtml(event.seconds ?? 0)}</span>
      <span>${escapeHtml(formatTime(event.created_at))}</span>
    </div>`).join("");
}

function openJobStream(jobId) {
  state.selectedJobId = jobId;
  state.streamText = "";
  state.events = [];
  if (state.eventSource) state.eventSource.close();
  // Authenticated SSE over fetch. EventSource cannot set an Authorization header, which forced
  // the API token into the URL (?token=...) where the reverse-proxy access log captures it. A
  // streamed fetch sends the Bearer header instead, keeping the token out of URLs and logs.
  const controller = new AbortController();
  state.eventSource = { close: () => controller.abort() };
  updateStreamHeader();
  $("#streamOutput").textContent = "";
  $("#eventList").innerHTML = "";
  $("#jobArtifactDownloads").innerHTML = '<div class="empty">No files collected from this job yet · <span lang="th">ยังไม่มีไฟล์จากงานนี้</span></div>';
  renderToolTimeline();
  renderJobs();
  // Load any already-collected files now (revisiting a finished job); collection for a still-
  // running job resolves at terminal, so refresh again on the stream `close` below.
  loadJobArtifacts(jobId).catch(() => {});

  let sawClose = false;
  const safeJson = (data) => { try { return JSON.parse(data); } catch { return { data }; } };
  const dispatch = (name, data) => {
    if (name === "text") {
      const payload = safeJson(data);
      state.streamText += payload.text || "";
      $("#streamOutput").textContent = state.streamText;
      $("#streamOutput").scrollTop = $("#streamOutput").scrollHeight;
    } else if (name === "close") {
      sawClose = true;
      appendEvent("close", safeJson(data));
      loadAll().catch((error) => toast(error.message));
      loadJobArtifacts(jobId).catch(() => {});  // collection resolved at terminal — pick up the files
    } else {
      // Every other frame — known structured events (tool_*/skill_*/thinking/…) AND unknown
      // future names — becomes a generic event entry; buildToolTimeline() picks out the
      // tool/skill ones. An unknown name must never crash the view, so parse defensively.
      appendEvent(name, safeJson(data));
    }
  };

  (async () => {
    const headers = new Headers({ Accept: "text/event-stream" });
    const token = localStorage.getItem("atlasApiToken");
    if (token) headers.set("Authorization", `Bearer ${token}`);
    try {
      const response = await fetch(`${API_BASE}/api/jobs/${jobId}/events?after=0`, { headers, signal: controller.signal });
      if (!response.ok || !response.body) throw new Error(`stream failed (${response.status})`);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let sep;
        while ((sep = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, sep);
          buffer = buffer.slice(sep + 2);
          let name = "message";
          const dataLines = [];
          for (const line of frame.split("\n")) {
            if (line.startsWith("event:")) name = line.slice(6).trim();
            else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
          }
          if (dataLines.length) dispatch(name, dataLines.join("\n"));
        }
      }
      // The server always sends a `close` event before ending the stream. Reaching EOF
      // without it means the connection dropped mid-stream — surface it instead of leaving
      // partial output looking complete.
      if (!sawClose) appendEvent("stream", { error: "event stream disconnected" });
    } catch (error) {
      if (error.name !== "AbortError") appendEvent("stream", { error: error.message || "event stream disconnected" });
    }
  })();
}

function appendEvent(type, payload) {
  state.events.unshift({ type, payload });
  $("#eventList").innerHTML = state.events.slice(0, 30).map((entry) => `
    <article class="event-item">
      <div class="event-type">${escapeHtml(entry.type)}</div>
      <pre class="event-payload">${escapeHtml(JSON.stringify(entry.payload, null, 2))}</pre>
    </article>
  `).join("");
  renderToolTimeline();
}

const TOOL_START_KIND = { tool_use_start: "tool", skill_invoked: "skill" };
const TOOL_END_EVENTS = new Set(["tool_use_result", "skill_invoked_result", "tool_use_denied"]);

// Pure: fold a job's structured events into an ordered tool/skill call timeline built from
// STRUCTURAL METADATA ONLY (name, status, byte sizes, hashes — never payloads, which Atlas does
// not store). Pairs *_start / *_invoked with their *_result / *_denied by id. Exposed separately
// from rendering so it is unit-testable without a DOM; unknown event types are simply ignored.
function buildToolTimeline(events) {
  const ordered = (events || [])
    .filter((entry) => TOOL_START_KIND[entry.type] || TOOL_END_EVENTS.has(entry.type))
    .slice()
    .sort((a, b) => ((a.payload || {}).seq || 0) - ((b.payload || {}).seq || 0));
  const calls = [];
  const byId = new Map();
  for (const entry of ordered) {
    const p = entry.payload || {};
    const id = p.id != null ? String(p.id) : null;
    if (TOOL_START_KIND[entry.type]) {
      const call = {
        id, kind: TOOL_START_KIND[entry.type], name: p.name != null ? String(p.name) : "",
        status: p.status || "started", started_at: p.created_at || null, finished_at: null,
        duration_ms: null, input_bytes: p.input_bytes ?? null, output_bytes: null,
        input_sha256: p.input_sha256 || null, output_sha256: null,
      };
      calls.push(call);
      if (id) byId.set(id, call);
      continue;
    }
    // result / denied: attach to its start by id, or stand alone if the start was never seen.
    let call = id && byId.has(id) ? byId.get(id) : null;
    if (!call) {
      call = { id, kind: entry.type.startsWith("skill") ? "skill" : "tool", name: p.name != null ? String(p.name) : "",
        status: null, started_at: null, finished_at: null, duration_ms: null,
        input_bytes: null, output_bytes: null, input_sha256: null, output_sha256: null };
      calls.push(call);
    }
    call.status = p.status || (entry.type === "tool_use_denied" ? "denied" : "ok");
    call.finished_at = p.created_at || call.finished_at;
    call.output_bytes = p.output_bytes ?? call.output_bytes;
    call.output_sha256 = p.output_sha256 || call.output_sha256;
    if (call.started_at && call.finished_at) {
      const ms = Date.parse(call.finished_at) - Date.parse(call.started_at);
      call.duration_ms = Number.isFinite(ms) && ms >= 0 ? ms : null;
    }
  }
  return calls;
}

function toolCounters(calls) {
  return {
    run: calls.length,
    denied: calls.filter((call) => call.status === "denied").length,
    failed: calls.filter((call) => call.status === "error").length,
  };
}

function toolDotClass(status) {
  return status === "ok" ? "ok" : status === "denied" ? "denied" : status === "error" ? "err" : "run";
}

function renderToolTimeline() {
  const calls = buildToolTimeline(state.events);
  const counts = toolCounters(calls);
  const countersNode = document.getElementById("toolCounters");
  if (countersNode) {
    countersNode.innerHTML = `
      <span class="tl-count">tools · <span lang="th">เครื่องมือ</span> <b>${counts.run}</b></span>
      <span class="tl-count denied">denied · <span lang="th">ปฏิเสธ</span> <b>${counts.denied}</b></span>
      <span class="tl-count err">failed · <span lang="th">ล้มเหลว</span> <b>${counts.failed}</b></span>`;
  }
  const listNode = document.getElementById("toolTimelineList");
  if (!listNode) return;
  if (!calls.length) {
    listNode.innerHTML = `<div class="empty">No tool calls yet · <span lang="th">ยังไม่มีการเรียกเครื่องมือ</span></div>`;
    return;
  }
  listNode.innerHTML = calls.map((call) => {
    // Tool/skill NAME is worker-controlled: escape it AND length-cap it (defence in depth).
    const name = escapeHtml((call.name || "(unnamed)").slice(0, 80));
    const status = call.status || "started";
    const bytes = [
      call.input_bytes != null ? `in ${call.input_bytes}B` : "",
      call.output_bytes != null ? `out ${call.output_bytes}B` : "",
    ].filter(Boolean).join(" · ");
    const dur = call.duration_ms != null ? `${call.duration_ms} ms` : (status === "started" ? "running…" : "");
    const hash = call.output_sha256 || call.input_sha256;
    // sub is built from structural fields only (kind, numbers, hex hash) — no worker text.
    const sub = [call.kind, dur, bytes, hash ? `sha ${hash.slice(0, 12)}` : ""].filter(Boolean).join(" · ");
    return `
      <div class="tl-item" data-tool-status="${escapeHtml(status)}" data-tool-kind="${escapeHtml(call.kind)}">
        <span class="tl-dot ${toolDotClass(status)}"></span>
        <div class="grow">
          <div class="ttl">${name} <span class="tl-badge ${toolDotClass(status)}">${escapeHtml(status)}</span></div>
          <div class="sub">${escapeHtml(sub)}</div>
        </div>
      </div>`;
  }).join("");
}

function updateStreamHeader() {
  const job = state.jobs.find((item) => item.id === state.selectedJobId);
  const st = statusClass(job?.state || "queued");
  const set = (id, fn) => { const node = document.getElementById(id); if (node) fn(node); };
  set("jobDetailDot", (n) => { n.className = `status ${st} dot-only`; });
  set("jobDetailChip", (n) => { n.className = `status ${st}`; n.textContent = job ? statusLabel(job.state) : "—"; });
  set("jobDetailId", (n) => { n.textContent = job ? job.id : ""; });
  set("jobDetailPrompt", (n) => { n.textContent = job ? job.prompt : "Select a job to watch its live stream · เลือกงานเพื่อดูสตรีมสด"; });
  set("jobDetailRoute", (n) => {
    n.textContent = job
      ? `${job.route_reason || `${job.worker_name || shortId(job.worker_id)} · ${job.workspace_key || "auto"}`}${job.handoff_job_id ? ` · handoff ${shortId(job.handoff_job_id)}` : ""}`
      : "";
  });
  // When the job belongs to a workflow run, surface a chip that jumps to that run in Monitor.
  set("jobDetailRunChip", (n) => {
    const runId = job?.run_id;
    n.hidden = !runId;
    if (runId) {
      n.dataset.runId = runId;
      const name = state.workflowRuns.find((run) => run.id === runId)?.name || shortId(runId);
      n.textContent = `${shortId(runId)} · ${name} →`;
    }
  });
  const live = !!job && ["queued", "running", "cancel_requested"].includes(job.state);
  const operator = ["admin", "operator"].includes(state.currentUser?.role);
  set("cancelJobBtn", (n) => { n.disabled = !job || !operator || !["queued", "running", "cancel_requested"].includes(job.state); });
  set("streamOutput", (n) => { n.classList.toggle("stream-live", live); });
}

async function saveWorker(event) {
  event.preventDefault();
  const formElement = event.currentTarget || event.target.closest("form");
  if (!formElement) throw new Error("Worker form is not available; refresh the page");
  const submitBtn = $("#workerSubmitBtn");
  submitBtn.disabled = true;
  try {
    const form = new FormData(formElement);
    const payload = Object.fromEntries(form.entries());
    const data = await api("/api/workers", { method: "POST", body: JSON.stringify(payload) });
    formElement.reset();
    closeModals();
    toast("Worker saved; polling status");
    await api(`/api/workers/${data.worker.id}/poll`, { method: "POST" });
    await loadAll();
    const worker = state.workers.find((item) => item.id === data.worker.id);
    toast(`Worker saved · ${worker?.status || "unknown"}`);
  } finally {
    submitBtn.disabled = false;
  }
}

async function saveWorkspace(event) {
  event.preventDefault();
  const formElement = event.currentTarget || event.target.closest("form");
  if (!formElement) throw new Error("Workspace form is not available; refresh the page");
  const submitBtn = $("#workspaceSubmitBtn");
  submitBtn.disabled = true;
  try {
    const form = new FormData(formElement);
    const payload = Object.fromEntries(form.entries());
    await api("/api/workspaces", { method: "POST", body: JSON.stringify(payload) });
    formElement.reset();
    closeModals();
    toast("Workspace saved");
    await loadAll();
  } finally {
    submitBtn.disabled = false;
  }
}

async function pollWorker(workerId) {
  await api(`/api/workers/${workerId}/poll`, { method: "POST" });
  await loadAll();
  const worker = state.workers.find((item) => item.id === workerId);
  toast(`${worker?.name || "Worker"} is ${worker?.status || "unknown"}`);
}

// T4: enabling tunnel/forward_auth runs a server-side pre-enable sync probe; a rejected probe
// (400) leaves the mode unchanged, so on any error we reload to snap the select back to truth.
async function setSyncMode(workerId, mode) {
  await api(`/api/workers/${workerId}/sync-mode`, { method: "POST", body: JSON.stringify({ sync_mode: mode }) });
  await loadAll();
  toast(`Sync mode → ${mode}`);
}

async function cancelSelectedJob() {
  if (!state.selectedJobId) return;
  await api(`/api/jobs/${state.selectedJobId}/cancel`, { method: "POST" });
  toast("Cancel requested");
  await loadAll();
}

async function selectWorkflowRun(runId) {
  state.selectedWorkflowRunId = runId;
  await loadWorkflowRunDetail(runId);
  renderWorkflowRuns();
}

async function loadWorkflowRunDetail(runId) {
  const [detail, artifacts, events] = await Promise.all([
    api(`/api/workflow-runs/${runId}`),
    api(`/api/workflow-runs/${runId}/artifacts`),
    api(`/api/workflow-runs/${runId}/events`),
  ]);
  state.workflowRunDetail = detail;
  state.workflowRuns = state.workflowRuns.map((run) => run.id === runId ? detail.run : run);
  state.workflowArtifacts = artifacts.artifacts || [];
  state.workflowEvents = events.events || [];
  state.approvals = [
    ...state.approvals.filter((approval) => approval.run_id !== runId),
    ...(detail.approvals || []).filter((approval) => approval.state === "pending"),
  ];
}

async function controlWorkflowRun(action) {
  if (!state.selectedWorkflowRunId) return;
  await api(`/api/workflow-runs/${state.selectedWorkflowRunId}/${action}`, { method: "POST" });
  await loadAll();
  toast(`Workflow ${action} requested`);
}

// Two-step arm for irreversible controls: the first click arms the button for 4 seconds
// (solid danger + confirm label), a second click within the window fires the action. A stray
// click reverts on its own — cancelling an hours-long run should never be a single mis-click.
function armedClick(button, armedLabel, action) {
  if (button.dataset.armed) {
    clearTimeout(button.armTimer);
    disarmButton(button, { silent: true }); // action() below announces its own result
    action();
    return;
  }
  button.dataset.armed = "1";
  button.dataset.restoreLabel = button.textContent;
  button.textContent = armedLabel;
  button.classList.add("is-armed");
  announce(`${armedLabel} — press again to confirm`);
  button.armTimer = setTimeout(() => disarmButton(button), 4000);
}

// silent=true is the confirmed-click path (action() is about to announce its own result);
// the default (timeout) path announces, since the arm otherwise expires with no SR signal
// that the confirmation window closed and nothing happened (WCAG 2.2.1 timing concern).
function disarmButton(button, { silent = false } = {}) {
  if (!button.dataset.armed) return;
  const label = button.dataset.restoreLabel || button.textContent;
  delete button.dataset.armed;
  button.classList.remove("is-armed");
  button.textContent = label;
  delete button.dataset.restoreLabel;
  if (!silent) announce(`${label} — confirmation window closed, not confirmed`);
}

async function decideApproval(approvalId, action, body) {
  await api(`/api/approvals/${encodeURIComponent(approvalId)}/${action}`, {
    method: "POST",
    body: body ? JSON.stringify(body) : undefined,
  });
  await loadAll();
}

async function retryInterruptedRun() {
  if (!state.selectedWorkflowRunId) return;
  const warning = state.workflowRunDetail?.run?.counters?.recovery?.warning || "Retry may duplicate worker side effects.";
  if (!(await confirmDialog({
    title: "Retry interrupted nodes",
    body: `${warning}\nAuthorize retry of interrupted nodes? · อนุญาตให้รันโหนดที่ค้างใหม่?`,
    confirmLabel: "Authorize retry",
  }))) return;
  await api(`/api/workflow-runs/${state.selectedWorkflowRunId}/resume`, {
    method: "POST",
    body: JSON.stringify({ retry_interrupted: true }),
  });
  await loadAll();
  toast("Interrupted-node retry authorized");
}

let lastModalTrigger = null;

function openWorkerModal(worker = null) {
  lastModalTrigger = document.activeElement;
  const form = $("#workerForm");
  form.reset();
  form.elements.id.value = worker?.id || "";
  form.elements.name.value = worker?.name || "";
  form.elements.base_url.value = worker?.base_url || "";
  form.elements.token.value = "";
  form.elements.role.value = worker?.role || "";
  form.elements.tags.value = tagsToText(worker?.tags);
  $("#workerFormTitle").textContent = worker ? "Edit Worker" : "Add Worker";
  $("#workerSubmitBtn").textContent = worker ? "Save Changes" : "Save Worker";
  $("#workerModal").hidden = false;
  form.elements.name.focus();
}

function openWorkspaceModal(workspace = null) {
  lastModalTrigger = document.activeElement;
  if (!state.workers.length) {
    toast("Add a worker first");
    return;
  }
  const form = $("#workspaceForm");
  form.reset();
  form.elements.id.value = workspace?.id || "";
  form.elements.worker_id.value = workspace?.worker_id || state.workers[0].id;
  form.elements.workspace_key.value = workspace?.workspace_key || "";
  form.elements.workspace_dir.value = workspace?.workspace_dir || "";
  form.elements.company.value = workspace?.company || "";
  form.elements.tags.value = tagsToText(workspace?.tags);
  $("#workspaceFormTitle").textContent = workspace ? "Edit Workspace" : "Add Workspace";
  $("#workspaceSubmitBtn").textContent = workspace ? "Save Changes" : "Save Workspace";
  $("#workspaceModal").hidden = false;
  form.elements.workspace_key.focus();
}

// Styled confirm dialog replacing native confirm(): destructive flows share one visual
// language with the rest of the console, render bilingual copy properly (pre-line), and
// reuse the existing modal focus trap + Escape handling. Resolves false on any dismiss.
let confirmResolve = null;
function confirmDialog({ title, body, confirmLabel = "Confirm" }) {
  lastModalTrigger = document.activeElement;
  $("#confirmTitle").textContent = title;
  $("#confirmBody").textContent = body;
  $("#confirmOkBtn").textContent = confirmLabel;
  $("#confirmModal").hidden = false;
  $("#confirmOkBtn").focus();
  return new Promise((resolve) => { confirmResolve = resolve; });
}
function settleConfirm(result) {
  if (!confirmResolve) return;
  const resolve = confirmResolve;
  confirmResolve = null;
  resolve(result);
}

function closeModals() {
  const wasOpen = !$("#workerModal").hidden || !$("#workspaceModal").hidden || !$("#confirmModal").hidden;
  $("#workerModal").hidden = true;
  $("#workspaceModal").hidden = true;
  $("#confirmModal").hidden = true;
  settleConfirm(false);
  if (wasOpen) {
    // Return focus to the control that opened the modal (a11y); fall back to the content
    // region if that trigger was re-rendered away by a poll while the modal was open.
    const target = lastModalTrigger && lastModalTrigger.isConnected ? lastModalTrigger : document.getElementById("main");
    target?.focus?.();
  }
  lastModalTrigger = null;
}

async function deleteWorker(workerId) {
  const worker = state.workers.find((item) => item.id === workerId);
  if (!(await confirmDialog({
    title: "Delete worker",
    body: `Delete ${worker?.name || workerId}? Its workspaces are removed too. Workers with job history are kept for audit and cannot be deleted.\nลบ worker นี้? workspace ของมันจะถูกลบด้วย`,
    confirmLabel: "Delete worker",
  }))) return;
  await api(`/api/workers/${workerId}`, { method: "DELETE" });
  if (state.selectedJobId) state.selectedJobId = null;
  toast("Worker deleted");
  await loadAll();
}

async function deleteWorkspace(workspaceId) {
  const workspace = state.workspaces.find((item) => item.id === workspaceId);
  if (!(await confirmDialog({
    title: "Delete workspace",
    body: `Delete workspace ${workspace?.workspace_key || workspaceId}?\nลบ workspace นี้?`,
    confirmLabel: "Delete workspace",
  }))) return;
  await api(`/api/workspaces/${workspaceId}`, { method: "DELETE" });
  toast("Workspace deleted");
  await loadAll();
}

// ----- Health chip · Accounts -----

let lastHealth = null;
function setHealth(ok) {
  const chip = document.getElementById("healthChip");
  const text = document.getElementById("healthChipText");
  if (!chip || !text) return;
  chip.classList.toggle("err", !ok);
  text.textContent = ok ? "Connected · ปกติ" : "Connection lost · เชื่อมต่อไม่ได้";
  if (lastHealth !== null && lastHealth !== ok) announce(ok ? "Connection restored" : "Connection lost");
  lastHealth = ok;
}

async function loadAccounts() {
  if (state.currentUser?.role !== "admin") return;
  const [users, tokens] = await Promise.all([api("/api/users"), api("/api/tokens")]);
  state.users = users.users || [];
  state.apiTokens = tokens.tokens || [];
  renderAccounts();
}

function renderAccounts() {
  const userList = document.getElementById("userList");
  const tokenList = document.getElementById("tokenList");
  const tokenUserSelect = document.getElementById("tokenUserSelect");
  if (!userList || !tokenList) return;
  userList.innerHTML = state.users.length ? state.users.map((user) => `
    <article class="workflow-run-item">
      <div class="item-title">
        <span>${escapeHtml(user.username)}</span>
        <span class="role-chip">${escapeHtml(user.role)}</span>
      </div>
      <div class="item-sub">${escapeHtml(user.status)} · tokens ${Number(user.token_count) || 0} · created ${escapeHtml(formatTime(user.created_at))}</div>
      <div class="item-actions">
        <button class="preview-btn toggle-user-status" type="button" data-user-id="${escapeHtml(user.id)}" data-next-status="${user.status === "active" ? "disabled" : "active"}" aria-label="${user.status === "active" ? "Disable" : "Enable"} user ${escapeHtml(user.username)}" ${user.id === state.currentUser?.id ? "disabled" : ""}>${user.status === "active" ? "Disable" : "Enable"}</button>
        <button class="preview-btn danger-mini delete-user" type="button" data-user-id="${escapeHtml(user.id)}" aria-label="Delete user ${escapeHtml(user.username)}" ${user.id === state.currentUser?.id ? "disabled" : ""}>Delete</button>
      </div>
    </article>
  `).join("") : '<div class="empty">No users yet · <span lang="th">ยังไม่มีผู้ใช้</span></div>';
  if (tokenUserSelect) {
    // Preserve the admin's picked target across re-renders (a status toggle or revoke
    // refreshes the list); falling back silently to the current user would mint the
    // next token for the wrong account.
    const previous = tokenUserSelect.value;
    tokenUserSelect.innerHTML = state.users.map((user) =>
      `<option value="${escapeHtml(user.id)}">${escapeHtml(user.username)}</option>`).join("");
    const desired = previous || state.currentUser?.id || "";
    if ([...tokenUserSelect.options].some((option) => option.value === desired)) tokenUserSelect.value = desired;
  }
  tokenList.innerHTML = state.apiTokens.length ? state.apiTokens.map((token) => `
    <article class="workflow-run-item ${token.revoked_at ? "is-revoked" : ""}">
      <div class="item-title">
        <span>${escapeHtml(token.name || "(unnamed)")}</span>
        <span class="item-sub">${escapeHtml(token.username || "")}</span>
      </div>
      <div class="item-sub">created ${escapeHtml(formatTime(token.created_at))} · last used ${token.last_used_at ? escapeHtml(formatTime(token.last_used_at)) : "—"}${token.revoked_at ? " · revoked" : ""}</div>
      ${token.revoked_at ? "" : `<button class="preview-btn danger-mini revoke-token" type="button" data-token-id="${escapeHtml(token.id)}" aria-label="Revoke token ${escapeHtml(token.name || "(unnamed)")}">Revoke</button>`}
    </article>
  `).join("") : '<div class="empty">No tokens yet · <span lang="th">ยังไม่มี token</span></div>';
}

async function createUser(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submitBtn = form.querySelector('button[type="submit"]');
  submitBtn.disabled = true;
  try {
    await api("/api/users", { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(form).entries())) });
    form.reset();
    toast("User added · เพิ่มผู้ใช้แล้ว");
    await loadAccounts();
  } finally {
    submitBtn.disabled = false;
  }
}

async function createToken(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submitBtn = form.querySelector('button[type="submit"]');
  submitBtn.disabled = true;
  try {
    const data = await api("/api/tokens", { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(form).entries())) });
    form.reset();
    const reveal = document.getElementById("newTokenReveal");
    if (reveal) {
      reveal.hidden = false;
      reveal.innerHTML = `<div class="token-reveal-title">New token — shown only once, copy it now · <span lang="th">แสดงครั้งเดียว คัดลอกเก็บทันที</span></div>
        <code>${escapeHtml(data.api_token)}</code>
        <button class="preview-btn" type="button" data-copy="${escapeHtml(data.api_token)}">Copy</button>`;
    }
    await loadAccounts();
  } finally {
    submitBtn.disabled = false;
  }
}

document.addEventListener("change", async (event) => {
  const syncSelect = event.target.closest(".sync-mode-select");
  if (syncSelect) {
    await setSyncMode(syncSelect.dataset.workerId, syncSelect.value).catch((error) => {
      toast(error.message);
      loadAll();  // rejected pre-enable probe left the mode unchanged; reset the select to truth.
    });
  }
});

document.addEventListener("click", async (event) => {
  if (event.target.closest("[data-close-modal]")) {
    closeModals();
    return;
  }
  if (event.target.closest("#staleRetryBtn")) {
    await loadAll().catch((error) => { setStale(error); toast(error.message); });
    return;
  }
  const copyButton = event.target.closest("[data-copy]");
  if (copyButton) {
    try {
      await navigator.clipboard.writeText(copyButton.dataset.copy);
      toast("Copied · คัดลอกแล้ว");
    } catch {
      toast("Copy failed · คัดลอกไม่สำเร็จ");
    }
    return;
  }
  const pwToggle = event.target.closest(".pw-toggle");
  if (pwToggle) {
    const input = pwToggle.previousElementSibling;
    const noun = pwToggle.getAttribute("aria-label").replace(/^(Show|Hide) /, "");
    const wasShown = input.type === "text";
    input.type = wasShown ? "password" : "text";
    pwToggle.querySelector(".pw-icon-open").hidden = !wasShown;
    pwToggle.querySelector(".pw-icon-closed").hidden = wasShown;
    pwToggle.setAttribute("aria-pressed", String(!wasShown));
    pwToggle.setAttribute("aria-label", `${wasShown ? "Show" : "Hide"} ${noun}`);
    return;
  }
  const editWorkerButton = event.target.closest(".edit-worker");
  if (editWorkerButton) {
    const worker = state.workers.find((item) => item.id === editWorkerButton.dataset.workerId);
    openWorkerModal(worker);
    return;
  }
  const deleteWorkerButton = event.target.closest(".delete-worker");
  if (deleteWorkerButton) {
    await deleteWorker(deleteWorkerButton.dataset.workerId).catch((error) => toast(error.message));
    return;
  }
  const editWorkspaceButton = event.target.closest(".edit-workspace");
  if (editWorkspaceButton) {
    const workspace = state.workspaces.find((item) => item.id === editWorkspaceButton.dataset.workspaceId);
    openWorkspaceModal(workspace);
    return;
  }
  const deleteWorkspaceButton = event.target.closest(".delete-workspace");
  if (deleteWorkspaceButton) {
    await deleteWorkspace(deleteWorkspaceButton.dataset.workspaceId).catch((error) => toast(error.message));
    return;
  }
  const pollButton = event.target.closest(".poll-worker");
  if (pollButton) {
    await pollWorker(pollButton.dataset.workerId).catch((error) => toast(error.message));
    return;
  }
  const downloadArtifactButton = event.target.closest(".download-artifact");
  if (downloadArtifactButton) {
    await downloadArtifact(downloadArtifactButton.dataset.artifactId, downloadArtifactButton.dataset.filename).catch((error) => toast(error.message));
    return;
  }
  const runRow = event.target.closest(".run-row, .workflow-run-item[data-run-id]");
  if (runRow && runRow.dataset.runId) {
    await selectWorkflowRun(runRow.dataset.runId).catch((error) => toast(error.message));
    return;
  }
  const toggleUserButton = event.target.closest(".toggle-user-status");
  if (toggleUserButton) {
    await api(`/api/users/${encodeURIComponent(toggleUserButton.dataset.userId)}`, { method: "PUT", body: JSON.stringify({ status: toggleUserButton.dataset.nextStatus }) })
      .then(() => loadAccounts())
      .catch((error) => toast(error.message));
    return;
  }
  const deleteUserButton = event.target.closest(".delete-user");
  if (deleteUserButton) {
    if (!(await confirmDialog({
      title: "Delete user",
      body: "Delete this user? All of their API tokens are removed too.\nลบผู้ใช้นี้? token ทั้งหมดของผู้ใช้จะถูกลบด้วย",
      confirmLabel: "Delete user",
    }))) return;
    await api(`/api/users/${encodeURIComponent(deleteUserButton.dataset.userId)}`, { method: "DELETE" })
      .then(() => loadAccounts())
      .catch((error) => toast(error.message));
    return;
  }
  const revokeTokenButton = event.target.closest(".revoke-token");
  if (revokeTokenButton) {
    // Dashboard sessions are themselves tokens (named "dashboard login") and look identical —
    // warn before revoking, because killing the current session's token logs you out instantly.
    if (!(await confirmDialog({
      title: "Revoke token",
      body: "Revoke this token? Revoking your current session's token signs you out immediately.\nเพิกถอน token นี้? ถ้าเป็นของเซสชันปัจจุบัน คุณจะหลุดจากระบบทันที",
      confirmLabel: "Revoke token",
    }))) return;
    await api(`/api/tokens/${encodeURIComponent(revokeTokenButton.dataset.tokenId)}`, { method: "DELETE" })
      .then(() => loadAccounts())
      .catch((error) => toast(error.message));
    return;
  }
  const jobItem = event.target.closest(".job-row, .dash-job");
  if (jobItem) {
    if (jobItem.classList.contains("dash-job")) {
      // Recent jobs on Overview should open the same selected detail in Jobs, even when a
      // previous visit left the list filtered to a specific workflow run.
      state.jobRunFilter = "all";
      showView("jobs");
    }
    openJobStream(jobItem.dataset.jobId);
  }
});

// Jobs: stream / events tab toggle. ARIA tabs pattern: aria-selected + roving tabindex,
// arrow keys move and activate (screen readers announce "tab, 2 of 4, selected").
for (const tab of document.querySelectorAll(".tab[data-job-tab]")) {
  tab.addEventListener("click", () => {
    for (const other of document.querySelectorAll(".tab[data-job-tab]")) {
      const active = other === tab;
      other.classList.toggle("is-active", active);
      other.setAttribute("aria-selected", String(active));
      other.tabIndex = active ? 0 : -1;
    }
    for (const pane of document.querySelectorAll("[data-job-pane]")) pane.hidden = pane.dataset.jobPane !== tab.dataset.jobTab;
  });
}
document.querySelector(".tabs")?.addEventListener("keydown", (event) => {
  if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
  const tabs = [...document.querySelectorAll(".tab[data-job-tab]")];
  const index = tabs.indexOf(document.activeElement);
  if (index === -1) return;
  event.preventDefault();
  const next = tabs[(index + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
  next.focus();
  next.click();
});
$("#refreshBtn").addEventListener("click", (event) => {
  const btn = event.currentTarget;
  btn.classList.add("spinning");
  refreshAll({ poll: true, notice: true })
    .catch((error) => toast(error.message))
    .finally(() => btn.classList.remove("spinning"));  // spinner reflects the real request
});
// Overview "ดูทั้งหมด" buttons and list rows navigate to a view.
document.addEventListener("click", (event) => {
  const link = event.target.closest("[data-view-link]");
  if (!link) return;
  // An approval CTA carries the waiting run's id — select it so Monitor opens on that run
  // instead of making the operator re-find which run needs them.
  if (link.dataset.runId) selectWorkflowRun(link.dataset.runId).catch((error) => toast(error.message));
  showView(link.dataset.viewLink);
});
$("#pollAllBtn").addEventListener("click", async () => {
  await refreshAll({ poll: true, notice: true }).catch((error) => toast(error.message));
});
$("#cancelJobBtn").addEventListener("click", (event) => armedClick(event.currentTarget, "Confirm cancel", () => cancelSelectedJob().catch((error) => toast(error.message))));
$("#pauseWorkflowRunBtn").addEventListener("click", () => controlWorkflowRun("pause").catch((error) => toast(error.message)));
$("#resumeWorkflowRunBtn").addEventListener("click", () => controlWorkflowRun("resume").catch((error) => toast(error.message)));
$("#retryInterruptedRunBtn").addEventListener("click", () => retryInterruptedRun().catch((error) => toast(error.message)));
$("#cancelWorkflowRunBtn").addEventListener("click", (event) => armedClick(event.currentTarget, "Confirm cancel", () => controlWorkflowRun("cancel").catch((error) => toast(error.message))));
$("#addWorkerBtn").addEventListener("click", () => openWorkerModal());
$("#addWorkspaceBtn").addEventListener("click", () => openWorkspaceModal());
$("#createUserForm").addEventListener("submit", (event) => createUser(event).catch((error) => toast(error.message)));
$("#createTokenForm").addEventListener("submit", (event) => createToken(event).catch((error) => toast(error.message)));
$("#workerForm").addEventListener("submit", (event) => saveWorker(event).catch((error) => toast(error.message)));
$("#workspaceForm").addEventListener("submit", (event) => saveWorkspace(event).catch((error) => toast(error.message)));
$("#loginForm").addEventListener("submit", (event) => login(event).catch((error) => {
  $("#loginMessage").textContent = error.message === "unauthorized" ? "Wrong username or password · ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง" : error.message;
}));
$("#signOutBtn").addEventListener("click", () => signOut());
// Mobile nav: hamburger toggles the sidebar drawer; picking a view collapses it again.
const sidebarEl = document.querySelector(".sidebar");
const navToggleBtn = document.getElementById("navToggle");
navToggleBtn?.addEventListener("click", () => {
  const open = sidebarEl.classList.toggle("nav-open");
  navToggleBtn.setAttribute("aria-expanded", String(open));
});
for (const button of document.querySelectorAll(".nav-item")) {
  button.addEventListener("click", () => {
    showView(button.dataset.view);
    sidebarEl?.classList.remove("nav-open");
    navToggleBtn?.setAttribute("aria-expanded", "false");
  });
}
for (const chip of document.querySelectorAll(".audit-chip[data-audit-filter]")) {
  chip.addEventListener("click", () => {
    state.auditFilter = chip.dataset.auditFilter;
    for (const other of document.querySelectorAll(".audit-chip")) other.classList.toggle("is-active", other === chip);
    renderAudit();
  });
}
// Audit cross-nav: a routable row jumps to its target view and selects the resource there.
document.getElementById("auditList")?.addEventListener("click", (event) => {
  const row = event.target.closest(".audit-row[data-nav-view]");
  if (!row) return;
  if (row.dataset.navJob) { state.jobRunFilter = "all"; openJobStream(row.dataset.navJob); }  // "all" so the job is visible in the list
  else if (row.dataset.navRun) selectWorkflowRun(row.dataset.navRun).catch((error) => toast(error.message));
  showView(row.dataset.navView);
});

// Jobs run filter: narrow the list to the workflow run that produced each job.
document.getElementById("jobRunFilter")?.addEventListener("change", (event) => {
  state.jobRunFilter = event.target.value;
  renderJobs();
});

// Monitor approval gate: approve / choose continue the run; reject fails it (two-step arm).
// Buttons in the gate disable while the request is in flight (double-submission guard).
document.getElementById("monApprovals")?.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-approval-id]");
  if (!button || button.disabled) return;
  const approvalId = button.dataset.approvalId;
  const gate = button.closest(".approval-gate");
  const setBusy = (busy) => { for (const node of gate.querySelectorAll("button")) node.disabled = busy; };
  const fire = (action, body, message) => {
    setBusy(true);
    decideApproval(approvalId, action, body)
      .then(() => toast(message))
      .catch((error) => { toast(error.message); setBusy(false); loadAll().catch(() => undefined); });
  };
  if (button.classList.contains("approval-approve")) fire("approve", null, "Approved — run continuing");
  else if (button.classList.contains("approval-choose")) fire("choose", { choice: button.dataset.choice }, "Choice submitted — run continuing");
  else if (button.classList.contains("approval-reject")) armedClick(button, "Confirm reject", () => fire("reject", null, "Rejected — run stopped"));
});

// Monitor node chip → open that node's job in Jobs, filtered to the run it belongs to.
document.getElementById("monNodeChips")?.addEventListener("click", (event) => {
  const chip = event.target.closest(".nchip[data-node-job]");
  if (!chip) return;
  if (chip.dataset.nodeRun) state.jobRunFilter = chip.dataset.nodeRun;
  openJobStream(chip.dataset.nodeJob);
  showView("jobs");
});

// Job detail → open the job's workflow run in Monitor (reverse of the node chip).
document.getElementById("jobDetailRunChip")?.addEventListener("click", (event) => {
  const runId = event.currentTarget.dataset.runId;
  if (!runId) return;
  selectWorkflowRun(runId).catch((error) => toast(error.message));
  showView("monitor");
});

// Theme: light/dark toggle persisted to localStorage. The <head> inline script sets the
// initial data-theme before first paint (no flash); this reflects it on the toggle and flips it.
function applyTheme(theme) {
  const dark = theme === "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  const label = document.getElementById("themeToggleLabel");
  if (label) label.textContent = dark ? "Light" : "Dark";
  const toggle = document.getElementById("themeToggle");
  if (toggle) {
    // The label already reads as "the theme you'll switch to"; the icon and aria-label now
    // say the same thing explicitly, so neither reads as a status readout of the current theme.
    toggle.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
    toggle.querySelector(".theme-icon-moon").hidden = dark;
    toggle.querySelector(".theme-icon-sun").hidden = !dark;
  }
}
(function initTheme() {
  let saved = "light";
  try { saved = localStorage.getItem("atlas-theme") || "light"; } catch { /* private mode */ }
  applyTheme(saved === "dark" ? "dark" : "light");
})();
document.getElementById("themeToggle")?.addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  try { localStorage.setItem("atlas-theme", next); } catch { /* private mode */ }
  applyTheme(next);
});
$("#confirmOkBtn").addEventListener("click", () => { settleConfirm(true); closeModals(); });
$("#confirmCancelBtn").addEventListener("click", () => closeModals());
document.getElementById("auditSearchInput")?.addEventListener("input", (event) => {
  state.auditFilterText = event.currentTarget.value;
  renderAudit();
});
document.getElementById("auditMoreBtn")?.addEventListener("click", () => {
  state.auditLimit += 50;
  loadAll().catch((error) => toast(error.message));
});
document.getElementById("auditCsvBtn")?.addEventListener("click", () => downloadAudit().catch((error) => toast(error.message)));
$("#loadUsageBtn").addEventListener("click", () => loadUsage().catch((error) => toast(error.message)));
$("#usageJsonBtn").addEventListener("click", () => downloadUsage("").catch((error) => toast(error.message)));
$("#usageCsvBtn").addEventListener("click", () => downloadUsage("csv").catch((error) => toast(error.message)));
// URL hash wins over the remembered view so shared/bookmarked links open the right screen.
window.addEventListener("hashchange", () => {
  const view = location.hash.slice(1);
  if (!VIEWS.includes(view)) return;
  // Ignore the echo from showView's own hash write, else every navigation runs twice
  // (doubling per-view loaders like loadUsage/loadAccounts).
  if (document.querySelector(".view.is-active")?.dataset.view === view) return;
  showView(view);
});
const bootHash = location.hash.slice(1);
showView(VIEWS.includes(bootHash) ? bootHash : (localStorage.getItem("atlasView") || "overview"));

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeModals();
  // Focus trap: while a modal is open, Tab cycles inside it instead of escaping to the
  // page behind the backdrop (which stays interactive in the DOM).
  if (event.key !== "Tab") return;
  const modal = document.querySelector(".modal-backdrop:not([hidden]) .modal");
  if (!modal) return;
  const focusables = modal.querySelectorAll("button, input:not([type=hidden]), select, textarea, a[href]");
  if (!focusables.length) return;
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && (document.activeElement === last || !modal.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
});

loadAll()
  .then(() => {
    document.body.classList.remove("is-loading");
    // If the Usage/Accounts view was restored from localStorage, load it now that auth/role are known.
    if (document.querySelector("#view-usage.is-active")) loadUsage().catch(() => undefined);
    if (document.querySelector("#view-accounts.is-active")) loadAccounts().catch((error) => toast(error.message));
    const firstActive = state.jobs.find((job) => ["running", "queued", "cancel_requested"].includes(job.state));
    if (firstActive) openJobStream(firstActive.id);
  })
  .catch((error) => {
    document.body.classList.remove("is-loading");
    // A pre-auth failure shows the login screen via api()'s 401 path; anything else here is a
    // real load failure — make it look like one instead of an eternal "Loading…".
    if ($("#loginScreen").hidden) {
      setStale(error);
      markLoadFailed();
    }
    toast(error.message);
  });

setInterval(() => {
  if (!$("#loginScreen").hidden) return;
  loadAll().catch((error) => setStale(error));  // health chip tracks transport; banner tracks API
}, 5000);

setInterval(() => {
  if (!$("#loginScreen").hidden) return;
  refreshAll({ poll: true }).catch((error) => setStale(error));
}, AUTO_POLL_MS);
