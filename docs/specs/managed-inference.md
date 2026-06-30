# Managed Inference Gateway (M7 / B7) — Readiness

> TL;DR (ไทย): "Managed inference" (NT ให้บริการ inference หลาย provider เอง) ออกแบบให้
> อยู่ใน **เลเยอร์ worker/gateway ไม่ใช่ Atlas core** — Atlas core ไม่ต้องแก้โค้ด. Gateway
> เป็น worker ตัวหนึ่ง (ใช้ worker abstraction เดิม) ที่หลัง endpoint เดียวเชื่อมหลาย
> provider และ "วัด" token / GPU-hour แล้ว emit เป็น usage event ชนิดใหม่ ซึ่งไหลเข้าสู่
> ท่อ usage→CDR เดิมเป็น **แถว CDR เพิ่ม** (NT เป็นผู้ตั้งราคา/ออกบิล). เอกสารนี้คือแบบ
> พร้อมสร้าง ยังไม่ลงมือในรอบนี้.

This is a **design + interface doc**, not Atlas-core code. Per the sovereign plan (M7),
managed multi-provider inference lives in the **worker/gateway layer**; Atlas core needs
no change.

## Gateway worker

The inference gateway is just another **worker** behind Atlas's existing worker
abstraction (registered in the `workers` table, polled at `/healthz`, invoked like any
thClaws worker). Behind its single endpoint it fans out to multiple providers
(OpenAI/Anthropic/Azure/self-hosted GPU), selecting by model/role/policy. Atlas routes
to it exactly as it routes to any worker today — no new core concept.

- Keys: provider keys are injected into the gateway worker via
  [byok-key-injection.md](byok-key-injection.md); Atlas core still stores none.
- Auth/RBAC: unchanged — the gateway is a worker; existing worker tokens apply.

## Token / GPU-hour metering interface

The gateway **measures** usage and emits it through the **existing** usage-event ledger,
so it flows into the same usage→CDR pipeline as **extra CDR rows** (new `event_type`s):

| Proposed `event_type` | `units` | `seconds` | Meaning |
|---|---|---|---|
| `inference_tokens` | total tokens (prompt+output) | — | per-request token usage |
| `gpu_seconds` | — | GPU wall seconds | self-hosted GPU time |

These reuse the `usage_events` shape already in Atlas (`kind`, `units`, `seconds`,
`model`, `tokens_prompt`, `tokens_output`, `metadata`). `inference_tokens` and `gpu_seconds`
are the **canonical** `kind` values — they are listed in the `UsageEvent` schema's `kind`
enum in [openapi.yaml](openapi.yaml), so a consumer validating `/api/usage` against the spec
accepts gateway rows. Because [fleet/cdr.py](../../fleet/README.md) aggregates per
`event_type`, the gateway's events appear as additional CDR rows automatically — **no CDR or
Atlas-core change required**. NT rates and bills these rows; Atlas/Fleet only export them
(see [cdr-schema.md](cdr-schema.md)).

> **CDR column note.** The CDR aggregator sums each group's `units` into a column named
> `budget_units` regardless of `event_type`. For an `inference_tokens` group that column
> therefore carries the **token total** (the meter's `units`), and for `gpu_seconds` the time
> lives in the `seconds` column (`units` is empty). NT's rating step should read the value by
> `event_type`, not by the column name. The CDR schema is PROPOSED and may rename this column
> before NT sign-off — see [cdr-schema.md](cdr-schema.md).

**Per-request limits** (e.g. `max_tokens`) are applied at the **gateway/worker layer**, not
Atlas core. Atlas forwards the `model` (and prompt/session) to the worker; it does not
currently thread a per-node `max_tokens` from a workflow definition. Wiring a workflow-node
token cap through to the gateway is part of the B7 gateway work, not shipped in core today.

## Why readiness-only now

- **Blocked on the worker/gateway layer**, which is outside Atlas core and depends on the
  thClaws/worker roadmap (and, for option-a key handling, the thClaws save-key endpoint).
- Building it now would add a multi-provider service that the silo/worker layer should
  own, not the control plane. The seam already exists (worker abstraction + usage ledger
  + per-`event_type` CDR), so this is ready to execute when the gateway work is scheduled.

## What Atlas core would change: nothing

No new tables, no `/api/*` changes, no rating logic. The gateway emits standard usage
events; everything downstream already handles them.
