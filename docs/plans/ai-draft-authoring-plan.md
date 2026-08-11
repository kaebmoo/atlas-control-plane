# AI Draft Authoring Plan — plain language → validated flow (Atlas + flow-designer)

> TL;DR (ภาษาไทย): แผนพา "พิมพ์อธิบายงานเป็นภาษาคน → AI ร่าง workflow ให้ → คนรีวิวบน
> canvas" จากสถานะปัจจุบัน (backend มีครบใน Atlas แต่เป็น API-only, ทดสอบสนามจริงแล้ว
> 3 รอบ) ไปจนใช้งานได้จริงใน flow-designer. ลำดับ: **D1 commit งาน hardening ที่ทำแล้ว →
> D2 ปิดช่อง role grounding → D3 ปุ่ม "Draft with AI" ใน flow-designer → D4 ปุ่มช่วยใน
> editor (Explain/Repair/Suggest)**; D5 (`/revise`) + D6 (refine แบบแชท) เป็น backlog
> ที่ต้องยืนยันก่อนสร้าง. กติกาเดิมทั้งหมดคงอยู่: stdlib-only, additive API, one check
> per milestone, commit เมื่อ gate เขียว (ไม่ push), เอกสาร EN/TH ตามโค้ดทุก milestone.
> คู่กับชุด prompt สั่งงาน: [../prompts/ai-draft-authoring-spin-prompts.md](../prompts/ai-draft-authoring-spin-prompts.md).

This plan takes the existing, API-only AI-draft capability to a usable product
feature. It spans TWO repos — `atlas-control-plane` (backend hardening) and
`flow-designer` (all new UI) — and lives here because Atlas is the project hub;
each flow-designer stage must additionally honor that repo's own process docs
(`CLAUDE.md`, `docs/IMPLEMENTATION_PLAN.md` phase-gate style, `docs/CHECKLIST.md`).

---

## 1. Where we are (verified against code + live field test, 2026-08-10)

**Backend exists and is API-only.** Atlas ships `POST /api/workflows/draft`
(plain-language → validated draft), `POST /api/workflows/{id}/repair`,
`/{id}/explain`, `/{id}/suggest-triggers`, and `POST /api/workflows/suggest-workers`
(the last works with no AI worker). The builder is the first worker (name order)
whose role or tags contain `workflow_builder`; it runs through the normal job
pipeline, so BYOK custody stays in thClaws and every draft is audited and metered.
Model output is a proposal only: deterministic validation runs before the API
returns, nothing is auto-saved, and a created workflow defaults to status `draft`
(test-mode only). **No frontend has any UI for these** — verified for both the
slimmed Atlas console and flow-designer (see flow-designer
`docs/guides/web-user-guide-en.md`, written from source).

**Field test log (real thClaws worker `permit`, real model, user's machine):**

| Run | Setup | Result |
|---|---|---|
| 1 | stock context | 400 `workflow human_gate node officer_approval choice requires id` — model guessed `choices` as plain strings; context listed field names only, no nested shapes; no template shows a human_gate |
| 2 | same prompt + hand-written shape hints | **Success, first try** — correct `{"id","label"}` choices, `human_selected` edges, revise loop bounded by `max_iterations 3`, sensible policy, useful model-authored warnings |
| 3 | after context/retry hardening (D1 code) | 400 `workflow node summarizer role has no matching worker: summarizer` — model invented role `summarizer`; instance roles are only `reporter`/`permit` (+ tags). New failure class: **role grounding** |

**Diagnosis.** Every failure so far is the same class: a validation rule that the
builder context never states. Run 1's class (nested shapes for `human_gate.choices`
and per-condition fields) is fixed by the D1 code. Run 3's class remains: the
context lists real workers but never says *"a node `role` set without
`worker_id`/`workspace_id` must match some worker's role or tag
(`_worker_matches_role`: case-insensitive role equality or tag membership), else
omit `role`"*. The bounded retry is a safety net for occasional slips, not a fix
for missing contract text — with the rule absent, both attempts can fail the same
way.

**Built on 2026-08-10, sitting UNCOMMITTED in the working tree** (D1 commits it):

