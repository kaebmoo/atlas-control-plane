# AI Draft Authoring — Spin Prompts (D1–D6)

Ready-to-run prompts that drive a coding agent (Claude Code / Codex) through
[../plans/ai-draft-authoring-plan.md](../plans/ai-draft-authoring-plan.md). The
plan file is the source of truth; these prompts only scope, sequence, and set the
stop conditions.

> Run **Shared Preamble + one stage block** per session (the stages are small and
> span two repos, so per-stage sessions beat one long driver), or paste the
> **Driver (D1→D4)** to run the four active stages in order. **Never** run D5/D6
> without an explicit human go-ahead in the session — they are backlog by design.

---

## Shared Preamble (paste first, every session)

```text
Repos:
  Atlas         /Users/seal/Documents/GitHub/atlas-control-plane
  flow-designer /Users/seal/Documents/GitHub/flow-designer
Atlas needs Python 3.11+ (datetime.UTC). flow-designer uses bun + vite + vitest +
playwright (see package.json scripts).

Read FIRST, in this order, before editing anything:
- atlas-control-plane/docs/plans/ai-draft-authoring-plan.md   (THE plan: state, DoD, contract)
- For Atlas stages (D1, D2, D5):
  - atlas/app.py — the whole builder section. Anchors (current tree, also greppable):
      _build_workflow_draft @1736 (bounded retry lives here)
      _attempt_workflow_draft @1764 (returns (draft,None) or (None,failure))
      _run_workflow_builder @1958, _builder_prompt @1969, _builder_context @1980
      _workflow_builder_worker @2035, class _BuilderReplyError @2043
  - atlas/workflows.py — validate_workflow_graph (human_gate choices), _validate_edge /
      _validate_condition (per-type condition fields), validate_workflow_references +
      _worker_matches_role @376 (role grounding rule: lowercase role equality OR tag membership)
  - scripts/check_workflow_api.py — check_milestone_7 @766: builder stub with
      response_queue @774 + fail_job; this is the harness every new assertion extends
  - docs/specs/api-reference-en.md + -th.md — the AI Draft sections
- For flow-designer stages (D3, D4, D6):
  - CLAUDE.md and AGENTS.md (architecture + process rules — binding)
  - docs/BACKEND_INTEGRATION.md (Atlas contract as the frontend consumes it)
  - src/lib/atlas-api.server.ts, atlas-mutations.functions.ts, atlas-mutations.ts,
    atlas-queries.ts (the exact server-fn/mutation pattern to copy)
  - src/routes/_app/workflows.index.tsx (createWorkflow → navigate pattern; the
    "Draft with AI" button lands here)
  - src/components/atlas/workflow-pack-import-dialog.tsx and
    workflow-test-run-dialog.tsx (house dialog precedents)
  - docs/CHECKLIST.md + docs/IMPLEMENTATION_PLAN.md (phase close-out ritual)

House rules (binding, both repos):
- Atlas core: Python stdlib ONLY. All /api/* changes ADDITIVE — never change an
  existing path or response shape; every existing check keeps passing.
- Builder output is a PROPOSAL: deterministic validation before returning; never
  auto-save, auto-run, or auto-create triggers from model output; at most ONE
  self-repair retry per draft (already implemented — do not raise the ceiling).
- One hermetic runnable check per behavior (own temp DB, ephemeral port, stubbed
  builder via runtime.jobs.submit — the check_milestone_7 pattern). Never tick a
  DoD item before its check is green.
- Docs move with code at every stage close-out: any /api/* change updates
  openapi.yaml + api-reference-en.md + api-reference-th.md (EN+TH parity, never
  English only).
- flow-designer (from its CLAUDE.md): server functions live in *.functions.ts,
  validate the flow-designer session via requireAtlasToken(), and call one typed
  fixed Atlas operation; client code never imports *.server.ts; Atlas is the only
  authorization authority; use design tokens + the shared primitives in
  src/components/atlas/page.tsx; mutations NEVER auto-retry (a draft retry
  re-bills the user's model); WCAG 2.1 AA; explicit loading/empty/error/forbidden
  states; never edit src/routeTree.gen.ts; repo is Lovable-connected — commit
  small phases, never rewrite published history.

Gates:
- Atlas gate (run from atlas-control-plane; fix until green before commit):
    python3 scripts/check_workflow_db.py
    python3 scripts/check_workflows.py
    python3 scripts/check_workflow_api.py
    python3 scripts/check_auth.py
    python3 scripts/check_usage.py
    python3 scripts/check_docs.py   # NOTE: reads `git ls-files` — `git add -A` any
                                    # new docs BEFORE running, or their README links
                                    # read as 404-on-fresh-clone and the check reds
    scripts/lint.sh          # pinned ruff/bandit/mypy via uvx
- flow-designer gate (run from flow-designer):
    bun run lint && bun run typecheck && bun run test && bun run test:contract
    bun run scan:bundle && bun run build
    # + docs/CHECKLIST.md items for the touched surface; e2e where it demands

Close-out, every stage: full repo gate green → docs synced → conventional commit
(no push) → report what changed + gate output summary. Live-test steps in a DoD
need the user's machine (thclaws --serve running, worker tagged workflow_builder,
Atlas RESTARTED after any atlas/app.py change); if you cannot run them, say so
explicitly and hand the user the exact commands instead of skipping silently.

Hard stops (pause and ask the human): an existing /api/* path/shape would change;
a runtime dependency looks unavoidable; anything touches the Atlas contract, auth
boundary, or data ownership from the frontend; a DoD cannot be met as written.
Scope discipline: do ONLY the stage's DoD. No gold-plating, no pulling work
forward. D5 and D6 are BACKLOG — building them requires the human's explicit
go-ahead inside the session, not this file.
```

