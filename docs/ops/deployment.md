# Atlas Deployment (Production)

> TL;DR (ไทย): รัน Atlas บน production ด้วย `scripts/run-prod.sh` (บังคับตั้ง
> `ATLAS_SECRET_KEY`, ปิด loopback bypass, เปิด request log). Atlas ฟังที่ loopback
> แล้ววาง **reverse proxy** (nginx/Caddy) ไว้หน้าเพื่อทำ TLS, gzip และจำกัดขนาด
> request. ใช้ `docs/ops/atlas.service` เป็นตัวอย่าง systemd unit. ค่า default
> ปลอดภัยอยู่แล้ว: ต้องมี token (ไม่มี bypass) เว้นแต่จะตั้ง `ATLAS_LOOPBACK_NO_AUTH=true`
> สำหรับ dev บนเครื่อง local เท่านั้น.

Atlas core is Python standard library only — no app server, no build step. In
production it runs as a long-lived process behind a reverse proxy that terminates
TLS.

## 1. Run it

```bash
export ATLAS_SECRET_KEY="$(openssl rand -hex 32)"   # HMAC for tokens/usage/pack signing
scripts/run-prod.sh
```

`run-prod.sh` enforces a secure posture:

- **`ATLAS_SECRET_KEY` is required** — the launcher aborts if it is unset.
- **`ATLAS_LOOPBACK_NO_AUTH=false`** — no auth bypass; every `/api/*` call needs a
  token. (The dev convenience bypass on `127.0.0.1`/`::1` only activates when this is
  explicitly `true`.)
- **`ATLAS_REQUEST_LOG=true`** — structured request logging on (see §3).
- Binds to `127.0.0.1` by default. Set `ATLAS_HOST=0.0.0.0` **only** when a proxy or
  firewall sits in front.

Seed an admin token before first use:

```bash
python3 -m atlas.admin create-admin admin   # prints a one-time API token
```

### User & token management

Day-2 user/token lifecycle is CLI-only (no dashboard signup flow beyond the Accounts
screen for already-authenticated admins):

```bash
python3 -m atlas.admin create-user alice --role operator   # prompts for a password; no token
python3 -m atlas.admin create-token alice                  # prints a one-time API token for an existing user
python3 -m atlas.admin revoke-token <token_id>
python3 -m atlas.admin list-users
```

### systemd

Copy [`atlas.service`](atlas.service) to `/etc/systemd/system/`, adjust the user and
paths, put secrets in a `0600` `EnvironmentFile` (e.g. `/etc/atlas/atlas.env`), then:

```bash
systemctl daemon-reload
systemctl enable --now atlas
```

## 2. Reverse proxy (TLS, gzip, request size, and SSE)

Atlas does not terminate TLS or compress responses itself. Front it with nginx or
Caddy:

- **TLS** — terminate HTTPS at the proxy; proxy to `http://127.0.0.1:8787`.
- **gzip** — enable on the proxy for JSON/HTML/JS responses.
- **Request-size limit** — cap the body at the proxy (e.g. nginx
  `client_max_body_size 12m`) to back up Atlas's own `ATLAS_MAX_UPLOAD_BYTES`
  (default 10 MiB). Keep the proxy limit slightly above the app limit so Atlas
  returns its own clean error instead of the proxy cutting the connection.
- **SSE** — disable proxy buffering for every `*/events` stream and keep its idle
  timeout above 45 seconds. Buffering makes timelines look frozen even when Atlas
  is sending events; a too-short idle timeout turns normal waiting into misleading
  client reconnects.

Minimal nginx example:

```nginx
server {
  listen 443 ssl;
  server_name atlas.example.gov;
  ssl_certificate     /etc/ssl/atlas/fullchain.pem;
  ssl_certificate_key /etc/ssl/atlas/privkey.pem;
  client_max_body_size 12m;
  gzip on;
  gzip_types application/json text/html application/javascript text/css;
  location / {
    proxy_pass http://127.0.0.1:8787;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
  }
  location ~ /events$ {
    proxy_pass http://127.0.0.1:8787;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_set_header X-Accel-Buffering no;
    proxy_read_timeout 60s;
    proxy_set_header Host $host;
  }
}
```

**Security warning — loopback bypass behind a reverse proxy.** `ATLAS_LOOPBACK_NO_AUTH`
grants the built-in admin identity to any request whose *source address* is
`127.0.0.1`/`::1`; it does not verify who is actually behind the connection.
A same-host reverse proxy makes **every** proxied request appear to
originate from `127.0.0.1`, so it would silently grant admin to anyone the
proxy accepts a connection from. Keep `ATLAS_LOOPBACK_NO_AUTH=false` (the
default, and what `scripts/run-prod.sh` enforces) whenever a reverse proxy —
or anything else — sits in front of Atlas.