- `atlas/app.py` — `_builder_context` now spells out `choices_item` `{"id","label"}`
  + human-gate edge rules + join quorum rule + per-condition field contracts
  (`condition_types` is now a per-type contract map, not a bare name list);
  `_build_workflow_draft` gained exactly ONE self-repair retry via
  `_attempt_workflow_draft` + `_BuilderReplyError` — retry only for model-output
  failures (non-JSON reply, failed draft validation), never for a missing builder
  or a failed job; a successful retry appends a `Draft needed one self-repair
  retry…` warning.
- `scripts/check_workflow_api.py` — builder stub gained `response_queue` +
  `fail_job`; new assertions: context carries the nested contracts; retry success
  costs exactly +1 call and surfaces the warning; fenced/non-JSON first reply
  retries; persistent failure = 2 calls then the validation 400; job failure = 1
  call, no retry. Mutation-tested (removing `choices_item` or the retry turns the
  check red).
- `docs/specs/api-reference-en.md` / `-th.md` — AI Draft sections describe the
  bounded retry and the enriched context.
- `poc/try_ai_draft.py` (new) — field-test harness: list workers, safe GET-merge
  `--tag-worker`, poll, draft, summary + raw JSON, `--create` to save as status
  `draft` for canvas review.
- Gates already run green in a sandbox (py3.11): `check_workflow_api`,
  `check_workflows`, and the pinned `lint.sh` trio (ruff/bandit/mypy).
- **Atlas must be restarted to load the new `app.py`** before any live retest.

## 2. Sequence and dependencies

```
D1  commit shipped hardening (Atlas)            ← working tree → history; gate green from clean tree
D2  role grounding in builder context (Atlas)   ← closes run-3's failure class; live retest passes
D3  "Draft with AI" dialog (flow-designer)      ← first user-facing surface; needs D2 quality
D4  editor assists (flow-designer)              ← Explain / Repair / Suggest workers / Suggest triggers
--- backlog, confirm demand before building ---
D5  POST /api/workflows/{id}/revise (Atlas)     ← instruction + current definition → new draft
D6  chat-refine panel in the editor (flow-designer) ← needs D5
```

D2 before D3 deliberately: shipping a UI on top of a builder that fails on invented
roles would burn users' model calls on known-bad drafts. D5/D6 are backlog by the
house scope-discipline rule — do not start them from this plan alone.

## 3. Definition of Done per stage

| Stage | Repo | Definition of Done | Check |
|---|---|---|---|
| **D1** | atlas | Two commits, no push: first `docs(plans)` landing this plan + spin prompts + their `docs/README.md` index entries; then the five 2026-08-10 code/docs changes reviewed as one unit with the full completion gate (incl. `check_docs.py`) green **from the current tree**. Live smoke: restart Atlas, rerun `python3 poc/try_ai_draft.py` (original prompt, no hints) — outcome recorded; role-class failure is EXPECTED and tolerated (that is D2), shape-class failure is not. | extended `scripts/check_workflow_api.py` (already written) |
| **D2** | atlas | `_builder_context` additions: (a) `available_roles` — sorted unique lowercase union of every worker's `role` + `tags`; (b) worker/manager `rules`: `role` optional; if set without `worker_id`/`workspace_id` it must be one of `available_roles`, else omit `role` (Atlas auto-routes) or pick a `worker_id` from `workers`; never invent roles or ids. Check extended: `available_roles` reaches the prompt; a first draft with an unknown role → retry prompt carries `has no matching worker` → corrected reply passes (proves the retry covers this class); docs EN/TH one-liner. Live retest: original prompt passes first-try or with one self-repair warning. | extend `scripts/check_workflow_api.py` |
| **D3** | flow-designer | `atlasDraftWorkflow` in `atlas-api.server.ts` (typed, fixed op; timeout sized for up to two model calls) + `draftWorkflowFn` in `atlas-mutations.functions.ts` (session-validated, **no retry** — a retry re-bills the model) + `useDraftWorkflow`. Workflows list gets a "Draft with AI" action beside "New workflow": dialog (precedents: `workflow-pack-import-dialog.tsx`, `workflow-test-run-dialog.tsx`) with prompt textarea → busy state that says drafting may take minutes → result: name, description, explanation, warnings, node/edge/policy summary → "Create as draft & open" (existing `useCreateWorkflow`, omit `status` so Atlas defaults to `draft`, then navigate to the editor exactly like "New workflow") or "Discard". Proposed triggers display-only with a pointer to the Triggers page (never auto-created). Atlas 400 text shown verbatim; `No workflow_builder worker configured` mapped to a setup hint. Design tokens + shared primitives + WCAG AA per `CLAUDE.md`; loading/empty/error/forbidden states explicit. Tests: unit (dialog state/mapping), contract (server fn), `lint`+`typecheck`+`test`+`test:contract`+`scan:bundle` green; `docs/CHECKLIST.md` pass; guides EN/TH + `BACKEND_INTEGRATION.md` updated. | vitest unit + contract projects |
| **D4** | flow-designer | Editor gains: **Explain** (dialog rendering `/{id}/explain`); **Repair** — offered when an Atlas save/validate 400 arrives; sends current graph/policy preview to `/{id}/repair`, result previews and replaces the canvas draft only on explicit accept, never auto-saves; **Suggest workers** — for role-only nodes, renders matched/fallback/unavailable per suggestion, applies `worker_id` per node on click (works without an AI worker); **Suggest triggers** — display suggestions, each created only by explicit user action via the existing trigger mutation. Same test/docs/checklist bar as D3. | extend vitest suites |
| **D5** *(backlog)* | atlas | Additive `POST /api/workflows/{id}/revise` `{instruction, graph?, policy?, triggers?}` → same draft schema; prompt embeds the current (or previewed) definition + instruction; same `_attempt_workflow_draft` bounded retry; same deterministic validation; openapi.yaml + EN/TH docs. Requires explicit human go-ahead first. | extend `scripts/check_workflow_api.py` |
| **D6** *(backlog)* | flow-designer | Editor side panel: conversational refine over D5 — instruction → draft → visual preview/diff against current canvas → apply replaces the unsaved draft state; iterate; never auto-save. Requires D5 + explicit go-ahead. | extend vitest suites |

