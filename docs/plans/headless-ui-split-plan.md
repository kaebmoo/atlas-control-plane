# Headless API / Web UI Split Plan

Goal: make Atlas runnable as a pure API server ("headless") so any external web UI or
application can consume `/api/*` without the built-in dashboard, while keeping the
current single-process mode (API + built-in UI) and the local dev workflow fully
working. All changes are additive and backward compatible.

Executor: Claude Code. Work on a branch off `main`. Follow `AGENTS.md` invariants:

- Atlas core stays **Python stdlib only**; dashboard stays **no framework, no build step**.
- `/api/*` changes are **additive only** — no endpoint path or response-shape changes
  (this plan adds no new endpoints, so `openapi.yaml` / api-reference EN+TH stay untouched).
- Preserve dashboard **gate-marker substrings** asserted by `scripts/check_workflow_api.py`.
- Every non-trivial behavior gets **one hermetic check** folded into `scripts/gate.sh`,
  and must be **mutation-tested** (break the code → gate must go red).
- Never log/store/return tokens.

## Current state (verified against source)

- All API lives under `/api/*` with Bearer auth; dispatch in `atlas/app.py`
  `_dispatch()` (~line 213). Anything not `/api/` or `/healthz` falls through to
  `_handle_static()` (~line 844), which serves `atlas/static/` with SPA fallback to
  `index.html`. This fallthrough is the **only** UI/API coupling point.
- CORS is already wide open: `_cors_headers()` sends `Access-Control-Allow-Origin: *`
  (~line 1140) and `do_OPTIONS` exists. Auth is Bearer-token, no cookies, so
  cross-origin clients already work at the protocol level.
- The dashboard (`atlas/static/app.js`, ~2760 lines) calls the API via **relative
  paths**: one central authed helper (~line 47–51) plus three direct `fetch` sites
  (usage export ~1329, artifact content ~1348, SSE-over-fetch ~1571). SSE uses fetch
  streaming, not `EventSource`, so it works cross-origin with a header.
- `ATLAS_LOOPBACK_NO_AUTH` grants admin to loopback clients (`_is_authorized`,
  ~line 1148). Dev-only; must keep working in both modes.

## Deliverables

1. `ATLAS_SERVE_UI` flag → headless mode.
2. Configurable `API_BASE` in the dashboard via `static/config.js` → UI hostable anywhere.
3. `ATLAS_CORS_ORIGINS` allowlist (default keeps current `*` behavior).
4. `scripts/serve_ui.py` — stdlib dev static server for split-mode development.
5. `scripts/check_headless.py` hermetic check, folded into `scripts/gate.sh`.
6. **Developer integration guide (EN + TH)** — how external apps/UIs call Atlas.
7. Docs: architecture + ops deployment topologies + PROGRESS.md entry.

---

## Phase 1 — `ATLAS_SERVE_UI` flag (headless mode)

`atlas/config.py`:

- Add `serve_ui: bool = True` to `Config`.
- In `from_env()`: `serve_ui=_bool_env("ATLAS_SERVE_UI", True)`.

`atlas/app.py` `_dispatch()`:

- At the existing fallthrough (`self._handle_static(path)`), guard:
  when `runtime.config.serve_ui` is false, respond
  `self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)` instead.
- `/healthz` and the `/api/*` branches are above the fallthrough and must be unaffected.
- Keep `/favicon.ico` behavior irrelevant in headless mode (it also 404s as JSON — fine).

Acceptance: with `ATLAS_SERVE_UI=0`, `GET /` and `GET /static/app.js` → 404 JSON;
`GET /healthz` → 200; authed `GET /api/...` unchanged. With flag unset, behavior is
byte-identical to today.

## Phase 2 — Dashboard `API_BASE` (UI hostable on any origin)

Keep the no-build invariant: configuration is a plain JS file, not an env substitution.

1. New file `atlas/static/config.js` (shipped default):

   ```js
   // Optional deploy-time config. Default: same-origin API.
   window.ATLAS_API_BASE = "";
   ```

2. `atlas/static/index.html`: add `<script src="static/config.js"></script>` **before**
   `app.js`. (Match the existing script-tag path style used for `app.js`.)

3. `atlas/static/app.js`: near the top add

   ```js
   const API_BASE = (window.ATLAS_API_BASE || "").replace(/\/+$/, "");
   ```

   and prefix every API URL with it — the central helper (~line 51) plus the three
   direct fetch sites (~1329, ~1348, ~1571). Grep for `fetch(` and `"/api` to catch
   all of them; do NOT touch non-API fetches if any exist.

4. Do not rename any element id/class (gate markers). `node --check` on `app.js`
   must pass (existing check does this).

