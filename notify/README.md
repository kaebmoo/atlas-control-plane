# Atlas Notify

> **TL;DR (ไทย):** receiver ที่ใช้งานจริงได้สำหรับ delivery `approval_overdue` ของ Atlas —
> ตรวจลายเซ็นจาก raw bytes, dedup ถาวรด้วย SQLite (restart แล้วไม่เตือนซ้ำ), ส่งอีเมล (SMTP)
> และ Telegram, เลือกผู้รับจากไฟล์ config JSON ตาม `node_key` + `level` รันข้าง silo โดย NT
> (managed) หรือลูกค้ารันเองในเครือข่ายตัวเอง (BYO) — artifact เดียวกันทั้งสองแบบ
> secret ทุกตัวอยู่ใน environment ไม่อยู่ในไฟล์ config

The operational half of Atlas approval reminders. Atlas states a fact — "this approval is
130 hours old, level 2" — and POSTs it, signed, to one URL
([contract v1](../docs/specs/api-reference-en.md), §14). This sidecar turns that fact into
a message a human sees. It is deliberately **not** a notification platform: one process,
one JSON config, two channels, no Atlas credential, no core changes.

The didactic contract reference (single file, heavily commented, in-memory) stays at
[`poc/approval_reminder_receiver.py`](../poc/approval_reminder_receiver.py) — start there
to understand the wire rules; run *this* in production.

## Run

```bash
export ATLAS_SECRET_KEY="the same value Atlas signs with"
python3 -m notify --config /etc/atlas/notify.json --port 9100
```

Atlas side: set `ATLAS_APPROVAL_WEBHOOK_URL` (or per-workflow
`policy.approval_webhook_url`) to `http://<host>:9100/` and allowlist the host in
`ATLAS_OUTBOUND_ALLOWLIST`. Verify with
`POST /api/workflows/{id}/test-approval-webhook` — the probe carries `test: true` and this
receiver logs it without paging anyone.

## Config

```json
{
  "channels": {
    "smtp":     {"host": "smtp.example.co.th", "port": 587, "starttls": true,
                 "from": "atlas-notify@example.co.th", "username": "atlas-notify"},
    "telegram": {"api_base": "https://api.telegram.org"}
  },
  "routes": {
    "dept_head_approval": {
      "1": [{"channel": "smtp", "to": "somchai@example.co.th"}],
      "2": [{"channel": "smtp", "to": "director@example.co.th"},
            {"channel": "telegram", "chat_id": "-100123456"}]
    }
  },
  "fallback": [{"channel": "smtp", "to": "ops@example.co.th"}]
}
```

- Levels are the workflow's escalation ladder positions (1-based). A delivery at a level
  beyond the last configured one notifies the **last configured** level; an unrouted
  `node_key` goes to `fallback`.
- Validation is fail-closed at startup — a broken route dies at boot, not at 03:00 when
  the first escalation fires. A config containing any `password`/`token`/`secret` key is
  refused outright.

## Environment (all secrets live here)

| Variable | Required | Meaning |
|---|---|---|
| `ATLAS_SECRET_KEY` | always | Shared HMAC key — must equal the value Atlas signs with |
| `ATLAS_NOTIFY_SMTP_PASSWORD` | iff `channels.smtp.username` is set | SMTP auth password |
| `ATLAS_NOTIFY_TELEGRAM_TOKEN` | iff any telegram target exists | Bot token from @BotFather |
| `ATLAS_NOTIFY_CONFIG` | or `--config` | Path to the JSON config |
| `ATLAS_NOTIFY_DB` | no (default `data/notify.sqlite`) | Durable dedup + outcome store |

## What it records

Every handled delivery gets one row in the SQLite store: `delivery_id` (the dedup key —
stable across Atlas retries), the event, when it arrived, and the per-channel outcome
(`{"smtp:somchai@…": "sent", "telegram:-100…": "error: …"}`). One channel failing never
blocks another; the failure is in the outcomes and in the log.

Whether Atlas managed to *reach* this receiver at all is the other half of the story, and
it lives on the Atlas side: `GET /api/deliveries` (a `failed` `dlv_apr_*` row means a
human was supposed to be chased and was not).

## Checks

`notify/check_notify.py` runs in the gate — hermetic, loopback-only, with a fake SMTP
server and a Telegram stub. The mutation targets are listed in its docstring.
