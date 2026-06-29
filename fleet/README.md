# Atlas Fleet

> TL;DR (ไทย): Fleet คือเครื่องมือฝั่ง operator สำหรับดูแล Atlas หลาย instance (แบบ
> silo หนึ่ง instance ต่อหนึ่ง tenant). มี registry เป็น SQLite ของตัวเอง (แยกจาก DB
> ของ tenant อย่างสิ้นเชิง) และ CLI `atlas-fleet`: `provision` (สร้าง+seed token+ลงทะเบียน),
> `list`, `health` (poll `/healthz`), `usage-pull` (ดึง `GET /api/usage`). Atlas core
> ไม่รู้จัก Fleet และไม่มี tenant logic — silo invariant ยังอยู่ครบ.

Fleet is a **separate component** from Atlas core. It owns a small SQLite registry of
instances and talks to them over HTTP. It adds **no** tenant logic to `atlas/` and shares
no database with any tenant — consistent with the instance-per-tenant silo decision.

## Registry

`instances` (default `data/fleet.sqlite`, override with `ATLAS_FLEET_DB` or `--registry`):

| Column | Meaning |
|---|---|
| `id` | `inst_…` registry id |
| `tenant` | tenant the instance serves |
| `base_url` | instance HTTP base URL |
| `region` | deployment region/label |
| `version` | Atlas version reported by `/healthz` |
| `admin_token_ref` | **handle** to the admin token — never the raw token |
| `status` | `online` / `offline` / `unknown` |
| `last_health_at` | last successful/attempted health poll |
| `created_at` | registration time |

The raw admin token is **never** stored in the registry, logs, or any output. It lives in
a sidecar `fleet-secrets.json` (chmod `0600`) next to the registry, keyed by
`admin_token_ref`; `health` and `usage-pull` look it up there to authenticate.

## CLI

```bash
python3 -m fleet provision --tenant acme        # provision + register a local instance
python3 -m fleet list                            # show instances + status/version
python3 -m fleet health                          # poll /healthz, update status/last_health_at
python3 -m fleet usage-pull --from 2026-06-01 --to 2026-07-01   # raw usage events per instance
python3 -m fleet cdr --from 2026-06-01 --to 2026-06-30 --out-dir ./cdr   # per-tenant CDR CSVs
python3 -m fleet --registry /srv/fleet.sqlite list              # custom registry path
```

### Provisioning targets

`provision` deploys an instance, runs its migrations, seeds an admin token, and registers
it. Two targets:

- **`--target local`** (default) — start the instance as a local subprocess
  (`python3 -m atlas`) with its own data dir/DB. Used for dev and the hermetic check.
  Auth stays on; the seeded token is stored by reference.
- **`--target compose`** — print a **docker-compose IaC stub** (not a bespoke
  orchestrator). The operator fills in the image/volumes, runs `docker compose up -d`,
  then seeds an admin token on the box with `python3 -m atlas.admin create-admin` and
  registers the resulting `base_url`. **systemd** is the alternative target
  ([docs/ops/atlas.service](../docs/ops/atlas.service)); GDCC/k8s are noted as alternates
  in the GA plan's external-decision register.

## Health

`health` polls each instance's unauthenticated `GET /healthz` (additive endpoint:
`{ok, service, version}`, leaks nothing) and records `status` + `version` +
`last_health_at`. A non-200/unreachable instance is marked `offline`.

## Usage pull

`usage-pull` calls each instance's `GET /api/usage` (authenticated with the instance's
seeded token), optionally bounded by `--from`/`--to`, and prints the **raw** usage events
per instance.

## CDR export

`cdr` pulls usage from every instance, aggregates per tenant per period, and writes one
`cdr-<tenant>.csv` per tenant under `--out-dir`. Monthly vs annual is just the
`--from`/`--to` range; re-exporting a period is byte-identical (deterministic). This is
**export only** — no rating, no invoices (NT bills from the CDR). The record schema is
proposed and pending NT confirmation — see
[docs/specs/cdr-schema.md](../docs/specs/cdr-schema.md).

## Check

`python3 fleet/check_fleet.py` (in the completion gate) spins a throwaway instance and
asserts: provision → register → health green → usage-pull returns the instance's events →
offline detection after shutdown, and that the raw token never appears in the registry row.
