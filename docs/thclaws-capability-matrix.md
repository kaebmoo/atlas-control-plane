# thClaws Capability Matrix

This records what Atlas can do without changing thClaws and what still needs native thClaws support later.

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