Acceptance: default `config.js` (`""`) → identical same-origin behavior. Setting
`window.ATLAS_API_BASE = "http://127.0.0.1:8787"` and serving `static/` from another
port yields a fully working dashboard (login, SSE job events, artifact download,
usage CSV export) against the headless API.

## Phase 3 — `ATLAS_CORS_ORIGINS` allowlist (optional hardening, additive)

`atlas/config.py`: add `cors_origins: tuple[str, ...] = ()` via
`_csv_env("ATLAS_CORS_ORIGINS")`.

`atlas/app.py` `_cors_headers()`:

- Empty tuple (default) → current behavior exactly (`Access-Control-Allow-Origin: *`).
- Non-empty → if request `Origin` header matches an entry exactly, echo that origin
  and add `Vary: Origin`; otherwise send no `Access-Control-Allow-Origin` header.
- Never send `Access-Control-Allow-Credentials` (auth is Bearer, not cookies).

Acceptance: default env → responses identical to today (`*`). With allowlist set,
allowed origin is echoed, disallowed origin gets no ACAO header. Same-origin and
non-browser clients (no `Origin` header) are unaffected either way.

## Phase 4 — Dev mode: `scripts/serve_ui.py`

Purpose: split-mode development — edit files in `atlas/static/` live while the API
runs headless on another port. Stdlib only (`http.server`), dev-only, never part of
production deploy.

Behavior:

- `python scripts/serve_ui.py --port 8000 --api-base http://127.0.0.1:8787
  [--static-dir atlas/static]`
- Serves the static dir with SPA fallback to `index.html` (mirror `_handle_static`
  semantics) and `Cache-Control: no-store` so edits show up on refresh.
- Intercepts `GET /static/config.js` (and `/config.js` if index references it that
  way) to return `window.ATLAS_API_BASE = "<--api-base>";` — the on-disk default
  `config.js` is never modified.
- Binds 127.0.0.1 by default.

Dev workflows to document (Phase 6 §7) and support:

