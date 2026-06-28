# Atlas Web User Guide

This guide covers the current Atlas Control Plane web UI end to end, from
registering thClaws workers to running and monitoring workflows.

> Atlas is the control plane; thClaws workers perform the work. A workspace path
> must therefore exist on the worker machine, not necessarily on the Atlas host.

## 1. Start the system

Start at least one thClaws worker:

```bash
THCLAWS_API_TOKEN="dev-token-1" \
thclaws --serve --bind 127.0.0.1 --port 4317
```

Use a different port for each additional worker. Assign meaningful roles and
tags such as `reporter`, `reviewer`, `coder`, or `workflow_builder` in Atlas.

Start Atlas:

```bash
cd /Users/seal/Documents/GitHub/atlas-control-plane
python3 -m atlas --host 127.0.0.1 --port 8787
```

Alternatively, run `./scripts/run.sh`. Open `http://127.0.0.1:8787` and stop
the server with `Ctrl+C`. Atlas uses `data/atlas.sqlite` by default.

## 2. Web UI overview

| View | Purpose |
| --- | --- |
| **Command** | Submit a single job or a two-stage handoff |
| **Workflows** | Create, validate, explain, repair, and run workflows |
| **Monitor** | Inspect workflow runs, approvals, artifacts, and triggers |
| **Jobs** | Inspect jobs, live output, events, and cancellation |
| **Audit** | Review recent control-plane actions |
| **Fleet** | Manage workers and workspaces |

The sidebar counters show workers, active jobs, and finished jobs. The Monitor
badge counts pending approvals; the Jobs badge counts `queued`, `running`, and
`cancel_requested` jobs.

**Refresh & poll** reloads data and polls every worker immediately. The UI also
reloads data every 5 seconds and polls workers every 60 seconds.

For first-time setup, use **Fleet → Command → Jobs**. Use **Workflows → Monitor**
for multi-step orchestration.

## 3. Fleet: workers and workspaces

### Add a worker

Open **Fleet**, click **Add worker**, and complete:

| Field | Meaning |
| --- | --- |
| **Name** | Human-readable name, for example `Reporter` |
| **Base URL** | thClaws URL, for example `http://127.0.0.1:4317` |
| **Token** | The worker's `THCLAWS_API_TOKEN` |
| **Role** | Primary routing role, for example `reporter` |
| **Tags** | Comma-separated routing hints, for example `local,news,thai` |

Click **Save Worker**. Atlas immediately polls the worker; a reachable worker
with a valid token becomes `online`.

Each worker card provides:

- **Poll** — refresh that worker's health and capabilities.
- **Edit** — update it; leave Token blank to retain the stored token.
- **Delete** — delete the worker and its workspaces after confirmation.
- **Poll all workers** — the arrow button in the Workers header.

Common states are `online`, `offline`, and `unknown`. Poll failures are visible
in **Audit**.

### Add a workspace

Click **Add workspace** after at least one worker exists:

| Field | Meaning |
| --- | --- |
| **Worker** | Worker that owns the directory |
| **Key** | Routing key such as `atlas` or `company-a` |
| **Directory** | Absolute path on the worker machine |
| **Company** | Organization/data scope used as a routing hint |
| **Tags** | Comma-separated routing hints |

Click **Save Workspace**. Workspace cards provide **Edit** and confirmed
**Delete** actions.

**Cancel**, the **×** button, or `Escape` closes an Add/Edit modal without saving.

## 4. Command: jobs and handoffs

### Submit a job

| Field | Usage |
| --- | --- |
| **Prompt** | Required task for the worker |
| **Conversation** | Start a new conversation or reuse an existing session binding |
| **Worker** | Auto-route or force a worker |
| **Workspace** | Auto-route or force a workspace |
| **Model** | Optional model override; blank uses the worker default |

The route preview under the Command heading reflects precedence:

1. An explicit workspace wins.
2. Otherwise, an explicit worker wins.
3. An existing conversation prefers its existing binding.
4. Full auto-routing considers online state, workspace key, company, tags, role,
   and prompt hints.

Click **Run**. Atlas creates the job, clears Prompt, opens **Jobs**, and selects
the new job.

### Handoff after success

Enable **Hand off after success** when job B should start only after job A
succeeds.

| Field | Usage |
| --- | --- |
| **Send to worker** | Destination worker |
| **Send to workspace** | Destination workspace; takes precedence over worker |
| **Handoff prompt** | Child-job prompt |

Handoff prompt variables are `{result}`, `{source_prompt}`, and
`{source_job_id}`. Select at least one destination worker or workspace. Job cards
show `handoff armed`, `handoff ->`, `child of`, or `handoff error` as applicable.

