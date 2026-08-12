# AI Draft Contract Hardening — Spin Prompts (D2b-1 → D2b-5)

Ready-to-run prompts that drive a coding agent (Codex / Claude Code) through
[../plans/ai-draft-contract-hardening-plan.md](../plans/ai-draft-contract-hardening-plan.md).
The plan file is the source of truth; these prompts only scope, sequence, and set
the stop conditions.

> Run **Shared Preamble + one stage block** per session. D2b-1 → D2b-2 → D2b-3 are
> Atlas commits, D2b-4 is a single live retest on the user's machine, D2b-5 is
> flow-designer. D2b-4 must not run before 1–3 are committed and Atlas is
> restarted — it spends real model money.

---

## Shared Preamble (paste first, every session)

```text
Repos:
  Atlas         /Users/seal/Documents/GitHub/atlas-control-plane
  flow-designer /Users/seal/Documents/GitHub/flow-designer
Atlas needs Python 3.11+ (datetime.UTC). flow-designer uses bun + vite + vitest.

Read FIRST, in this order, before editing anything:
- atlas-control-plane/docs/plans/ai-draft-contract-hardening-plan.md  (THE plan for this work)
- atlas-control-plane/docs/plans/ai-draft-authoring-plan.md           (parent plan: D1-D4 shipped, D5/D6 backlog)
- For Atlas stages (D2b-1, D2b-2, D2b-3):
  - atlas/app.py — the whole builder section. Anchors (tree 56ae303; grep by name if drifted):
      _WORKFLOW_DRAFT_FIELDS @1661        (closed 7-key draft field set)
      _validate_workflow_payload @1641    (CLIENT input path — must stay strict)
      _validate_workflow_draft @1664      (model-output path)
      _validate_workflow_draft_triggers @1716  (raises the reported error, line 1721)
      _build_workflow_draft @1736         (the one bounded retry)
      _attempt_workflow_draft @1764       (returns (draft,None) or (None,failure))
      _repair_workflow @1785              (third setdefault site)
      _run_workflow_builder @1958, _builder_prompt @1969, _builder_context @1980
      _BuilderReplyError @2066, _json_from_text @2072
  - atlas/workflows.py — the CLOSED vocabularies this work documents:
      node types @221            {"worker","manager","join","human_gate"}
      condition dispatch @2477   always | artifact_equals | artifact_in |
                                 manager_selected | human_selected | max_iterations_below
      _TRIGGER_STATES @97, _TRIGGER_CONFIG_KEYS @108 (manual/webhook configs are OPEN by design)
      validate_workflow_trigger_payload @2054
  - scripts/check_workflow_api.py — check_milestone_7 @766: builder stub with
      response_queue @774 + fail_job. Every new assertion extends THIS harness.
  - docs/specs/api-reference-en.md + -th.md — the AI Draft sections
- For D2b-5 (flow-designer):
  - CLAUDE.md and AGENTS.md (binding), docs/CHECKLIST.md
  - flow-designer/docs/AI_DRAFT_ERROR_UX_PLAN.md  (THE plan for that stage)
  - src/lib/workflow-ai-draft.ts, src/components/atlas/workflow-ai-draft-dialog.tsx
  - tests/unit/workflow-ai-draft.test.ts

House rules (binding, both repos):
- Atlas core: Python stdlib ONLY. All /api/* changes ADDITIVE — never change an
  existing path or response shape; every existing check keeps passing.
- Builder output is a PROPOSAL: deterministic validation before returning; never
  auto-save, auto-run, or auto-create triggers from model output; at most ONE
  self-repair retry per draft — do NOT raise that ceiling.
- Atlas may mutate model output in exactly two sanctioned ways after this work:
  appending its own warning string, and DROPPING a malformed trigger suggestion
  (with a warning that quotes it). Never fabricate trigger content.
- One hermetic runnable check per behavior (own temp DB, ephemeral port, stubbed
  builder via runtime.jobs.submit). MUTATION-TEST every new check: break the code
  it covers, confirm the gate goes RED, restore. A check that stays green is worthless.
- Docs move with code: any behavior change updates api-reference-en.md AND -th.md
  (EN+TH parity, never English only).

Gates:
- Atlas gate (run from atlas-control-plane; green before every commit):
    python3 scripts/check_workflow_db.py
    python3 scripts/check_workflows.py
    python3 scripts/check_workflow_api.py
    python3 scripts/check_auth.py
    python3 scripts/check_usage.py
    python3 scripts/check_docs.py   # reads `git ls-files` — `git add -A` any NEW docs
                                    # BEFORE running, or their README links read as
                                    # 404-on-fresh-clone and the check reds
    scripts/lint.sh                 # pinned ruff/bandit/mypy via uvx
  Full ./scripts/gate.sh before the FINAL Atlas commit of the session.
- flow-designer gate (run from flow-designer):
    bun run lint && bun run typecheck && bun run test && bun run test:contract
    bun run scan:bundle && bun run build

Close-out, every stage: gate green → docs synced → conventional commit (no push)
→ report what changed + gate summary. Live steps need the user's machine
(thclaws --serve running, a worker tagged workflow_builder, Atlas RESTARTED after
any atlas/app.py change); if you cannot run them, hand the user the exact commands
instead of skipping silently.

Hard stops (pause and ask the human): an existing /api/* path or response shape
would change; a runtime dependency looks unavoidable; a DoD cannot be met as
written; you are about to coerce or fabricate model output beyond the two
sanctioned mutations above; you are about to relax an EXISTING assertion in
scripts/check_workflow_api.py.
Scope discipline: do ONLY the stage's DoD. The gaps in plan §7 (draft `interface`
field, SLA/timer primitive) are NOT in scope and need a human go-ahead.
```

