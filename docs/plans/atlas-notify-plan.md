# Atlas Notify — a deployable receiver for approval reminders

> **TL;DR (ไทย):** ยกระดับ `poc/approval_reminder_receiver.py` เป็น sidecar ที่ใช้งานจริงได้
> ชื่อ `notify/` — dedup ถาวรด้วย SQLite, ส่งอีเมล (SMTP) และ Telegram, routing จากไฟล์
> config JSON — **ไม่ใช่ notification platform**: artifact ตัวเดียวใช้ได้ทั้งแบบ NT ดูแลให้
> (managed, รันข้าง silo ของลูกค้า) และแบบลูกค้ารันเอง (BYO) ต่างกันแค่ใครเป็นคน operate
> Atlas core ไม่ต้องแก้อะไรเลย

**Status:** Implemented — contract v1 declared (PR #61); the `notify/` sidecar shipped in
the follow-up PR (`notify/check_notify.py` in the gate, 9 mutation targets in its
docstring). The table below stays the record of what was deliberately deferred.

## Why

Atlas states a fact ("this approval is 130 hours old, level 2") and POSTs it, signed, to
one URL. Turning that fact into a message a human sees — routing, org chart, channel
credentials — is deliberately outside the core (see
[ai-draft-contract-hardening-plan.md](ai-draft-contract-hardening-plan.md) §8). The POC
receiver demonstrates the contract but is not operable: in-memory dedup re-notifies after a
restart, and `notify()` is a placeholder. Real deployments need an artifact an operator can
run, whether that operator is NT (managed) or the customer (BYO). Same artifact, same
contract, two operating modes — never two products.

## Shape (one sidecar, fleet/-style package)

- `notify/` package mirroring `fleet/`: own SQLite store, own check
  (`notify/check_notify.py` in the gate), no Atlas credential, no core changes.
- Durable dedup: `INSERT OR IGNORE` on `delivery_id` in a single-table SQLite store that
  also records per-channel outcomes (forensics).
- Channels: **SMTP** (smtplib, stdlib) and **Telegram** (Bot API via urllib, stdlib).
- Routing: one JSON config file — `routes[node_key][level] → targets`, walk-down to the
  last configured level, global fallback. Secrets live only in env vars
  (`ATLAS_SECRET_KEY`, `ATLAS_NOTIFY_SMTP_PASSWORD`, `ATLAS_NOTIFY_TELEGRAM_TOKEN`),
  never in the config file.
- Honors the four receiver rules from the POC docstring: raw-byte HMAC verify, answer 2xx
  fast then notify, dedupe on `delivery_id`, receiver owns routing.
- Consumes `approval_overdue` **contract v1** (API reference §14): branch on `event`,
  ignore unrecognised events.

## Deferred (explicitly out of scope, not rejected)

| Item | Why deferred | Revisit trigger |
|---|---|---|
| LINE Messaging API channel | LINE Notify was discontinued (2025); the replacement needs an Official Account with per-push quotas/cost — a product decision, not a code one | A customer commits to LINE (OA + budget confirmed) |
| Calendar / business-day send timing | Atlas emits wall-clock `age_hours` by design; holiday calendars are receiver policy that no customer has specified yet | A paying customer states a business-day SLA |
| Routing-config UI | Hand-counted silos with high-touch onboarding; a JSON file per silo is enough | A second customer asks to self-serve routing edits |
| Per-channel rate limiting | Reminder volume is bounded by approvals × thresholds; nowhere near channel limits | A real deployment hits a provider limit |
| Other event types (run completion, quota alerts) | `approval_overdue` is the SLA-critical case; the channel already discriminates by `event` so reuse is additive later | The first customer request for another event on this path |
