"""Atlas Notify: a deployable receiver for approval_overdue deliveries.

Separate from Atlas core — own SQLite dedup store, no Atlas credential, no tenant
logic in `atlas/`. The didactic contract reference stays at
poc/approval_reminder_receiver.py; this package is the operational artifact
(durable dedup + SMTP + Telegram + JSON routing). See notify/README.md.
"""
