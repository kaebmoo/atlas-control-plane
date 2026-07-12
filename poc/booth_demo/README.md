# PoC — Booth Demo (Atlas @ Chiang Mai AI Party 2026)

A **stdlib-only** web PoC that shows Atlas's full surface on **two real thClaws
workers**, including the newest capability: **files moving between agents**.

- **T9a `collect_files`** — a worker *writes real files*; Atlas snapshots them through the
  thClaws Job Artifacts API (Bearer, immutable, SHA-256-verified) and publishes them as
  downloadable `file_ref` artifacts.
- **T9b `push_files`** — Atlas places those files (or files a user uploaded) into the
  **next** worker's workspace via Bearer `POST /v1/inputs` before its job starts; the
  downstream prompt reads them from `{files_dir}`.
- Plus everything the permit_web PoC already showed: input envelope + `_meta.source`
  provenance, fixed worker routing, **human approval gate**, artifacts,
  budget-capped policy, audit, poll return path (token stays server-side).

Two pages (per the booth script in [`docs/booth-ai-party-2026-en.md`](../../docs/booth-ai-party-2026-en.md)):

| Page | Story | File path shown |
| --- | --- | --- |
| `/news` | **News Desk** — reporter researches a topic, writes `article.md` + `sources.md` → Atlas collects them and pushes them into the anchor's workspace → anchor writes `broadcast.md` → human approves → publish note | worker → worker (collect + push) |
| `/permit` | **Permit Desk** — citizen submits a form and uploads real attachment files → Atlas pushes the uploads into the examiner's workspace → summary brief → officer approves → official notice produced **as a file** | user upload → worker (push), worker → user (collect) |

```
browser ── /api/submit ──▶ Atlas POST /api/workflow-runs        (envelope + _meta.source)
browser ── /api/upload ──▶ Atlas POST /api/workflow-runs/{id}/files   (permit attachments)
browser ── /api/activate ─▶ Atlas POST /api/approvals/{id}/approve    (after all uploads finish)
browser ── /api/cancel ───▶ Atlas POST /api/workflow-runs/{id}/cancel (best-effort on upload failure)
browser ── /api/status ──▶ Atlas GET  /api/workflow-runs/{id} (+ artifacts + events)
browser ── /api/file   ──▶ Atlas GET  /api/artifacts/{id}/content    (download collected files)
approver ── /api/decide ──▶ Atlas POST /api/approvals/{id}/approve|reject
```

## Requirements

- **Python 3.11+** for Atlas (the PoC scripts are 3.9+). No pip installs — stdlib only.
- **thClaws v0.88+** (Job Artifacts API + `/v1/inputs`) — the contract Atlas is pinned to
  is v0.89.0 / commit `bf1d6bb`, see
  [`docs/specs/thclaws-worker-contract.md`](../../docs/specs/thclaws-worker-contract.md).
- A model key for each worker (BYOK — thClaws holds it, Atlas never sees it), or a local
  model configured in thClaws.

## Quick start (5 terminals)

> **Each worker needs its OWN working directory.** Collected files come from — and pushed
> files land in — the worker's current directory. Two workers sharing one directory would
> read each other's files and break the story.

```bash
# 1) reporter worker
mkdir -p ~/booth/reporter && cd ~/booth/reporter
ANTHROPIC_API_KEY='sk-…' THCLAWS_API_TOKEN='dev-token-1' \
  thclaws --serve --bind 127.0.0.1 --port 4317

# 2) anchor worker
mkdir -p ~/booth/anchor && cd ~/booth/anchor
ANTHROPIC_API_KEY='sk-…' THCLAWS_API_TOKEN='dev-token-2' \
  thclaws --serve --bind 127.0.0.1 --port 4318

# 3) Atlas (separate DB so the demo doesn't touch your real one)
cd <repo root>
ATLAS_LOOPBACK_NO_AUTH=true ATLAS_DB=./data/booth.sqlite \
  python3 -m atlas --host 127.0.0.1 --port 8787

# 4) one-time setup (registers both workers + creates both workflows; idempotent)
python3 poc/booth_demo/setup.py

# 5) the booth web app
python3 poc/booth_demo/app.py
```

Open **http://127.0.0.1:8090** — the index page shows both workers' online status, then
pick a story. The Atlas dashboard at http://127.0.0.1:8787 stays useful side-by-side
(live job streams, Fleet, run Monitor with the same approval buttons, Usage/token view).

With a real API token instead of the loopback bypass:

```bash
python3 -m atlas.admin create-admin admin      # prints the token once
export ATLAS_TOKEN='<token>'
python3 poc/booth_demo/setup.py
ATLAS_TOKEN="$ATLAS_TOKEN" python3 poc/booth_demo/app.py
```

## What each run demonstrates (talk track)

**News Desk (`/news`)** — the booth star, ~2 minutes:

