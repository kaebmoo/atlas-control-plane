# ADR 0002 — `workflow.interface` v1: an authoritative, versioned input/output contract

- **Status:** Accepted — v1 scope below. Full JSON Schema, UI form layout, and other
  items in "Deferred" are explicitly out of scope, not rejected.
- **Date:** 2026-07-31
- **Deciders:** Atlas platform; implements the companion plan
  `flow-designer` `docs/WORKFLOW_TEST_INTEGRATION_CONTRACT_PLAN.md` (flow-designer
  `ab61f5b`, Atlas `4b837cc`).

## Context and forces

Today a workflow definition's `graph`/`policy`/`default_reply` are authoritative and
validated, but the run `input` a caller must supply is undocumented and unvalidated:
Flow Designer's Milestone A Test Run dialog can only *infer* an "observed contract" by
walking `{input.*}` placeholders in prompts, and a caller integrating against Atlas has
no machine-checkable declaration of what a workflow expects or what it might produce.
This ADR adds that declaration as `workflow.interface`: optional, nullable, additive.

## Decision

### 1. `interface.schema_version === 1`

The only supported value today. A workflow with no `interface` (`null`, the default)
keeps exact legacy behavior — no business-input validation, no size cap beyond what
already exists, no run snapshot. `schema_version` is checked as strict equality (`1`),
not a floor, so a future v2 cannot be silently accepted by v1 code.

### 2. Optional, nullable legacy semantics

`interface` is a new, optional, nullable field on a workflow definition. Absent on
create = no contract (unchanged legacy behavior). On `PUT`: **absent preserves** the
stored value, **explicit `null` clears** it, an **object replaces** it after validation
— the same three-state pattern `default_reply` already uses (`atlas/db.py`
`update_workflow_definition`). Changing only `interface` still goes through the
existing `expected_version` optimistic-save path and increments `workflow.version`
exactly once, with no new concurrency mechanism.

### 3. Bounded JSON-Schema-compatible `input_schema` profile

`atlas/workflow_interface.py` implements a **profile**, not a JSON Schema engine:

- Root must declare exactly `type: "object"` — the single-element list form
  `"type": ["object"]` is accepted as equivalent (a one-entry union of `object` IS
  exactly `object`); any other list at the root is rejected. The same equivalence
  applies to the "exactly object" rule for required start-path intermediate segments.
- Supported keywords: `type` (one primitive or a unique array of primitives),
  `properties`, `required`, boolean `additionalProperties`, `items`, `enum`, `const`,
  `minLength`, `maxLength`, `minimum`, `maximum`, `minItems`, `maxItems`, and
  annotation-only `title`/`description`/`default`/`examples`.
- Optional `$schema`, accepted **only** when it is exactly
  `https://atlas.local/schemas/workflow-interface-input-v1.schema.json` — an
  identifier, never fetched.
- Rejected: `$ref`, `oneOf`/`anyOf`/`allOf`/`not`/`if`-`then`-`else`, `pattern`,
  `patternProperties`, arbitrary `format`, dependent/dynamic/unevaluated keywords, and
  any unrecognized keyword — fail closed, nothing silently ignored.
- Bounds: depth ≤ 16, ≤ 256 declared properties, ≤ 256 entries per `required`/`enum`
  list, ≤ 256 `outputs`, ≤ 10,000 instance nodes walked during validation, `title` ≤
  256 Unicode code points, `description` ≤ 2,048.
- JSON type fidelity is preserved: `bool` is never treated as `int`/`number`, and enum/
  const comparison distinguishes `true` from `1`.
- Byte caps — serialized `interface` ≤ 64 KiB, serialized `sample_input` ≤ 64 KiB, and
  an interface-enabled run's complete effective input ≤ 1 MiB — are all measured with
  the same canonical serialization:
  `json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
  allow_nan=False).encode("utf-8")`. Non-finite numbers are rejected outright.

