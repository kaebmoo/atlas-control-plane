# Solution Pack Format (`atlas.pack.v1`)

> TL;DR (ไทย): "Pack" คือไฟล์ JSON ก้อนเดียวที่บรรจุ workflow definition + trigger +
> ข้อมูลตัวอย่าง ไว้ติดตั้งซ้ำได้ (เช่น ชุดงานรับเรื่องร้องเรียนของภาครัฐ). นำเข้าได้ที่
> `POST /api/packs/import` (สร้าง definition/trigger จริงโดย validate graph เหมือนปกติ)
> และ export กลับเป็น bundle ที่ `GET /api/packs/{workflow_id}/export`. โครงสร้างปัจจุบัน
> คือ `schema_version: 1`.

A **solution pack** is a single, versioned, signable JSON bundle that packages one or
more workflow definitions, their triggers, the involved RBAC roles, a sample input, and
docs — so a complete solution (e.g. the government complaint flow) can be imported into
any Atlas instance.

Packs ship under `atlas/packs/*.json`. The reference pack is
[`atlas/packs/gov_complaint.json`](../../atlas/packs/gov_complaint.json).

## Bundle schema

```jsonc
{
  "schema_version": 1,            // bundle format version (this doc); required, must be 1
  "name": "gov_complaint",        // pack id/name; required, non-empty
  "version": "1.0.0",             // the pack's own version; required string
  "description": "…",             // optional
  "docs": "markdown…",            // optional human docs
  "roles": ["operator", "auditor"], // optional; each MUST be a known RBAC role
  "sample_input": { … },          // optional example run input
  "workflows": [                  // required, non-empty
    {
      "name": "…",                // required
      "description": "…",         // optional
      "status": "active",         // optional (default active)
      "graph": { … },             // required; validated by the workflow graph validator
      "policy": { … }             // optional; same shape as a workflow definition policy
    }
  ],
  "triggers": [                   // optional
    {
      "workflow": 0,              // index into workflows[] (default 0)
      "name": "…",
      "type": "manual",           // any supported trigger type
      "config": { },
      "enabled": true             // optional (default true); preserved on export
    }
  ],
  "signature": {                  // optional; HMAC signature (see Signing below)
    "algorithm": "HMAC-SHA256",
    "value": "…"
  }
}
```

### Validation rules

A bundle is rejected (`400`, clear error message) unless **all** hold:

- `schema_version == 1`.
- `name` is a non-empty string; `version` is a non-empty string.
- `workflows` is a non-empty list; every workflow has a `name` and a `graph` that
  passes the engine's `validate_workflow_graph` (node types, edges, conditions, joins,
  cycles-need-a-guard — exactly the same rules as `POST /api/workflows`).
- every entry in `roles` is one of `admin`, `operator`, `viewer`, `auditor`.
- every trigger's `workflow` index points at an existing workflow and its `type` passes
  `validate_workflow_trigger_payload`.

Validation never bypasses the real engine validators (graph **and** policy caps), so a
pack can only create workflows that the workflow API would otherwise accept.

On import, a `schedule` trigger's first `next_fire_at` is computed exactly as the
trigger API does, so imported schedules become due; `enabled: false` is honored (a
disabled trigger imports disabled and exports disabled).

## Endpoints (additive)

| Method | Path | Permission | Purpose |
|---|---|---|---|
| `GET` | `/api/packs` | `read` | List available packs (summaries; invalid bundles carry an `error`). |
| `POST` | `/api/packs/import` | `workflows.manage` | Validate a bundle, then create its definitions + triggers. Returns the created `workflows` and `triggers`. |
| `GET` | `/api/packs/{workflow_id}/export` | `read` | Serialize one workflow definition (and its triggers) back into a bundle. |

Import reuses the existing writers `create_workflow_definition` and
`create_workflow_trigger`; roles map only to existing RBAC roles. See
[openapi.yaml](openapi.yaml) for full request/response schemas.

## Reference pack: `gov_complaint`

Citizen complaint handling:

```
intake (trigger) → triage (worker) → draft (worker) → review (human gate) → publish (worker)
```

The `Citizen complaint intake` trigger fires a run with the complaint as input;
`triage` classifies it; `draft` writes an official response; the `review` human gate
offers **approve** / **reject**; on **approve** the `publish` worker releases the
response; **reject** ends the run without publishing (the draft remains for revision).

## Signing & verification

Packs can be signed with **HMAC-SHA256** using `ATLAS_SECRET_KEY` (the same approach as
signed usage exports). The signature covers the canonical bundle with the `signature`
field excluded.

```bash
ATLAS_SECRET_KEY=… python3 -m atlas.packs sign pack.json --output pack.signed.json
ATLAS_SECRET_KEY=… python3 -m atlas.packs verify pack.signed.json
```

**Import policy** (`POST /api/packs/import`, which uses the server's `ATLAS_SECRET_KEY`):

- A bundle that **carries a signature** must verify — a tampered or wrong-key signed
  pack is rejected (`pack signature is invalid`). A signed pack also fails if the server
  has no `ATLAS_SECRET_KEY` to verify against.
- An **unsigned** bundle is accepted (the shipped `gov_complaint` pack is unsigned),
  unless the caller sets `require_signature` (then `pack is unsigned but a signature is
  required`).

`GET /api/packs` reports a `signed` boolean per pack (whether a signature is present).

## Future marketplace (readiness, not built in core)

A public, hosted pack **marketplace** (a signed registry of community packs with
discovery and ratings) is intentionally **not** in Atlas core. It would live as a
Fleet-side service: a catalog API serving signed bundles, with Atlas verifying each
bundle's signature on import exactly as above (the trust mechanism already exists). The
extension path: stand up the registry service, publish signed bundles, and point
operators at it; Atlas core needs no change because import already validates and
verifies. Ratings/curation are a registry-service concern, never Atlas core.