Atlas uses the direct TCP peer IP for its pre-password login limit and deliberately
does **not** trust `X-Forwarded-For`; configure a separate public-edge rate limit
at the proxy/WAF. The Atlas limiter is a bounded in-memory second layer and resets
when the process restarts.

**Headless / split UI.** Set `ATLAS_SERVE_UI=false` to run Atlas as an
API-only server and host the dashboard (or a custom UI) separately — see
[API Integration Guide](../guides/api-integration-guide-en.md) for the full
walkthrough and `scripts/serve_ui.py` for the local dev static server used
while iterating on `atlas/static/` against a headless instance.

**Separate Flow Designer deployment handoff.** The following are deployment-platform
inputs for the UI repository, **not** Atlas environment variables and not values to
commit here: `PUBLIC_ORIGIN`, `ATLAS_API_ORIGIN`, `SESSION_SECRET`, and Node.js 24.
Set the first two to the public UI/API origins, store `SESSION_SECRET` only in the
platform secret manager, and use Node 24 in that UI build/runtime. Atlas's side of
the handoff is its TLS proxy, correct CORS allowlist, SSE configuration above, DB/
upload/key persistence, and a tested backup-and-restore procedure.

## 3. Request logging

Set `ATLAS_REQUEST_LOG=true` (default in `run-prod.sh`) to emit one JSON line per
request to **stderr**. Response bodies and shapes are unaffected.

```json
{"ts":"2026-06-29T12:00:00Z","method":"GET","path":"/api/usage","status":200,"client":"127.0.0.1","dur_ms":3.1}
```

Pipe stderr to your log collector (systemd captures it in the journal by default).
Leave it off (`false`) to stay completely silent, as in dev.

Configure the log sink to exclude HTTP request/response bodies, `Authorization`
headers, and query strings. Atlas itself logs only the path (not its query string)
because EventSource authentication can place a bearer token in `?token=`.

## 4. Configuration reference (production-relevant)

| Env var | Default | Notes |
|---|---|---|
| `ATLAS_SECRET_KEY` | — | **Required in prod.** HMAC key for token/usage/pack signing. |
| `ATLAS_LOOPBACK_NO_AUTH` | `false` | Dev-only auth bypass on loopback. Keep `false` in prod. |
| `ATLAS_REQUEST_LOG` | `false` | JSON request log to stderr. |
| `ATLAS_HOST` | `127.0.0.1` | `0.0.0.0` only behind a proxy/firewall. |
| `ATLAS_PORT` | `8787` | Upstream port for the proxy. |
| `ATLAS_SERVE_UI` | `true` | `false` runs Atlas headless — `GET /` and `GET /static/*` return 404 JSON; `/healthz` and `/api/*` are unaffected. Use when the dashboard is hosted elsewhere (see [API Integration Guide](../guides/api-integration-guide-en.md)). |
| `ATLAS_CORS_ORIGINS` | unset (`*`) | Comma-separated allowlist of browser origins allowed to call `/api/*` cross-origin. Unset keeps today's `Access-Control-Allow-Origin: *`; set it when serving the dashboard (or a custom UI) from a different origin than the API. |
| `ATLAS_SESSION_TOKEN_TTL_SECONDS` | `28800` | Dashboard-login session lifetime. Must be positive; default is 8 hours. |
| `ATLAS_MAX_ACTIVE_SESSIONS` | `5` | Maximum unexpired dashboard sessions per user. A new login revokes only the oldest excess session. |
| `ATLAS_LOGIN_RATE_LIMIT_ATTEMPTS` | `5` | Failed login attempts permitted in the rolling in-memory window per normalized username + direct peer IP. |
| `ATLAS_LOGIN_RATE_LIMIT_WINDOW_SECONDS` | `60` | Rolling login-attempt window. Process restart clears this in-memory state. |
| `ATLAS_LOGIN_RATE_LIMIT_COOLDOWN_SECONDS` | `60` | `429` cooldown after the failed-attempt threshold; clients must obey `Retry-After`. |
| `ATLAS_DB` | `./data/atlas.sqlite` | SQLite path; WAL mode is enabled automatically. |
| `ATLAS_MAX_UPLOAD_BYTES` | `10485760` | Upload cap; mirror at the proxy. |
| `ATLAS_REQUEST_TIMEOUT` | `30` | Worker request timeout (seconds, per recv). |
| `ATLAS_MAX_STREAM_SECONDS` | `3600` | Overall wall-clock bound on a single worker stream; a slow/dripping worker is cut at this deadline. |
| `ATLAS_MAX_JOB_OUTPUT_BYTES` | `16777216` | Cap on a single job's accumulated assistant output. |
| `ATLAS_ARTIFACT_MAX_BYTES` | `314572800` (300 MiB) | Cap on total bytes of artifacts collected from a worker after a job/workflow node finishes. Clamped to the pinned thClaws upstream limit (300 MiB); a higher value is silently capped there. |
| `ATLAS_ARTIFACT_MAX_FILES` | `256` | Cap on the number of artifact files collected from a worker per job/workflow node. Clamped to the pinned thClaws upstream limit (256 files). |
| `ATLAS_PUBLIC_BASE_URL` | — | Externally reachable Atlas base URL (e.g. `https://atlas.example.com`) that thClaws workers deliver `execution: "callback"` results to. Unset ⇒ async jobs are rejected at submit with 400; stream jobs are unaffected. |
| `ATLAS_CALLBACK_TIMEOUT_SECONDS` | `3600` | Deadline for an `execution: "callback"` job to deliver its terminal callback. The callback token — and the reaper's grace — extend ~5 min (the worker's retry envelope) past it, so the reaper fails the job only after the deadline plus that grace, never cutting off a still-valid retry. |
| `ATLAS_REQUIRE_SIGNED_PACKS` | `false` | **Set `true` in production (SHALL).** When `true`, `POST /api/packs/import` rejects unsigned packs. Running prod with `false` is an accepted risk owned by Pornthep Nivatyakul (see `specs/threat-model.md`). |
| `ATLAS_BACKUP_KEY` | — | Optional. When set, `scripts/backup.sh` writes AES-256-CBC `.enc` backups and removes the plaintext copies (see [backup-restore.md](backup-restore.md)). |

