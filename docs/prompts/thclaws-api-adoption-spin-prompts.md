# thClaws API Adoption — Autonomous Spin Prompts (with Claude review loop)

Ready-to-run prompts for the active milestones of
[../plans/thclaws-api-adoption-plan.md](../plans/thclaws-api-adoption-plan.md).
T0–T6 are complete and their prompts below are retained as execution history.
Run **T9a → T9b** next, one PR at a time, only after the gate is green and an
independent Claude review pass reports no unresolved actionable findings.

> Do not reimplement T5/T6's sync/tar path or add a compatibility fallback.
> T9 replaces it with thClaws Job Artifacts (v0.88.0+).

The plan file is the source of truth. These prompts only scope, sequence, and
set the stop conditions.

---

## Shared Preamble (paste/obey for every milestone)

```text
Repo: /Users/seal/Documents/GitHub/atlas-control-plane
Start from a clean `main` with `./scripts/gate.sh` passing. Requires
Python 3.11+ (datetime.UTC) and node. Reviews run as an INDEPENDENT Claude Code
review — a fresh `feature-dev:code-reviewer` subagent (or the `/code-review`
skill on the diff), never the context that wrote the code. No external review
CLI is used (codex is not required).

Read FIRST, before editing (source of truth, in this order):
- AGENTS.md                                        (invariants + workflow)
- docs/plans/thclaws-api-adoption-plan.md          (the plan; your milestone's
  Goal / Design decisions / Files / Work / Checks are the DoD)
- The plan's "Review deltas" section — it records five review rounds; do NOT
  re-introduce anything marked rejected or SUPERSEDED (e.g. tool-payload
  previews, callback-vs-SSE race tests, "rollback" naming).
- atlas/thclaws_client.py, atlas/jobs.py, atlas/db.py, atlas/app.py,
  atlas/usage.py  (the files your milestone touches, per its Files list)
- scripts/gate.sh (how checks are wired), one existing check script as a
  template (scripts/check_usage.py is a good model)

House rules (do NOT violate — the gate and review will catch you):
- Python stdlib ONLY in atlas/ core; dashboard has no framework/build step.
- All /api/* changes are ADDITIVE; never change existing paths/shapes.
- No tenant_id in atlas/ core (check_silo.py). Never log/store/return worker
  tokens or model keys — T2 explicitly stores structural tool metadata only,
  never tool input/output.
- Every non-trivial behavior: ONE hermetic check (own temp DB, ephemeral
  port, mock thClaws worker) appended to scripts/gate.sh, and MUTATION-TESTED
  (break the code, prove the gate goes red, revert; note it in the commit).
- Any /api/* change updates docs/specs/openapi.yaml + api-reference-en.md +
  api-reference-th.md (EN + TH parity, never English only).
- Preserve all dashboard gate-marker substrings.

Per-milestone loop (run in exactly this order; never skip a step):
1. Implement the milestone per its Work checklist in the plan.
2. Mutation-test every new check: break the code → run the check → MUST fail
   → revert → MUST pass. Record which mutation you used.
3. ./scripts/gate.sh   (fix until green; do not proceed while red)
4. ./scripts/lint.sh   (fix until clean)
5. INDEPENDENT Claude review of the uncommitted diff. Launch a fresh
   `feature-dev:code-reviewer` subagent (NOT the context that wrote the code —
   independence is the whole point) with this rubric: 'Review this
   implementation against AGENTS.md and
   docs/plans/thclaws-api-adoption-plan.md. Trace every implemented
   milestone end-to-end through the real execution path. Treat plan
   noncompliance, security regressions, state-machine races, API
   compatibility breaks, missing hermetic checks, missing mutation tests,
   and EN/TH documentation drift as findings. Run the relevant checks.
   Report only actionable findings ordered by severity with file:line and
   concrete evidence. If no findings remain, state exactly what paths and
   checks were verified.'
6. Fix every actionable finding the review reports. Do not argue a finding away
   without verifying against the real execution path; if a finding is
   invalid, record WHY with file:line evidence in the commit message.
7. Re-run ./scripts/gate.sh and ./scripts/lint.sh (both must be green after
   the fixes).
8. If the review reported findings in step 5, run one more independent review
   pass (a fresh subagent); repeat 6–8 until a pass reports none.
9. Tick the milestone's Work/Checks boxes in
   docs/plans/thclaws-api-adoption-plan.md, add one close-out row to
   PROGRESS.md, then commit (one commit per milestone; message lists the
   mutation tests performed and the review verdict).
10. Post-commit verification: one more INDEPENDENT Claude review scoped to the
    new commit — a fresh `feature-dev:code-reviewer` subagent given
    `git show HEAD`: 'Verify this commit against AGENTS.md and
    docs/plans/thclaws-api-adoption-plan.md. Report only confirmed actionable
    findings with file:line, impact, evidence, and reproduction steps.'  Any
    confirmed finding → fix as a follow-up commit through the same loop.

Stop conditions: STOP and report (do not improvise) if the gate cannot run,
if a plan Design decision conflicts with what you find in the code, or if a
milestone needs information only the operator has (e.g. T0's live worker
probes). Never weaken a check or suppress an error to get to green.
```