---

## Stage D2b-0 — Land the plan docs (Atlas) [only if not already committed]

```text
The tree contains two new untracked docs:
  docs/plans/ai-draft-contract-hardening-plan.md
  docs/prompts/ai-draft-contract-hardening-spin-prompts.md
Add index entries for BOTH in docs/README.md, matching the existing
"AI Draft Authoring Plan" / "AI Draft Authoring — Spin Prompts" entries in style
and placement (including the ASCII tree block further down the file, which lists
plans/ and prompts/ contents).
Then: git add -A → python3 scripts/check_docs.py (must be green; it resolves
README links against `git ls-files`) → commit, no push:
  docs(plans): AI draft contract hardening plan + spin prompts
```

---

## Stage D2b-1 — Trigger contract + DSL boundary in the builder context (Atlas)

```text
Follow ai-draft-contract-hardening-plan.md §3 F1 + F2 and §4 (D2b-1).

Field evidence (run 4): a plain-language Thai purchase-approval prompt produced
  "triggers": ["พนักงานส่งคำขอจัดซื้อ"]
and died with 400 `workflow draft trigger at index 0 must be an object` AFTER TWO
builder jobs. Cause: _builder_context publishes "trigger_types" as a bare list of
type NAMES (app.py:2049) — the same anti-pattern that made the model guess
human_gate choices as plain strings before D1 upgraded condition_types to a
per-type contract map. trigger_types was left behind. Fix it at the source.

Implement in atlas/app.py:

1. _builder_context(): replace the bare "trigger_types" list with a per-type
   CONTRACT MAP shaped like the existing "condition_types" map. Facts must come
   from the validator, not from imagination:
     - types: the six in workflows._TRIGGER_STATES (@97)
     - closed config keys: workflows._TRIGGER_CONFIG_KEYS (@108) —
         schedule: interval_minutes | daily_time
         workflow_run_completed: source_workflow_definition_id, state
         artifact_created: source_workflow_definition_id, key, kind
         worker_status_changed: worker_id, status
     - manual and webhook have OPEN configs by design — say "open", do NOT
       present them as closed.
     - schedule rule: config requires exactly one of interval_minutes (a positive
       number) or daily_time ("HH:MM").
   Fold the now-redundant top-level "schedule_configs" (@2050) into the schedule
   entry and delete that key.

2. _builder_context(): add the nested EXAMPLE and the rules — the example is what
   actually fixed this class for human_gate.choices:
     "trigger_item": {"type": "manual", "name": "Employee submits a purchase request", "enabled": false}
     trigger rules (as a "rules" list): triggers is a list of OBJECTS, never
     strings or sentences; a trigger object uses only type, name, config, enabled;
     if the described start condition does not map to a listed trigger type,
     return triggers: [] and record it in warnings.

3. _builder_context(): add a "dsl_boundary" block with ALL SIX rules from plan
   §3 F2, verbatim in meaning:
     a. node_types, condition_types, trigger_types, artifact_kinds are COMPLETE
        lists; never invent a node type, condition type, or trigger type.
     b. there is NO numeric comparison condition — to branch on an amount, add a
        worker node that classifies the value into a named bucket artifact (e.g.
        approval_tier = le_50k | le_200k | gt_200k), then branch with
        artifact_equals or artifact_in on that artifact.
     c. there is NO timer, deadline, reminder, or escalation construct — a
        human_gate waits indefinitely; put time-based requirements in warnings.
     d. there is NO email or notification node — put notification requirements in
        warnings.
     e. Atlas already audits every decision (who, when, outcome, reason) — do not
        model audit logging as a node.
     f. return ONLY the seven top-level keys; never add interface, inputs, or any
        other key. When part of the request cannot be modeled, still return a
        valid draft for the part that CAN be, and list EACH unmodeled requirement
        as its own warnings entry.
   Verify (a) against workflows.py:221 (node type set) AND BOTH condition lists —
   the runtime evaluator _evaluate_condition (@2452-2477) and the separate edge
   validator _validate_condition/_validate_edge (@2120-2173) each enumerate the
   six types independently; they agree today, and the context must match them
   exactly, not approximately.

4. _builder_prompt() (@1969): add ONE line to the four-rule preamble, e.g.
   "triggers is a list of objects; if the start condition does not map to a
   trigger type, return triggers: [] and say so in warnings." The preamble is read
   before the context JSON — cheapest, highest-leverage position in the prompt.

Extend scripts/check_workflow_api.py check_milestone_7 (plan §5 check 1):
- assert prompts[-1] carries the trigger-shape fragments (trigger_item; the
  literal phrase you chose for "list of objects"; "type, name, config, enabled")
- assert prompts[-1] carries the boundary fragments ("never invent", the numeric
  phrasing you chose, "classif", "warnings", "interface")
- where structure matters, parse the Context JSON block the way the existing
  available_roles assertion does, instead of substring-matching JSON.
Mutation test: delete the dsl_boundary block → check must go RED. Restore.

Docs: api-reference-en.md + -th.md AI Draft sections — the builder context now
states the trigger object contract and the closed DSL vocabularies, and instructs
the model to record unsupported requirements in warnings instead of inventing
types. NO /api/* path or response shape changes, so openapi.yaml needs NO edit —
say so explicitly in the commit body rather than inventing a change.

Atlas gate green → commit (no push):
  feat(ai-draft): trigger object contract + DSL boundary in builder context
Do NOT run a live model test in this stage — that is D2b-4, after D2b-2/3 land.
```

