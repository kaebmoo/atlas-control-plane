# thClaws Capability Matrix

This records what Atlas can do without changing thClaws, what it can approximate with control-plane workarounds, and what still needs native thClaws support later.

## Three Capability Levels

Atlas can already use thClaws as a worker runtime, but not every orchestration feature has the same quality level. Split features into three buckets before deciding whether to build in Atlas or request new thClaws APIs.

### Works Today Without Changing thClaws

- **Machine health**: use `GET /healthz`.
- **Capability discovery**: use `GET /v1/agent/info`.
- **Send work**: use `POST /agent/run`.
- **Live streaming**: read SSE from `/agent/run`.
- **Async jobs without holding the client connection**: use `x_callback`.
- **Session continuity**: persist thClaws `session_id` in Atlas and pass it back to `/agent/run`.
- **Deploy or sync selected config/files**: use `/v1/deploy`, `/v1/deploy/files`, and `/v1/deploy/manifest`.
- **Restart a worker**: use `/v1/restart`.
- **Multi-machine dashboard**: Atlas can proxy, aggregate, and persist state around the APIs above.

This is enough for the current control plane: worker registry, workspace
mapping, routing, streaming, history, audit, handoff, and Atlas-owned
deterministic workflows.

### Possible As Workarounds, But Not Native

- **Cancel job**: for streaming jobs, Atlas can close the stream, mark the job cancelled locally, or enforce timeouts. This is not a reliable thClaws-side `job_id -> cancel`, especially for detached `x_callback` jobs.
- **Central approval**: Atlas can do pre-approval before dispatch, such as prompt checks and policy gates. It cannot yet handle per-tool remote approval like "Bash is about to run; allow or deny from Atlas" for `/agent/run`.
- **Live reconnect**: Atlas can buffer SSE and replay events to browser clients after reconnect. thClaws itself does not provide a native resume cursor for a broken stream.
- **Team control**: Agent Teams can work inside normal thClaws sessions, but `/agent/run` does not expose a clean structured Team API for creating teams, listing teammates, or sending team messages.
- **Central audit**: Atlas can audit routing, worker changes, job lifecycle, and observed stream events. It cannot yet get a first-class remote tool-decision audit log from thClaws across machines.

These workarounds are useful for prototypes and local operation, but production-grade workflows should eventually move the critical pieces into native thClaws APIs.

### Not Native Without thClaws Changes

- Per-tool remote approval API for `/agent/run`.
- List running jobs, read job status, and cancel by thClaws job id.
- Resume a live stream directly from thClaws after a connection drop.
- Structured remote Team API, such as create team, list teammates, assign task, and send team message.
- First-class central audit log for tool decisions and permission decisions across machines.
- Native worker-side queue inspection and recovery after Atlas restarts.

The most valuable first native additions would be:

1. `job_id`, job status, and cancel APIs for `/agent/run`.
2. Remote approval protocol for `/agent/run`.
3. Stream resume cursor for job events.

## Native In thClaws Today

Based on the current `thclaws --serve` surface:

- `GET /healthz`: worker health.
- `GET /v1/agent/info`: worker/agent capability snapshot.
- `POST /agent/run`: run a prompt through the agent runtime.
- `POST /agent/run` with `stream: true`: stream results back as SSE.
- `POST /agent/run` with `workspace_dir`: run against a chosen local workspace.
- `POST /agent/run` with `session_id`: continue a thClaws session when the caller has the session id.
- `GET /v1/models` and `POST /v1/chat/completions`: OpenAI-compatible entry points.
- `POST /upload`: file upload support.
- `POST /v1/deploy`, `/v1/deploy/files`, `/v1/deploy/manifest`: deployment surface.
- `POST /v1/restart`: process restart surface.

## Works In Atlas Without Modifying thClaws

- Multi-machine registry by storing each `thclaws --serve` URL as a worker.
- Workspace-to-machine mapping by storing `workspace_dir` per worker.
- Central command dashboard.
- Job routing before calling thClaws.
- Live result streaming by proxying `/agent/run` SSE into Atlas SSE.
- Session continuity by persisting thClaws `session_id` against Atlas conversations.
- Worker capability polling through `/v1/agent/info`.
- Job history and replay through Atlas' own `job_events` table.
- Audit log for routing, worker changes, workspace changes, and job lifecycle.
- Best-effort cancel by marking the Atlas job cancelled and closing/releasing the stream when possible.
- Workflow graph state, fan-out, joins, conditions, limits, and artifacts in
  Atlas SQLite.
- Manual, schedule, webhook, workflow-completion, artifact, and worker-status
  triggers without worker-side orchestration APIs.

## Workarounds, Not Native

- **Job status**: Atlas stores status locally. thClaws does not expose a native remote job resource.
- **Cancel**: Atlas can request cancellation locally, but thClaws needs a native cancel endpoint for reliable remote termination.
- **Stream resume**: Atlas can replay persisted events after reconnect, but this is Atlas-level replay, not thClaws-native stream resume.
- **Approvals**: Atlas can store approval-like events if they appear in stream output, but remote approval is not yet a first-class thClaws API.
- **Team management**: Atlas can route to a manager prompt, but cannot call native TeamCreate/SpawnTeammate style operations through a structured HTTP team API.
- **Deep capability schema**: `/v1/agent/info` is useful, but Atlas still needs a stable machine-readable schema for tools, permissions, scopes, workspace metadata, and approval requirements.

## Not Possible Natively Yet

- Reliable remote job cancellation.
- Remote approval workflow with approve/reject callbacks.
- Querying running jobs from thClaws after Atlas restarts.
- Native worker-side queue inspection.
- Native remote agent/team graph CRUD.
- Native per-tool permission negotiation from the control plane.
- Native structured handoff between thClaws worker agents.
- Native event resume from a thClaws cursor.

## thClaws Feature Requests To Ask For Later

- `POST /v1/jobs` to create a job.
- `GET /v1/jobs/{id}` to read state.
- `GET /v1/jobs/{id}/events?cursor=...` to stream or resume events.
- `POST /v1/jobs/{id}/cancel` for reliable cancellation.
- `GET /v1/workspaces` for worker-known workspace metadata.
- `GET /v1/capabilities` with a versioned schema.
- `POST /v1/approvals/{id}/approve` and `/reject`.
- `GET/POST /v1/teams` for native team and manager graph operations.
