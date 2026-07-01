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

### systemd

Copy [`atlas.service`](atlas.service) to `/etc/systemd/system/`, adjust the user and
paths, put secrets in a `0600` `EnvironmentFile` (e.g. `/etc/atlas/atlas.env`), then:

```bash
systemctl daemon-reload
systemctl enable --now atlas
```

## 2. Reverse proxy (TLS, gzip, request size)

Atlas does not terminate TLS or compress responses itself. Front it with nginx or
Caddy:

- **TLS** — terminate HTTPS at the proxy; proxy to `http://127.0.0.1:8787`.
- **gzip** — enable on the proxy for JSON/HTML/JS responses.
- **Request-size limit** — cap the body at the proxy (e.g. nginx
  `client_max_body_size 12m`) to back up Atlas's own `ATLAS_MAX_UPLOAD_BYTES`
  (default 10 MiB). Keep the proxy limit slightly above the app limit so Atlas
  returns its own clean error instead of the proxy cutting the connection.

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
}
```

## 3. Request logging

Set `ATLAS_REQUEST_LOG=true` (default in `run-prod.sh`) to emit one JSON line per
request to **stderr**. Response bodies and shapes are unaffected.

```json
{"ts":"2026-06-29T12:00:00Z","method":"GET","path":"/api/usage","status":200,"client":"127.0.0.1","dur_ms":3.1}
```

Pipe stderr to your log collector (systemd captures it in the journal by default).
Leave it off (`false`) to stay completely silent, as in dev.

## 4. Configuration reference (production-relevant)

| Env var | Default | Notes |
|---|---|---|
| `ATLAS_SECRET_KEY` | — | **Required in prod.** HMAC key for token/usage/pack signing. |
| `ATLAS_LOOPBACK_NO_AUTH` | `false` | Dev-only auth bypass on loopback. Keep `false` in prod. |
| `ATLAS_REQUEST_LOG` | `false` | JSON request log to stderr. |
| `ATLAS_HOST` | `127.0.0.1` | `0.0.0.0` only behind a proxy/firewall. |
| `ATLAS_PORT` | `8787` | Upstream port for the proxy. |
| `ATLAS_DB` | `./data/atlas.sqlite` | SQLite path; WAL mode is enabled automatically. |
| `ATLAS_MAX_UPLOAD_BYTES` | `10485760` | Upload cap; mirror at the proxy. |
| `ATLAS_REQUEST_TIMEOUT` | `30` | Worker request timeout (seconds, per recv). |
| `ATLAS_MAX_STREAM_SECONDS` | `3600` | Overall wall-clock bound on a single worker stream; a slow/dripping worker is cut at this deadline. |
| `ATLAS_MAX_JOB_OUTPUT_BYTES` | `16777216` | Cap on a single job's accumulated assistant output. |
| `ATLAS_REQUIRE_SIGNED_PACKS` | `false` | **Set `true` in production (SHALL).** When `true`, `POST /api/packs/import` rejects unsigned packs. Running prod with `false` is an accepted risk owned by Pornthep Nivatyakul (see `specs/threat-model.md`). |
| `ATLAS_BACKUP_KEY` | — | Optional. When set, `scripts/backup.sh` writes AES-256-CBC `.enc` backups and removes the plaintext copies (see [backup-restore.md](backup-restore.md)). |

## 5. Schema migrations on deploy

The database self-migrates on startup. `Database.init()` runs an ordered, idempotent
migration runner (a `schema_version` table records applied steps), so deploying a
newer Atlas over an existing DB upgrades the schema forward automatically and
re-running is a no-op. Back up first — see [backup-restore.md](backup-restore.md).

## 6. Data retention

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