## 5. Schema migrations on deploy

The database self-migrates on startup. `Database.init()` runs an ordered, idempotent
migration runner (a `schema_version` table records applied steps), so deploying a
newer Atlas over an existing DB upgrades the schema forward automatically and
re-running is a no-op. Back up first — see [backup-restore.md](backup-restore.md).

## 6. thClaws worker connectivity

Atlas authenticates `/agent/run` and `/v1/*` with each worker's
`THCLAWS_API_TOKEN`. That Bearer token does **not** protect
`/workspace/sync/*` on a plain single-tenant `thclaws --serve` listener.

Keep worker sync disabled in Atlas unless the worker is reached through one of
these approved shapes:

- a private/SSH tunnel that is not reachable by untrusted network clients; or
- an ingress that enforces ForwardAuth before forwarding sync routes.

Binding plain `thclaws --serve` to `0.0.0.0` with only
`THCLAWS_API_TOKEN` is not sufficient protection for sync. Firewalling the port
is useful defense in depth but is not a substitute for an asserted `tunnel` or
`forward_auth` deployment shape. See the
[thClaws worker protocol contract](../specs/thclaws-worker-contract.md) for the
tested endpoint/auth matrix and capability-gating rules.

For a thClaws `--multiuser` worker, the outer HMAC identity middleware applies
to the complete worker surface (except `/healthz`) in addition to the
`THCLAWS_API_TOKEN` Bearer checks on `/agent/run` and `/v1/*`. Do not register a
multiuser worker unless the Atlas-to-worker path supplies that deployment
identity.

## 7. Data retention

Atlas never deletes data on its own. Retention is operator-driven:

- **Artifacts** (including `file_ref` bytes on disk): purge artifacts of
  *terminal* runs older than N days with
  `python3 -m atlas.admin purge-artifacts --older-than-days N` (add `--dry-run`
  to preview). Artifacts of live runs are never touched, and every purge is
  audited as `artifact.purge`. Schedule from cron alongside the backup job.
- **Backups**: prune with `find ... -mtime +N -delete` in the backup cron
  (see [backup-restore.md](backup-restore.md)).
- **Audit log / usage ledger**: append-only by design; export with
  `GET /api/audit?format=csv` / `GET /api/usage?format=csv` before any manual
  archive decision.
- Artifacts may carry a `classification` tag
  (`public`/`internal`/`confidential`/`secret`, validated on create) so purge
  and hand-off policy can key off data classification.