---

## Driver (D1→D4, the four active stages)

```text
Execute stages D1, D2, D3, D4 from ai-draft-authoring-plan.md §3 in order.
For each: implement to DoD → extend/run the stage's check → full repo gate green →
docs sync → commit (no push) → continue. Do not start D5 or D6. Where a DoD has a
live-test step you cannot perform, hand the user the exact commands and mark the
stage "pending live confirmation" instead of claiming it done. End with a report:
per-stage status, commit list, and any live steps left for the human.
```

---

## Stage D1 — Commit the shipped hardening (Atlas)  [tree → history]

```text
Follow ai-draft-authoring-plan.md §1 "Built on 2026-08-10" and §3 (D1).

The working tree already contains the finished change set (context contracts +
_BuilderReplyError + bounded retry in atlas/app.py; response_queue/fail_job stub +
retry/contract assertions in scripts/check_workflow_api.py; AI Draft retry
paragraphs in docs/specs/api-reference-en.md and -th.md; new poc/try_ai_draft.py).
Your job is to land it as ONE reviewed commit, not to rewrite it:

0. The tree also contains this plan/prompts pair + their docs/README.md index
   entries. `git add -A`, then land docs first as their own commit:
   docs(plans): AI draft authoring plan + spin prompts
1. git status / git diff — review every remaining hunk against the plan's
   description. Nothing unrelated may ride along.
2. Read the retry path end to end (_build_workflow_draft → _attempt_workflow_draft
   → _BuilderReplyError) and check_milestone_7's new assertions; confirm they
   agree with the api-reference text (at most 2 builder jobs; warning on
   successful retry; infra failures never retry).
3. Run the full Atlas gate (Shared Preamble), check_docs.py included — docs
   changed. Fix only what the gate flags.
4. Commit (no push), e.g.:
   feat(ai-draft): builder context contracts + bounded self-repair retry
   - context: human_gate choices_item {"id","label"} + per-condition field map
   - draft: one bounded retry on model-output failures, surfaced in warnings
   - check: response-queue stub, retry/contract assertions; docs EN/TH; poc harness
5. Live smoke (user's machine; hand over commands if you cannot run them):
   restart Atlas, then  python3 poc/try_ai_draft.py  with the default prompt.
   Record the outcome in the session report. A role-grounding 400
   ("role has no matching worker") is EXPECTED until D2 and does NOT block this
   commit; any choices/condition-shape 400 DOES block — investigate before landing.
```

---

