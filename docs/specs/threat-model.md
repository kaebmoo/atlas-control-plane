# Atlas Threat Model & Deployment Assumptions (Accepted release baseline — 2026-06-30)

Records the **deployment and trust assumptions** Atlas core is built on, so "is X a bug?" is
decidable: a defect is real only relative to a stated model. Several otherwise-flagged items are
**accepted residual risks** under the model below; each lists rationale, an owner, and the
trigger that re-opens it.

> Status: **Accepted release baseline — 2026-06-30.** Assumptions checked against the code (author
> self-check **+ independent review** — see *Targeted review log*); **named owner: Pornthep Nivatyakul**
> for all accepted risks ([ADR 0001](../adr/0001-multi-tenancy-silo-vs-pooled.md) backs the silo
> model). CI required-checks are **enforced**: `main` branch protection requires `gate` + `lint`
> (strict, enforce_admins), **verified** by a throwaway PR whose failing `lint` was blocked from
> merge. Baseline = an agreed posture, **not** a proof that no bugs exist (cold/LLM audits sample;
> they cannot prove absence). *Not yet enforced — future hardening if "no direct push to `main`" is
> required: PR-review approval + push restrictions; required status checks alone do not block a
> direct push.*

## Deployment model

- **Instance-per-tenant (silo).** Each tenant runs its **own** Atlas process against its **own**
  SQLite database; core tables carry no `tenant_id` (ADR 0001). Pooled/multi-tenant is deferred.
- **One writer per DB — an OPERATIONAL CONSTRAINT, not enforced.** SQLite is single-writer and
  Atlas serializes writes behind an in-process lock, so the operator must run exactly one Atlas
  process per database file. The code does **not** prevent a second process (no DB-level advisory
  lock); running two is operator error, not something Atlas detects today.
- **No built-in TLS.** A reverse proxy terminates TLS and may add request limits; Atlas binds
  loopback/private by default.

## Trust boundaries