- **Combined (today's default):** `python -m atlas` → UI + API on :8787.
  `ATLAS_LOOPBACK_NO_AUTH=1` still works.
- **Split dev:** terminal 1: `ATLAS_SERVE_UI=0 python -m atlas`; terminal 2:
  `python scripts/serve_ui.py --api-base http://127.0.0.1:8787`. Note in docs:
  loopback no-auth still applies because the browser calls the API from 127.0.0.1;
  default CORS `*` makes this work with zero extra env.

## Phase 5 — Hermetic check: `scripts/check_headless.py`

One check per AGENTS.md rules: own temp DB, ephemeral ports, no network beyond
loopback, cleans up after itself. Model it on an existing check (e.g.
`scripts/check_workflow_api.py`) for server bootstrap style. Assertions:

1. **Headless:** start Atlas with `ATLAS_SERVE_UI=0` → `GET /` and `GET /static/app.js`
   return 404 with JSON body; `GET /healthz` returns 200; an authed `/api/workers`
   (or equivalent cheap endpoint) returns 200.
2. **Default:** start Atlas without the flag → `GET /` returns 200 HTML containing a
   known index marker; `GET /static/config.js` returns the shipped default.
3. **CORS allowlist:** with `ATLAS_CORS_ORIGINS=http://ui.example`, an `OPTIONS`
   with `Origin: http://ui.example` echoes it (+ `Vary: Origin`); with
   `Origin: http://evil.example` there is no ACAO header. Without the env, header is `*`.
4. **Dev UI server:** start `scripts/serve_ui.py` on an ephemeral port with
   `--api-base http://127.0.0.1:9`; `GET /static/config.js` body contains that base;
   `GET /` and `GET /some/spa/route` both return `index.html` content.
5. **API_BASE wiring (static assertion):** `app.js` contains `API_BASE` and the
   central helper + the three direct fetch sites reference it (substring assertions,
   consistent with existing gate-marker style); `index.html` loads `config.js` before
   `app.js`; `node --check` passes on `app.js`.

Fold into `scripts/gate.sh` alongside the other checks.

**Mutation tests (do all, then revert):** (a) remove the `serve_ui` guard → check 1
must fail; (b) hardcode ACAO `*` ignoring allowlist → check 3 must fail; (c) drop
`API_BASE` prefix from the SSE fetch → check 5 must fail; (d) make `serve_ui.py`
serve the on-disk `config.js` instead of the injected one → check 4 must fail.
If any mutation stays green, strengthen the check before proceeding.

## Phase 6 — Developer integration guide (new docs, EN + TH parity)

Create `docs/guides/api-integration-guide-en.md` and
`docs/guides/api-integration-guide-th.md` (mirror the naming of the existing
`web-user-guide-en/th.md` pair; add both to the `docs/README.md` index). This is the
document an external developer reads to build a client — web UI or backend app —
against a headless Atlas. Content is derived from the **actual code paths**, with
`docs/specs/openapi.yaml` cited as the authoritative contract; do not duplicate the
full endpoint list from api-reference, link to it instead. Sections:

1. **Overview & base URL** — headless mode (`ATLAS_SERVE_UI=0`), `/healthz` for
   liveness, all functional endpoints under `/api/*`, JSON in/out, error shape
   `{"error": "..."}` with standard HTTP status codes.
2. **Authentication** — two paths, both verified against `_is_authorized()`:
   - Interactive: `POST /api/auth/login` with username/password → token → store
     client-side → send `Authorization: Bearer <token>` on every call.
   - Machine-to-machine: an admin issues a scoped token via `/api/tokens` (pick the
     right role; never share the admin token). Note RBAC: role → permission mapping
     decides which endpoints a token can reach (403 vs 401 semantics).
   - Explicitly warn: tokens must never be put in URLs except the documented SSE
     `?token=` fallback; never log them.
3. **CORS for browser clients** — default `*`; production: set `ATLAS_CORS_ORIGINS`
   to the UI origin(s). No cookies/credentials mode; Bearer only.
4. **Core call flows with runnable examples** (each in `curl` + browser `fetch` +
   Python stdlib `urllib.request` — no third-party libs, matching repo philosophy):
   - login → list workers → submit a job → poll job status
   - run a workflow and read run artifacts
   - upload a file to a workflow run (`Content-Length`, `X-Filename`, size cap from
     `ATLAS_MAX_UPLOAD_BYTES`) and download a `file_ref` artifact (must send the
     Authorization header — a bare `<a href>` will 401; use fetch + blob in browsers)
5. **Streaming job events (SSE)** — preferred: `fetch()` streaming with the
   Authorization header (as `atlas/static/app.js` does, ~line 1530); fallback:
   `EventSource` with `?token=` (only valid on `.../events` GETs); event format,
   `after` cursor, reconnect guidance.
6. **Building a replacement web UI** — point at the shipped dashboard as a reference
   client; `config.js` / `window.ATLAS_API_BASE` convention; serve statics anywhere;
   dev loop with `scripts/serve_ui.py`.
7. **Local dev quickstart** — combined vs split mode commands (from Phase 4),
   `ATLAS_LOOPBACK_NO_AUTH` for tokenless local hacking + the reverse-proxy warning.
8. **Versioning & compatibility** — `/api/*` is additive-only (cite AGENTS.md rule);
   clients should tolerate unknown JSON fields.

Every claim in the guide must be checked against the code before writing (endpoint
paths, header names, status codes, env var names). EN and TH content must stay in
parity section-by-section.

## Phase 7 — Remaining documentation

- `docs/architecture.md`: add a "Deployment topologies" subsection — combined
  (default), headless + external static host, headless + third-party app clients.
- `docs/ops/` (pick the existing deployment/ops guide): document `ATLAS_SERVE_UI`,
  `ATLAS_CORS_ORIGINS`, `scripts/serve_ui.py` dev workflow, and a **security warning**:
  when running behind a same-host reverse proxy, `ATLAS_LOOPBACK_NO_AUTH` must be
  off, because every proxied request appears to come from 127.0.0.1 and would be
  granted admin.
- `docs/specs/threat-model.md`: add the new assumption (external UI origin trusts
  Atlas over Bearer tokens; CORS allowlist is the browser-side boundary) if the
  threat model tracks CORS at all — otherwise skip.
- No `/api/*` surface change ⇒ `openapi.yaml` and api-reference EN/TH untouched.
  If any doc touched has an EN/TH pair, update both.
- `PROGRESS.md`: one entry for this milestone.

## Execution order & verification

1. Branch off `main`.
2. Phase 1 → Phase 2 → Phase 3 → Phase 4 (each compiles/runs standalone).
3. Phase 5 check written, wired into `gate.sh`, mutation-tested.
4. Phase 6 integration guide (EN + TH) → Phase 7 remaining docs.
5. `./scripts/gate.sh` and `./scripts/lint.sh` green (Python 3.11+, node available).
6. Manual smoke of both dev modes:
   - `python -m atlas` → dashboard works at `http://127.0.0.1:8787` as before.
   - `ATLAS_SERVE_UI=0 python -m atlas` + `python scripts/serve_ui.py --api-base
     http://127.0.0.1:8787` → dashboard at `http://127.0.0.1:8000` fully functional:
     login, workers list, submit job, live SSE events, artifact download, usage CSV.
7. PR with summary referencing this plan.

## Out of scope

- Moving `atlas/static/` to a separate repo (later, if ever — the split flag makes it possible).
- Tightening the SSE `?token=` query fallback (keep for EventSource compatibility).
- Any new `/api/*` endpoints, auth changes, or pooled tenancy.