## Stage D2 — Role grounding in the builder context (Atlas)

```text
Follow ai-draft-authoring-plan.md §3 (D2). Field evidence: run 3 failed with
"workflow node summarizer role has no matching worker: summarizer" — the model
invented a role because the context never states the grounding rule.

Implement in atlas/app.py:
- _builder_context(): add "available_roles": sorted unique lowercase union of
  every worker's role plus every worker's tags (mechanical truth — compute from
  runtime.db.list_workers(); empty list is fine and meaningful).
- node_types.worker and node_types.manager: add "rules" stating: role is
  optional; a role set WITHOUT worker_id/workspace_id MUST be one of
  available_roles (Atlas matches a worker whose role equals it or whose tags
  contain it, case-insensitively); if no available role fits the task, OMIT role
  (Atlas auto-routes at run time) or set worker_id to an id from workers; never
  invent roles, worker ids, or workspace ids.
Mirror _worker_matches_role (workflows.py:376) exactly — do not restate the rule
more strictly or more loosely than the validator enforces it.

Extend scripts/check_workflow_api.py check_milestone_7:
- assert "available_roles" (and one real value derived from the stub worker, e.g.
  "workflow_builder") reaches the builder prompt;
- new scenario via response_queue: first reply = a draft whose worker node uses a
  role that matches no worker → expect the retry; assert the retry prompt contains
  "has no matching worker"; second reply = corrected draft (role omitted or a
  matching role) → expect success + the self-repair warning. This proves the
  retry covers the role-grounding class end to end.
Then run a quick mutation test locally (delete available_roles → check must go
red) before trusting it; restore.

Docs: one sentence in api-reference-en.md + -th.md AI Draft sections (context now
lists available_roles and the role rules). Full Atlas gate green → commit (no
push):  feat(ai-draft): ground node roles in builder context

Live retest (user's machine; hand over if needed): restart Atlas, run
python3 poc/try_ai_draft.py  with the ORIGINAL default prompt, expect first-try
success or one self-repair warning. If it still fails, paste the error into the
session and STOP for diagnosis — do not iterate blind against a paid model.
```

---

## Stage D3 — "Draft with AI" dialog (flow-designer)

```text
Follow ai-draft-authoring-plan.md §3 (D3) plus flow-designer's own CLAUDE.md and
docs/CHECKLIST.md. This is that repo's first AI-assist surface — copy its existing
patterns exactly; invent no new architecture.

Build:
1. src/lib/atlas-api.server.ts: atlasDraftWorkflow(plainLanguagePrompt) → POST
   /api/workflows/draft. Match the file's existing client conventions (base URL,
   bearer from the server-side session, error mapping). Check how the file sets
   request timeouts: a draft spends up to TWO model calls and can run minutes —
   size this call's timeout accordingly instead of inheriting a short default.
2. src/lib/atlas-mutations.functions.ts: draftWorkflowFn — requireAtlasToken(),
   one typed fixed operation, input = { plainLanguagePrompt: string } validated
   server-side (non-empty, sane length cap). NO retry (house rule: a silent retry
   re-bills the user's model). Return the draft as-is: name, description, graph,
   policy, triggers, explanation, warnings.
3. src/lib/atlas-mutations.ts: useDraftWorkflow (invalidates nothing — the draft
   is unsaved; follow useExportPack's empty-key precedent).
4. src/routes/_app/workflows.index.tsx: a "Draft with AI" action beside "New
   workflow" opening a dialog (precedents: workflow-pack-import-dialog.tsx,
   workflow-test-run-dialog.tsx):
   - textarea for the plain-language description (Thai must work) + submit;
   - busy state telling the user drafting calls their model and may take minutes;
   - result view: name, description, explanation, warnings list, and a compact
     summary (node count/types, edge count, policy keys);
   - proposed triggers rendered DISPLAY-ONLY with a note pointing at the
     Triggers page — never created from here;
   - "Create as draft & open": call the existing useCreateWorkflow with the
     draft's name/description/graph/policy, OMIT status (Atlas defaults to
     'draft', test-only), then navigate to the editor exactly like the "New
     workflow" path; "Discard" closes clean;
   - Atlas 400 text shown verbatim (the messages are actionable);
     "No workflow_builder worker configured" additionally gets a setup hint
     (tag a worker with role/tag workflow_builder in Atlas).
   Design tokens, shared primitives (PageHeader/EmptyHint etc.), WCAG AA,
   explicit loading/empty/error/forbidden states.

Tests: vitest unit for the dialog's state/mapping logic; a contract-project test
for draftWorkflowFn following the existing contract-test pattern under tests/.
Full flow-designer gate (Shared Preamble) + docs/CHECKLIST.md for this surface.

Docs: flow-designer docs/guides/web-user-guide-en.md + -th.md gain the feature
section; docs/BACKEND_INTEGRATION.md gains the draft endpoint (shape, the
bounded-retry latency note, the workflow_builder prerequisite).

Commit (no push):  feat(workflows): Draft with AI dialog over Atlas /workflows/draft
```