---

## Historical milestone drivers (completed; do not rerun)

### T0 — Worker contract spike (docs only; no core code)

```text
Apply the Shared Preamble. Execute Milestone T0 of
docs/plans/thclaws-api-adoption-plan.md exactly as written.

Scope: NO atlas/ code changes. Deliverables are
docs/specs/thclaws-worker-contract.md, the ops-guide update, and the
findings table added under T0 in the plan.

You cannot run a live thclaws --serve probe yourself — generate the probe
script (curl commands for /workspace/sync/stat and /v1/deploy/manifest, with
and without Bearer, loopback and LAN bind), ask the operator to run it, and
STOP until the results are provided. Then write the contract doc from real
results, never from assumption. The SSE event-name list in the contract must
be pinned by reading the emitter code (crates/core/src/api_v1/agent.rs), not
by grepping for string literals — event names can be computed (skill_invoked).

Close out per the loop: check_docs.py green, independent Claude review,
commit.
```

### T1a — Token usage capture (+ stale-doc fix)

```text
Apply the Shared Preamble. Execute Milestone T1a of
docs/plans/thclaws-api-adoption-plan.md exactly as written.

Key constraints from the plan:
- extract_usage() mirrors extract_session_id(): tolerant, returns None on
  anything malformed, never raises.
- _record_job_usage passes tokens_prompt/tokens_output (db.emit_usage_event
  already binds them) and puts the full usage payload under
  metadata.measures. byok_token_counts_billable stays False — untouched.
- NO pricing, NO cost estimate, NO /v1/models call — that is T1b, blocked
  behind this milestone.
- Fix the stale "No token/usage capture" gap entry in
  docs/plans/usage-metering-billing-plan.md (thClaws emits usage since
  v0.85.0).

Checks to implement (see plan): matching tokens from a mock usage event; old
worker without usage → NULL tokens, job succeeds; malformed payloads
tolerated; mutation test: extract_usage returning {} must turn the gate red.
Close out per the loop.
```

### T2 — Structured event surfaces (parser fix first)

```text
Apply the Shared Preamble. Execute Milestone T2 of
docs/plans/thclaws-api-adoption-plan.md exactly as written.

Order matters:
1. Fix extract_text() scoping FIRST (assistant-text events only; thinking /
   user_message_injected / tool_* / skill_* / usage / result / error fall
   through to append_job_event; legacy unnamed frames preserved for older
   workers).
2. THEN the structural-metadata projection for tool/skill events:
   {id, name, status, input_bytes, output_bytes, input_sha256,
   output_sha256} — input/output NEVER reach SQLite. This projection is what
   makes step 1 safe to ship; do not land step 1 without it.
3. THEN the dashboard timeline, rendered from structural metadata only.
   NO payload preview of any kind, persistent or otherwise (five review
   rounds settled this — truncation is not redaction). Escape the stored
   fields that ARE rendered: tool/skill names, error strings.

Mandatory checks (see plan): thinking/user_message_injected absent from
assistant_text and present as job_events rows; planted secret literal in
mocked tool input AND output → zero occurrences in a byte-scan of the DB
file; timeline order/status markers; unknown event names never crash the
view. Mutation tests: revert the extract_text scoping → red; persist a
payload → red. Close out per the loop.
```

### T3 — Async execution via x_callback

```text
Apply the Shared Preamble. Execute Milestone T3 of
docs/plans/thclaws-api-adoption-plan.md exactly as written.

Non-negotiable design points (each exists because a review round caught the
opposite):
- Fix thclaws_client.py x_callback type: thClaws requires the object
  {url, api_key, run_id, idempotency_key?} — it is a str today (dead code).
- The callback route is dispatched BEFORE _is_authorized() (dedicated
  handler: body-size cap before reading, HMAC verify with constant-time
  compare, system audit actor `system:worker-callback`). Without the
  carve-out the worker's callback dies 401. Document the exception in
  docs/specs/threat-model.md.
- Token validity = callback deadline + the worker's 3-attempt retry envelope
  + clock-skew margin.
- reconcile_jobs must EXEMPT callback-pending jobs (they are in-flight
  remotely, not interrupted); the reaper owns their deadline.
- Race check is callback-vs-reaper (stream and callback modes are mutually
  exclusive per job — do not write a callback-vs-SSE test).
- Async is opt-in per job/node (execution: "callback"); default behavior is
  byte-identical. Reject async at validation time when ATLAS_PUBLIC_BASE_URL
  is unset.

Mandatory checks: valid HMAC + no user token reaches the handler (mutation:
route through _is_authorized() → 401 → red); oversized body rejected;
duplicate delivery converges; expired-token 401; reaper fires at deadline;
callback-vs-reaper single terminal state; restart preserves callback-pending;
skip-signature mutation → red. Close out per the loop.
```

