# Atlas Threat Model & Deployment Assumptions (DRAFT — pending sign-off)

Records the **deployment and trust assumptions** Atlas core is built on, so "is X a bug?" is
decidable: a defect is real only relative to a stated model. Several otherwise-flagged items are
**accepted residual risks** under the model below; each lists rationale, an owner, and the
trigger that re-opens it.

> Status: DRAFT inferred from the code + [ADR 0001](../adr/0001-multi-tenancy-silo-vs-pooled.md).
> Confirm/correct each assumption and assign owners, then this becomes authoritative.

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
| **Operators (CLI / BYOK / fleet)** | **Trusted, SERIAL** | `python -m atlas.byok`, `atlas.admin`, fleet provisioning are operator-run; no untrusted HTTP path reaches them; **must be invoked serially** (no cross-process write lock on the BYOK env file / fleet sidecar beyond the in-process + flock guards) |
| **thClaws workers** | **Semi-trusted** | SSE output is bounded — chunked-read **stream deadline** (`ATLAS_MAX_STREAM_SECONDS`), **output cap** (`ATLAS_MAX_JOB_OUTPUT_BYTES`), per-event buffer cap; tokens encrypted at rest; `base_url` scheme restricted to http(s) |
| **Pack files** | Semi-trusted | validated through the shared engine validators; signature optional, enforceable via `ATLAS_REQUIRE_SIGNED_PACKS` |

**Production pack-signing decision (confirm):** default is `ATLAS_REQUIRE_SIGNED_PACKS=false`
(unsigned accepted). Recommendation: production **SHOULD** set it `true`. ← operator's call.

## Accepted residual risks under this model

Not fixed because the model makes them unreachable / out-of-threat. Each: rationale → trigger
that turns it into real work. **Owner: `<assign on sign-off>`** for all (assign real owners).

1. **`claim_trigger_dedupe` is in-process atomic only** (RLock, no UNIQUE constraint). Fine
   under one-process-per-DB. → *Trigger:* multi-process/shared-volume deployment → add a
   `UNIQUE(trigger_id, dedupe_key)` claims table + `INSERT OR IGNORE`.
2. **Migration runner uses `executescript`** (implicit COMMIT). Fine because all shipped steps
   are `CREATE … IF NOT EXISTS` / guarded `ALTER`. → *Trigger:* any future raw-SQL step → run
   steps as discrete statements in one transaction.
3. **Removing `ATLAS_SECRET_KEY` after worker tokens are encrypted** 400s `GET /api/workers` +
   routing. Operational foot-gun, not an attacker path. → *Trigger:* key-rotation requirement →
   key list / decrypt-tolerant listing.
4. **BYOK env-file & fleet sidecar writes assume SERIAL operator invocation** across processes
   (in-process lock + a fleet flock exist; BYOK has none cross-process). → *Trigger:* concurrent
   automation invoking BYOK → add an OS file lock around the env read-modify-write.
5. **`max_minutes` counts paused / human-wait wall time** (intended total-wall budget). →
   *Trigger:* if it must be active-compute only → subtract paused intervals.

## Definition of done (stop criterion — replaces "audit until zero")

LLM cold passes SAMPLE; they can't prove absence, so "two clean passes" is not the bar. Done =:

1. No **known High/Medium** finding that isn't fixed or **formally accepted** here (with owner).
2. Every accepted risk has a **rationale, owner, and re-open trigger** (table above).
3. `scripts/gate.sh` passes from a **clean tree** (no uncommitted working-tree state).
4. `scripts/lint.sh` is **fail-closed** and green in required CI (no `|| true`, no global skip;
   suppressions are per-line `# nosec <code>` with the rationale in adjacent code/docstring).
5. This threat model is **accurate and linked** from `docs/README.md`.
6. **One targeted review** against this threat model + the trust-boundary matrix has been done.
7. **Low** findings live in a backlog; they do **not** block sign-off.

## In scope / out of scope

- **In scope:** auth/RBAC, secret handling (tokens, BYOK keys), HTTP input validation (400 not
  500), workflow/job state-machine integrity under concurrency, injection (SQL/CSV/env-file/URL
  scheme), data durability (atomic writes, backups), bounded worker streams.
- **Out of scope (by design):** pooled multi-tenancy, multi-writer/HA, TLS, a rating/billing
  engine (NT owns it), sandboxing untrusted operator CLI input.

## How it's enforced today

`scripts/gate.sh` — hermetic, offline, stdlib, 17 checks incl. concurrency **stress** + parser
**fuzz** (both EVIDENCE, not proofs of absence). `scripts/lint.sh` — fail-closed ruff + bandit
(pinned) as a required CI job. Every fix carries a hermetic check, and the guards are
mutation-tested (breaking one turns the gate red).
