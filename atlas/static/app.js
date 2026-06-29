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
  selectedWorkflowRunId: null,
  selectedWorkflowTriggerId: null,
  workflowRunDetail: null,
  workflowArtifacts: [],
  workflowEvents: [],
  workflowTriggerEvents: [],
  workerSuggestions: [],
  eventSource: null,
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
      showLogin("Your session is missing or expired.");
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

function statusClass(value) {
  return String(value || "unknown").replaceAll("_", "-");
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

const VIEWS = ["command", "workflows", "monitor", "jobs", "audit", "fleet"];

function showView(view) {
  if (!VIEWS.includes(view)) view = "command";
  for (const section of document.querySelectorAll(".view")) {
    section.classList.toggle("is-active", section.dataset.view === view);
  }
  for (const button of document.querySelectorAll(".nav-item")) {
    const active = button.dataset.view === view;
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
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
  graph.edges = [...(graph.edges || []), { from, to, condition }];
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
  $("#lastRefresh").textContent = `Refreshed ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
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
}

function showLogin(message = "Use your instance account.") {
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
  $("#loginMessage").textContent = "Use your instance account.";
}

function renderIdentity() {
  const panel = $("#signedInPanel");
  panel.hidden = !state.currentUser;
  $("#signedInIdentity").textContent = state.currentUser ? `${state.currentUser.username} (${state.currentUser.role})` : "";
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
  for (const node of document.querySelectorAll(".edit-worker, .delete-worker, .edit-workspace, .delete-workspace")) node.disabled = !admin;
  for (const node of document.querySelectorAll(".poll-worker, .approve-approval, .reject-approval, .choose-approval, .fire-trigger, .toggle-trigger, .delete-trigger, .apply-worker-suggestion")) node.disabled = !operator;
  if (auditNav?.hidden && document.querySelector("#view-audit.is-active")) showView("command");
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
  showLogin("Signed out.");
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
  $("#metricWorkers").textContent = state.workers.length;
  $("#metricRunning").textContent = state.jobs.filter((job) => ["queued", "running", "cancel_requested"].includes(job.state)).length;
  $("#metricDone").textContent = state.jobs.filter((job) => ["succeeded", "failed", "cancelled"].includes(job.state)).length;
}

function renderWorkers() {
  const list = $("#workerList");
  if (!state.workers.length) {
    list.innerHTML = '<div class="empty">No workers</div>';
    return;
  }
  list.innerHTML = state.workers.map((worker) => `
    <article class="worker-item">
      <div class="item-title">
        <span>${escapeHtml(worker.name)}</span>
        <span class="status ${statusClass(worker.status)}">${escapeHtml(worker.status)}</span>
      </div>
      <div class="item-sub">${escapeHtml(worker.role || "unassigned")} · ${escapeHtml(worker.base_url)}</div>
      <div class="item-sub">last seen ${escapeHtml(formatTime(worker.last_seen_at) || "never")} · ${workspaceCountForWorker(worker.id)} workspace(s)</div>
      <div class="item-actions">
        <button class="secondary-btn poll-worker" data-worker-id="${escapeHtml(worker.id)}">Poll</button>
        <button class="secondary-btn edit-worker" data-worker-id="${escapeHtml(worker.id)}">Edit</button>
        <button class="danger-btn delete-worker" data-worker-id="${escapeHtml(worker.id)}">Delete</button>
      </div>
    </article>
  `).join("");
}

function renderWorkspaces() {
  const list = $("#workspaceList");
  if (!state.workspaces.length) {
    list.innerHTML = '<div class="empty">No workspaces</div>';
    return;
  }
  list.innerHTML = state.workspaces.map((workspace) => `
    <article class="workspace-item">
      <div class="item-title">
        <span>${escapeHtml(workspace.workspace_key)}</span>
        <span>${escapeHtml(workspace.worker_name || shortId(workspace.worker_id))}</span>
      </div>
      <div class="item-sub">${escapeHtml(workspace.company || "no company")} · ${escapeHtml(workspace.workspace_dir)}</div>
      <div class="item-actions">
        <button class="secondary-btn edit-workspace" data-workspace-id="${escapeHtml(workspace.id)}">Edit</button>
        <button class="danger-btn delete-workspace" data-workspace-id="${escapeHtml(workspace.id)}">Delete</button>
      </div>
    </article>
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
  if (!state.jobs.length) {
    list.innerHTML = '<div class="empty">No jobs</div>';
    return;
  }
  list.innerHTML = state.jobs.map((job) => `
    <article class="job-item ${job.id === state.selectedJobId ? "selected" : ""}" data-job-id="${escapeHtml(job.id)}">
      <div class="item-title">
        <span>${escapeHtml(job.worker_name || shortId(job.worker_id))}</span>
        <span class="status ${statusClass(job.state)}">${escapeHtml(job.state)}</span>
      </div>
      <div class="item-sub">${escapeHtml(job.workspace_key || "auto")} · ${formatTime(job.created_at)} · ${shortId(job.id)}</div>
      ${job.parent_job_id ? `<div class="item-sub">child of ${shortId(job.parent_job_id)}</div>` : ""}
      ${job.handoff_job_id ? `<div class="item-sub">handoff -> ${shortId(job.handoff_job_id)}</div>` : ""}
      ${job.handoff_worker_id && !job.handoff_job_id && !job.handoff_error ? '<div class="item-sub">handoff armed</div>' : ""}
      ${job.handoff_error ? `<div class="item-sub">handoff error: ${escapeHtml(job.handoff_error)}</div>` : ""}
      <div class="job-prompt">${escapeHtml(job.prompt)}</div>
    </article>
  `).join("");
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
  renderWorkerSuggestions();
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

function renderWorkflowRuns() {
  const runs = state.selectedWorkflowId ? state.workflowRuns.filter((run) => run.workflow_definition_id === state.selectedWorkflowId) : state.workflowRuns;
  const list = $("#workflowRunList");
  if (!runs.length) {
    list.innerHTML = '<div class="empty">No workflow runs</div>';
  } else {
    list.innerHTML = runs.slice(0, 20).map((run) => `
      <article class="workflow-run-item ${run.id === state.selectedWorkflowRunId ? "selected" : ""}" data-run-id="${escapeHtml(run.id)}">
        <div class="item-title">
          <span>${escapeHtml(run.name)}</span>
          <span class="status ${statusClass(run.state)}">${escapeHtml(run.state)}</span>
        </div>
        <div class="item-sub">${formatTime(run.created_at)} · ${shortId(run.id)}</div>
      </article>
    `).join("");
  }
  const progress = state.workflowRunDetail?.run?.counters || {};
  $("#workflowRunDetail").textContent = state.workflowRunDetail ? prettyJson({
    completed_nodes: progress.completed_nodes || [],
    joins: progress.join_states || {},
    ...state.workflowRunDetail,
  }) : "";
  $("#workflowArtifactList").textContent = state.workflowArtifacts.length ? prettyJson(state.workflowArtifacts) : "";
  const files = state.workflowArtifacts.filter((artifact) => artifact.kind === "file_ref");
  $("#workflowArtifactDownloads").innerHTML = files.map((artifact) => `
    <a class="workflow-run-item" href="/api/artifacts/${encodeURIComponent(artifact.id)}/content">
      <span>${escapeHtml(artifact.metadata?.filename || artifact.key)}</span>
      <span class="item-sub">${escapeHtml(artifact.metadata?.size ?? 0)} bytes · ${escapeHtml(artifact.metadata?.sha256 || "")}</span>
    </a>
  `).join("");
  const selectedRun = state.workflowRunDetail?.run;
  $("#pauseWorkflowRunBtn").disabled = selectedRun?.state !== "running";
  $("#resumeWorkflowRunBtn").disabled = selectedRun?.state !== "paused";
  $("#retryInterruptedRunBtn").disabled = selectedRun?.state !== "recovery_required";
  $("#cancelWorkflowRunBtn").disabled = !selectedRun || ["succeeded", "failed", "cancelled"].includes(selectedRun.state);
  const recovery = selectedRun?.counters?.recovery;
  const recoveryEl = $("#workflowRecoveryWarning");
  recoveryEl.textContent = recovery ? `${recovery.warning} Interrupted: ${(recovery.interrupted || []).map((item) => `${item.node_key}${item.job_id ? ` (${item.job_id})` : ""}`).join(", ")}` : "";
  recoveryEl.hidden = !recovery;
  renderRunSummary();
  $("#workflowEventList").innerHTML = state.workflowEvents.length ? state.workflowEvents.map((event) => `
    <article class="event-item">
      <div class="item-title">
        <span>${escapeHtml(event.event_type)}</span>
        <span>${escapeHtml(formatTime(event.created_at))}</span>
      </div>
      <div class="item-sub">${escapeHtml(event.node_key || `run · #${event.seq}`)}</div>
      <pre class="event-payload">${escapeHtml(JSON.stringify(event.payload || {}, null, 2))}</pre>
    </article>
  `).join("") : '<div class="empty">Select a run</div>';
  const managerDecisions = state.workflowEvents.filter((event) => event.event_type.startsWith("manager_proposal_"));
  $("#managerDecisionList").innerHTML = managerDecisions.length ? managerDecisions.map((event) => `
    <article class="event-item">
      <div class="item-title">
        <span>${escapeHtml(event.node_key || "manager")}</span>
        <span class="status ${statusClass(event.payload?.state)}">${escapeHtml(event.payload?.state || "unknown")}</span>
      </div>
      <div class="item-sub">${escapeHtml(event.payload?.reason || "")}</div>
      <pre class="event-payload">${escapeHtml(JSON.stringify(event.payload?.proposal || event.payload?.response || {}, null, 2))}</pre>
    </article>
  `).join("") : '<div class="empty">No manager decisions</div>';
}

function renderApprovals() {
  let approvals = state.approvals;
  if (state.selectedWorkflowRunId) {
    approvals = approvals.filter((approval) => approval.run_id === state.selectedWorkflowRunId);
  } else if (state.selectedWorkflowId) {
    const runIds = new Set(state.workflowRuns.filter((run) => run.workflow_definition_id === state.selectedWorkflowId).map((run) => run.id));
    approvals = approvals.filter((approval) => runIds.has(approval.run_id));
  }
  $("#approvalList").innerHTML = approvals.length ? approvals.map((approval) => `
    <article class="workflow-run-item">
      <div class="item-title">
        <span>${escapeHtml(approval.label)}</span>
        <span class="status waiting-for-human">pending</span>
      </div>
      <div class="item-sub">${escapeHtml(approval.node_key)} · run ${shortId(approval.run_id)} · ${formatTime(approval.created_at)}</div>
      <div class="item-sub">${escapeHtml(approval.reason)}</div>
      <div class="item-actions">
        ${(approval.choices || []).map((choice) => `<button class="primary-btn choose-approval" type="button" data-approval-id="${escapeHtml(approval.id)}" data-choice="${escapeHtml(choice.id)}">${escapeHtml(choice.label)}</button>`).join("")}
        ${approval.choices?.length ? "" : `<button class="primary-btn approve-approval" type="button" data-approval-id="${escapeHtml(approval.id)}">Approve</button>`}
        <button class="danger-btn reject-approval" type="button" data-approval-id="${escapeHtml(approval.id)}">Reject</button>
      </div>
    </article>
  `).join("") : '<div class="empty">No pending approvals</div>';
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
  if (!state.audit.length) {
    list.innerHTML = '<div class="empty">No audit entries</div>';
    return;
  }
  list.innerHTML = state.audit.slice(0, 12).map((entry) => `
    <article class="audit-item">
      <div class="item-title">
        <span>${escapeHtml(entry.action)}</span>
        <span>${formatTime(entry.created_at)}</span>
      </div>
      <pre>${escapeHtml(JSON.stringify(entry.details || {}, null, 2))}</pre>
    </article>
  `).join("");
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
  await loadAll();
  openJobStream(data.job.id);
  showView("jobs");
}

function openJobStream(jobId) {
  state.selectedJobId = jobId;
  state.streamText = "";
  state.events = [];
  if (state.eventSource) state.eventSource.close();
  const token = localStorage.getItem("atlasApiToken");
  const url = token ? `/api/jobs/${jobId}/events?after=0&token=${encodeURIComponent(token)}` : `/api/jobs/${jobId}/events?after=0`;
  const source = new EventSource(url);
  state.eventSource = source;
  updateStreamHeader();
  $("#streamOutput").textContent = "";
  $("#eventList").innerHTML = "";

  source.addEventListener("text", (event) => {
    const payload = JSON.parse(event.data);
    state.streamText += payload.text || "";
    $("#streamOutput").textContent = state.streamText;
    $("#streamOutput").scrollTop = $("#streamOutput").scrollHeight;
  });
  for (const name of ["route", "session", "state", "error", "done", "cancel_requested", "handoff_configured", "handoff_started", "handoff_skipped", "handoff_error", "message", "close"]) {
    source.addEventListener(name, (event) => appendEvent(name, JSON.parse(event.data)));
  }
  source.addEventListener("close", () => {
    source.close();
    loadAll().catch((error) => toast(error.message));
  });
  source.onerror = () => {
    appendEvent("stream", { error: "event stream disconnected" });
    source.close();
  };
  renderJobs();
}

function appendEvent(type, payload) {
  state.events.unshift({ type, payload });
  $("#eventList").innerHTML = state.events.slice(0, 30).map((entry) => `
    <article class="event-item">
      <div class="event-type">${escapeHtml(entry.type)}</div>
      <pre class="event-payload">${escapeHtml(JSON.stringify(entry.payload, null, 2))}</pre>
    </article>
  `).join("");
}

function updateStreamHeader() {
  const job = state.jobs.find((item) => item.id === state.selectedJobId);
  $("#streamTitle").textContent = job ? `Job ${shortId(job.id)}` : "Live Stream";
  $("#streamMeta").textContent = job ? `${job.state} · ${job.worker_name || shortId(job.worker_id)} · ${job.route_reason || ""}${job.handoff_job_id ? ` · handoff ${shortId(job.handoff_job_id)}` : ""}` : "Select a job";
  $("#routePreview").textContent = job ? job.route_reason || "Route selected" : composerRouteText();
  $("#cancelJobBtn").disabled = !job || ["succeeded", "failed", "cancelled"].includes(job.state);
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

async function cancelSelectedJob() {
  if (!state.selectedJobId) return;
  await api(`/api/jobs/${state.selectedJobId}/cancel`, { method: "POST" });
  toast("Cancel requested");
  await loadAll();
}

function newWorkflow() {
  state.selectedWorkflowId = null;
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

function openWorkerModal(worker = null) {
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
  $("#workerModal").hidden = true;
  $("#workspaceModal").hidden = true;
}

async function deleteWorker(workerId) {
  const worker = state.workers.find((item) => item.id === workerId);
  if (!confirm(`Delete worker ${worker?.name || workerId}? Its workspaces will be removed too.`)) return;
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

document.addEventListener("click", async (event) => {
  if (event.target.closest("[data-close-modal]")) {
    closeModals();
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
  const workflowItem = event.target.closest(".workflow-item");
  if (workflowItem) {
    selectWorkflow(workflowItem.dataset.workflowId);
    return;
  }
  const workflowRunItem = event.target.closest(".workflow-run-item");
  if (workflowRunItem) {
    if (workflowRunItem.dataset.runId) {
      await selectWorkflowRun(workflowRunItem.dataset.runId).catch((error) => toast(error.message));
      return;
    }
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
  const jobItem = event.target.closest(".job-item");
  if (jobItem) {
    openJobStream(jobItem.dataset.jobId);
  }
});

$("#submitJobBtn").addEventListener("click", () => submitJob().catch((error) => toast(error.message)));
$("#refreshBtn").addEventListener("click", () => refreshAll({ poll: true, notice: true }).catch((error) => toast(error.message)));
$("#pollAllBtn").addEventListener("click", async () => {
  await refreshAll({ poll: true, notice: true }).catch((error) => toast(error.message));
});
$("#cancelJobBtn").addEventListener("click", () => cancelSelectedJob().catch((error) => toast(error.message)));
$("#newWorkflowBtn").addEventListener("click", () => newWorkflow());
$("#saveWorkflowBtn").addEventListener("click", () => saveWorkflow().catch((error) => toast(error.message)));
$("#validateWorkflowBtn").addEventListener("click", () => validateWorkflow().catch((error) => toast(error.message)));
$("#explainWorkflowBtn").addEventListener("click", () => explainWorkflow().catch((error) => toast(error.message)));
$("#repairWorkflowBtn").addEventListener("click", () => repairWorkflow().catch((error) => toast(error.message)));
$("#suggestWorkersBtn").addEventListener("click", () => suggestWorkers().catch((error) => toast(error.message)));
$("#draftWorkflowBtn").addEventListener("click", () => draftWorkflow().catch((error) => toast(error.message)));
$("#applyWorkflowTemplateBtn").addEventListener("click", () => {
  try { applyWorkflowTemplate(); } catch (error) { toast(error.message); }
});
$("#builderAddNodeBtn").addEventListener("click", () => {
  try { addBuilderNode(); } catch (error) { toast(error.message); }
});
$("#builderAddEdgeBtn").addEventListener("click", () => {
  try { addBuilderEdge(); } catch (error) { toast(error.message); }
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
  $("#loginMessage").textContent = error.message === "unauthorized" ? "Invalid username or password." : error.message;
}));
$("#signOutBtn").addEventListener("click", () => signOut());
for (const selector of ["#workflowNameInput", "#workflowDescriptionInput", "#workflowGraphInput", "#workflowPolicyInput"]) {
  $(selector).addEventListener("input", () => { setDirty(true); });
}
$("#workflowPolicyInput").addEventListener("input", () => { syncPolicyFormFromJson(); });
for (const [, selector] of POLICY_FORM_FIELDS) {
  $(selector).addEventListener("input", () => { syncPolicyJsonFromForm(); });
}

for (const button of document.querySelectorAll(".nav-item")) {
  button.addEventListener("click", () => showView(button.dataset.view));
}
showView(localStorage.getItem("atlasView") || "command");

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeModals();
});

loadAll()
  .then(() => {
    document.body.classList.remove("is-loading");
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