---

## Stage D2b-2 — Normalize model output, never client input (Atlas)

```text
Follow ai-draft-contract-hardening-plan.md §3 F3 and §4 (D2b-2).

Goal: a malformed trigger SUGGESTION must never cost a model call or a 400. It is
display-only data on a proposal the human reviews.

Implement in atlas/app.py:

1. Add ONE helper, e.g. _normalize_builder_draft(draft: dict[str, Any]) -> None,
   and make it the ONLY normalization point. It must:
     - keep today's behavior: draft.setdefault("triggers", []) and
       draft.setdefault("warnings", [])
     - if draft["triggers"] is a list: keep only dict items; if anything was
       dropped AND draft["warnings"] is a list, append exactly one warning naming
       the count and quoting each dropped value, JSON-rendered
       (json.dumps(item, ensure_ascii=False)) and truncated to ~120 chars, e.g.
         Ignored 1 trigger suggestion that was not a trigger object:
         "พนักงานส่งคำขอจัดซื้อ". Create triggers on the Triggers page.
     - append to warnings ONLY when warnings is already a list, so a reply with
       warnings: "text" still fails validation exactly as today.

2. Replace all THREE duplicated setdefault pairs with a call to it:
     _build_workflow_draft @1755-1756 (retry path)
     _attempt_workflow_draft @1776-1777 (first attempt)
     _repair_workflow @1807-1808
   It must run BEFORE the _validate_workflow_draft that follows each — that
   ordering is what saves the model call.

DO NOT (these are decisions in plan §3 F3, not oversights — changing them is a
hard stop that needs the human):
  - do NOT coerce a non-list "triggers" (None / string / dict) to []. That stays a
    validation failure handled by the existing bounded retry, and
    check_workflow_api.py already asserts triggers=None → 400 "triggers must be a
    list". Do not touch that assertion.
  - do NOT convert a dropped string into a manual trigger object. Dropping plus a
    quoting warning is the sanctioned mutation; fabricating content is not.
  - do NOT call the helper from _validate_workflow_payload (@1641/@1653) or from
    the POST /api/workflows path (@741). Client-supplied triggers must keep
    failing loudly. (PUT @793 builds a filtered validation_payload that never
    carries triggers, so it is not a second exposure — do not "fix" that here.)

Extend scripts/check_workflow_api.py check_milestone_7 (plan §5 checks 2-4):
  2. builder replies with
       triggers=["พนักงานส่งคำขอจัดซื้อ", {"type": "manual"}]
     → HTTP 200; draft["triggers"] == [{"type": "manual"}]; some warning contains
     "Ignored 1 trigger suggestion"; and — THE ASSERTION THAT PROTECTS THE USER'S
     MODEL BUDGET — len(prompts) == calls_before + 1 (no retry spent). Do not omit it.
  3. the existing triggers=None → 400 "triggers must be a list" assertion stays
     exactly as-is and green.
  4. regression lock: POST /api/workflows with triggers: ["x"] still returns 400
     with the full text "workflow draft trigger at index 0 must be an object".
     This proves the normalizer did not leak into the client-input path. Use POST,
     not PUT — PUT never validates triggers at all.
Mutation test: remove the drop logic → check 2 must go RED; call the helper from
_validate_workflow_payload → check 4 must go RED. Restore both.

Docs: api-reference-en.md + -th.md — at draft time, a trigger suggestion that is
not an object is dropped and reported in warnings rather than failing the request;
client-supplied triggers on POST/PUT /api/workflows are unchanged and still
rejected. Response shape unchanged (warnings already exists) → no openapi.yaml edit.

Atlas gate green → commit (no push):
  fix(ai-draft): drop malformed trigger suggestions instead of failing the draft
```