## 5. Jobs: output and events

The Jobs list shows worker, state, workspace, timestamp, short ID, handoff
relationships, and prompt. Select a card to open it.

**Live stream** replays and follows worker output. **Events** shows route,
session, state, error, completion, cancellation, handoff, message, and close
events.

**Cancel** is available while a job is active. Cancellation is best effort at
the Atlas layer: the job first becomes `cancel_requested`, and the worker may
already have performed side effects.

| State | Meaning |
| --- | --- |
| `queued` | Waiting to start |
| `running` | Worker is executing |
| `cancel_requested` | Atlas accepted a cancellation request |
| `succeeded` | Completed successfully |
| `failed` | Failed; inspect events and error data |
| `cancelled` | Cancelled |

If the stream disconnects, select the job card again to replay events.

## 6. Workflows: multi-step work

### Definitions and templates

- **New** resets the editor for a new definition.
- Select an item under **Definitions** to edit it.
- The dot beside the Workflows title indicates unsaved editor changes.
- Select a template and click **Copy template to editor**. This creates an
  unsaved preview.

Available templates are News Desk, Researcher → Writer → Reviewer, Coder →
Tester → Reviewer, and Manager-directed loop.

Save anything you need before clicking New, selecting another definition,
copying a template, or drafting: these actions can replace the preview, and the
current UI does not confirm when switching definitions. This UI version has no
workflow-definition delete action.

### Definition and Graph JSON

Enter **Name**, optional **Description**, and **Graph JSON**. A graph needs
`start`, `nodes`, and `edges`:

```json
{
  "start": "reporter",
  "nodes": [
    {
      "id": "reporter",
      "type": "worker",
      "role": "reporter",
      "prompt": "Research {input.topic}",
      "outputs": ["notes"]
    },
    {
      "id": "writer",
      "type": "worker",
      "role": "writer",
      "prompt": "Write from {artifact.notes}"
    }
  ],
  "edges": [
    {"from": "reporter", "to": "writer", "condition": {"type": "always"}}
  ]
}
```

| Node type | Purpose |
| --- | --- |
| `worker` | Creates a thClaws job |
| `manager` | Proposes allowed next nodes under graph and policy constraints |
| `join` | Joins fan-out with `all`, `any`, or `quorum`; creates no job |
| `human_gate` | Waits for approval or a choice; creates no job |

UI-supported conditions are `always`, `artifact_equals`, `artifact_in`,
`manager_selected`, `human_selected`, and `max_iterations_below`.

Use `{input.topic}` for run input and `{artifact.notes}` for artifacts. See
[Workflow Examples](../workflow-examples.md) for complete graphs.

### Builder Lite

Expand **Builder Lite — add nodes & edges...** to update the Graph JSON preview.
It does not save automatically.

Add node fields include Node ID, Node type, Role/label, Prompt/reason, Outputs,
Budget units, Human choices (`publish:Publish, revise:Revise`), Join mode, and
Join quorum. Add edge fields include From, To, Condition, Artifact/node, Path,
and Value(s)/max; the selected condition determines which fields are used.

Click **Suggest workers** to diagnose unresolved worker nodes. Where a suggestion
is available, **Apply To JSON** adds its `worker_id`/`workspace_id` to the
preview. Review it before saving.

### Policy

The form and **Policy JSON** remain synchronized while JSON is valid.

| Field | Constraint |
| --- | --- |
| **Max jobs** | Maximum jobs per run |
| **Max iterations** | Maximum iterations |
| **Max attempts / node** | Maximum executions per node |
| **Max minutes** | Overall runtime limit |
| **Human after iterations** | Require one human approval after the threshold |
| **Max budget units** | Integer budget limit; not money or token usage |
| **Allowed worker IDs** | Comma-separated allowlist |
| **Allowed workspace IDs** | Comma-separated allowlist |
| **Stop on first failure** | Stop on the first failed branch when enabled |

Invalid raw JSON is preserved and does not update the form; repair its syntax
before continuing.

### Editor actions

| Action | Result |
| --- | --- |
| **Save** | Create or update the definition |
| **Validate** | Validate the current graph/policy; requires a saved definition |
| **Explain** | Explain the saved definition without changing it |
| **Repair** | Copy a validated repair into previews; never saves automatically |

Review Repair output and explicitly click **Save** if it should become active.

### Draft from plain language

Register a worker whose role or tag is `workflow_builder`. Enter a description
in the Draft **Prompt** and click **Draft**. Graph and policy previews are loaded
into the editor; explanation, warnings, and trigger drafts appear below. Nothing
is saved automatically.

### Run a workflow

Save the definition first. Enter **Run input JSON**, for example:

