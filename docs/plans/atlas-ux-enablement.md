# Atlas UX Enablement Handoff

Status: implemented API contracts are listed below. This is a handoff for a
separate web UI such as Flow Designer; it does not make the UI repository's
deployment settings into Atlas configuration.

## Ready for UI implementation

| Need | Atlas contract | UI behavior |
| --- | --- | --- |
| Session expiry | Login returns `session.token_id` and `session.expires_at`; `GET /api/me` repeats session metadata. | Warn before expiry, then redirect to sign-in on `401`. Do not treat an admin-created API token as a browser session. |
| Concurrent sessions | Login sessions expire after 8h by default; only five active sessions per user; excess revokes the oldest. | Explain a `401` after sign-in as a possible session-cap/logout/expiry outcome; preserve unsaved local draft separately from the credential. |
| Login backoff | `429` carries `Retry-After`. | Disable submit/count down for that duration; never retry credentials in a loop. |
| Token management | Admin token metadata has immutable `purpose`, optional `expires_at`, and `revoked_at`; raw secrets appear only at creation. | Show expiry and lifecycle state; offer copy-once UX. Never display a token hash or use a token name to infer session status. |
| Job timeline liveness | Job SSE sends `retry: 3000` and a `: keepalive` comment every 15s; persisted events retain `seq`. | Ignore comments in the timeline; persist the last event `seq` and reconnect with `after=<seq>`. |
| Workflow event history | `GET /api/workflow-runs/{id}/events?after=N&limit=N` returns `events`, `next_after`, `has_more`. | Infinite-scroll/reload from `next_after`; append only sequences greater than the stored cursor. |
| Concurrent workflow editing | `PUT /api/workflows/{id}` accepts `expected_version`; a stale save returns `409`. | Keep the local graph, fetch server state, and offer merge/reload. Send neither a guessed version nor both `version` and `expected_version`. |
| HTTP transport | `HEAD` mirrors GET headers without a body; unsupported `PATCH` returns `405` with `Allow`; rejected requests with unread bodies close the connection. | Do not reuse an interrupted rejected request body; allow the browser/client to open a fresh connection. |

## Deployment handoff boundary

Set `PUBLIC_ORIGIN`, `ATLAS_API_ORIGIN`, `SESSION_SECRET`, and Node.js 24 in the
**UI deployment platform**, not in Atlas and never in this repository. Atlas
needs TLS termination, an explicit CORS allowlist for a split UI, unbuffered SSE
with idle timeout above 45 seconds, persistent DB/upload/key storage, and a
tested backup/restore process. Log sinks must exclude request/response bodies,
authorization headers, and token-bearing query strings.

## Deliberate non-code decisions still required

These limitations cannot be responsibly changed by a local Atlas patch alone:

1. **High availability and persistence topology.** Atlas remains one process and
   one SQLite writer per database. Active-active replicas, a shared event broker,
   server database, and object storage require an approved architecture and an
   operations owner; do not run multiple writers against one SQLite file.
2. **Shared versus personal canvas layout.** The workflow graph is shared. Decide
   whether node positions are shared workflow data or a per-user preference
   before persisting layout collaboration; the answer changes permissions,
   conflict semantics, and audit expectations.
3. **Human-gate assignment/escalation.** Assignee, deadline, reminder, and timeout
   action need a product policy (who may act, what happens on timeout, and which
   notifications are authoritative) before an API is added.

The silo tenancy decision remains authoritative: no `tenant_id` belongs in Atlas
core unless ADR 0001 is revisited.