---

## Stage D2b-3 — Accept fenced JSON without spending a retry (Atlas)

```text
Follow ai-draft-contract-hardening-plan.md §3 F4 and §4 (D2b-3).
Separate commit, deliberately: it must be droppable without unpicking D2b-1/2.

Evidence: ai_draft_result.json:80 in the repo root shows a SUCCESSFUL draft whose
warnings include
  "Draft needed one self-repair retry; first attempt was rejected:
   workflow_builder response must be one JSON object: Expecting value"
i.e. the model wrapped good JSON in a ```json fence and Atlas paid for a second
model call to get the same content back.

Implement in atlas/app.py _json_from_text (@2072), conservatively, stdlib only:
  - strip the reply
  - if it starts with "```": drop that first line (``` or ```json or ```JSON …)
    and, if the last line is "```", drop it; strip again
  - then json.loads as today
Do NOT brace-scan, regex-extract, or otherwise pull JSON out of arbitrary prose.
An unfenced prose reply MUST still raise _BuilderReplyError so the bounded retry
keeps its job. Keep the error messages byte-identical.

scripts/check_workflow_api.py (plan §5 check 5):
- the EXISTING fenced-reply scenario queues ```json\n{"name": "fenced"}\n``` —
  after stripping it parses but fails draft validation, so it still retries and
  the assertion stays green. Its comment ("A reply that is not one JSON object…")
  is now inaccurate: correct the comment, do not weaken the assertion.
- ADD: a fenced VALID draft → len(prompts) == calls_before + 1 and NO "self-repair"
  warning.
- ADD: a prose reply (e.g. "Here is your workflow: it starts with a gate.") →
  still len(prompts) == calls_before + 2, retry then success.
Mutation test: remove the fence stripping → the fenced-valid-draft assertion must
go RED. Restore.

Docs: api-reference-en.md + -th.md — a builder reply wrapped in a markdown code
fence is parsed directly and does not consume the self-repair retry.

Atlas gate green (run the FULL ./scripts/gate.sh here — this is the last Atlas
code commit of the sequence) → commit (no push):
  fix(ai-draft): parse fenced builder replies without spending the retry
PR body must call out explicitly that this changes behavior on a tested path
(fenced replies used to always retry) and that it is a tightening — strictly
fewer paid model calls, no new acceptance of malformed input.
```

---

## Stage D2b-4 — One live retest with the purchase prompt (user's machine)

```text
Follow ai-draft-contract-hardening-plan.md §4 (D2b-4) and §6.
THIS SPENDS REAL MODEL MONEY. Preconditions, all mandatory:
  - D2b-1, D2b-2, D2b-3 committed and the Atlas gate green
  - thclaws --serve running; a worker tagged workflow_builder
    (python3 poc/try_ai_draft.py --tag-worker <id> does a safe GET-merge upsert)
  - Atlas RESTARTED after the app.py changes — otherwise you are testing old code

Run the ORIGINAL Thai purchase-approval prompt verbatim (the one from the run-4
report), ONCE, via poc/try_ai_draft.py. Do not add schema hints to the prompt —
the entire point is that a plain-language prompt now works.

PASS requires all three:
  a. HTTP 200 with a draft
  b. amount branching modeled through a classifier worker node emitting a bucket
     artifact + artifact_equals / artifact_in edges (NOT an invented numeric condition)
  c. triggers is [] or a list of valid trigger objects, AND the unsupported asks
     (reminder after 2 days, escalation after 5 days, email notifications) appear
     as warnings entries rather than as invented node/condition types

Then: record the verbatim outcome as run 5 in the plan's §1 field-test table
(and mirror one line into ai-draft-authoring-plan.md §1 so the parent plan stays
current). Commit:  docs(plans): record AI draft field-test run 5

FAIL: capture the raw error AND the builder reply, add the new failure class as a
row in the table, and STOP. Do not re-run to "see if it passes this time" — each
attempt is up to two paid model calls. Hand the human the evidence and the
proposed next contract line.
```

---

## Stage D2b-5 — Friendly draft errors in flow-designer

```text
Follow flow-designer/docs/AI_DRAFT_ERROR_UX_PLAN.md (and
atlas-control-plane/docs/plans/ai-draft-contract-hardening-plan.md §3 F5 for the why).
Read flow-designer CLAUDE.md + AGENTS.md first — they are binding.

Problem: describeWorkflowDraftError (src/lib/workflow-ai-draft.ts:39-53) passes
the Atlas message through verbatim, so an end user reads
"workflow draft trigger at index 0 must be an object". That string is correct for
an engineer and useless for the person who typed a plain-language description.

Constraint that shapes the fix: D3's Definition of Done in
ai-draft-authoring-plan.md §3 requires Atlas's 400 text be shown verbatim. So do
NOT replace it — DEMOTE it. Headline in plain language, raw text kept verbatim
inside a collapsed disclosure.

Implement (the UX plan has the full rationale — read it, it explains why the
obvious text-prefix approach is WRONG):
1. describeWorkflowDraftError(error, phase: "draft" | "create" = "draft") returns
   { message, detail?, forbidden, needsBuilderSetup }.
   Classify on the STRUCTURED KIND, not on message text: ClientAtlasError.kind
   === "validation" (atlas-types.ts:93-103 — every Atlas 400/422). Do NOT use a
   /^workflow / regex: real validation strings such as "duplicate node id: …",
   "unsupported workflow condition: …" and "unknown workflow trigger config
   key(s) for …" do not carry that prefix, while "workflow job timed out: …"
   does. Within kind === "validation", keep two existing special cases ahead of
   the generic one: /No workflow_builder worker configured/i → needsBuilderSetup
   (message unchanged, no detail); /^workflow_builder job failed/i → builder
   infrastructure headline + detail. Everything else in that kind gets the
   phase-appropriate headline + detail = the raw Atlas text, unmodified.
   Non-Atlas Errors and every other kind keep today's behavior exactly
   (message = the text, detail = undefined); forbidden semantics unchanged.
2. ActionError (src/components/atlas/workflow-ai-draft-dialog.tsx:33-55) takes a
   phase prop and renders detail, when present, inside a COLLAPSED <details>
   labelled "Technical details", using existing design tokens and shared
   primitives. Pass phase="draft" for draftRequest.error and phase="create" for
   createError — the dialog reuses ActionError for both, and the draft headline's
   "simplify your description" advice is wrong on the create path.
   Keyboard reachable, WCAG 2.1 AA, no new dependency, no layout regression.

Tests (extend tests/unit/workflow-ai-draft.test.ts) — all inputs are
ClientAtlasError-shaped objects unless noted:
- { kind: "validation", message: "workflow draft trigger at index 0 must be an
  object" } → headline message, detail === the raw string, forbidden false,
  needsBuilderSetup false
- { kind: "validation", message: "duplicate node id: gate" } → SAME
  classification (this is the case a prefix regex would have missed)
- { kind: "validation", message: "No workflow_builder worker configured" } →
  unchanged; needsBuilderSetup true; detail undefined
- { kind: "validation", message: "workflow_builder job failed: builder worker
  exploded" } → builder-infrastructure headline, detail set, needsBuilderSetup false
- { kind: "server", message: "Atlas failed to process the request." } →
  unchanged, detail undefined
- { kind: "forbidden", message: "Access denied" } → unchanged
- plain new Error("No workflow_builder worker configured") → unchanged (the
  existing test must stay green; assert detail is undefined)
- phase: "create" yields a different headline than phase: "draft"
Plus a dialog-level test that the disclosure renders and contains the raw text.

flow-designer gate: bun run lint && typecheck && test && test:contract &&
scan:bundle && build. Walk docs/CHECKLIST.md for the touched surface. Update the
EN + TH web guides only where they quote the old error copy.
Commit (no push), small phase, never rewrite published history:
  fix(workflows): surface AI draft failures in plain language
```

---

## Stop conditions (all stages)

```text
Stop and ask the human if:
- a fix would require changing an existing /api/* path or response shape
- you are about to relax, delete, or "adjust" an existing assertion in
  scripts/check_workflow_api.py to make something pass
- you are about to coerce or fabricate model output beyond the two sanctioned
  mutations (append Atlas's own warning; drop a malformed trigger suggestion)
- the live retest (D2b-4) fails with a NEW class — report it, do not iterate
  against a paid model
- you find yourself wanting a draft `interface` field or an SLA/timer primitive:
  both are recorded in plan §7 as unscheduled gaps and need a product decision
```