```json
{"topic": "technology news"}
```

Click **Run workflow**. Atlas creates the run and opens **Monitor**.

## 7. Monitor: workflow operations

### Runs and controls

**Runs** shows runs for the selected workflow, or all runs when no definition is
selected. Select a run to inspect state, jobs, budget, completed/failed nodes,
join progress, and full detail JSON.

- **Pause** pauses a running run.
- **Resume** continues a paused run without repeating completed nodes.
- **Cancel** cancels a non-terminal run.
- **Retry interrupted** is only for `recovery_required` and requires explicit
  confirmation of duplicate-side-effect risk.

After an Atlas restart, interrupted worker/manager nodes are not retried
automatically. Review the warning's node and job IDs before authorizing retry.

### Artifacts

Artifacts and metadata appear for the selected run. `file_ref` artifacts provide
a download link, byte size, and SHA-256.

To upload, select a run, enter **File key**, choose **File**, and click
**Upload file**. The default limit is 10 MiB; administrators can change it with
`ATLAS_MAX_UPLOAD_BYTES`.

### Approvals, manager decisions, and timeline

For `waiting_for_human` runs, a normal gate provides **Approve** and **Reject**;
a choice gate provides one button per choice plus **Reject**. A gate can be
decided only once.

**Manager decisions** shows proposals and acceptance/rejection reasons.
**Timeline** shows ordered workflow events and payloads.

### Triggers

Select a saved workflow definition before creating a trigger.

| Quick trigger | Quick value | Generated config |
| --- | --- | --- |
| `manual` | unused | `{}` |
| `webhook` | unused | `{}` |
| `schedule interval` | minutes, e.g. `15` | `{"interval_minutes": 15}` |
| `schedule daily` | local time, e.g. `09:30` | `{"daily_time": "09:30"}` |

Click **Apply to JSON**, then review Name, Type, Enabled, and Config JSON before
clicking **Create trigger**.

Supported types are `manual`, `schedule`, `webhook`,
`workflow_run_completed`, `artifact_created`, and `worker_status_changed`.

Optional internal-event filters are:

- `workflow_run_completed`: `source_workflow_definition_id`, `state`
- `artifact_created`: `source_workflow_definition_id`, `key`, `kind`
- `worker_status_changed`: `worker_id`, `status`

For a webhook, create a `webhook` trigger and send its payload to the trigger
endpoint described in [Workflow Examples](../workflow-examples.md). Reuse a
stable `dedupe_key` when retrying the same event. Atlas emits the three internal
event types; their cards do not provide Fire.

**Suggest triggers** asks the workflow builder for validated configurations,
using the Draft prompt when present. The first suggestion is copied into the
form but is not created.

Trigger cards provide **Enable/Disable**, **Fire** for manual/schedule/webhook,
and confirmed **Delete**. Fire uses the current Run input JSON as payload. Select
the card itself to inspect recent `received`, `started`, `ignored`, or `failed`
events.

## 8. Audit

**Audit** shows recent control-plane actions such as `worker.poll`, `job.create`,
`job.succeeded`, and `session.bind`, with timestamps and JSON details. Use it to
trace state changes and poll/run errors. The UI renders a subset of the latest
30 fetched audit entries.

## 9. Security and remote access

- Use real, separate worker tokens.
- Worker tokens are stored in SQLite and are never returned by the dashboard
  API; responses only expose `token_set`.
- Loopback access is tokenless by default.
- When `ATLAS_API_TOKEN` is enabled, non-loopback API requests require its Bearer
  token. This UI version has no token settings form; remote deployments should
  use an authenticated reverse proxy or provision `atlasApiToken` in browser
  local storage according to administrator policy.
- Do not expose Atlas or thClaws publicly without authentication and TLS.

## 10. Troubleshooting

| Symptom | Check |
| --- | --- |
| Worker is `offline` | Process/port, Base URL, firewall, and worker token |
| `No workers registered` | Add and poll a worker in Fleet |
| `role has no matching worker` | Add a matching role/tag or apply a suggestion |
| `unknown worker_id` | Graph references a deleted or incorrect worker ID |
| `missing prompt variable` | Verify referenced input and artifact paths |
| `output_format=json` fails | Worker must return parseable JSON only |
| Manager is invalid/rejected | Inspect schema, edge, artifact, allowlist, and guard reasons |
| Run does not start | Save first; validate Run input JSON and the workflow |
| Resume is disabled | Resume is for `paused`; use Retry interrupted for recovery |
| Upload fails | Select a run, supply a key, and check the size limit |

For schemas and API examples, see [Workflow Examples](../workflow-examples.md)
and [Architecture](../architecture.md).