---

## Stage D4 — Editor assists (flow-designer)

```text
Follow ai-draft-authoring-plan.md §3 (D4) plus CLAUDE.md/CHECKLIST.md. Four
assists inside the workflow editor, all strictly proposal-then-explicit-apply:

1. Explain: editor action calling POST /api/workflows/{id}/explain (saved id
   exists in the editor route); render the explanation in a dialog.
2. Repair: when a save/validate returns an Atlas 400, offer "Repair with AI";
   send the CURRENT graph/policy preview to POST /api/workflows/{id}/repair;
   preview the returned draft (name/explanation/warnings + summary); on explicit
   accept, replace the unsaved canvas draft state — never save automatically.
3. Suggest workers: for worker/manager nodes carrying only a role, call POST
   /api/workflows/suggest-workers with the current graph/policy; render each
   suggestion's state (matched / fallback / unavailable) with its reason; a
   per-node apply click writes worker_id into the canvas draft. Works without an
   AI builder (Atlas falls back to local matching) — do not gate the button on
   builder presence.
4. Suggest triggers: from the editor's triggers surface, call POST
   /api/workflows/{id}/suggest-triggers; render suggestions; each is created
   only by explicit click through the existing trigger-create mutation.

Server fns follow the D3 pattern (session-validated, typed, no retry; repair and
suggest-triggers share the long-timeout treatment since they hit the builder).
Tests: vitest unit + contract additions. Full gate + CHECKLIST + guide EN/TH +
BACKEND_INTEGRATION updates.

Commit (no push):  feat(workflows): editor AI assists (explain/repair/suggest)
```

---

## Stage D5 — POST /api/workflows/{id}/revise (Atlas)  [BACKLOG — human go-ahead required]

```text
STOP unless the human has explicitly approved D5 in THIS session.

Follow ai-draft-authoring-plan.md §3 (D5). Additive endpoint next to repair
(app.py dispatch around the existing /{id}/repair route):
  POST /api/workflows/{id}/revise  {"instruction": str, optional graph/policy/
  triggers preview overriding the saved definition}
→ {"draft": <same ai-draft schema>}. Builder prompt embeds the effective current
definition + the instruction + the standard context; reuse
_attempt_workflow_draft for the SAME bounded one-retry behavior; validate with
_validate_workflow_draft; never persist anything.

Check: extend check_milestone_7 — revise happy path (instruction produces the
queued corrected draft), retry path, and a 404/400 matrix consistent with repair.
Docs: openapi.yaml + api-reference EN/TH. Full gate → commit (no push).
```

---

## Stage D6 — Chat-refine panel (flow-designer)  [BACKLOG — needs D5 + human go-ahead]

```text
STOP unless D5 is merged AND the human has explicitly approved D6 in THIS session.

Follow ai-draft-authoring-plan.md §3 (D6). Editor side panel: instruction input →
draftWorkflow-style server fn calling /{id}/revise with the CURRENT unsaved
graph/policy as the preview → render the returned draft as a preview/diff against
the canvas (at minimum: node/edge add/remove/change lists) → explicit apply
replaces the unsaved draft state → iterate. Saving remains the editor's normal
explicit save. Same test/docs/checklist bar as D3/D4.
```