This is a conscious ceiling: it is enough to describe flat-to-moderately-nested
business objects (the Permit fixture, gov-complaint intake, etc.) without taking on a
general-purpose validator's attack surface (recursive `$ref`, catastrophic regex,
unbounded schema composition).

### 4. Business projection excludes exactly `_meta` and `_trigger_chain`

Before schema validation, Atlas removes only these two reserved top-level input keys —
never every underscore-prefixed key, which would open a validation bypass. The
complete input (including reserved fields) is what gets persisted and byte-capped;
projection only narrows what gets validated against `input_schema`. `_meta` keeps its
existing, independent envelope validation (`validate_run_input_envelope`); `interface`
adds no new reserved keys beyond the two already established by
`docs/specs/input-adapter-contract.md` and the trigger-chain guard.

### 5. Synthetic sample policy

`sample_input`, when present, must validate against `input_schema` and is
documentation/test data only — never a default silently merged into a production run.
Flow Designer and this repo's own docs must say samples have to be synthetic; Atlas
does not invent a secrets/PII detector to enforce that mechanically. Committed samples
(the bundled pack example, any docs examples) are reviewed manually for this.

### 6. Outputs are public, POSSIBLE artifact keys — not guaranteed branch results

Each `outputs[]` entry declares a `key` (`^[A-Za-z_][A-Za-z0-9_]{0,127}$`) produced by
exactly one worker node, and a `kind` (`text`/`json`) that must match that node's
`output_format` (omitted/`"text"` vs `"json"`, mirroring
`WorkflowRunner._store_output_artifact`'s existing kind derivation). All v1 outputs are
**possible**, not required on every successful run — a graph can branch, so nothing
proves an output was produced on a given execution. Required-on-success proof,
output-content schemas, and webhook/callback output filtering are explicitly deferred
(§12); every artifact — declared or not — keeps flowing through the existing polling
and webhook shapes unchanged.

### 7. Optional `primary_output`

When present, must name one entry in `outputs`. It is a client hint for which artifact
to prefer, not an execution dependency or a guarantee.

### 8. Optional direct-run `expected_workflow_version`

`POST /api/workflow-runs` may carry an optional positive-integer
`expected_workflow_version` (boolean is rejected — the same type-fidelity rule as
everywhere else in this contract). It is compared against the **same** definition row
already loaded for graph/policy/interface, avoiding a second read and a
time-of-check/time-of-use race. A mismatch reuses the existing
`WorkflowVersionConflict` → HTTP 409 path (`PUT /api/workflows/{id}` already has this
exact mapping wired in `_dispatch`); no run is created. This is **direct-start only**.
Trigger `fire` does not gain a version pin in v1 (§12) — deferred, documented as a
known limitation.

### 9. Run interface/version snapshots

Every definition-backed run snapshots `interface` (object or `null`) and
`workflow_version` alongside the existing `graph_snapshot`/`policy_snapshot`, taken
from the one definition object loaded at start. A later definition edit or delete can
never reinterpret a historical run's input against a different contract; resume/
recovery always reads the snapshot, never the live definition. Legacy/standalone rows
(pre-migration, or runs of a since-deleted definition) keep nullable snapshots, exactly
like `graph_snapshot`/`policy_snapshot` already do.

### 10. Pack behavior

`interface` becomes an optional field inside each `workflows[]` pack entry: validated
with the shared validator (+ graph cross-check) on import, persisted, preserved on
export, and naturally covered by the existing whole-bundle HMAC signature — no second
signing scheme. The pack schema version stays `1` (additive field). The legacy
bundle-level `sample_input` field keeps its current authoring-only, not-persisted,
always-emptied-on-export behavior; it is never silently promoted into any workflow's
`interface.sample_input` — that would create two sources of truth for the same
concept. One bundled example (`atlas/packs/gov_complaint.json`) gets a synthetic
per-workflow `interface` to demonstrate the shape without introducing real PII.

### 11. Deferred (explicitly out of scope for v1)

1. A full JSON Schema implementation, or any remote/local `$ref` resolution.
2. A visual end-user input form/page builder or UI-layout schema language.
3. Pre-start atomic binary file staging/mounting for the start node — the existing
   `/workflow-runs/{id}/files` endpoint requires an existing run; it is post-run, not
   atomic start-time file intake.
4. Required-on-success output/path proof.
5. JSON artifact content-schema validation (beyond the `kind` check in §6).
6. Filtering existing webhook callbacks down to declared/public outputs only.
7. A trigger-level `expected_workflow_version` pin (§8).
8. Public anonymous ingress or browser-held Atlas tokens.
9. Importing/copying thClaws GUI components into Flow Designer.
10. A general pre-decode request-body cap for Atlas's shared `_read_json` transport —
    the 1 MiB effective-input bound in §3 applies after JSON decoding, to
    interface-enabled runs specifically, and is not a transport-level hardening
    control.
11. thClaws/MCP-tool exposure of workflows — a later, separate track, once this
    contract is authoritative.

## Prerequisite fixed by this track: manager/worker prompt-interpolation parity

Verified against `atlas/workflows.py`: worker node prompts render through
`render_prompt` (`{input.*}`, `{artifact.*}`, `{run.*}`, `{node.*}`, `{job.*}`,
fail-closed on an unresolved placeholder); manager node prompts were built by a
separate function, `_manager_prompt`, that dropped the node's authored prompt text in
verbatim via an f-string — no placeholder substitution, no error on an unresolved
reference, silently literal. This is a real behavioral gap, not merely a docs gap: a
manager prompt author writing `{input.topic}` got the literal string `{input.topic}`
sent to the model. Fixed by routing `_manager_prompt`'s node-authored prompt text
through the same `render_prompt` call workers use (same placeholder namespace, same
fail-closed behavior on an unresolved reference) before appending the unchanged
`manager_decision_v1` instruction suffix and context JSON. The `manager_decision_v1`
response contract (`_parse_manager_decision`) is untouched — this only changes how the
*outbound* prompt text is built. A hermetic regression check
(`check_workflow_interface.py`) proves both worker and manager prompts render
`{input.*}` identically and that the manager suffix still parses.

This fix is a prerequisite for §6's prompt-path cross-check: that check proves every
path a *start* node's prompt references is representable and required under the
schema — before the fix, a manager start node's `{input.*}` references would never
have been real placeholders to cross-check in the first place.

## Consequences

- **Positive:** callers get a machine-checkable input contract with fail-fast (400)
  validation before any run/job/audit side effect exists; workflow authors get an
  early, save-time cross-check that a schema they write doesn't make their own start
  prompt impossible to satisfy; historical runs are immune to later definition edits
  reinterpreting their input; the fix to manager prompt rendering closes a
  previously-silent authoring footgun independent of `interface` itself.
- **Negative / accepted:** the bounded profile cannot express everything real JSON
  Schema can (conditionals, `$ref`, regex `pattern`) — authors hitting that ceiling
  must simplify their schema, there is no escape hatch in v1. Outputs remain
  possible-not-guaranteed, so a client cannot treat "artifact present" as a correctness
  signal without also checking run state.

## Enforcement

`scripts/check_workflow_interface.py` (folded into `scripts/gate.sh`) covers the
validator's bounds/keywords, CRUD + version-increment semantics, direct-run valid/
invalid/version-boundary/1-MiB-boundary behavior, trigger-fire valid/invalid/
non-object-payload behavior, snapshot survival across definition edit/delete, pack
round-trip + tamper, and worker/manager placeholder-rendering parity. Each invariant
listed in §3–§10 was mutation-tested (broken, confirmed the check goes red, restored,
confirmed green) — evidence recorded in `PROGRESS.md`'s Milestone B row.

## Revisit trigger

Reopen this ADR when a real caller needs a §11 deferred item — most likely `$ref`/
conditional schemas (a genuinely nested business object) or required-on-success output
proof (a caller that cannot tolerate "possible" outputs). Until then, the bounded
profile and the possible-outputs model stand.