---

## Legacy branch close-out (completed milestones)

```text
Run the branch-level review before opening the PR:

./scripts/gate.sh && ./scripts/lint.sh

Then an INDEPENDENT Claude branch review — a fresh `feature-dev:code-reviewer`
subagent given the branch diff (`git diff main...HEAD`): 'Review this branch
against AGENTS.md and docs/plans/thclaws-api-adoption-plan.md. Verify the
implementation end-to-end, including security boundaries, backward-compatible
API behavior, recovery/race paths, tests, mutation coverage, and documentation
parity. Report actionable findings ordered by severity with file:line.'

Fix findings through the per-milestone loop, then open the PR (CI runs gate
+ lint as required checks). T1b and T4 get their own driver prompts only
after T1a lands and T0's contract doc defines sync_mode gating semantics.
```

---

## Active milestone drivers (run one at a time, in order)

### T9a — Collect frozen Job Artifacts

~~~text
Apply the Shared Preamble. Execute T9a from the adoption plan against thClaws
>= v0.88.0.

Replace, do not supplement, T5 collection:
- Forward collect_files as thClaws glob patterns on BOTH stream and x_callback
  agent-run calls.
- After successful worker completion and BEFORE Atlas writes succeeded, use
  jobs.thclaws_session_id plus resolved workspace_dir to read the frozen
  manifest and each declared artifact. Never call workspace sync export and do
  not consult sync_mode for collection.
- Treat manifest and bytes as semi-trusted. Validate ids, unique jailed paths,
  non-negative sizes, lowercase 64-hex SHA-256 values, aggregate limits, and
  skipped[]. For every download require x-sha256, actual length, and local SHA
  to equal the manifest before opaque-store staging. Publish file_ref rows all
  at once or not at all.
- Empty non-skipped manifest is valid zero-file collection. Any skipped[] or
  malformed/mismatched member is visible and failure-isolated, never partial
  success.
- Do NOT put worker network I/O inside T3's callback terminal transaction.
  Add a pre-terminal callback-success collection phase; prove duplicate callback
  and callback-vs-reaper convergence is still correct.
- Atlas reuses thClaws session ids for conversation continuity while upstream
  uses that id as the artifact scope. Add a durable worker/workspace/session
  lease from dispatch through collection/terminalization so a later continued
  turn cannot overwrite the previous snapshot. Cover crash, cancel, and worker
  failure recovery; an in-memory-only lock is insufficient.

Remove obsolete sync-export and dead tar-ingestion only after all callers are
gone; sync_mode remains solely for T4's advisory busy probe.

Mandatory checks: forwarding and correct session/workspace lookup; frozen bytes
after live-file mutation; malformed/duplicate manifest, unsafe path, caps,
skipped[], hash/header/length mismatch, and short/oversized reads publish
nothing; stream/callback barrier ordering; no-collect zero request; old worker
no sync fallback; and two continued-session jobs cannot interleave collection.
Mutation-test forwarding, local SHA comparison, atomic publication, callback
barrier ordering, and session lease enforcement. Update contract, threat model,
OpenAPI, EN/TH docs, gate, and PROGRESS; close out through the Shared Preamble.
~~~

### T9b — Handoff through Bearer-authenticated inputs

~~~text
Apply the Shared Preamble. Execute T9b only after T9a is merged and green.

Replace T6 tar/sync push with POST /v1/inputs:
- Keep policy.file_handoff and edge push_files as save-time and runtime guards;
  resolve only already-verified file_ref artifacts.
- Revalidate upload-store containment and every relative path. Target only
  inputs/incoming/<run_id>/<node_key>/<relpath>; never use a caller destination
  or write .git/.thclaws.
- Send one request only: <=100 files, <=64 MiB decoded, <=96 MiB JSON. Do not
  batch or blindly retry an ambiguous write; upstream has no input transaction
  or idempotency key. A failure creates no downstream job.
- Require exactly one written[] acknowledgement per submitted path with matching
  path, size, and SHA-256 before audit and downstream dispatch.
- files_dir points to inputs/incoming/<run_id>/<node_key>. Remove final
  sync-push/tar callers; sync_mode=disabled must not block this path.

Mandatory two-worker checks: exact bytes at B; normal Bearer auth; zero sync
calls; policy guards; unsafe/out-of-store source; 100-file and 64-MiB bounds;
malformed/missing/mismatched acknowledgements; transport failure; and no false
audit/downstream job in every failure case. Mutation-test runtime policy,
acknowledgement hash, prefix containment, and single-request cap. Update
contract, threat model, EN/TH docs, gate, and PROGRESS.
~~~

## Branch close-out

~~~text
Run gate and lint unpiped. Then use an INDEPENDENT Claude review over
git diff main...HEAD against AGENTS.md and the T9 milestone. Trace stream and
callback paths end to end; verify token handling, manifest/byte/input
acknowledgement validation, races, API compatibility, mutation coverage, and
EN/TH parity. Fix actionable findings and repeat until a clean review.
~~~