| Actor | Trust | Enforcement |
|---|---|---|
| **HTTP API callers** | Authenticated + RBAC | per-user **API token = SHA-256 of a 256-bit random token** (high-entropy, so no KDF needed); **passwords = PBKDF2-HMAC-SHA256, 600k iters**; role→permission map; `?token=` only on SSE streams; secrets never returned |
| **Loopback / `ATLAS_LOOPBACK_NO_AUTH`** | Full admin (dev only) | off by default; documented admin bypass |
| **Operators (CLI / BYOK / fleet)** | **Trusted** | `python -m atlas.byok`, `atlas.admin`, fleet provisioning are operator-run; no untrusted HTTP path reaches them. Fleet secret writes hold a **cross-process `flock`** across the read-modify-write (concurrent-safe). The **BYOK env-file write has no cross-process lock** (atomic temp+replace only — a crash can't truncate, but two concurrent writers can lose an update) → run `atlas.byok` **serially** per env file. |
| **thClaws workers** | **Semi-trusted** | SSE output is bounded — chunked-read **stream deadline** (`ATLAS_MAX_STREAM_SECONDS`), **output cap** (`ATLAS_MAX_JOB_OUTPUT_BYTES`), per-event buffer cap; tokens encrypted at rest; `base_url` scheme restricted to http(s); **tool/skill event payloads are never persisted** — `input`/`output` are projected to structural metadata (`{id, name, status, *_bytes, *_sha256}`) before storage, since a payload can carry a BYOK key Atlas cannot detect (truncation is not redaction) |
| **Worker callbacks (T3): `POST /api/worker-callbacks/{job_id}`** | Semi-trusted inbound surface — **the one documented pre-auth exception** to "all `/api/*` behind `_is_authorized()`" | The route is dispatched in `_dispatch` BEFORE the generic auth gate because thClaws delivers with the per-dispatch HMAC `api_key`, not a user token (routing it through `_is_authorized()` would 401 legitimate deliveries). Compensating controls, in order: **body-size cap checked from Content-Length before any byte is read** (4 MiB, connection closed on reject); **constant-time HMAC-SHA256 verification** of the signed token (keyed by `ATLAS_SECRET_KEY`, binds job id + expiry — cross-job replay and expiry tampering break the signature; expiry covers the callback deadline + the worker's 3-attempt retry envelope + skew margin); token **never logged or stored** (byte-scan checked); **idempotent apply** via an atomic non-terminal→terminal transition, so duplicate delivery / callback-vs-reaper races converge to one terminal state and replays after terminal are `200` no-ops; unverified requests get `401` and touch nothing — with a durable `job.callback_rejected` audit row **only when the job id is real AND at most once per job per window, check-and-reserved under a lock, failing CLOSED when the tracking cache saturates** (concurrent rejections and >cap job-id rotation both stay bounded — durable rows ≤ cap per window, so even a compromised worker cannot grow the DB/WAL); the whole apply (terminal state + text + events + audit + usage) is **one transaction**, so a mid-apply crash preserves the worker's retry as recovery; all writes audit as the **`system:worker-callback`** actor; jobs that never call back are bounded by the **reaper** (`ATLAS_CALLBACK_TIMEOUT_SECONDS`), which waits the deadline PLUS the retry-envelope grace so a worker's still-valid retry near the deadline is never terminal-ized out from under it; both directions of the exchange are time-and-size bounded — the dispatch-side ACK read (64 KiB + wall-clock deadline) and the inbound callback body read (per-recv socket timeout + wall-clock deadline + a global read-slot bound, both a stalled read and a full read-slot table answered with a RETRYABLE 503 so a transient stall never abandons a real delivery, so a token-holding worker cannot pin handler threads with one slow body or many parallel ones) — the dispatch-side error-body read is likewise bounded in BOTH bytes (64 KiB) and wall-clock time (a chunked read under a deadline, so a slow-drip error body can't pin the thread), and the dispatch socket timeout is capped at the ACK deadline — and only a contract-conforming 202 ACK (status `accepted`, echoed run id) is recorded as a dispatch — a non-202 2xx fails fast, a mismatched ACK stays pending and never binds a session from the untrusted echo; a delivered payload whose `run_id` mismatches the URL's job is a 400 that touches nothing; the terminal payload stores tool **names/counters only** (same no-payload rule as streamed tool events); the callback route closes the connection on every pre-body-read rejection so an unread declared body can't desync a keep-alive/proxy connection; a protocol-level ACK/error-body failure (IncompleteRead/BadStatusLine, an HTTPException not an OSError) is caught everywhere it can occur — treated as AMBIGUOUS for the dispatch ACK — the run may be executing, so the job stays callback-pending rather than failing and discarding a real result; the read-slot bound is held through the ENTIRE read+parse+apply, not just the read, so concurrent PROCESSING (JSON decode, token scans, DB work) is bounded, not just reading; the callback token is a live credential, so a single `_redact_token` helper scrubs it from EVERY worker-controlled persisted field (ACK session_id, summary, tool names, error message) — a session_id carrying the token is skipped, not bound; the replay-recovery handoff claim is serialized (one-writer-per-DB, like `claim_trigger_dedupe`) AND the child has a deterministic id derived from the source, so neither concurrent duplicate callbacks nor a crash between child creation and the link can spawn a second child (the deterministic id flows only through a private keyword arg, never a request-body `id`, so it can't be pre-occupied to hijack the linkage); a callback `run_id` must be a nonempty string exactly equal to the URL job id. Enforced by `scripts/check_async_jobs.py` (mutation-locked: carve-out removal, skipped verification, dropped caps, dropped envelope, non-atomic/split-transaction apply, 5xx-as-definitive, unconditional rejection audit all turn the gate red). |
| **Worker-supplied Job Artifacts (T9a): `/v1/sessions/{sid}/artifacts*`** | Semi-trusted Bearer-authenticated manifest and bytes; a compromised/buggy worker can lie about metadata or return changed/truncated content | Atlas forwards bounded glob patterns to `/agent/run`, then reads the frozen session manifest and validates session id, unique ids/paths, safe relative paths, non-negative sizes, lowercase 64-hex SHA-256 values, `patterns[]`, optional `skipped[]` (absent = empty, matching the wire — thClaws serde-omits the key when nothing was skipped; a present non-list is rejected), and the 256-file/300-MiB aggregate caps before downloading. Every member is staged under a fresh opaque upload id only after `x-sha256`, exact byte length, and a locally calculated SHA-256 match the manifest. Any malformed member, skipped entry, cap breach, truncated/oversized read, or mismatch records failure-isolated `files.collection_failed`; artifact rows and blobs publish all-or-nothing, and collection resolves before `succeeded`. A job without `collect_files` makes no artifact request. Atlas never calls `/workspace/sync/export` and never consults `sync_mode` for this path. A durable worker/workspace/session lease spans dispatch, collection, and terminalization; continued jobs wait, and cancel/failure/restart release or recover it — a terminal owner keeps its lease while its collector is still mid-download (the lease-claim backstop honors `collection_inflight`), the LOSING side of a duplicate/reaper terminal race clears its own flag and lease, and startup clears stale collection flags so a crash mid-collection cannot wedge the session's waiters. Enforced by `scripts/check_job_artifacts.py` (stream/callback forwarding, frozen bytes, malformed manifests, integrity, no-fallback, barrier, and lease checks including the reaper-vs-collector race). |
| **File handoff to workers (T9b): `POST /v1/inputs`** | Atlas WRITES to a semi-trusted worker's workspace | **Additive and jailed, by construction.** Atlas builds every destination as `inputs/incoming/<run_id>/<node_key>/…` — inside thClaws's default `inputs/` destination jail (upstream independently rejects `..`/absolute/`.git`/`.thclaws` paths), and the API has no replace/trash/delete semantics, so a handoff can only add files under an opaque per-run prefix and can never clobber the target worker's own files. **Opt-in only:** an edge `push_files` requires `policy.file_handoff` — enforced at graph-save time AND re-checked as a runtime guard in the node loop (a push cannot happen without the policy even if validation were bypassed). The normal worker Bearer authenticates the call; workspace sync, tar, 409 retries, and `sync_mode` are NOT used — a handoff works on a sync-`disabled` worker. Capped at min(`ATLAS_SYNC_MAX_FILES`/`ATLAS_SYNC_MAX_BYTES`, upstream 100 files / 64 MiB decoded), checked on ACTUAL byte length BEFORE the request: upstream writes files one at a time with no transaction or idempotency key, so Atlas never sends a batch that could trip an upstream limit mid-write, sends exactly ONE request per edge, and never blindly retries — any residue from a failed request stays confined to the unique, undispatched prefix. **The source `file_ref` artifacts are NOT assumed pre-validated:** `POST /api/artifacts` lets any authenticated caller create a `file_ref` with an arbitrary `content` path and `relpath`, so `_push_files_to_worker` RE-VALIDATES every selected artifact before use — the same containment `_download_artifact` enforces (`(upload_dir/content).resolve().parent == upload_dir.resolve()`, so a `../…`/absolute `content` can't exfiltrate a host file) plus `_reject_unsafe_path` on the relpath (so it can't escape the `inputs/incoming/` prefix); a violation fails the edge. **The `written[]` acknowledgment must cover the exact sent set with matching size and SHA-256** before `files.pushed` is audited or the downstream job is created — a missing, extra, or mismatched entry (or a non-JSON ack) fails the edge. Deadline-bounded; the network call runs OUTSIDE the shared workflow-runner lock (mirroring the unlocked job wait) so it can't stall other runs' stepping or operator pause/cancel. A failed handoff raises and fails the edge (no partial-success handoff). Enforced by `scripts/check_file_handoff.py` (two mock workers; mutation-locked: dropping the runtime file_handoff guard makes a no-policy push reach the worker; removing the save-time cross-check stops rejecting push_files without the policy; dropping the upload-store containment check lets a crafted artifact exfiltrate an out-of-store file; skipping the written[] validation lets a corrupted ack succeed; dropping the inputs prefix or batching into per-file requests goes red; the mock asserts legacy `/workspace/sync/*` is never called). |
| **Pack files** | Semi-trusted | validated through the shared engine validators; signature optional, enforceable via `ATLAS_REQUIRE_SIGNED_PACKS` |

T9a path uniqueness is checked after POSIX normalization and control characters are rejected in
worker manifest paths and forwarded glob patterns. A known upstream limitation remains: thClaws
can leave a prior session manifest in place if snapshot creation fails before its final manifest
write. Atlas's bounded clock-skew check cannot distinguish that prior snapshot when a continued
turn finishes within the skew window; this is tracked as an upstream generation/atomic-clear ask,
not treated as a silent compatibility fallback.

**Production pack-signing decision:** code default is `ATLAS_REQUIRE_SIGNED_PACKS=false` (unsigned
accepted, for backward-compat with the unsigned shipped `gov_complaint` pack). **Production
deployments SHALL set it `true`.** Running production with `false` is an **accepted risk** owned by
**Pornthep Nivatyakul** — rationale: packs are semi-trusted and unsigned import is an operator
foot-gun; re-open trigger: any externally-sourced pack. Reflected in `ops/deployment.md` §4.

## Accepted residual risks under this model

Not fixed because the model makes them unreachable / out-of-threat. Each: rationale → trigger that
turns it into real work. **Owner of all six accepted risks: Pornthep Nivatyakul** (named sign-off,
2026-06-30; DNS risk #6 added 2026-07-06 under the same owner — real accountability, not an
auto-assigned name).

1. **`claim_trigger_dedupe` is in-process atomic only** (RLock, no UNIQUE constraint). Fine
   under one-process-per-DB. → *Trigger:* multi-process/shared-volume deployment → add a
   `UNIQUE(trigger_id, dedupe_key)` claims table + `INSERT OR IGNORE`.
2. **Migration runner uses `executescript`** (implicit COMMIT). Fine because all shipped steps
   are `CREATE … IF NOT EXISTS` / guarded `ALTER`. → *Trigger:* any future raw-SQL step → run
   steps as discrete statements in one transaction.
3. **Removing `ATLAS_SECRET_KEY` after worker tokens are encrypted** 400s `GET /api/workers` +
   routing. Operational foot-gun, not an attacker path. → *Trigger:* key-rotation requirement →
   key list / decrypt-tolerant listing.
4. **BYOK env-file write has no cross-process lock** → assumes SERIAL operator invocation (atomic
   temp+replace prevents truncation, but two concurrent writers can lose an update). Fleet secret
   writes already hold a cross-process `flock`. → *Trigger:* concurrent automation invoking BYOK →
   add an OS file lock around the env read-modify-write.
5. **`max_minutes` counts paused / human-wait wall time** (intended total-wall budget). →
   *Trigger:* if it must be active-compute only → subtract paused intervals.
6. **Worker-hostname DNS resolution (`getaddrinfo`) is bounded only by the OS resolver**, not by
   Atlas. The open-phase watchdog (`_urlopen_deadline`, `atlas/thclaws_client.py`) force-closes a
   worker's socket once its deadline passes, but a `getaddrinfo` that hangs blocks the caller with
   no socket to close. Rationale: a semi-trusted worker cannot lengthen its OWN resolution without
   also controlling the operator's DNS resolver (a strictly larger compromise, outside this worker
   trust boundary); IP / tunnel-address workers skip resolution entirely, and the actual
   worker-controlled vector — a drip-fed status-line/header read on the connected socket — IS
   bounded by the watchdog. → *Trigger:* workers addressed by attacker-influenced hostnames, OR a
   hard per-call wall-clock SLA that must include name resolution → resolve in a bounded helper
   thread (or pre-resolve with a timeout) and connect to the resulting IP.

## Targeted review log (DoD #6)

**2026-06-30.** Two passes against the code (not a random cold pass):

1. **Author self-check** (table below) — every trust-boundary claim and accepted residual risk
   mapped to its implementation; all matched, 0 discrepancies.
2. **Independent review** — a separate reviewer, given the claims but **not** the self-check,
   re-verified them cold against `atlas/` + `fleet/`: **all 15 CONFIRMED, 0 discrepancies**,
   corroborating the self-check. Precision notes it added (none change a verdict): the `?token=`
   gate is a literal `path.endswith("/events")` suffix test, not an allow-list of the three SSE
   routes (a future GET route ending `/events` would also accept a query token — still RBAC-gated);
   the three SSE bounds are split across `iter_sse` (deadline + per-event cap) and the jobs consumer
   (deadline + total-output cap), covering only in combination; `base_url` is a scheme **prefix**
   check, not a parsed scheme; and of the four migration steps only `SCHEMA` is a string under
   `executescript` (002–004 are callables guarded by `PRAGMA table_info`).

Author self-check evidence:

| Claim | Code | Verdict |
|---|---|---|
| API token = SHA-256 of a 256-bit random token | `auth.py` `generate_api_token`/`hash_api_token` (`token_urlsafe(32)` → `sha256`) | match |
| Passwords = PBKDF2-HMAC-SHA256, 600k iters | `auth.py:9` `PASSWORD_ITERATIONS`, `hash_password`/`verify_password` (`compare_digest`) | match |
| role→permission map enforced | `app.py:167` `ROLE_PERMISSIONS[role]`; `_required_permission` (`app.py:895`) | match |
| `?token=` only on SSE streams | `app.py:875-881` (GET + path ends `/events`) | match |
| Secrets never returned | `_public_worker` pops `token` (`app.py:935`); applied on every GET `/api/workers` path | match |
| `ATLAS_LOOPBACK_NO_AUTH` off by default | `config.py:40` (`_bool_env(..., False)`) | match |
| Worker SSE bounded (deadline / output cap / per-event cap) | `jobs.py:35-36,237-281` (3600s / 16 MiB); `iter_sse` chunked + 32 MiB cap (`thclaws_client.py:116`) | match |
| Tool/skill payloads never persisted (structural metadata only) | `project_structured_event` (`thclaws_client.py`) whitelists `{id,name,status,*_bytes,*_sha256}`; applied before `append_job_event` (`jobs.py`); **read path also redacts** legacy raw rows via `redact_tool_payload_for_read` in `_stream_job_events` (`app.py`); byte-scan tests (write + legacy read) in `check_jobs.py` | match |
| Total worker output bounded (raw wire bytes) | `iter_sse(max_total_bytes=…)` caps CUMULATIVE bytes read at the source (`thclaws_client.py`), so data, framing/whitespace padding, comment and data-less frames all count; `JobManager` passes `max_output_bytes`; per-event 32 MiB cap + stream deadline still apply; flood tests (tool payload / event name / terminal / whitespace-padded / comment) in `check_jobs.py` | match |
| Worker tokens encrypted at rest | `db.py:1569-1624` encrypt-then-MAC (HMAC-CTR, random nonce, domain-separated keys) | match |
| `base_url` scheme http(s)-only | `db.py:1493-1496` (raises `ValueError` otherwise) | match |
| Packs via shared validators; signing optional | `packs.py:14` imports `validate_workflow_references`; `verify_pack_signature` + `require_signed_packs` (`config.py:45`) | match |
| Risk 1 — dedupe in-process atomic, no UNIQUE | `db.py:1348-1364` SELECT+INSERT under `_lock`; schema `dedupe_key` has a plain INDEX, no UNIQUE | match |
| Risk 2 — migration `executescript` | `db.py:549`; string steps are `CREATE … IF NOT EXISTS` (`MIGRATIONS`, `db.py:503`) | match |
| Risk 3 — removing `ATLAS_SECRET_KEY` 400s GET `/api/workers` | `db.py:1594` raises `ValueError` → `app.py:174` maps to 400 | match |
| Risk 4 — BYOK env-file has no cross-process lock; fleet sidecar does | `byok.py:41-64` atomic-write, no flock; `fleet.py:138-164` `fcntl.flock` across read-modify-write | match |
| Risk 5 — `max_minutes` = wall-clock from start | `workflows.py:1681-1685` (`started_at + timedelta(minutes=max_minutes)`) | match |

## Definition of done (stop criterion — replaces "audit until zero")

LLM cold passes SAMPLE; they can't prove absence, so "two clean passes" is not the bar. Done =:

1. No **known High/Medium** finding that isn't fixed or **formally accepted** here (with owner).
2. Every accepted risk has a **rationale, owner, and re-open trigger** (table above).
3. `scripts/gate.sh` passes from a **clean tree** (no uncommitted working-tree state).
4. `scripts/lint.sh` is **fail-closed** and green in required CI (no `|| true`, no global skip;
   suppressions are per-line `# nosec <code>` with the rationale in adjacent code/docstring).
5. This threat model is **accurate and linked** from `docs/README.md`.
6. **One targeted review** against this threat model + the trust-boundary matrix has been done.
7. **Low** findings live in a backlog ([backlog.md](backlog.md)); they do **not** block sign-off.

## In scope / out of scope

- **In scope:** auth/RBAC, secret handling (tokens, BYOK keys), HTTP input validation (400 not
  500), workflow/job state-machine integrity under concurrency, injection (SQL/CSV/env-file/URL
  scheme), data durability (atomic writes, backups), bounded worker streams.
- **Out of scope (by design):** pooled multi-tenancy, multi-writer/HA, TLS, a rating/billing
  engine (NT owns it), sandboxing untrusted operator CLI input.

## How it's enforced today

`scripts/gate.sh` — hermetic, offline, stdlib checks (one per subsystem/behavior) incl.
concurrency **stress** + parser **fuzz** (both EVIDENCE, not proofs of absence). `scripts/lint.sh` — fail-closed ruff + bandit +
mypy (pinned). Both run in CI ([`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)) as jobs
`gate` and `lint`. **"Required" is a branch-protection setting, not a YAML self-declaration** — a
sign-off prerequisite is that `main` branch protection lists both as required status checks and a
failing PR is actually blocked (command in the CI file). Every fix carries a hermetic check, and
the guards are mutation-tested (breaking one turns the gate red).
