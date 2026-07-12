# Atlas Backup & Restore

> TL;DR (ไทย): สำรองฐานข้อมูลด้วย `scripts/backup.sh` ซึ่งใช้ SQLite online
> `.backup` — ปลอดภัยแม้ Atlas กำลังรันอยู่ (ไม่ต้องหยุดเซิร์ฟเวอร์). คืนค่าโดย
> หยุด Atlas, วางไฟล์ snapshot ทับ `ATLAS_DB`, แตกไฟล์ `atlas-uploads-*.tar.gz`
> (ไฟล์แนบชนิด `file_ref` อยู่นอก SQLite) คืนลงโฟลเดอร์ uploads แล้วสตาร์ทใหม่ (schema จะ
> migrate เดินหน้าให้อัตโนมัติ). ข้อจำกัด: SQLite รองรับ **ผู้เขียนทีละรายเดียว** — รับได้
> ที่สเกล single-tenant; ทำ backup ตามรอบ (เช่น cron รายชั่วโมง/รายวัน).

Atlas stores all state in one SQLite file (`ATLAS_DB`, default
`./data/atlas.sqlite`) with WAL mode enabled.

## Backup

```bash
scripts/backup.sh                 # -> ./backups/atlas-<UTC timestamp>.sqlite
scripts/backup.sh /srv/atlas-bak  # custom destination directory
ATLAS_DB=/opt/atlas/data/atlas.sqlite scripts/backup.sh
```

`backup.sh` runs SQLite's online `.backup`, which copies a transactionally
consistent snapshot — including committed WAL pages — **while Atlas keeps running**.
You do not need to stop the server.

It also archives the **upload store** (`file_ref` artifact bytes live outside SQLite,
under `$ATLAS_UPLOAD_DIR`, default `<db-dir>/uploads/`) to
`atlas-uploads-<UTC timestamp>.tar.gz` next to the `.sqlite` snapshot, taken **after** the
DB snapshot so every artifact row in the snapshot has its file present on restore. If the
upload dir does not exist yet, no tarball is written. Both files share the same timestamp —
restore them as a pair.

Schedule it from cron (hourly example):

```cron
0 * * * * cd /opt/atlas && ATLAS_DB=/opt/atlas/data/atlas.sqlite scripts/backup.sh /srv/atlas-bak >> /var/log/atlas-backup.log 2>&1
```

Retention is your call — the script never deletes old snapshots. Add a `find ... -mtime +N -delete` to the cron line if you want pruning.

### Encrypted backups

Set `ATLAS_BACKUP_KEY` to encrypt both files at rest (AES-256-CBC via `openssl`,
PBKDF2 KDF). The plaintext copies are removed after successful encryption, and the
key is passed via the environment — never argv — so it does not leak in `ps`:

```bash
ATLAS_BACKUP_KEY="$(cat /etc/atlas/backup.key)" scripts/backup.sh /srv/atlas-bak
# -> atlas-<ts>.sqlite.enc, atlas-uploads-<ts>.tar.gz.enc
```

Decrypt before restoring:

```bash
ATLAS_BACKUP_KEY="$(cat /etc/atlas/backup.key)" \
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:ATLAS_BACKUP_KEY \
  -in atlas-20260629T120000Z.sqlite.enc -out atlas-20260629T120000Z.sqlite
```

Store the key outside the backup destination (secret store / offline), or the
encryption buys nothing.

## Restore

1. **Stop Atlas** (`systemctl stop atlas`) so nothing is writing.
2. Move the current DB aside (keep it until the restore is verified):
   ```bash
   mv /opt/atlas/data/atlas.sqlite /opt/atlas/data/atlas.sqlite.bak
   # WAL/SHM sidecars are regenerated; remove any stale ones:
   rm -f /opt/atlas/data/atlas.sqlite-wal /opt/atlas/data/atlas.sqlite-shm
   ```
3. Copy the snapshot into place:
   ```bash
   cp /srv/atlas-bak/atlas-20260629T120000Z.sqlite /opt/atlas/data/atlas.sqlite
   ```
4. **Restore the upload store** from the matching tarball (same timestamp), so
   `file_ref` artifacts referenced by the DB are present and downloads don't 404.
   The tarball contains the `uploads/` directory, so extract into the upload dir's parent:
   ```bash
   # default upload dir is <db-dir>/uploads — restore it alongside the DB
   rm -rf /opt/atlas/data/uploads
   tar -xzf /srv/atlas-bak/atlas-uploads-20260629T120000Z.tar.gz -C /opt/atlas/data
   ```
   Skip this step only if the snapshot has no `file_ref` artifacts (no tarball was produced).
5. **Start Atlas** (`systemctl start atlas`). On startup the migration runner brings
   the restored schema forward to the current version if the snapshot is older;
   restoring a snapshot at the same or newer version is a no-op. `ATLAS_SECRET_KEY`
   must be set to the same key that was active when the snapshot was taken — worker
   tokens are encrypted at rest keyed by it, and a mismatched (or missing) key makes
   every stored token undecryptable.
6. Verify (e.g. dashboard loads, `GET /api/usage` responds, `GET /api/workers`
   responds without error, an artifact download succeeds), then delete the `.bak`
   file. A `GET /api/workers` failure with a 400 about the stored worker token is
   the signature of a wrong or missing `ATLAS_SECRET_KEY`.

A snapshot is a normal SQLite file — you can also inspect it directly:
`sqlite3 atlas-<ts>.sqlite "SELECT MAX(version) FROM schema_version;"`.

## Single-writer caveat

SQLite permits **one writer at a time**. Atlas serializes writes behind a process
lock, which is fine at single-tenant / instance-per-tenant scale (the design target —
see the silo decision in the sovereign plan). Implications:

- Run **one** Atlas process per database file. Do not point two instances at the
  same `ATLAS_DB`.
- Backups are non-blocking for readers and the single writer, so scheduled
  `.backup` runs are safe under normal load.
- High-availability (multi-writer / replicated) is out of scope for GA; the GA
  posture is a single VM plus scheduled `.backup`. HA is noted as a later phase in
  the GA plan's external-decision register.
