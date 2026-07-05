const state = {
  currentUser: null,
  workers: [],
  workspaces: [],
  conversations: [],
  jobs: [],
  audit: [],
  workflows: [],
  workflowTemplates: [],
  workflowRuns: [],
  workflowTriggers: [],
  approvals: [],
  selectedJobId: null,
  selectedWorkflowId: null,
  selectedWorkflowNode: null,
  selectedWorkflowRunId: null,
  selectedWorkflowTriggerId: null,
  workflowRunDetail: null,
  workflowArtifacts: [],
  workflowEvents: [],
  workflowTriggerEvents: [],
  workerSuggestions: [],
  eventSource: null,
  auditFilter: "all",
  workflowEditorDirty: false,
  streamText: "",
  events: [],
};

const $ = (selector) => document.querySelector(selector);
const AUTO_POLL_MS = 60000;
const DEFAULT_NEWS_HANDOFF_PROMPT = `คุณคือผู้ประกาศข่าว

ให้นำข่าวที่นักข่าวรวบรวมมาเรียบเรียงเป็นสคริปต์รายงานข่าวที่พูดได้จริง กระชับ ชัดเจน และไม่แต่งเติมข้อเท็จจริงที่ไม่มีในต้นฉบับ

ข่าวจากนักข่าว:
{result}`;

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (!headers.has("Content-Type") && options.body) headers.set("Content-Type", "application/json");
  const token = localStorage.getItem("atlasApiToken");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(path, { ...options, headers });
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
    const message = data?.error || data?.message || response.statusText || `HTTP ${response.status}`;
    if (response.status === 401 && path !== "/api/auth/login") {
      localStorage.removeItem("atlasApiToken");
      showLogin("เซสชันหมดอายุหรือไม่พบ กรุณาเข้าสู่ระบบอีกครั้ง");
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

function shortId(value) {
  return String(value || "").split("_").at(-1)?.slice(0, 8) || "";
}

function formatTime(value) {
  if (!value) return "";
  return new Date(value).toLocaleString([], { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message || "Done";
  node.classList.add("visible");
  setTimeout(() => node.classList.remove("visible"), 2600);
}

function reflectEditorDirty() {
  $("#view-workflows")?.classList.toggle("is-dirty", state.workflowEditorDirty);
}

function setDirty(value) {
  state.workflowEditorDirty = value;
  reflectEditorDirty();
}

const VIEWS = ["overview", "command", "workflows", "monitor", "jobs", "audit", "usage", "fleet"];
const VIEW_META = {
  overview:  ["ภาพรวม", "Overview", "สรุปสถานะฟลีตและงานล่าสุด"],
  command:   ["สั่งงาน", "Command", "ส่งงานไปยัง thClaws worker ที่เหมาะสมโดยอัตโนมัติ"],
  workflows: ["เวิร์กโฟลว์", "Workflows", "ออกแบบและจัดการ workflow แบบกราฟโหนด"],
  monitor:   ["ติดตาม", "Monitor", "คิวอนุมัติและสถานะ run ที่ทำงานพร้อมกัน"],
  jobs:      ["งาน", "Jobs", "สตรีมผลงานแบบสดและประวัติงาน"],
  fleet:     ["ฟลีต", "Fleet", "จัดการ worker และ workspace"],
  audit:     ["ตรวจสอบ", "Audit", "30 การกระทำล่าสุดบน control plane"],
  usage:     ["การใช้งาน", "Usage", "Workflow run, งาน และ budget units ต่อช่วงเวลา"],
};

function showView(view) {
  if (!VIEWS.includes(view)) view = "overview";
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
  if (view === "workflows") requestAnimationFrame(() => wfApplyScale());
  try { localStorage.setItem("atlasView", view); } catch { /* private mode */ }
}

function setNavBadge(name, count) {
  const badge = document.querySelector(`.nav-badge[data-badge="${name}"]`);
  if (!badge) return;
  badge.textContent = count > 99 ? "99+" : String(count);
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

function defaultWorkflowGraph() {
  return {
    start: "reporter",
    nodes: [
      { id: "reporter", type: "worker", prompt: "Research {input.topic}", outputs: ["notes"] },
      { id: "anchor", type: "worker", prompt: "Write from {artifact.notes}", outputs: ["script"] },
    ],
    edges: [{ from: "reporter", to: "anchor", condition: { type: "always" } }],
  };
}

const POLICY_FORM_FIELDS = [
  ["max_jobs", "#policyMaxJobsInput", "number"],
  ["max_iterations", "#policyMaxIterationsInput", "number"],
  ["max_attempts_per_node", "#policyMaxAttemptsInput", "number"],
  ["max_minutes", "#policyMaxMinutesInput", "number"],
  ["requires_human_after_iterations", "#policyHumanAfterInput", "number"],
  ["max_budget_units", "#policyMaxBudgetInput", "number"],
  ["allowed_worker_ids", "#policyAllowedWorkersInput", "list"],
  ["allowed_workspace_ids", "#policyAllowedWorkspacesInput", "list"],
  ["stop_on_first_failure", "#policyStopOnFailureInput", "boolean"],
  // T6: default-OFF boolean (unlike stop_on_first_failure, which defaults ON) — a missing
  // file_handoff must render UNCHECKED, so it uses the "boolean_off" handling below.
  ["file_handoff", "#policyFileHandoffInput", "boolean_off"],
];

function syncPolicyFormFromJson() {
  let policy;
  try {
    policy = parseJsonField("#workflowPolicyInput");
  } catch {
    return false;
  }
  if (!policy || Array.isArray(policy) || typeof policy !== "object") return false;
  for (const [key, selector, type] of POLICY_FORM_FIELDS) {
    const value = policy[key];
    if (type === "boolean") {
      $(selector).checked = value !== false;
    } else if (type === "boolean_off") {
      // default-off: only an explicit true checks the box (a missing key stays unchecked).
      $(selector).checked = value === true;
    } else {
      $(selector).value = type === "list" ? (Array.isArray(value) ? value.join(", ") : "") : (value ?? "");
    }
  }
  return true;
}

function syncPolicyJsonFromForm() {
  let policy;
  try {
    policy = parseJsonField("#workflowPolicyInput");
  } catch {
    return false;
  }
  if (!policy || Array.isArray(policy) || typeof policy !== "object") return false;
  for (const [key, selector, type] of POLICY_FORM_FIELDS) {
    if (type === "boolean") {
      policy[key] = $(selector).checked;
      continue;
    }
    if (type === "boolean_off") {
      // default-off: write true only when checked; drop the key entirely when unchecked so the
      // saved policy stays clean (no noisy `file_handoff: false` on every workflow).
      if ($(selector).checked) policy[key] = true;
      else delete policy[key];
      continue;
    }
    const raw = $(selector).value.trim();
    if (!raw) {
      delete policy[key];
    } else if (type === "number") {
      policy[key] = Number.parseInt(raw, 10);
    } else {
      policy[key] = raw.split(",").map((value) => value.trim()).filter(Boolean);
    }
  }
  $("#workflowPolicyInput").value = prettyJson(policy);
  setDirty(true);
  return true;
}

function addBuilderNode() {
  const graph = parseJsonField("#workflowGraphInput");
  const id = $("#builderNodeIdInput").value.trim();
  const type = $("#builderNodeTypeSelect").value;
  if (!id) throw new Error("Node ID is required");
  if ((graph.nodes || []).some((node) => node.id === id)) throw new Error(`Duplicate node ID: ${id}`);
  const role = $("#builderNodeRoleInput").value.trim();
  const prompt = $("#builderNodePromptInput").value.trim();
  const budgetUnits = Number.parseInt($("#builderNodeBudgetInput").value, 10);
  let node;
  if (type === "manager") {
    node = { id, type, schema: "manager_decision_v1", prompt: prompt || "Choose the next bounded workflow action." };
    if (role) node.role = role;
  } else if (type === "join") {
    node = { id, type, mode: $("#builderJoinModeSelect").value };
    if (node.mode === "quorum") node.quorum = Number.parseInt($("#builderJoinQuorumInput").value, 10);
  } else if (type === "human_gate") {
    node = { id, type, label: role || id, reason: prompt };
    const choices = $("#builderHumanChoicesInput").value.split(",").map((item) => item.trim()).filter(Boolean).map((item) => {
      const [choiceId, ...labelParts] = item.split(":");
      return { id: choiceId.trim(), label: (labelParts.join(":").trim() || choiceId.trim()) };
    });
    if (choices.length) node.choices = choices;
  } else {
    node = { id, type, prompt };
    if (role) node.role = role;
    const outputs = $("#builderNodeOutputsInput").value.split(",").map((value) => value.trim()).filter(Boolean);
    if (outputs.length) node.outputs = outputs;
  }
  if (["worker", "manager"].includes(type) && Number.isInteger(budgetUnits) && budgetUnits > 0) node.budget_units = budgetUnits;
  graph.nodes = [...(graph.nodes || []), node];
  graph.edges ||= [];
  graph.start ||= id;
  $("#workflowGraphInput").value = prettyJson(graph);
  setDirty(true);
  toast(`${type} node added to JSON preview`);
}

function addBuilderEdge() {
  const graph = parseJsonField("#workflowGraphInput");
  const from = $("#builderEdgeFromInput").value.trim();
  const to = $("#builderEdgeToInput").value.trim();
  const type = $("#builderConditionTypeSelect").value;
  const subject = $("#builderConditionSubjectInput").value.trim();
  const value = $("#builderConditionValueInput").value.trim();
  if (!from || !to) throw new Error("Edge From and To are required");
  let condition = { type };
  if (type === "artifact_equals") {
    condition = { type, artifact: subject, path: $("#builderConditionPathInput").value.trim(), value };
  } else if (type === "artifact_in") {
    condition = { type, artifact: subject, path: $("#builderConditionPathInput").value.trim(), values: value.split(",").map((item) => item.trim()).filter(Boolean) };
  } else if (type === "manager_selected") {
    condition = { type, target: to };
  } else if (type === "human_selected") {
    condition = { type, choice: value || subject };
  } else if (type === "max_iterations_below") {
    condition = { type, node: subject, max: Number.parseInt(value, 10) };
  }
  const edge = { from, to, condition };
  // T6: optional artifact-key globs pushed to the target worker before its job (needs
  // policy.file_handoff; the server validator rejects push_files without it at save time).
  const pushFiles = $("#builderEdgePushFilesInput").value.split(",").map((pattern) => pattern.trim()).filter(Boolean);
  if (pushFiles.length) edge.push_files = pushFiles;
  graph.edges = [...(graph.edges || []), edge];
  $("#workflowGraphInput").value = prettyJson(graph);
  setDirty(true);
  toast(`${type} edge added to JSON preview`);
}

function applyQuickTrigger() {
  const quickType = $("#triggerQuickTypeSelect").value;
  const value = $("#triggerQuickValueInput").value.trim();
  if (quickType === "interval") {
    const minutes = Number(value);
    if (!Number.isFinite(minutes) || minutes <= 0) throw new Error("Interval minutes must be positive");
    $("#triggerTypeSelect").value = "schedule";
    $("#triggerConfigInput").value = prettyJson({ interval_minutes: minutes });
  } else if (quickType === "daily") {
    $("#triggerTypeSelect").value = "schedule";
    $("#triggerConfigInput").value = prettyJson({ daily_time: value || "09:30" });
  } else {
    $("#triggerTypeSelect").value = quickType;
    $("#triggerConfigInput").value = "{}";
  }
  toast("Trigger JSON preview updated");
}

async function loadAll() {
  const me = await api("/api/me");
  state.currentUser = me.user;
  hideLogin();
  const canReadAudit = ["admin", "auditor"].includes(state.currentUser?.role);
  const [workers, workspaces, conversations, jobs, workflows, workflowTemplates, workflowRuns, workflowTriggers, approvals, audit] = await Promise.all([
    api("/api/workers"),
    api("/api/workspaces"),
    api("/api/conversations"),
    api("/api/jobs"),
    api("/api/workflows"),
    api("/api/workflow-templates"),
    api("/api/workflow-runs"),
    api("/api/workflow-triggers"),
    api("/api/approvals?state=pending"),
    canReadAudit ? api("/api/audit?limit=30") : Promise.resolve({ audit: [] }),
  ]);
  state.workers = workers.workers || [];
  state.workspaces = workspaces.workspaces || [];
  state.conversations = conversations.conversations || [];
  state.jobs = jobs.jobs || [];
  state.workflows = workflows.workflows || [];
  state.workflowTemplates = workflowTemplates.templates || [];
  state.workflowRuns = workflowRuns.runs || [];
  state.workflowTriggers = workflowTriggers.triggers || [];
  state.approvals = approvals.approvals || [];
  state.audit = audit.audit || [];
  if (state.selectedWorkflowRunId && state.workflowRuns.some((run) => run.id === state.selectedWorkflowRunId)) {
    await loadWorkflowRunDetail(state.selectedWorkflowRunId);
  }
  render();
  const lastRefresh = document.getElementById("lastRefresh");
  if (lastRefresh) lastRefresh.textContent = `อัปเดต ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
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
  renderWorkflows();
  renderAudit();
  updateComposerRoutePreview();
  reflectEditorDirty();
  renderIdentity();
  applyRoleGate();
  // Last: keep the selected job's header/cancel/cursor in sync on every poll, after
  // applyRoleGate (which would otherwise re-enable Cancel for operators on finished jobs).
  updateStreamHeader();
}

function showLogin(message = "ลงชื่อเข้าใช้ด้วยบัญชีผู้ดูแลของอินสแตนซ์นี้") {
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
  $("#loginMessage").textContent = "ลงชื่อเข้าใช้ด้วยบัญชีผู้ดูแลของอินสแตนซ์นี้";
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
    "#submitJobBtn", "#cancelJobBtn", "#newWorkflowBtn", "#saveWorkflowBtn", "#validateWorkflowBtn",
    "#explainWorkflowBtn", "#repairWorkflowBtn", "#suggestWorkersBtn", "#draftWorkflowBtn",
    "#runWorkflowBtn", "#uploadWorkflowFileBtn", "#pauseWorkflowRunBtn", "#resumeWorkflowRunBtn",
    "#retryInterruptedRunBtn", "#cancelWorkflowRunBtn", "#saveTriggerBtn", "#suggestTriggersBtn", "#pollAllBtn",
  ]) {
    const node = $(selector);
    if (node) node.disabled = !operator;
  }
  for (const selector of ["#addWorkerBtn", "#addWorkspaceBtn"]) {
    const node = $(selector);
    if (node) node.disabled = !admin;
  }
  const auditNav = document.querySelector('.nav-item[data-view="audit"]');
  if (auditNav) auditNav.hidden = !["admin", "auditor"].includes(role);
  const usageNav = document.querySelector('.nav-item[data-view="usage"]');
  if (usageNav) usageNav.hidden = !["admin", "auditor"].includes(role);
  for (const node of document.querySelectorAll(".edit-worker, .delete-worker, .edit-workspace, .delete-workspace, .sync-mode-select")) node.disabled = !admin;
  for (const node of document.querySelectorAll(".poll-worker, .approve-approval, .reject-approval, .choose-approval, .fire-trigger, .toggle-trigger, .delete-trigger, .apply-worker-suggestion")) node.disabled = !operator;
  if (auditNav?.hidden && document.querySelector("#view-audit.is-active")) showView("overview");
  if (usageNav?.hidden && document.querySelector("#view-usage.is-active")) showView("overview");
}

async function login(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = await api("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ username: form.elements.username.value, password: form.elements.password.value }),
  });
  localStorage.setItem("atlasApiToken", data.token);
  form.elements.password.value = "";
  await loadAll();
}

async function signOut() {
  try { await api("/api/auth/logout", { method: "POST" }); } catch { /* token may already be invalid */ }
  localStorage.removeItem("atlasApiToken");
  state.currentUser = null;
  showLogin("ออกจากระบบแล้ว");
}

function renderRunSummary() {
  const summary = $("#workflowRunSummary");
  const run = state.workflowRunDetail?.run;
  if (!run) {
    summary.innerHTML = "";
    return;
  }
  const counters = run.counters || {};
  const chips = [`<span class="chip">state <b>${escapeHtml(run.state)}</b></span>`];
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
  set("metricWorkersDelta", total ? `ออนไลน์ ${online} · ออฟไลน์ ${offline}` : "ยังไม่มี worker");
  set("metricRunning", active);
  set("metricRunningDelta", `queued ${queued} · running ${running}`);
  const midnight = new Date(); midnight.setHours(0, 0, 0, 0);
  const runsToday = state.workflowRuns.filter((run) => run.created_at && new Date(run.created_at) >= midnight).length;
  set("metricRuns", runsToday);
  set("metricRunsDelta", activeRuns ? `กำลังทำงาน ${activeRuns} · ทั้งหมด ${state.workflowRuns.length}` : `จากทั้งหมด ${state.workflowRuns.length}`);
  set("metricApprovals", state.approvals.length);
  set("metricApprovalsDelta", state.approvals.length ? "ต้องการการอนุมัติ" : "ไม่มีรายการรออนุมัติ");
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
        <div class="dash-job">
          <span class="status ${statusClass(job.state)} dot-only" aria-hidden="true"></span>
          <div class="grow">
            <div class="title">${escapeHtml(job.prompt || job.id)}</div>
            <div class="meta">${escapeHtml(job.worker_name || shortId(job.worker_id) || "—")} · ${escapeHtml(shortId(job.id))}</div>
          </div>
          <span class="status ${statusClass(job.state)}">${escapeHtml(job.state)}</span>
          <span class="ago">${escapeHtml(formatTime(job.created_at))}</span>
        </div>`).join("")
      : '<div class="empty">ยังไม่มีงาน</div>';
  }
  const apBox = document.getElementById("overviewApproval");
  if (apBox) {
    const a = state.approvals[0];
    apBox.innerHTML = a
      ? `<div class="approve-row">
          <span class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg></span>
          <div class="grow"><div class="title">${escapeHtml(a.label || a.reason || "รออนุมัติ")}</div><div class="meta">run ${escapeHtml(shortId(a.run_id))} · ${escapeHtml(formatTime(a.created_at))}</div></div>
          <button class="primary-btn btn-sm" type="button" data-view-link="monitor">ตรวจสอบ</button>
        </div>`
      : '<div class="approve-empty">ไม่มีรายการรออนุมัติในขณะนี้</div>';
  }
}

function renderWorkers() {
  const list = $("#workerList");
  if (!state.workers.length) {
    list.innerHTML = '<div class="empty">No workers</div>';
    return;
  }
  list.innerHTML = state.workers.map((worker) => {
    const tags = (worker.tags || []).map((tag) => `<span class="wc-tag">${escapeHtml(tag)}</span>`).join("");
    const seen = worker.last_seen_at ? `เห็นล่าสุด ${formatTime(worker.last_seen_at)}` : "ยังไม่เคยเห็น";
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
        <span class="status ${statusClass(worker.status)}">${escapeHtml(worker.status)}</span>
      </div>
      <div class="wc-url">${escapeHtml(worker.base_url)}</div>
      <div class="wc-url" title="Worker ID">${escapeHtml(worker.id)}<button class="copy-btn" type="button" data-copy="${escapeHtml(worker.id)}" title="คัดลอก Worker ID" aria-label="Copy worker id">⧉</button></div>
      <div class="wc-role">
        <span class="role-chip">${escapeHtml(worker.role || "unassigned")}<button class="copy-btn" type="button" data-copy="${escapeHtml(worker.role || "unassigned")}" title="คัดลอก role" aria-label="Copy role">⧉</button></span>
        <span class="latency">${escapeHtml(seen)}</span>
      </div>
      <div class="wc-tags">${tags}</div>
      ${versionWarn}
      <div class="wc-advisory">${busyBadge}${syncControl}${uiLink}</div>
      ${skillsBlock}
      <div class="wc-actions">
        <button class="secondary-btn poll-worker" data-worker-id="${escapeHtml(worker.id)}">Poll</button>
        <button class="secondary-btn edit-worker" data-worker-id="${escapeHtml(worker.id)}">แก้ไข</button>
        <button class="danger-btn delete-worker" data-worker-id="${escapeHtml(worker.id)}">ลบ</button>
      </div>
    </article>`;
  }).join("");
}

function renderWorkspaces() {
  const list = $("#workspaceList");
  if (!state.workspaces.length) {
    list.innerHTML = '<div class="empty">No workspaces</div>';
    return;
  }
  list.innerHTML = state.workspaces.map((workspace) => `
    <div class="ws-row">
      <span class="key">${escapeHtml(workspace.workspace_key)}</span>
      <span>${escapeHtml(workspace.worker_name || shortId(workspace.worker_id))}</span>
      <span class="dir">${escapeHtml(workspace.workspace_dir)}</span>
      <span>${escapeHtml(workspace.company || "—")}</span>
      <span class="ws-actions">
        <button class="ws-mini edit-workspace" data-workspace-id="${escapeHtml(workspace.id)}">แก้ไข</button>
        <button class="ws-mini danger delete-workspace" data-workspace-id="${escapeHtml(workspace.id)}">ลบ</button>
      </span>
    </div>
  `).join("");
}

function renderSelects() {
  const selected = {
    conversation: $("#conversationSelect").value,
    worker: $("#workerSelect").value,
    workspace: $("#workspaceSelect").value,
    handoffWorker: $("#handoffWorkerSelect").value,
    handoffWorkspace: $("#handoffWorkspaceSelect").value,
  };
  const conversationSelect = $("#conversationSelect");
  conversationSelect.innerHTML = '<option value="">New conversation</option>' + state.conversations.map((conversation) => (
    `<option value="${escapeHtml(conversation.id)}">${escapeHtml(conversation.title || conversation.id)}</option>`
  )).join("");
  setSelectValue(conversationSelect, selected.conversation);

  const workerOptions = '<option value="">Auto route</option>' + state.workers.map((worker) => (
    `<option value="${escapeHtml(worker.id)}">${escapeHtml(worker.name)} (${escapeHtml(worker.status)})</option>`
  )).join("");
  $("#workerSelect").innerHTML = workerOptions;
  setSelectValue($("#workerSelect"), selected.worker);
  document.querySelector('#workspaceForm select[name="worker_id"]').innerHTML = state.workers.map((worker) => (
    `<option value="${escapeHtml(worker.id)}">${escapeHtml(worker.name)}</option>`
  )).join("");

  $("#workspaceSelect").innerHTML = '<option value="">Auto route</option>' + state.workspaces.map((workspace) => (
    `<option value="${escapeHtml(workspace.id)}">${escapeHtml(workspace.workspace_key)} · ${escapeHtml(workspace.worker_name || "")}</option>`
  )).join("");
  setSelectValue($("#workspaceSelect"), selected.workspace);

  $("#handoffWorkerSelect").innerHTML = '<option value="">Choose worker</option>' + state.workers.map((worker) => (
    `<option value="${escapeHtml(worker.id)}">${escapeHtml(worker.name)} (${escapeHtml(worker.status)})</option>`
  )).join("");
  setSelectValue($("#handoffWorkerSelect"), selected.handoffWorker);

  $("#handoffWorkspaceSelect").innerHTML = '<option value="">No workspace override</option>' + state.workspaces.map((workspace) => (
    `<option value="${escapeHtml(workspace.id)}">${escapeHtml(workspace.workspace_key)} · ${escapeHtml(workspace.worker_name || "")}</option>`
  )).join("");
  setSelectValue($("#handoffWorkspaceSelect"), selected.handoffWorkspace);

  if (!$("#handoffPromptInput").value.trim()) $("#handoffPromptInput").value = DEFAULT_NEWS_HANDOFF_PROMPT;
}

function setSelectValue(select, value) {
  if (!value) {
    select.value = "";
    return;
  }
  select.value = [...select.options].some((option) => option.value === value) ? value : "";
}

function workspaceCountForWorker(workerId) {
  return state.workspaces.filter((workspace) => workspace.worker_id === workerId).length;
}

function renderJobs() {
  const list = $("#jobList");
  const count = document.getElementById("jobCount");
  if (count) count.textContent = `${state.jobs.length} รายการ`;
  if (!state.jobs.length) {
    list.innerHTML = '<div class="empty">ยังไม่มีงาน</div>';
    return;
  }
  list.innerHTML = state.jobs.map((job) => {
    const handoff = job.handoff_error
      ? { label: "handoff error", cls: "err" }
      : job.handoff_job_id
        ? { label: "child", cls: "child" }
        : (job.handoff_worker_id ? { label: "handoff armed", cls: "armed" } : null);
    return `
    <button class="job-row ${job.id === state.selectedJobId ? "selected" : ""}" type="button" data-job-id="${escapeHtml(job.id)}">
      <div class="job-row-top">
        <span class="status ${statusClass(job.state)} dot-only" aria-hidden="true"></span>
        <span class="status ${statusClass(job.state)}">${escapeHtml(job.state)}</span>
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

function renderWorkflows() {
  if (state.selectedWorkflowId && !state.workflows.some((workflow) => workflow.id === state.selectedWorkflowId)) {
    state.selectedWorkflowId = null;
  }
  renderWorkflowList();
  renderWorkflowTemplatePicker();
  renderWorkflowEditor();
  renderWorkflowRuns();
  renderApprovals();
  renderWorkflowTriggers();
}

function renderWorkflowTemplatePicker() {
  const select = $("#workflowTemplateSelect");
  const selected = select.value;
  select.innerHTML = '<option value="">Choose a template</option>' + state.workflowTemplates.map((template) => (
    `<option value="${escapeHtml(template.id)}">${escapeHtml(template.name)}</option>`
  )).join("");
  setSelectValue(select, selected);
}

function renderWorkflowList() {
  const list = $("#workflowList");
  if (!state.workflows.length) {
    list.innerHTML = '<div class="empty">No workflow definitions</div>';
    return;
  }
  list.innerHTML = state.workflows.map((workflow) => `
    <article class="workflow-item ${workflow.id === state.selectedWorkflowId ? "selected" : ""}" data-workflow-id="${escapeHtml(workflow.id)}">
      <div class="item-title">
        <span>${escapeHtml(workflow.name)}</span>
        <span class="status ${statusClass(workflow.status)}">${escapeHtml(workflow.status)}</span>
      </div>
      <div class="item-sub">${escapeHtml(workflow.description || "no description")} · v${escapeHtml(workflow.version)} · ${shortId(workflow.id)}</div>
    </article>
  `).join("");
}

function renderWorkflowEditor(force = false) {
  const workflow = state.workflows.find((item) => item.id === state.selectedWorkflowId);
  if (!force && state.workflowEditorDirty) return;
  const editorIds = new Set([
    "workflowNameInput", "workflowDescriptionInput", "workflowGraphInput", "workflowPolicyInput", "workflowRunInput",
    "builderNodeIdInput", "builderNodeTypeSelect", "builderNodeRoleInput", "builderNodePromptInput", "builderNodeOutputsInput",
    "builderJoinModeSelect", "builderAddNodeBtn", "builderEdgeFromInput", "builderEdgeToInput", "builderConditionTypeSelect",
    "builderConditionSubjectInput", "builderConditionPathInput", "builderConditionValueInput", "builderAddEdgeBtn",
  ]);
  if (!force && workflow && $("#workflowNameInput").dataset.workflowId === workflow.id && editorIds.has(document.activeElement?.id)) {
    return;
  }
  $("#workflowMeta").textContent = workflow ? `${workflow.status} · ${shortId(workflow.id)} · updated ${formatTime(workflow.updated_at)}` : "New workflow";
  $("#workflowNameInput").dataset.workflowId = workflow?.id || "";
  $("#workflowNameInput").value = workflow?.name || "Untitled workflow";
  $("#workflowDescriptionInput").value = workflow?.description || "";
  $("#workflowGraphInput").value = prettyJson(workflow?.graph || defaultWorkflowGraph());
  $("#workflowPolicyInput").value = prettyJson(workflow?.policy || { max_jobs: 5, max_iterations: 10 });
  syncPolicyFormFromJson();
  if (!$("#workflowRunInput").value.trim()) $("#workflowRunInput").value = "{}";
  const nameDisplay = document.getElementById("workflowName");
  if (nameDisplay) nameDisplay.textContent = workflow?.name || "เวิร์กโฟลว์ใหม่";
  renderWorkflowGraph();
  renderWorkerSuggestions();
}

// ----- Visual node graph (parses the editable Graph JSON above) -----
const WF_NODE_W = 158, WF_NODE_H = 88, WF_COL_GAP = 60, WF_ROW_GAP = 30, WF_START_W = 92, WF_PAD = 36;
let wfUserZoom = 1;
let wfResizeObserver = null;

function wfParseGraph() {
  try {
    const graph = parseJsonField("#workflowGraphInput", null);
    if (!graph || !Array.isArray(graph.nodes)) return null;
    return graph;
  } catch {
    return null;
  }
}

function wfNodeSubline(node) {
  if (node.type === "human_gate") return node.reason || "รออนุมัติ";
  const role = node.role || node.worker_id || node.id;
  const outputs = Array.isArray(node.outputs) ? node.outputs.join(",") : (node.outputs || "");
  return outputs ? `${role}→${outputs}` : String(role);
}

function wfLayout(graph) {
  const nodes = graph.nodes.filter((node) => node && node.id);
  const ids = nodes.map((node) => node.id);
  const idset = new Set(ids);
  const edges = (graph.edges || []).filter((edge) => edge && idset.has(edge.from) && idset.has(edge.to));
  // Longest-path layering; cap iterations at node count so cycles (manager loops) terminate.
  const depth = Object.fromEntries(ids.map((id) => [id, 0]));
  for (let k = 0; k < nodes.length; k++) {
    let changed = false;
    for (const edge of edges) {
      if (depth[edge.to] < depth[edge.from] + 1) { depth[edge.to] = depth[edge.from] + 1; changed = true; }
    }
    if (!changed) break;
  }
  const columns = {};
  for (const id of ids) (columns[depth[id]] ||= []).push(id);
  const maxCol = Math.max(0, ...Object.keys(columns).map(Number));
  let maxRows = 1;
  for (let c = 0; c <= maxCol; c++) maxRows = Math.max(maxRows, (columns[c] || []).length);
  const totalH = maxRows * WF_NODE_H + (maxRows - 1) * WF_ROW_GAP;
  const colX = (c) => WF_PAD + WF_START_W + WF_COL_GAP + c * (WF_NODE_W + WF_COL_GAP);
  const place = {};
  for (let c = 0; c <= maxCol; c++) {
    const colIds = columns[c] || [];
    const colH = colIds.length * WF_NODE_H + (colIds.length - 1) * WF_ROW_GAP;
    const y0 = WF_PAD + (totalH - colH) / 2;
    colIds.forEach((id, row) => { place[id] = { x: colX(c), y: y0 + row * (WF_NODE_H + WF_ROW_GAP), w: WF_NODE_W, h: WF_NODE_H }; });
  }
  const width = colX(maxCol) + WF_NODE_W + WF_PAD;
  const height = totalH + WF_PAD * 2;
  const startId = (graph.start && idset.has(graph.start)) ? graph.start : ids[0];
  const start = startId ? { x: WF_PAD, y: (height - 40) / 2, w: WF_START_W, h: 40, to: startId } : null;
  return { nodes, edges, place, width, height, start };
}

function renderWorkflowGraph() {
  const host = document.getElementById("workflowGraph");
  if (!host) return;
  const graph = wfParseGraph();
  if (!graph || !graph.nodes.length) {
    host.innerHTML = '<div class="wf-canvas-empty">Graph JSON ไม่ถูกต้อง หรือยังไม่มีโหนด</div>';
    renderWorkflowNodeInspector();
    return;
  }
  const layout = wfLayout(graph);
  const byId = Object.fromEntries(graph.nodes.map((node) => [node.id, node]));
  if (!byId[state.selectedWorkflowNode]) state.selectedWorkflowNode = (layout.start && layout.start.to) || graph.nodes[0]?.id || null;

  const edgePath = (sx, sy, tx, ty) => {
    const dx = Math.max(46, (tx - sx) / 2);
    return `M ${sx} ${sy} C ${sx + dx} ${sy}, ${tx - dx} ${ty}, ${tx} ${ty}`;
  };
  const segments = [];
  if (layout.start) {
    const target = layout.place[layout.start.to];
    if (target) segments.push({ sx: layout.start.x + layout.start.w, sy: layout.start.y + layout.start.h / 2, tx: target.x, ty: target.y + target.h / 2, color: "var(--nt-grey-300)", dash: "none", width: 1.8 });
  }
  for (const edge of layout.edges) {
    const source = layout.place[edge.from];
    const target = layout.place[edge.to];
    if (!source || !target) continue;
    const cond = edge.condition?.type || "always";
    const human = cond === "human_selected";
    segments.push({
      sx: source.x + source.w, sy: source.y + source.h / 2, tx: target.x, ty: target.y + target.h / 2,
      color: human ? "var(--nt-yellow-500)" : "var(--nt-grey-300)",
      dash: cond.startsWith("artifact") ? "5 4" : "none",
      width: human ? 2.5 : 1.8,
    });
  }
  const svg = `<svg class="wf-edges" width="${layout.width}" height="${layout.height}" aria-hidden="true">${
    segments.map((seg) => `<path d="${edgePath(seg.sx, seg.sy, seg.tx, seg.ty)}" fill="none" stroke="${seg.color}" stroke-width="${seg.width}" stroke-dasharray="${seg.dash}"/><circle cx="${seg.tx}" cy="${seg.ty}" r="4" fill="${seg.color}"/>`).join("")
  }</svg>`;

  const tagFor = (type) => type === "human_gate" ? { cls: "gate", label: "human gate" }
    : type === "manager" ? { cls: "manager", label: "manager" }
    : type === "join" ? { cls: "join", label: "join" }
    : { cls: "worker", label: "worker" };
  const nodeEls = graph.nodes.map((node) => {
    const place = layout.place[node.id];
    if (!place) return "";
    const tag = tagFor(node.type);
    const cls = node.type === "manager" ? "manager" : node.type === "human_gate" ? "human_gate" : "worker";
    const selected = node.id === state.selectedWorkflowNode ? " selected" : "";
    return `<button class="wf-node ${cls}${selected}" type="button" data-wf-node="${escapeHtml(node.id)}" style="left:${place.x}px;top:${place.y}px;width:${place.w}px;height:${place.h}px">
      <span class="wf-node-head"><span class="wf-node-name">${escapeHtml(node.id)}</span><span class="wf-node-tag ${tag.cls}">${tag.label}</span></span>
      <span class="wf-node-sub">${escapeHtml(wfNodeSubline(node))}</span>
    </button>`;
  }).join("");
  const startEl = layout.start
    ? `<div class="wf-node start" style="left:${layout.start.x}px;top:${layout.start.y}px;width:${layout.start.w}px;height:${layout.start.h}px"><span class="wf-node-label">อินพุต</span></div>`
    : "";

  host.innerHTML = `<div class="wf-fit-wrap"><div class="wf-fit-inner" style="width:${layout.width}px;height:${layout.height}px">${svg}${startEl}${nodeEls}</div></div>`;
  state.wfGraphSize = { w: layout.width, h: layout.height };
  wfApplyScale();
  if (!wfResizeObserver && window.ResizeObserver) {
    wfResizeObserver = new ResizeObserver(() => wfApplyScale());
    wfResizeObserver.observe(host);
  }
  renderWorkflowNodeInspector();
}

function wfApplyScale() {
  const host = document.getElementById("workflowGraph");
  const inner = host?.querySelector(".wf-fit-inner");
  const wrap = host?.querySelector(".wf-fit-wrap");
  if (!inner || !wrap || !state.wfGraphSize) return;
  const available = host.clientWidth - WF_PAD;
  if (available <= 0) return;
  const fit = Math.max(0.4, Math.min(1, available / state.wfGraphSize.w));
  const scale = Math.max(0.3, Math.min(1.6, fit * wfUserZoom));
  inner.style.transform = `scale(${scale})`;
  wrap.style.width = `${state.wfGraphSize.w * scale}px`;
  wrap.style.height = `${state.wfGraphSize.h * scale}px`;
}

function renderWorkflowNodeInspector() {
  const box = document.getElementById("workflowNodeInspector");
  const labelEl = document.getElementById("wfNodeLabel");
  const typeEl = document.getElementById("wfNodeType");
  if (!box) return;
  const graph = wfParseGraph();
  const node = graph?.nodes?.find((item) => item.id === state.selectedWorkflowNode);
  if (!node) {
    box.innerHTML = '<div class="hint">คลิกโหนดในกราฟด้านบนเพื่อดูฟิลด์ของโหนด</div>';
    if (labelEl) labelEl.textContent = "เลือกโหนด";
    if (typeEl) typeEl.textContent = "คลิกโหนดในกราฟ";
    return;
  }
  if (labelEl) labelEl.textContent = node.id;
  const typeName = { worker: "Worker node", human_gate: "Human gate", manager: "Manager node", join: "Join node" }[node.type] || node.type;
  if (typeEl) typeEl.textContent = typeName;
  const fields = [];
  if (node.type === "human_gate") {
    fields.push(["เหตุผล", node.reason || "—", false]);
    const decisions = (node.choices || []).map((choice) => choice.label || choice.id).join(" / ");
    fields.push(["การตัดสินใจ", decisions || "อนุมัติ / ปฏิเสธ", false]);
    fields.push(["สร้าง job", "ไม่ — โหนด control plane", false]);
  } else {
    fields.push([node.type === "manager" ? "Manager" : "Worker", node.worker_id || (node.role ? `role=${node.role}` : "auto"), false]);
    if (node.prompt) fields.push(["Prompt", node.prompt, true]);
    if (node.outputs) fields.push(["Outputs", Array.isArray(node.outputs) ? node.outputs.join(", ") : node.outputs, false]);
    if (node.budget_units != null) fields.push(["Budget units", String(node.budget_units), false]);
    if (node.mode) fields.push(["Join mode", `${node.mode}${node.quorum ? ` · quorum ${node.quorum}` : ""}`, false]);
  }
  box.innerHTML = `<div class="wf-fields">${fields.map(([label, value, mono]) => `<div><div class="wf-field-l">${escapeHtml(label)}</div><span class="wf-field-v ${mono ? "mono" : ""}">${escapeHtml(value)}</span></div>`).join("")}</div>`;
}

function renderWorkerSuggestions() {
  $("#workerSuggestionList").innerHTML = state.workerSuggestions.length ? state.workerSuggestions.map((item) => `
    <article class="workflow-run-item">
      <div class="item-title"><span>${escapeHtml(item.node_id)} · ${escapeHtml(item.role || "no role")}</span><span>${escapeHtml(item.state)}</span></div>
      <div class="item-sub">${escapeHtml(item.reason)}</div>
      <div class="item-sub">${escapeHtml(item.worker_id || "no worker")} ${item.workspace_id ? `· ${escapeHtml(item.workspace_id)}` : ""}</div>
      ${item.worker_id || item.workspace_id ? `<button class="preview-btn apply-worker-suggestion" type="button" data-node-id="${escapeHtml(item.node_id)}">Apply To JSON</button>` : ""}
    </article>
  `).join("") : "";
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
  if (minutes < 1) return "ไม่ถึง 1 น.";
  if (minutes < 60) return `${minutes} น.`;
  return `${Math.floor(minutes / 60)} ชม. ${minutes % 60} น.`;
}
function wfBarColor(runState) {
  const cls = statusClass(runState);
  if (["failed", "recovery-required"].includes(cls)) return "var(--nt-danger)";
  if (cls === "succeeded") return "var(--nt-success)";
  return "var(--nt-yellow-500)";
}
function nodeChip(stateName, label) {
  return `<span class="nchip ${stateName}"><span class="d"></span>${escapeHtml(label)}</span>`;
}
function renderNodeChips(run) {
  const definition = state.workflows.find((item) => item.id === run.workflow_definition_id);
  const nodes = definition?.graph?.nodes;
  const counters = run.counters || {};
  const done = new Set(counters.completed_nodes || []);
  const failed = new Set(counters.failed_nodes || []);
  const waiting = new Set(state.approvals.filter((approval) => approval.run_id === run.id).map((approval) => approval.node_key));
  if (!Array.isArray(nodes) || !nodes.length) {
    const chips = [...done].map((id) => nodeChip("done", id)).concat([...failed].map((id) => nodeChip("err", id)));
    return chips.length ? chips.join("") : '<span class="hint">ไม่มีข้อมูลโหนด</span>';
  }
  let markedRun = false;
  return nodes.map((node) => {
    let stateName;
    if (done.has(node.id)) stateName = "done";
    else if (failed.has(node.id)) stateName = "err";
    else if (waiting.has(node.id)) stateName = "wait";
    else if (run.state === "running" && !markedRun) { stateName = "run"; markedRun = true; }
    else stateName = "pend";
    return nodeChip(stateName, node.id);
  }).join("");
}

function renderWorkflowRuns() {
  const runs = state.selectedWorkflowId ? state.workflowRuns.filter((run) => run.workflow_definition_id === state.selectedWorkflowId) : state.workflowRuns;
  const list = $("#workflowRunList");
  const legend = document.getElementById("monRunsLegend");
  if (legend) {
    const running = runs.filter((run) => run.state === "running").length;
    const waiting = runs.filter((run) => run.state === "waiting_for_human").length;
    legend.innerHTML = `<span><span class="d" style="background:var(--nt-yellow-500)"></span>${running} ทำงาน</span><span><span class="d" style="background:var(--nt-warning)"></span>${waiting} รออนุมัติ</span>`;
  }
  if (!runs.length) {
    list.innerHTML = '<div class="empty">ยังไม่มี run</div>';
  } else {
    list.innerHTML = runs.slice(0, 20).map((run) => {
      const progress = wfRunProgress(run);
      return `
      <button class="run-row ${run.id === state.selectedWorkflowRunId ? "selected" : ""}" type="button" data-run-id="${escapeHtml(run.id)}">
        <div class="run-row-top">
          <span class="status ${statusClass(run.state)} dot-only" aria-hidden="true"></span>
          <span class="name">${escapeHtml(run.name)}</span>
          <span class="status ${statusClass(run.state)}">${escapeHtml(run.state)}</span>
        </div>
        <div class="run-bar"><div class="track"><div class="fill" style="width:${progress.pct}%;background:${wfBarColor(run.state)}"></div></div><span class="pct">${progress.pct}%</span></div>
        <div class="run-row-meta"><span class="mono">${escapeHtml(shortId(run.id))}</span><span>·</span><span>${escapeHtml(wfElapsed(run))}</span></div>
      </button>`;
    }).join("");
  }

  const run = state.workflowRunDetail?.run;
  const set = (id, fn) => { const node = document.getElementById(id); if (node) fn(node); };
  set("monRunName", (node) => { node.textContent = run ? run.name : "เลือก run จากรายการ"; });
  set("monRunChip", (node) => { node.hidden = !run; if (run) { node.className = `status ${statusClass(run.state)}`; node.textContent = run.state; } });
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

  const progress = run ? wfRunProgress(run) : { done: 0, total: 0, pct: 0 };
  set("monProgressBar", (node) => { node.style.width = `${progress.pct}%`; node.style.background = run ? wfBarColor(run.state) : "var(--nt-yellow-500)"; });
  set("monStats", (node) => {
    if (!run) { node.innerHTML = ""; return; }
    const counters = run.counters || {};
    const stats = [
      ["โหนดเสร็จ", `${progress.done} / ${progress.total}`],
      ["งาน (jobs)", String(counters.jobs_started ?? 0)],
      ["budget units", String(counters.budget_units_spent ?? 0)],
      ["iteration", counters.iteration != null ? String(counters.iteration) : "—"],
      ["เวลาที่ใช้", wfElapsed(run)],
    ];
    node.innerHTML = stats.map(([label, value]) => `<div class="mon-stat"><div class="l">${escapeHtml(label)}</div><div class="v">${escapeHtml(value)}</div></div>`).join("");
  });
  set("monNodeChips", (node) => { node.innerHTML = run ? renderNodeChips(run) : '<span class="hint">เลือก run เพื่อดูความคืบหน้าของโหนด</span>'; });
  set("workflowEventList", (node) => {
    node.innerHTML = state.workflowEvents.slice(0, 14).map((event) => {
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
  const managerDecisions = state.workflowEvents.filter((event) => event.event_type.startsWith("manager_proposal_"));
  $("#managerDecisionList").innerHTML = managerDecisions.length ? managerDecisions.map((event) => `
    <article class="event-item">
      <div class="item-title"><span>${escapeHtml(event.node_key || "manager")}</span><span class="status ${statusClass(event.payload?.state)}">${escapeHtml(event.payload?.state || "unknown")}</span></div>
      <div class="item-sub">${escapeHtml(event.payload?.reason || "")}</div>
      <pre class="event-payload">${escapeHtml(JSON.stringify(event.payload?.proposal || event.payload?.response || {}, null, 2))}</pre>
    </article>`).join("") : '<div class="empty">No manager decisions</div>';
  renderRunSummary();
}

function renderApprovals() {
  let approvals = state.approvals;
  if (state.selectedWorkflowRunId) {
    approvals = approvals.filter((approval) => approval.run_id === state.selectedWorkflowRunId);
  } else if (state.selectedWorkflowId) {
    const runIds = new Set(state.workflowRuns.filter((run) => run.workflow_definition_id === state.selectedWorkflowId).map((run) => run.id));
    approvals = approvals.filter((approval) => runIds.has(approval.run_id));
  }
  const count = document.getElementById("approvalCount");
  if (count) count.textContent = `${approvals.length} รายการรออยู่`;
  $("#approvalList").innerHTML = approvals.length ? approvals.map((approval) => {
    const wfName = state.workflowRuns.find((run) => run.id === approval.run_id)?.name || approval.node_key;
    const choices = (approval.choices || []).map((choice) => `<button class="primary-btn choose-approval" type="button" data-approval-id="${escapeHtml(approval.id)}" data-choice="${escapeHtml(choice.id)}">${escapeHtml(choice.label)}</button>`).join("");
    return `
    <div class="approval-card">
      <div class="ac-top"><span class="ac-run">${escapeHtml(shortId(approval.run_id))}</span><span class="ac-wf">· ${escapeHtml(wfName)}</span><span class="ac-ago">${escapeHtml(formatTime(approval.created_at))}</span></div>
      <div class="ac-title">${escapeHtml(approval.label)}</div>
      <div class="ac-reason">${escapeHtml(approval.reason || "")}</div>
      <div class="ac-actions">
        ${choices || `<button class="primary-btn approve-approval" type="button" data-approval-id="${escapeHtml(approval.id)}">อนุมัติ</button>`}
        <button class="secondary-btn reject-approval" type="button" data-approval-id="${escapeHtml(approval.id)}">ปฏิเสธ</button>
      </div>
    </div>`;
  }).join("") : '<div class="empty">ไม่มีรายการรออนุมัติ</div>';
}

function renderWorkflowTriggers() {
  const list = $("#triggerList");
  const triggers = state.selectedWorkflowId ? state.workflowTriggers.filter((trigger) => trigger.workflow_definition_id === state.selectedWorkflowId) : state.workflowTriggers;
  if (state.selectedWorkflowTriggerId && !triggers.some((trigger) => trigger.id === state.selectedWorkflowTriggerId)) {
    state.selectedWorkflowTriggerId = null;
    state.workflowTriggerEvents = [];
  }
  if (!triggers.length) {
    list.innerHTML = '<div class="empty">No triggers</div>';
    $("#triggerEventList").textContent = "";
    return;
  }
  list.innerHTML = triggers.map((trigger) => `
    <article class="trigger-item ${trigger.id === state.selectedWorkflowTriggerId ? "selected" : ""}" data-trigger-id="${escapeHtml(trigger.id)}">
      <div class="item-title">
        <span>${escapeHtml(trigger.name)}</span>
        <span>${escapeHtml(trigger.type)}</span>
      </div>
      <div class="item-sub">${trigger.enabled ? "enabled" : "disabled"} · next ${escapeHtml(formatTime(trigger.next_fire_at) || "manual")} · ${shortId(trigger.id)}</div>
      <div class="item-sub">last ${escapeHtml(trigger.last_event_state || "never")} ${escapeHtml(formatTime(trigger.last_event_at))}${trigger.last_event_error ? ` · ${escapeHtml(trigger.last_event_error)}` : ""}</div>
      <div class="item-actions">
        <button class="secondary-btn toggle-trigger" data-trigger-id="${escapeHtml(trigger.id)}" data-enabled="${trigger.enabled ? "false" : "true"}">${trigger.enabled ? "Disable" : "Enable"}</button>
        ${["manual", "schedule", "webhook"].includes(trigger.type) ? `<button class="secondary-btn fire-trigger" data-trigger-id="${escapeHtml(trigger.id)}">Fire</button>` : ""}
        <button class="danger-btn delete-trigger" data-trigger-id="${escapeHtml(trigger.id)}">Delete</button>
      </div>
    </article>
  `).join("");
  $("#triggerEventList").textContent = state.workflowTriggerEvents.length ? prettyJson(state.workflowTriggerEvents.slice(0, 10)) : "";
}

function renderAudit() {
  const list = $("#auditList");
  const filter = state.auditFilter || "all";
  let rows = state.audit;
  if (filter !== "all") {
    rows = rows.filter((entry) => (entry.resource_type || "").includes(filter) || (entry.action || "").includes(filter));
  }
  if (!rows.length) {
    list.innerHTML = '<div class="empty">ไม่มีรายการ</div>';
    return;
  }
  list.innerHTML = rows.slice(0, 30).map((entry) => {
    const action = entry.action || "";
    let tone = "grey";
    if (/fail|error|cancel|reject|refused|denied|delete/.test(action)) tone = "danger";
    else if (/grant|approv/.test(action)) tone = "brand";
    else if (/succeed|complete|online|bind/.test(action)) tone = "success";
    const details = entry.details && typeof entry.details === "object"
      ? Object.entries(entry.details).map(([key, value]) => `${key}=${value}`).join(" · ")
      : String(entry.details || "");
    return `
    <div class="audit-row">
      <span class="audit-time">${escapeHtml(formatTime(entry.created_at))}</span>
      <span class="audit-action ${tone}">${escapeHtml(action)}</span>
      <span class="audit-target">${escapeHtml(entry.resource_id || entry.resource_type || "")}</span>
      <span class="audit-detail">${escapeHtml(details)}</span>
      <span class="audit-actor"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>${escapeHtml(entry.actor || "system")}</span>
    </div>`;
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
  const response = await fetch(`/api/usage?${params.toString()}`, { headers });
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

// Authenticated artifact download: a plain <a href> can't attach the Bearer token, so an
// auth-enabled instance would 401. Fetch with the header (like downloadUsage) and save the blob.
async function downloadArtifact(artifactId, filename) {
  const headers = new Headers();
  const token = localStorage.getItem("atlasApiToken");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`/api/artifacts/${encodeURIComponent(artifactId)}/content`, { headers });
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
  $("#usageEstCost").textContent = estCost ? `$${estCost.toFixed(4)}` : "$0";
  $("#usageMeta").textContent = (data.from || data.to)
    ? `ช่วง ${data.from || "…"} → ${data.to || "…"}`
    : "ทุกช่วงเวลา";
  renderUsageBars(data.usage || []);
  renderUsageQuota(totals.workflow_runs ?? 0);
  renderUsageEvents(data.usage || []);
  renderUsageAlert(totals.workflow_runs ?? 0);
}

const USAGE_DOW = ["อา", "จ", "อ", "พ", "พฤ", "ศ", "ส"];

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
  if (!days.length) { box.innerHTML = '<div class="hint">ยังไม่มีข้อมูล run ในช่วงนี้</div>'; return; }
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
    box.innerHTML = '<p class="hint" style="margin-top:14px">ใส่ Expected runs เพื่อดูโควตาและการแจ้งเตือน</p>';
    return;
  }
  const pct = Math.round((usedRuns / expected) * 100);
  const over = pct >= threshold;
  box.innerHTML = `
    <div class="usage-quota-val"><b>${usedRuns}</b><span>/ ${expected} run ที่คาดไว้</span></div>
    <div class="usage-quota-track"><div class="usage-quota-fill ${over ? "over" : ""}" style="width:${Math.min(100, pct)}%"></div></div>
    <div class="usage-quota-note ${over ? "over" : ""}"><span class="d"></span><span>ใช้ไป <b>${pct}%</b> ของเป้า · ${over ? `เกินเกณฑ์แจ้งเตือน ${threshold}%` : `ต่ำกว่าเกณฑ์แจ้งเตือน ${threshold}%`}</span></div>`;
}

function renderUsageEvents(events) {
  const box = document.getElementById("usageEvents");
  if (!box) return;
  if (!events.length) { box.innerHTML = '<div class="empty">ไม่มีเหตุการณ์ในช่วงนี้</div>'; return; }
  box.innerHTML = events.slice(0, 30).map((event) => `
    <div class="ue-row">
      <span>${escapeHtml(event.kind || "—")}</span>
      <span class="ref">${escapeHtml(event.reference || event.resource_id || event.idempotency_key || "—")}</span>
      <span>${escapeHtml(event.units ?? 0)}</span>
      <span class="secs">${escapeHtml(event.seconds ?? 0)}</span>
      <span>${escapeHtml(formatTime(event.created_at))}</span>
    </div>`).join("");
}

async function submitJob() {
  const prompt = $("#promptInput").value.trim();
  if (!prompt) {
    toast("Prompt is required");
    return;
  }
  const payload = {
    prompt,
    conversation_id: $("#conversationSelect").value || undefined,
    worker_id: $("#workerSelect").value || undefined,
    workspace_id: $("#workspaceSelect").value || undefined,
    model: $("#modelInput").value.trim() || undefined,
  };
  // T5: optional collect_files (relative paths, comma-separated); T3: opt-in async callback.
  const collectFiles = $("#collectFilesInput").value.split(",").map((path) => path.trim()).filter(Boolean);
  if (collectFiles.length) payload.collect_files = collectFiles;
  if ($("#jobExecutionCallback").checked) payload.execution = "callback";
  if ($("#handoffEnabled").checked) {
    const handoffWorkspaceId = $("#handoffWorkspaceSelect").value || undefined;
    const handoffWorkerId = $("#handoffWorkerSelect").value || undefined;
    if (!handoffWorkspaceId && !handoffWorkerId) {
      toast("Choose a handoff worker or workspace");
      return;
    }
    payload.handoff = {
      enabled: true,
      worker_id: handoffWorkspaceId ? undefined : handoffWorkerId,
      workspace_id: handoffWorkspaceId,
      prompt: $("#handoffPromptInput").value.trim() || DEFAULT_NEWS_HANDOFF_PROMPT,
    };
  }
  const data = await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
  state.selectedJobId = data.job.id;
  state.streamText = "";
  state.events = [];
  $("#promptInput").value = "";
  $("#collectFilesInput").value = "";
  await loadAll();
  openJobStream(data.job.id);
  showView("jobs");
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
  renderToolTimeline();
  renderJobs();

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
      const response = await fetch(`/api/jobs/${jobId}/events?after=0`, { headers, signal: controller.signal });
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
      <span class="tl-count">เครื่องมือ · tools <b>${counts.run}</b></span>
      <span class="tl-count denied">ปฏิเสธ · denied <b>${counts.denied}</b></span>
      <span class="tl-count err">ล้มเหลว · failed <b>${counts.failed}</b></span>`;
  }
  const listNode = document.getElementById("toolTimelineList");
  if (!listNode) return;
  if (!calls.length) {
    listNode.innerHTML = `<div class="empty">ยังไม่มีการเรียกเครื่องมือ</div>`;
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
  set("jobDetailChip", (n) => { n.className = `status ${st}`; n.textContent = job ? job.state : "—"; });
  set("jobDetailId", (n) => { n.textContent = job ? job.id : ""; });
  set("jobDetailPrompt", (n) => { n.textContent = job ? job.prompt : "เลือกงานจากรายการเพื่อดูสตรีมสด"; });
  set("jobDetailRoute", (n) => {
    n.textContent = job
      ? `${job.route_reason || `${job.worker_name || shortId(job.worker_id)} · ${job.workspace_key || "auto"}`}${job.handoff_job_id ? ` · handoff ${shortId(job.handoff_job_id)}` : ""}`
      : "";
  });
  const live = !!job && ["queued", "running", "cancel_requested"].includes(job.state);
  const operator = ["admin", "operator"].includes(state.currentUser?.role);
  set("cancelJobBtn", (n) => { n.disabled = !job || !operator || !["queued", "running", "cancel_requested"].includes(job.state); });
  set("streamOutput", (n) => { n.classList.toggle("stream-live", live); });
}

function composerRouteText() {
  const workspaceId = $("#workspaceSelect").value;
  const workerId = $("#workerSelect").value;
  const conversationId = $("#conversationSelect").value;
  if (workspaceId) {
    const workspace = state.workspaces.find((item) => item.id === workspaceId);
    return `Explicit workspace: ${workspace?.workspace_key || shortId(workspaceId)} · ${workspace?.worker_name || ""}`;
  }
  if (workerId) {
    const worker = state.workers.find((item) => item.id === workerId);
    return `Explicit worker: ${worker?.name || shortId(workerId)}`;
  }
  if (conversationId) return "Auto route: existing conversation binding first";
  return "Auto route: online status, workspace key, company, tags, role";
}

function updateComposerRoutePreview() {
  if (state.selectedJobId) return;
  $("#routePreview").textContent = composerRouteText();
}

async function saveWorker(event) {
  event.preventDefault();
  const formElement = event.currentTarget || event.target.closest("form");
  if (!formElement) throw new Error("Worker form is not available; refresh the page");
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
}

async function saveWorkspace(event) {
  event.preventDefault();
  const formElement = event.currentTarget || event.target.closest("form");
  if (!formElement) throw new Error("Workspace form is not available; refresh the page");
  const form = new FormData(formElement);
  const payload = Object.fromEntries(form.entries());
  await api("/api/workspaces", { method: "POST", body: JSON.stringify(payload) });
  formElement.reset();
  closeModals();
  toast("Workspace saved");
  await loadAll();
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

function newWorkflow() {
  state.selectedWorkflowId = null;
  state.selectedWorkflowNode = null;
  state.selectedWorkflowRunId = null;
  state.selectedWorkflowTriggerId = null;
  state.workflowRunDetail = null;
  state.workflowArtifacts = [];
  state.workflowEvents = [];
  state.workflowTriggerEvents = [];
  state.workerSuggestions = [];
  setDirty(false);
  $("#draftResult").textContent = "";
  $("#workflowExplanation").textContent = "";
  $("#workflowRunInput").value = "{}";
  renderWorkflowEditor(true);
  renderWorkflowList();
  renderWorkflowRuns();
}

function selectWorkflow(workflowId) {
  state.selectedWorkflowId = workflowId;
  state.selectedWorkflowNode = null;
  state.selectedWorkflowRunId = null;
  state.selectedWorkflowTriggerId = null;
  state.workflowRunDetail = null;
  state.workflowArtifacts = [];
  state.workflowEvents = [];
  state.workflowTriggerEvents = [];
  state.workerSuggestions = [];
  setDirty(false);
  $("#draftResult").textContent = "";
  $("#workflowExplanation").textContent = "";
  renderWorkflowEditor(true);
  renderWorkflows();
}

async function saveWorkflow() {
  const workflowId = $("#workflowNameInput").dataset.workflowId;
  const payload = {
    name: $("#workflowNameInput").value.trim() || "Untitled workflow",
    description: $("#workflowDescriptionInput").value.trim(),
    graph: parseJsonField("#workflowGraphInput"),
    policy: parseJsonField("#workflowPolicyInput"),
  };
  const path = workflowId ? `/api/workflows/${workflowId}` : "/api/workflows";
  const method = workflowId ? "PUT" : "POST";
  const data = await api(path, { method, body: JSON.stringify(payload) });
  state.selectedWorkflowId = data.workflow.id;
  setDirty(false);
  toast("Workflow saved");
  await loadAll();
}

async function validateWorkflow() {
  const workflowId = $("#workflowNameInput").dataset.workflowId;
  if (!workflowId) {
    toast("Save before validating");
    return;
  }
  await api(`/api/workflows/${workflowId}/validate`, {
    method: "POST",
    body: JSON.stringify({ graph: parseJsonField("#workflowGraphInput"), policy: parseJsonField("#workflowPolicyInput") }),
  });
  toast("Workflow valid");
}

async function explainWorkflow() {
  const workflowId = $("#workflowNameInput").dataset.workflowId;
  if (!workflowId) throw new Error("Save before explaining");
  const data = await api(`/api/workflows/${workflowId}/explain`, { method: "POST" });
  $("#workflowExplanation").textContent = data.explanation || "";
  toast("Explanation ready; workflow unchanged");
}

async function repairWorkflow() {
  const workflowId = $("#workflowNameInput").dataset.workflowId;
  if (!workflowId) throw new Error("Save before repairing");
  const data = await api(`/api/workflows/${workflowId}/repair`, {
    method: "POST",
    body: JSON.stringify({
      graph: parseJsonField("#workflowGraphInput"),
      policy: parseJsonField("#workflowPolicyInput"),
      triggers: [],
    }),
  });
  const draft = data.draft || {};
  $("#workflowGraphInput").value = prettyJson(draft.graph || {});
  $("#workflowPolicyInput").value = prettyJson(draft.policy || {});
  syncPolicyFormFromJson();
  $("#workflowExplanation").textContent = draft.explanation || "";
  $("#draftResult").textContent = prettyJson({ warnings: draft.warnings || [], triggers: draft.triggers || [] });
  const firstTrigger = (draft.triggers || [])[0];
  if (firstTrigger) {
    $("#triggerNameInput").value = firstTrigger.name || "Repaired trigger";
    $("#triggerTypeSelect").value = firstTrigger.type || "manual";
    $("#triggerConfigInput").value = prettyJson(firstTrigger.config || {});
    $("#triggerEnabledInput").checked = firstTrigger.enabled !== false;
  }
  setDirty(true);
  toast("Validated repair copied to previews; not saved");
}

async function suggestWorkers() {
  const data = await api("/api/workflows/suggest-workers", {
    method: "POST",
    body: JSON.stringify({
      graph: parseJsonField("#workflowGraphInput"),
      policy: parseJsonField("#workflowPolicyInput"),
    }),
  });
  state.workerSuggestions = data.suggestions || [];
  renderWorkerSuggestions();
  toast(state.workerSuggestions.length ? "Worker suggestions ready" : "No unresolved worker nodes");
}

function applyWorkerSuggestion(nodeId) {
  const suggestion = state.workerSuggestions.find((item) => item.node_id === nodeId);
  if (!suggestion) throw new Error("Worker suggestion is no longer available");
  const graph = parseJsonField("#workflowGraphInput");
  const node = (graph.nodes || []).find((item) => item.id === nodeId);
  if (!node) throw new Error(`Node no longer exists: ${nodeId}`);
  if (suggestion.worker_id) node.worker_id = suggestion.worker_id;
  if (suggestion.workspace_id) node.workspace_id = suggestion.workspace_id;
  $("#workflowGraphInput").value = prettyJson(graph);
  setDirty(true);
  toast("Suggestion applied to Graph JSON; not saved");
}

async function draftWorkflow() {
  const plain = $("#draftPromptInput").value.trim();
  if (!plain) {
    toast("Draft prompt is required");
    return;
  }
  const data = await api("/api/workflows/draft", { method: "POST", body: JSON.stringify({ plain_language_prompt: plain }) });
  const draft = data.draft || {};
  state.selectedWorkflowId = null;
  $("#workflowNameInput").dataset.workflowId = "";
  $("#workflowNameInput").value = draft.name || "Draft workflow";
  $("#workflowDescriptionInput").value = draft.description || "";
  $("#workflowGraphInput").value = prettyJson(draft.graph || defaultWorkflowGraph());
  $("#workflowPolicyInput").value = prettyJson(draft.policy || {});
  setDirty(true);
  $("#draftResult").textContent = prettyJson({
    explanation: draft.explanation || "",
    warnings: draft.warnings || [],
    triggers: draft.triggers || [],
  });
  renderWorkflowList();
  toast("Draft ready");
}

function applyWorkflowTemplate() {
  const template = state.workflowTemplates.find((item) => item.id === $("#workflowTemplateSelect").value);
  if (!template) throw new Error("Choose a template first");
  state.selectedWorkflowId = null;
  setDirty(true);
  $("#workflowNameInput").dataset.workflowId = "";
  $("#workflowNameInput").value = template.name;
  $("#workflowDescriptionInput").value = template.description || "";
  $("#workflowGraphInput").value = prettyJson(template.graph);
  $("#workflowPolicyInput").value = prettyJson(template.policy);
  $("#workflowMeta").textContent = "Template copied · not saved";
  renderWorkflowList();
  toast("Template copied to editor; save when ready");
}

async function runWorkflow() {
  const workflowId = $("#workflowNameInput").dataset.workflowId;
  if (!workflowId) {
    toast("Save before running");
    return;
  }
  const data = await api("/api/workflow-runs", {
    method: "POST",
    body: JSON.stringify({ workflow_definition_id: workflowId, input: parseJsonField("#workflowRunInput") }),
  });
  state.selectedWorkflowRunId = data.run.id;
  await loadAll();
  await selectWorkflowRun(data.run.id);
  showView("monitor");
  toast(`Workflow ${data.run.state}`);
}

async function uploadWorkflowFile() {
  if (!state.selectedWorkflowRunId) throw new Error("Select a workflow run first");
  const file = $("#workflowFileInput").files[0];
  const key = $("#workflowFileKeyInput").value.trim();
  if (!file || !key) throw new Error("File and file key are required");
  await api(`/api/workflow-runs/${state.selectedWorkflowRunId}/files?key=${encodeURIComponent(key)}`, {
    method: "POST",
    headers: { "Content-Type": file.type || "application/octet-stream", "X-Filename": file.name },
    body: file,
  });
  await loadWorkflowRunDetail(state.selectedWorkflowRunId);
  renderWorkflowRuns();
  $("#workflowFileInput").value = "";
  toast("File artifact uploaded");
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

async function retryInterruptedRun() {
  if (!state.selectedWorkflowRunId) return;
  const warning = state.workflowRunDetail?.run?.counters?.recovery?.warning || "Retry may duplicate worker side effects.";
  if (!confirm(`${warning}\n\nAuthorize retry of interrupted nodes?`)) return;
  await api(`/api/workflow-runs/${state.selectedWorkflowRunId}/resume`, {
    method: "POST",
    body: JSON.stringify({ retry_interrupted: true }),
  });
  await loadAll();
  toast("Interrupted-node retry authorized");
}

async function decideApproval(approvalId, action) {
  const approval = state.approvals.find((item) => item.id === approvalId);
  if (approval) state.selectedWorkflowRunId = approval.run_id;
  const data = await api(`/api/approvals/${approvalId}/${action}`, { method: "POST" });
  await loadAll();
  toast(`Approval ${data.approval.state}`);
}

async function chooseApproval(approvalId, choice) {
  const approval = state.approvals.find((item) => item.id === approvalId);
  if (approval) state.selectedWorkflowRunId = approval.run_id;
  const data = await api(`/api/approvals/${approvalId}/choose`, {
    method: "POST",
    body: JSON.stringify({ choice }),
  });
  await loadAll();
  toast(`Selected ${data.approval.selected_choice}`);
}

async function saveTrigger() {
  if (!state.selectedWorkflowId) {
    toast("Select a workflow first");
    return;
  }
  await api("/api/workflow-triggers", {
    method: "POST",
    body: JSON.stringify({
      workflow_definition_id: state.selectedWorkflowId,
      name: $("#triggerNameInput").value.trim() || "Manual",
      type: $("#triggerTypeSelect").value,
      config: parseJsonField("#triggerConfigInput"),
      enabled: $("#triggerEnabledInput").checked,
    }),
  });
  toast("Trigger created");
  await loadAll();
}

async function suggestTriggers() {
  if (!state.selectedWorkflowId) {
    toast("Select a saved workflow first");
    return;
  }
  const data = await api(`/api/workflows/${state.selectedWorkflowId}/suggest-triggers`, {
    method: "POST",
    body: JSON.stringify({ plain_language_prompt: $("#draftPromptInput").value.trim() || undefined }),
  });
  const triggers = data.triggers || [];
  $("#draftResult").textContent = prettyJson({ triggers });
  if (triggers[0]) {
    $("#triggerNameInput").value = triggers[0].name || "Suggested trigger";
    $("#triggerTypeSelect").value = triggers[0].type || "manual";
    $("#triggerConfigInput").value = prettyJson(triggers[0].config || {});
  }
  toast("Validated trigger drafts ready");
}

async function fireTrigger(triggerId) {
  const data = await api(`/api/workflow-triggers/${triggerId}/fire`, {
    method: "POST",
    body: JSON.stringify({ payload: parseJsonField("#workflowRunInput") }),
  });
  state.selectedWorkflowTriggerId = triggerId;
  if (data.run?.id) state.selectedWorkflowRunId = data.run.id;
  await loadAll();
  await selectWorkflowTrigger(triggerId);
  if (data.run?.id) {
    await selectWorkflowRun(data.run.id);
    showView("monitor");
  }
  toast(data.event?.state === "ignored" ? "Trigger ignored" : "Trigger fired");
}

async function toggleTrigger(triggerId, enabled) {
  const data = await api(`/api/workflow-triggers/${triggerId}`, {
    method: "PUT",
    body: JSON.stringify({ enabled }),
  });
  state.workflowTriggers = state.workflowTriggers.map((trigger) => trigger.id === triggerId ? data.trigger : trigger);
  renderWorkflowTriggers();
  toast(`Trigger ${enabled ? "enabled" : "disabled"}`);
}

async function selectWorkflowTrigger(triggerId) {
  state.selectedWorkflowTriggerId = triggerId;
  const data = await api(`/api/workflow-triggers/${triggerId}/events`);
  state.workflowTriggerEvents = data.events || [];
  renderWorkflowTriggers();
}

async function deleteTrigger(triggerId) {
  if (!confirm(`Delete trigger ${shortId(triggerId)}?`)) return;
  await api(`/api/workflow-triggers/${triggerId}`, { method: "DELETE" });
  if (state.selectedWorkflowTriggerId === triggerId) {
    state.selectedWorkflowTriggerId = null;
    state.workflowTriggerEvents = [];
  }
  toast("Trigger deleted");
  await loadAll();
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

function closeModals() {
  const wasOpen = !$("#workerModal").hidden || !$("#workspaceModal").hidden;
  $("#workerModal").hidden = true;
  $("#workspaceModal").hidden = true;
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
  if (!confirm(`Delete worker ${worker?.name || workerId}? Its workspaces will be removed too. Workers with job history are kept for audit and cannot be deleted.`)) return;
  await api(`/api/workers/${workerId}`, { method: "DELETE" });
  if (state.selectedJobId) state.selectedJobId = null;
  toast("Worker deleted");
  await loadAll();
}

async function deleteWorkspace(workspaceId) {
  const workspace = state.workspaces.find((item) => item.id === workspaceId);
  if (!confirm(`Delete workspace ${workspace?.workspace_key || workspaceId}?`)) return;
  await api(`/api/workspaces/${workspaceId}`, { method: "DELETE" });
  toast("Workspace deleted");
  await loadAll();
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
  const copyButton = event.target.closest("[data-copy]");
  if (copyButton) {
    try {
      await navigator.clipboard.writeText(copyButton.dataset.copy);
      toast("คัดลอกแล้ว");
    } catch {
      toast("คัดลอกไม่สำเร็จ");
    }
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
  const workflowItem = event.target.closest(".workflow-item");
  if (workflowItem) {
    selectWorkflow(workflowItem.dataset.workflowId);
    return;
  }
  const wfNode = event.target.closest("[data-wf-node]");
  if (wfNode) {
    state.selectedWorkflowNode = wfNode.dataset.wfNode;
    renderWorkflowGraph();
    return;
  }
  const runRow = event.target.closest(".run-row, .workflow-run-item[data-run-id]");
  if (runRow && runRow.dataset.runId) {
    await selectWorkflowRun(runRow.dataset.runId).catch((error) => toast(error.message));
    return;
  }
  const applyWorkerButton = event.target.closest(".apply-worker-suggestion");
  if (applyWorkerButton) {
    try { applyWorkerSuggestion(applyWorkerButton.dataset.nodeId); } catch (error) { toast(error.message); }
    return;
  }
  const approveButton = event.target.closest(".approve-approval");
  if (approveButton) {
    await decideApproval(approveButton.dataset.approvalId, "approve").catch((error) => toast(error.message));
    return;
  }
  const rejectButton = event.target.closest(".reject-approval");
  if (rejectButton) {
    await decideApproval(rejectButton.dataset.approvalId, "reject").catch((error) => toast(error.message));
    return;
  }
  const chooseButton = event.target.closest(".choose-approval");
  if (chooseButton) {
    await chooseApproval(chooseButton.dataset.approvalId, chooseButton.dataset.choice).catch((error) => toast(error.message));
    return;
  }
  const fireTriggerButton = event.target.closest(".fire-trigger");
  if (fireTriggerButton) {
    await fireTrigger(fireTriggerButton.dataset.triggerId).catch((error) => toast(error.message));
    return;
  }
  const toggleTriggerButton = event.target.closest(".toggle-trigger");
  if (toggleTriggerButton) {
    await toggleTrigger(toggleTriggerButton.dataset.triggerId, toggleTriggerButton.dataset.enabled === "true").catch((error) => toast(error.message));
    return;
  }
  const deleteTriggerButton = event.target.closest(".delete-trigger");
  if (deleteTriggerButton) {
    await deleteTrigger(deleteTriggerButton.dataset.triggerId).catch((error) => toast(error.message));
    return;
  }
  const triggerItem = event.target.closest(".trigger-item");
  if (triggerItem) {
    await selectWorkflowTrigger(triggerItem.dataset.triggerId).catch((error) => toast(error.message));
    return;
  }
  const jobItem = event.target.closest(".job-row");
  if (jobItem) {
    openJobStream(jobItem.dataset.jobId);
  }
});

$("#submitJobBtn").addEventListener("click", () => submitJob().catch((error) => toast(error.message)));
// Command: handoff switch reveals its form; ⌘/Ctrl+Enter submits the prompt.
$("#handoffEnabled").addEventListener("change", (event) => {
  $("#handoffBox").classList.toggle("is-open", event.currentTarget.checked);
});
$("#promptInput").addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
    event.preventDefault();
    submitJob().catch((error) => toast(error.message));
  }
});
// Jobs: stream / events tab toggle.
for (const tab of document.querySelectorAll(".tab[data-job-tab]")) {
  tab.addEventListener("click", () => {
    for (const other of document.querySelectorAll(".tab[data-job-tab]")) other.classList.toggle("is-active", other === tab);
    for (const pane of document.querySelectorAll("[data-job-pane]")) pane.hidden = pane.dataset.jobPane !== tab.dataset.jobTab;
  });
}
$("#refreshBtn").addEventListener("click", (event) => {
  const btn = event.currentTarget;
  btn.classList.add("spinning");
  setTimeout(() => btn.classList.remove("spinning"), 600);
  refreshAll({ poll: true, notice: true }).catch((error) => toast(error.message));
});
// Overview "ดูทั้งหมด" buttons and list rows navigate to a view.
document.addEventListener("click", (event) => {
  const link = event.target.closest("[data-view-link]");
  if (!link) return;
  showView(link.dataset.viewLink);
  if (link.dataset.viewLink === "usage") loadUsage().catch((error) => toast(error.message));
});
$("#pollAllBtn").addEventListener("click", async () => {
  await refreshAll({ poll: true, notice: true }).catch((error) => toast(error.message));
});
$("#cancelJobBtn").addEventListener("click", () => cancelSelectedJob().catch((error) => toast(error.message)));
$("#newWorkflowBtn").addEventListener("click", () => newWorkflow());
$("#saveWorkflowBtn").addEventListener("click", () => saveWorkflow().catch((error) => toast(error.message)));
$("#validateWorkflowBtn").addEventListener("click", () => validateWorkflow().catch((error) => toast(error.message)));
$("#explainWorkflowBtn").addEventListener("click", () => explainWorkflow().catch((error) => toast(error.message)));
$("#repairWorkflowBtn").addEventListener("click", () => repairWorkflow().then(() => renderWorkflowGraph()).catch((error) => toast(error.message)));
$("#suggestWorkersBtn").addEventListener("click", () => suggestWorkers().catch((error) => toast(error.message)));
$("#draftWorkflowBtn").addEventListener("click", () => draftWorkflow().catch((error) => toast(error.message)));
$("#applyWorkflowTemplateBtn").addEventListener("click", () => {
  try {
    applyWorkflowTemplate();
    syncPolicyFormFromJson();
    const display = document.getElementById("workflowName");
    if (display) display.textContent = $("#workflowNameInput").value || "เวิร์กโฟลว์ใหม่";
    renderWorkflowGraph();
  } catch (error) { toast(error.message); }
});
$("#builderAddNodeBtn").addEventListener("click", () => {
  try { addBuilderNode(); renderWorkflowGraph(); } catch (error) { toast(error.message); }
});
$("#builderAddEdgeBtn").addEventListener("click", () => {
  try { addBuilderEdge(); renderWorkflowGraph(); } catch (error) { toast(error.message); }
});
$("#runWorkflowBtn").addEventListener("click", () => runWorkflow().catch((error) => toast(error.message)));
$("#uploadWorkflowFileBtn").addEventListener("click", () => uploadWorkflowFile().catch((error) => toast(error.message)));
$("#pauseWorkflowRunBtn").addEventListener("click", () => controlWorkflowRun("pause").catch((error) => toast(error.message)));
$("#resumeWorkflowRunBtn").addEventListener("click", () => controlWorkflowRun("resume").catch((error) => toast(error.message)));
$("#retryInterruptedRunBtn").addEventListener("click", () => retryInterruptedRun().catch((error) => toast(error.message)));
$("#cancelWorkflowRunBtn").addEventListener("click", () => controlWorkflowRun("cancel").catch((error) => toast(error.message)));
$("#saveTriggerBtn").addEventListener("click", () => saveTrigger().catch((error) => toast(error.message)));
$("#applyTriggerQuickBtn").addEventListener("click", () => {
  try { applyQuickTrigger(); } catch (error) { toast(error.message); }
});
$("#suggestTriggersBtn").addEventListener("click", () => suggestTriggers().catch((error) => toast(error.message)));
$("#addWorkerBtn").addEventListener("click", () => openWorkerModal());
$("#addWorkspaceBtn").addEventListener("click", () => openWorkspaceModal());
$("#conversationSelect").addEventListener("change", () => {
  state.selectedJobId = null;
  updateComposerRoutePreview();
});
$("#workerSelect").addEventListener("change", () => {
  state.selectedJobId = null;
  updateComposerRoutePreview();
});
$("#workspaceSelect").addEventListener("change", () => {
  state.selectedJobId = null;
  updateComposerRoutePreview();
});
$("#workerForm").addEventListener("submit", (event) => saveWorker(event).catch((error) => toast(error.message)));
$("#workspaceForm").addEventListener("submit", (event) => saveWorkspace(event).catch((error) => toast(error.message)));
$("#loginForm").addEventListener("submit", (event) => login(event).catch((error) => {
  $("#loginMessage").textContent = error.message === "unauthorized" ? "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง" : error.message;
}));
$("#signOutBtn").addEventListener("click", () => signOut());
for (const selector of ["#workflowNameInput", "#workflowDescriptionInput", "#workflowGraphInput", "#workflowPolicyInput"]) {
  $(selector).addEventListener("input", () => { setDirty(true); });
}
$("#workflowPolicyInput").addEventListener("input", () => { syncPolicyFormFromJson(); });
for (const [, selector] of POLICY_FORM_FIELDS) {
  $(selector).addEventListener("input", () => { syncPolicyJsonFromForm(); });
}
// Workflows: editing the Graph JSON re-renders the node graph; the name display tracks the input.
$("#workflowGraphInput").addEventListener("input", () => { renderWorkflowGraph(); });
$("#workflowNameInput").addEventListener("input", () => {
  const display = document.getElementById("workflowName");
  if (display) display.textContent = $("#workflowNameInput").value || "เวิร์กโฟลว์ใหม่";
});
for (const tab of document.querySelectorAll(".tab-pill[data-wf-tab]")) {
  tab.addEventListener("click", () => {
    for (const other of document.querySelectorAll(".tab-pill[data-wf-tab]")) other.classList.toggle("is-active", other === tab);
    for (const pane of document.querySelectorAll("[data-wf-pane]")) pane.hidden = pane.dataset.wfPane !== tab.dataset.wfTab;
  });
}
$("#wfZoomIn").addEventListener("click", () => { wfUserZoom = Math.min(2, wfUserZoom * 1.15); wfApplyScale(); });
$("#wfZoomOut").addEventListener("click", () => { wfUserZoom = Math.max(0.5, wfUserZoom / 1.15); wfApplyScale(); });

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
    if (button.dataset.view === "usage") loadUsage().catch((error) => toast(error.message));
  });
}
for (const chip of document.querySelectorAll(".audit-chip[data-audit-filter]")) {
  chip.addEventListener("click", () => {
    state.auditFilter = chip.dataset.auditFilter;
    for (const other of document.querySelectorAll(".audit-chip")) other.classList.toggle("is-active", other === chip);
    renderAudit();
  });
}
$("#loadUsageBtn").addEventListener("click", () => loadUsage().catch((error) => toast(error.message)));
$("#usageJsonBtn").addEventListener("click", () => downloadUsage("").catch((error) => toast(error.message)));
$("#usageCsvBtn").addEventListener("click", () => downloadUsage("csv").catch((error) => toast(error.message)));
showView(localStorage.getItem("atlasView") || "overview");

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeModals();
});

loadAll()
  .then(() => {
    document.body.classList.remove("is-loading");
    // If the Usage view was restored from localStorage, load it now that auth/role are known.
    if (document.querySelector("#view-usage.is-active")) loadUsage().catch(() => undefined);
    const firstActive = state.jobs.find((job) => ["running", "queued", "cancel_requested"].includes(job.state));
    if (firstActive) openJobStream(firstActive.id);
  })
  .catch((error) => {
    document.body.classList.remove("is-loading");
    toast(error.message);
  });

setInterval(() => {
  if (!$("#loginScreen").hidden) return;
  loadAll().catch(() => undefined);
}, 5000);

setInterval(() => {
  if (!$("#loginScreen").hidden) return;
  refreshAll({ poll: true }).catch(() => undefined);
}, AUTO_POLL_MS);
