const state = {
  workers: [],
  workspaces: [],
  conversations: [],
  jobs: [],
  audit: [],
  workflows: [],
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
  eventSource: null,
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

async function loadAll() {
  const [workers, workspaces, conversations, jobs, workflows, workflowRuns, workflowTriggers, approvals, audit] = await Promise.all([
    api("/api/workers"),
    api("/api/workspaces"),
    api("/api/conversations"),
    api("/api/jobs"),
    api("/api/workflows"),
    api("/api/workflow-runs"),
    api("/api/workflow-triggers"),
    api("/api/approvals?state=pending"),
    api("/api/audit?limit=30"),
  ]);
  state.workers = workers.workers || [];
  state.workspaces = workspaces.workspaces || [];
  state.conversations = conversations.conversations || [];
  state.jobs = jobs.jobs || [];
  state.workflows = workflows.workflows || [];
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
  if (poll) {
    await api("/api/workers/poll", { method: "POST" });
  }
  await loadAll();
  if (notice) toast(poll ? "Refreshed and polled workers" : "Refreshed");
}

function render() {
  renderMetrics();
  renderWorkers();
  renderWorkspaces();
  renderSelects();
  renderJobs();
  renderWorkflows();
  renderAudit();
  updateComposerRoutePreview();
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
  renderWorkflowEditor();
  renderWorkflowRuns();
  renderApprovals();
  renderWorkflowTriggers();
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
  const editorIds = new Set(["workflowNameInput", "workflowDescriptionInput", "workflowGraphInput", "workflowPolicyInput", "workflowRunInput"]);
  if (!force && workflow && $("#workflowNameInput").dataset.workflowId === workflow.id && editorIds.has(document.activeElement?.id)) {
    return;
  }
  $("#workflowMeta").textContent = workflow ? `${workflow.status} · ${shortId(workflow.id)} · updated ${formatTime(workflow.updated_at)}` : "New workflow";
  $("#workflowNameInput").dataset.workflowId = workflow?.id || "";
  $("#workflowNameInput").value = workflow?.name || "Untitled workflow";
  $("#workflowDescriptionInput").value = workflow?.description || "";
  $("#workflowGraphInput").value = prettyJson(workflow?.graph || defaultWorkflowGraph());
  $("#workflowPolicyInput").value = prettyJson(workflow?.policy || { max_jobs: 5, max_iterations: 10 });
  if (!$("#workflowRunInput").value.trim()) $("#workflowRunInput").value = "{}";
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
  const selectedRun = state.workflowRunDetail?.run;
  $("#pauseWorkflowRunBtn").disabled = selectedRun?.state !== "running";
  $("#resumeWorkflowRunBtn").disabled = selectedRun?.state !== "paused";
  $("#cancelWorkflowRunBtn").disabled = !selectedRun || ["succeeded", "failed", "cancelled"].includes(selectedRun.state);
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
        <button class="primary-btn approve-approval" type="button" data-approval-id="${escapeHtml(approval.id)}">Approve</button>
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
  $("#draftResult").textContent = "";
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
  $("#draftResult").textContent = "";
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
  $("#draftResult").textContent = prettyJson({
    explanation: draft.explanation || "",
    warnings: draft.warnings || [],
    triggers: draft.triggers || [],
  });
  renderWorkflowList();
  toast("Draft ready");
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
  toast(`Workflow ${data.run.state}`);
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

async function decideApproval(approvalId, action) {
  const approval = state.approvals.find((item) => item.id === approvalId);
  if (approval) state.selectedWorkflowRunId = approval.run_id;
  const data = await api(`/api/approvals/${approvalId}/${action}`, { method: "POST" });
  await loadAll();
  toast(`Approval ${data.approval.state}`);
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

async function fireTrigger(triggerId) {
  const data = await api(`/api/workflow-triggers/${triggerId}/fire`, {
    method: "POST",
    body: JSON.stringify({ payload: parseJsonField("#workflowRunInput") }),
  });
  state.selectedWorkflowTriggerId = triggerId;
  if (data.run?.id) state.selectedWorkflowRunId = data.run.id;
  await loadAll();
  await selectWorkflowTrigger(triggerId);
  if (data.run?.id) await selectWorkflowRun(data.run.id);
  toast(data.event?.state === "ignored" ? "Trigger ignored" : "Trigger fired");
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
  const fireTriggerButton = event.target.closest(".fire-trigger");
  if (fireTriggerButton) {
    await fireTrigger(fireTriggerButton.dataset.triggerId).catch((error) => toast(error.message));
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
$("#draftWorkflowBtn").addEventListener("click", () => draftWorkflow().catch((error) => toast(error.message)));
$("#runWorkflowBtn").addEventListener("click", () => runWorkflow().catch((error) => toast(error.message)));
$("#pauseWorkflowRunBtn").addEventListener("click", () => controlWorkflowRun("pause").catch((error) => toast(error.message)));
$("#resumeWorkflowRunBtn").addEventListener("click", () => controlWorkflowRun("resume").catch((error) => toast(error.message)));
$("#cancelWorkflowRunBtn").addEventListener("click", () => controlWorkflowRun("cancel").catch((error) => toast(error.message)));
$("#saveTriggerBtn").addEventListener("click", () => saveTrigger().catch((error) => toast(error.message)));
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

loadAll()
  .then(() => {
    const firstActive = state.jobs.find((job) => ["running", "queued", "cancel_requested"].includes(job.state));
    if (firstActive) openJobStream(firstActive.id);
  })
  .catch((error) => toast(error.message));

setInterval(() => {
  loadAll().catch(() => undefined);
}, 5000);

setInterval(() => {
  refreshAll({ poll: true }).catch(() => undefined);
}, AUTO_POLL_MS);