Every stage also carries the docs deliverable at close-out, per each repo's
documentation policy. "Done" is never code-only.

## 4. Execution contract

1. **Order is D1 → D2 → D3 → D4; stop before D5.** D5/D6 exist so the decision is
   pre-designed, not so they get built by momentum.
2. **Commit per stage, only when green, no push.** Atlas stages: the completion
   gate + `scripts/lint.sh`. flow-designer stages: `lint`, `typecheck`, `test`,
   `test:contract`, `scan:bundle`, `build` (+ e2e where the checklist asks).
   flow-designer is Lovable-connected — never rewrite published history.
3. **House rules hold everywhere.** Atlas: Python stdlib only; additive `/api/*`
   (never change an existing path/shape); one hermetic check per behavior; EN/TH
   docs parity. flow-designer: per its `CLAUDE.md` — server fns validate the
   session and call typed fixed Atlas operations; no `*.server.ts` import from
   client code; Atlas is the only authorization authority; design tokens + shared
   primitives; mutations do not retry.
4. **Builder output stays a proposal.** No stage may auto-save, auto-run, or
   auto-create triggers from model output. A human click stands between every
   draft and every persisted object. (The one sanctioned mutation of model output
   is Atlas appending its own retry warning string.)
5. **Live tests are part of DoD where stated** and need the operational
   prerequisites: a running `thclaws --serve`, a worker tagged `workflow_builder`
   (`python3 poc/try_ai_draft.py --tag-worker <id>` does a safe GET-merge upsert),
   and an Atlas restart after any `atlas/app.py` change. Builder jobs are real
   model calls — metered, BYOK-billed, up to 2 per draft; do not loop live tests
   mindlessly.
6. **Hard stops (ask the human):** an existing `/api/*` path or shape would have
   to change; a runtime dependency seems unavoidable; a flow-designer change would
   touch the Atlas contract, auth boundary, or data ownership; a stage's DoD
   cannot be met as specified.

## 5. Deferred by design (recorded so nobody "helpfully" adds them)

- **Auto-creating drafted triggers** — a drafted `schedule`/`webhook` trigger that
  arms itself is a footgun; display-only until a real need is confirmed.
- **Auto-repair loops beyond one retry** — unbounded self-repair burns BYOK money
  silently; one retry is the agreed ceiling (api-reference documents it).
- **Draft-time streaming/progress events** — the draft call blocks by design
  today (bounded by `workflows.max_wait_seconds`); revisit only if D3 UX proves
  the wait intolerable.
- **suggest-workers auto-apply** — suggestions apply per explicit click, matching
  the proposal principle.