1. Submit a topic → watch the pipeline chips: **ผู้สื่อข่าว** runs first.
2. When the reporter finishes, `article.md` + `sources.md` appear as downloadable files
   (T9a — hashes shown), and a blue banner announces Atlas **pushed them into the
   anchor's workspace** (T9b: `POST /v1/inputs` → `inputs/incoming/<run>/anchor/…`).
3. The anchor reads those files (not a copy-pasted prompt!) and writes `broadcast.md`.
4. The run pauses at **อนุมัติการออกอากาศ** — click Approve → publish note; Reject →
   the run fails at the gate, and no publish happens. *This is what separates it from a
   chatbot.*

**Permit Desk (`/permit`)** — same engine, document-work tone:

1. Fill the form, attach one or two real files (small text/markdown/pdf), submit.
2. The run waits at an upload gate until every attachment reaches Atlas; only then does intake
   start. After intake, Atlas pushes them into the **examiner's** workspace — the examiner
   reports on the *actual file contents*.
3. Officer approves at the gate → the notice node writes `notice.md`, Atlas collects it,
   and the page offers **the official letter as a downloadable file**.

Everything above is also visible in the Atlas dashboard Monitor view: node timeline,
`files_pushed` events, artifacts, audit, and token usage per run.

## Booth-day checklist (from `docs/booth-ai-party-2026-en.md`)

- [ ] Big screen: `/news` page on the left, Atlas dashboard Monitor on the right.
- [ ] Both workers started in their own directories, keys loaded, `setup.py` re-run
      (it re-polls; both must show **online** on the index page).
- [ ] **Bad-wifi fallback:** point one worker at a local model; record a backup GIF of a
      clean run in case the live one breaks.
- [ ] Rehearse `/news` to finish in ~3 minutes; one sentence you can repeat all day:
      *"Orchestrate many AI agents — on your own hardware, budget-capped, fully auditable."*
- [ ] Old inputs accumulate under each worker's `inputs/incoming/`; wipe the two booth
      directories between sessions for tidy demos.

## Design notes / constraints baked in

- `push_files` **must ride a worker→worker edge.** Edges leaving a human gate are taken
  by the approval decision path, which doesn't carry push intents — so both graphs place
  the gate *after* the push. (If you rearrange the graphs, keep that rule.)
- The permit page creates a run at an initial upload gate (the upload API needs a `run_id`),
  uploads every attachment, then releases that gate. The intake→examiner push edge therefore
  cannot resolve `upload_*` before the uploads exist. If an upload or activation fails, the
  page best-effort cancels the run so it does not remain stranded at that gate.
- Every state-changing PoC proxy request requires the page's per-process CSRF token and an
  exact loopback `Origin`/`Host` match; the server-held Atlas token is never usable by a
  cross-origin form or `fetch`.
- `policy.file_handoff: true` is the explicit opt-in for push edges; collection caps,
  path jails, SHA-256 verification, and the exact-`written[]` acknowledgment are enforced
  by Atlas core (see the threat model), not by this PoC.
- Upload keys are sanitized to `upload_<ascii-name>` (Atlas artifact-key charset). Thai
  filenames still display on the page; only the key is transliterated.
- This is a demo, not production: keep `ATLAS_LOOPBACK_NO_AUTH` strictly on localhost.
  Nothing here imports or changes `atlas/` core — but the PoC's own proxy logic (this
  `app.py` Handler, `setup.py`'s graph wiring) **is** exercised by `scripts/check_booth_poc.py`,
  which runs as part of the canonical gate (`scripts/gate.sh`).

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| index page shows a worker offline | thClaws not running / wrong `THCLAWS_API_TOKEN` / wrong port → fix and re-run `setup.py` (it re-polls) |
| `workflow node reporter role no matching worker` from setup | workers must be registered before the workflow — `setup.py` does this in order; check the worker step didn't fail |
| reporter succeeds but no files appear | the model didn't actually write `article.md`/`sources.md` (check the job stream in the dashboard); the run continues — collection is failure-isolated (`files.collection_failed` in job events) |
| push banner says 0 files | nothing matched the edge's glob (`files.reporter.*` / `upload_*`) — see the previous row, or no attachments were uploaded |
| anchor says it can't find the folder | worker started in the wrong directory, or an older thClaws without `/v1/inputs` (need v0.88+) |
| run stuck `running` on a node | that worker is offline or mid-model-call; watch the live stream in the dashboard Jobs view |
| `file handoff … requires policy.file_handoff` on save | keep `file_handoff: true` in the policy (setup.py sets it) |
| upload rejected / connection cut mid-upload | per-file cap is **8 MiB** (`MAX_UPLOAD_BYTES` in `app.py`) — split or shrink the attachment |
| upload gate never releases / activation fails after ~5s | `/api/activate` polls Atlas for the upload gate for **`UPLOAD_ACTIVATION_TIMEOUT_SECONDS = 5`** — if an upload is still in flight or Atlas is slow, it times out; retry once the upload finishes |
