# AI Draft Contract Hardening — stop the builder from inventing shapes (Atlas + flow-designer)

> **Status 2026-08-12.** Implemented and gate-green: **D2b-1** (trigger contract,
> `dsl_boundary`, F1b vocabulary lock), **D2b-2** (`_normalize_builder_draft`),
> **D2b-3** (fence-tolerant `_json_from_text`), **D2b-5** (flow-designer
> headline/detail split), **D2c-1** (gate wait excluded from `max_minutes`),
> **D2c-2** (approval SLA reminders → outbound delivery). **D2b-4 ran on
> 2026-08-12 and PASSED** — see run 5 in §1. Nothing is outstanding; the one
> residual (the model branching on routing workers instead of on its own bucket
> artifact) is a context-quality note, not a failure. §8.2 was confirmed
> empirically before the fix and is now covered by `scripts/check_workflows.py`.

> TL;DR (ภาษาไทย): field test รอบที่ 4 (prompt อนุมัติจัดซื้อภาษาไทย) ล้มด้วย 400
> `workflow draft trigger at index 0 must be an object` หลังเสีย model call ไป **2 ครั้ง**
> สาเหตุคือคลาสเดิมที่เจอมาแล้ว 3 รอบ: *กฎที่ validator บังคับ แต่ builder context ไม่เคยบอก*
> — `trigger_types` ยังเป็น "รายชื่อ type เปล่า ๆ" แบบเดียวกับที่ `condition_types` เคยเป็นก่อน D1.
> แผนนี้ปิดคลาสนี้ให้จบ ไม่ใช่ปะทีละจุด: **D2b-1 สัญญา trigger + ขอบเขต DSL ใน context →
> D2b-2 normalizer สำหรับ output ของโมเดลเท่านั้น → D2b-3 รับ JSON ที่ห่อ ``` โดยไม่เสีย retry →
> D2b-4 live retest 1 ครั้ง → D2b-5 flow-designer เลิกโชว์ error ดิบ**. สำคัญ: ต่อให้แก้ trigger
> อย่างเดียว prompt จัดซื้อก็ยังพังต่ออีก เพราะ DSL ไม่มี condition เชิงตัวเลข, ไม่มี timer/SLA,
> และ draft schema ไม่รับคีย์ `interface` — D2b-1 จึงต้องบอกขอบเขตพร้อมทางออก (`warnings`) ไปด้วย.
> **แก้ไข 2026-08-12:** ฉบับแรกของแผนนี้นับ "ส่งอีเมล" เป็นข้อจำกัดของ DSL ด้วย ซึ่ง**ผิด** —
> มันเป็น capability ของ worker ไม่ใช่ node type (ดู §2). เหลือข้อจำกัดจริงข้อเดียวคือ **เวลา**
> (reminder/escalation/รอข้ามวัน) และการไล่โค้ดพบว่าตัวบล็อกจริงคือ `max_minutes` นับ wall clock
> ทับเวลารอมนุษย์ ทำให้ human gate ที่นานเกิน 24 ชม. ฆ่า run ทิ้ง — ทางออกอยู่ใน §8.
> กติกาเดิมคงทุกข้อ: stdlib-only, additive API,
> one hermetic check per behavior + mutation test, EN/TH docs parity, commit เมื่อ gate เขียว.
> คู่กับชุด prompt สั่งงาน: [../prompts/ai-draft-contract-hardening-spin-prompts.md](../prompts/ai-draft-contract-hardening-spin-prompts.md).

This plan is a defect-fix continuation of
[ai-draft-authoring-plan.md](ai-draft-authoring-plan.md) (D1–D4 shipped; D5/D6
still backlog). It is numbered **D2b** because it closes the same class D2
addressed — grounding the builder context — not because it is scheduled between
D2 and D3. It executes **now**, after D4, and does not unblock or authorize D5/D6.

Line references are against the tree at `56ae303` (atlas) / `0fa2385`
(flow-designer); re-locate by symbol name if they have drifted.

---

## 1. What happened (field test run 4, 2026-08-11)

**Prompt.** A long, plain-language Thai request: an internal purchase-approval
workflow — requester fields plus a quotation file, completeness check, approval
routed by amount (≤50,000 / >50,000 / >200,000 THB), finance budget check,
procurement + legal vendor review, PO number saved as an artifact, email
notifications, reminders after 2 business days and escalation after 5, and an
audit log on every decision.

**Result.** HTTP 400, body `workflow draft trigger at index 0 must be an object`
— after **two** builder jobs (first attempt plus the bounded self-repair retry).
Two paid model calls, zero usable output, and an internal validator string shown
to the user verbatim.

**Chain.**

1. `_builder_context()` publishes `"trigger_types": ["manual", "schedule", …]`
   (`atlas/app.py:2049`) — a bare list of *type names*. Nothing states that
   `triggers` is a list of **objects**, nor which keys an item may carry. This is
   the identical anti-pattern that produced run 1's `choice requires id`: back
   then `condition_types` was also a bare name list, and D1 upgraded it to a
   per-type contract map. `trigger_types` was left behind.
2. The prompt opens with a natural-language start condition
   ("เริ่มต้นเมื่อพนักงานส่งคำขอจัดซื้อ"). With no shape stated, the model wrote it
   as prose inside the array: `"triggers": ["พนักงานส่งคำขอจัดซื้อ"]`.
3. `_validate_workflow_draft` → `_validate_workflow_draft_triggers`
   (`atlas/app.py:1716-1722`) raises. `_attempt_workflow_draft`
   (`atlas/app.py:1764-1782`) turns that into one bounded retry whose prompt
   carries the raw error text — which restates the *type* requirement but still
   never shows the *shape*. The second reply repeated the mistake, so the
   `ValueError` escaped `_build_workflow_draft` (`atlas/app.py:1757`) as a 400.
4. flow-designer's `describeWorkflowDraftError`
   (`src/lib/workflow-ai-draft.ts:39-53`) passes the Atlas message through
   unchanged, so an end user reads validator jargon.

**The user's own workaround confirms the diagnosis.** Prepending
"สำคัญ: ให้สร้าง trigger เป็น object เท่านั้น…" makes the draft succeed. A prompt
that only works when the human already knows the internal schema is a defect in
the pipeline, not in the prompt. No user should have to type the word *object*.

**Updated field-test log** (extends §1 of the authoring plan):

| Run | Setup | Result |
|---|---|---|
| 1 | stock context | 400 `human_gate node officer_approval choice requires id` — model guessed `choices` as plain strings |
| 2 | run-1 prompt + hand-written shape hints | success, first try |
| 3 | after D1 context/retry hardening | 400 `node summarizer role has no matching worker` — invented role |
| 3b | after D2 role grounding | success; `warnings` shows one self-repair retry caused by a **fenced** first reply (`ai_draft_result.json:80`) |
| **4** | **Thai purchase-approval prompt** | **400 `workflow draft trigger at index 0 must be an object`, 2 builder jobs spent** |
| **5** | **D2b-4: same prompt, after D2b-1/2/3** | **PASS.** 200; 12 nodes / 16 edges; `triggers` a valid object list; amount handled by a `classify_budget` worker; reminders/escalation, the missing email role, and the audit non-requirement all in `warnings`. Residual: the model produced the `budget_tier` bucket artifact and then **branched on two extra routing workers instead of on it** — the classifier half of the rule landed, the `artifact_equals`-on-the-bucket half did not, costing two model calls per run. Candidate for a future context example, not a 400. |

Three of the four failures are one class: **a rule the validator enforces that
the context never states.** Run 3b adds a fourth, cheaper class: a well-formed
draft wrapped in a ```` ```json ```` fence, which costs a full extra model call.

## 2. Why fixing triggers alone is not enough — and what is *not* actually a limit

The purchase prompt asks for five things beyond a bare trigger fix. The first
version of this section called all five "DSL cannot express it". Re-checking each
against the engine showed **three of them are expressible today**; only one is a
genuine engine gap. Getting this line right matters more than the trigger fix
itself: a context that teaches the model a *false* limit is worse than a silent
one, because the model will dump a doable requirement into `warnings` and nobody
will ever see a 400 telling them it was wrong.

**The rule that separates them:** `node_types` is a closed vocabulary, but *what a
`worker` node does* is not. A `worker` node is a dispatch of a prompt to a
thClaws worker; Atlas neither knows nor constrains what that worker can do.
`_public_worker` (`atlas/app.py:1594`) already ships every worker row (minus the
token) into the builder context, and `role_rules` already tells the model to pick
from `available_roles` and never invent. So **any capability gap is a roster
question, not a DSL question** — and the honest warning is "no worker with this
capability is registered", not "the system does not support this".

| Ask in the prompt | Expressible today? | Detail |
|---|---|---|
| "ส่งอีเมลแจ้งผู้ขอ หัวหน้าแผนก ฝ่ายการเงิน" | **✅ yes — this table's original claim was wrong** | a `worker` node whose role has an email tool. Nothing in the validator forbids it. Only fails if no such role is in `available_roles`, which is a roster gap the warning should name |
| "ตรวจสอบ vendor และเงื่อนไขสัญญา" | ✅ yes | an ordinary `worker` node |
| "บันทึกเลข PO เป็น artifact" | ✅ yes, native | `outputs` on the worker node |
| "ถ้าวงเงินไม่เกิน 50,000 / เกิน 200,000" | ✅ yes, indirectly | conditions are exactly `always`, `artifact_equals`, `artifact_in`, `manager_selected`, `human_selected`, `max_iterations_below` — no numeric comparison (`atlas/workflows.py:2477`). A `worker` node classifies the amount into a bucket artifact, then edges branch on it (below). Silence here → model invents `artifact_greater_than` → `unsupported workflow condition` |
| "ทุก decision ต้องมี audit log" | ✅ already done by Atlas | nothing to model; silence → model spends nodes on a non-requirement |
| "ไม่ตอบภายใน 2 วันทำการ ส่ง reminder … 5 วัน escalate" | ❌ **the one real gap** | no timer/deadline/reminder/escalation construct, *and* a gate cannot outlive `policy.max_minutes` at all — see §8. No worker can close this: it is temporal control flow, not a capability |
| "ต้องมีชื่อผู้ขอ แผนก … ไฟล์ใบเสนอราคา" | ❌ schema gap | `_WORKFLOW_DRAFT_FIELDS` (`atlas/app.py:1661`) has no `interface`; model emits it → `workflow draft has unknown field(s): interface`. Feature decision, see §7 |

The durable answer is one rule rather than five patches: **state the closed
vocabularies, say explicitly which closed vocabulary each gap belongs to, give the
model a legal escape hatch, and tell it how to express the cases that ARE
expressible.** Amount-based branching maps cleanly onto today's DSL — a `worker`
node classifies the amount into a named bucket artifact (e.g. `approval_tier` ∈
`le_50k` / `le_200k` / `gt_200k`), then edges branch with `artifact_equals` /
`artifact_in` on that artifact. That is exactly how the engine is designed to
branch; the model just has never been told.

Two different warnings, not one:

- **capability not on the roster** → `warnings` names the missing worker role, so
  the operator's next action is "register a `notifier` worker", which is
  actionable. Never phrase this as an Atlas limitation.
- **temporal requirement** → `warnings` always, no matter what workers exist,
  with the §8 workaround named.

## 3. Fix design

### F1 — trigger contract in the builder context *(D2b-1)*

`_builder_context` (`atlas/app.py:1980-2055`): replace the bare
`"trigger_types"` list with a per-type contract map, mirroring the shape
`condition_types` already uses, and fold in the now-redundant `schedule_configs`
(`atlas/app.py:2050`). Config-key facts come from `_TRIGGER_CONFIG_KEYS`
(`atlas/workflows.py:108-113`); `manual` and `webhook` intentionally have open
configs and must not be described as closed.

Ship alongside it an explicit item example and rule list — a nested example is
what fixed the same class for `human_gate.choices` in D1:

- `trigger_item`: `{"type": "manual", "name": "Employee submits a purchase request", "enabled": false}`
- rules: `triggers` is a list of **objects**, never strings; an item uses only
  `type`, `name`, `config`, `enabled`; if the described start condition does not
  map to a listed type, return `triggers: []` and record it in `warnings`.

Also add one line to the `_builder_prompt` preamble
(`atlas/app.py:1969-1977`) — four rules are read there before any context JSON,
which is the cheapest, highest-leverage position in the whole prompt.

**F1b — lock the context vocabularies to the validator's, or this class returns.**
The context is a hand-maintained *second copy* of vocabularies whose truth lives
in the validator, with nothing coupling them:

| Truth | Context copy |
|---|---|
| `{"worker","manager","join","human_gate"}` literal (`atlas/workflows.py:221`) | `node_types` (`atlas/app.py:2000`) |
| the `_evaluate_condition` if-chain (`atlas/workflows.py:2440-2477`) | `condition_types` (`atlas/app.py:2029`) |
| `_TRIGGER_STATES` / `_TRIGGER_CONFIG_KEYS` (`atlas/workflows.py:108-113`) | `trigger_types` (D2b-1 rewrites it) |

They agree today only because a human kept them in step. `condition_types`
(shipped by D1) already carries this debt; D2b-1 adds a third copy. Add a
seventh condition type or a fifth node type and the context goes stale silently —
the exact failure class this plan exists to close, rediscovered in the field at a
cost of two model calls.

Fix: export the two inline literals as module constants
(`WORKFLOW_NODE_TYPES`, `WORKFLOW_CONDITION_TYPES`) next to the existing
`_TRIGGER_CONFIG_KEYS`, have the validator use them, and add a **set-equality
assertion** (§5 check 1b) between each context vocabulary's keys and its
constant. Prose descriptions of each field can still drift — that is tolerable;
the *vocabulary* is what turns drift into a 400, and after this it cannot drift
without the gate going red. Small change, and it converts "durable because
someone is diligent" into "durable because CI says so".

### F2 — capability boundary + escape hatch *(D2b-1, same commit)*

Add a `dsl_boundary` block to the context stating that `node_types`,
`condition_types`, `trigger_types`, and `artifact_kinds` are **complete
vocabularies**, plus:

- **actions are not a vocabulary.** Every action — send an email, call an API,
  check a vendor, classify a value — is a `worker` node. `node_types` is closed;
  what a worker *does* is not. Choose a `role` from `available_roles`;
- if a required capability matches no role in `available_roles`, do **not** call it
  unsupported: emit a `warnings` entry naming the worker role that would need to
  exist (e.g. "no worker with email capability is registered; add one with role
  `notifier`, then add a worker node before this edge");
- no numeric comparison condition exists — to branch on an amount, add a `worker`
  node that classifies the value into a named bucket artifact, then branch with
  `artifact_equals` / `artifact_in` on it;
- no timer, deadline, reminder, or escalation construct exists, **and no worker can
  substitute for one** — a run cannot outlive `policy.max_minutes` even while
  parked at a `human_gate` (§8). Put every time-based requirement in `warnings`,
  always;
- Atlas already audits every decision (who, when, outcome, reason) — do not model
  audit logging;
- return only the seven top-level keys; never add `interface`, `inputs`, or any
  other key;
- when part of the request cannot be modeled, still return a valid draft for the
  part that can, and list **each** unmodeled requirement as its own `warnings`
  entry.

The capability/vocabulary split is the load-bearing rule here. Without it the
model has to guess whether "send an email" is a missing node type or a missing
worker, and both wrong answers are expensive: inventing an `email` node costs a
400, and refusing into `warnings` when a `notifier` worker exists silently drops a
requirement the engine could have run.

The last rule is the one that turns an entire family of hard 400s into a useful
draft. It is also consistent with the product principle already in force: the
draft is a proposal a human reviews, so an honest partial proposal beats a
refusal.

### F3 — normalize model output, never client input *(D2b-2)*

Three call sites already duplicate the same two-line normalization
(`atlas/app.py:1755-1756`, `1776-1777`, `1807-1808`). Replace them with one
helper, `_normalize_builder_draft(draft)`, applied **before** the first
`_validate_workflow_draft` so a malformed suggestion never costs a model call:

- keep the existing `setdefault("triggers", [])` / `setdefault("warnings", [])`;
- if `triggers` is a list, drop every item that is not a dict and append one
  warning naming the count and quoting each dropped value (JSON-rendered,
  truncated to ~120 chars) so nothing is silently lost —
  e.g. `Ignored 1 trigger suggestion that was not a trigger object:
  "พนักงานส่งคำขอจัดซื้อ". Create triggers on the Triggers page.`;
- only append to `warnings` when it is already a list, so `warnings: "text"`
  still fails validation exactly as today.

**Deliberately NOT normalized — decision record, not an oversight:**

- **`triggers` that is not a list at all** (`None`, a string, a dict) stays a
  validation failure. `scripts/check_workflow_api.py` asserts
  `triggers=None → 400 "triggers must be a list"` as part of schema-equivalence;
  coercing it would weaken an existing hermetic check to buy a case the bounded
  retry already covers. A reply whose `triggers` field is structurally wrong is a
  broken reply, not a malformed suggestion.
- **Converting a dropped string into a `manual` trigger object.** Tempting, and
  safe on the wire (drafted triggers are display-only and never armed), but it
  invents content. Plan §4.4 of the authoring plan permits exactly one sanctioned
  mutation of model output — Atlas appending its own warning. Dropping plus a
  quoting warning stays inside that rule; fabricating a trigger does not.
- **`_validate_workflow_payload` (`atlas/app.py:1653`) is untouched.** On
  `POST /api/workflows` that path validates `triggers` supplied by an API client,
  and client input must keep failing loudly; the normalizer must never reach it. A
  regression check locks this (§5, check 4). (`PUT /api/workflows/{id}` is not a
  second exposure: it builds a filtered `validation_payload` at
  `atlas/app.py:793` that never carries `triggers` at all — see §7.)

### F4 — accept fenced JSON without spending a retry *(D2b-3)*

`_json_from_text` (`atlas/app.py:2072-2080`) does `json.loads` on the stripped
reply. A reply wrapped in ```` ```json … ``` ```` therefore raises
`_BuilderReplyError` and burns a full extra model call — visible in the user's own
`ai_draft_result.json:80`, on a run that otherwise succeeded.

Fix conservatively, in about six stdlib lines: if the stripped text starts with
```` ``` ````, drop that first line and a trailing ```` ``` ```` line, strip, then
parse. **Do not** brace-scan or regex-extract JSON out of arbitrary prose — an
unfenced prose reply must remain a `_BuilderReplyError` so the retry keeps its
job.

Test impact is smaller than it looks. The existing fenced-reply assertion queues
```` ```json\n{"name": "fenced"}\n``` ````, which after stripping parses but fails
draft validation — so it still retries and the assertion stays green. Its comment
becomes inaccurate, and two assertions must be added (§5, check 5). This is a
tightening (fewer paid calls), not a weakening; call it out explicitly in the PR
body because it changes a tested code path.

### F5 — stop leaking validator strings to end users *(D2b-5, flow-designer)*

Detail in flow-designer `docs/AI_DRAFT_ERROR_UX_PLAN.md` (delivered with this
plan). Summary: give `describeWorkflowDraftError` a `message` (plain-language
headline) / `detail` (raw Atlas text) split, and render `detail` inside a
collapsed `Technical details` disclosure in `ActionError`
(`src/components/atlas/workflow-ai-draft-dialog.tsx:33-55`). D3's DoD requires
Atlas's 400 text be shown verbatim — a disclosure keeps it verbatim while getting
it out of the headline.

Classify on the **structured error kind**, not on the message text. A first pass
of this plan proposed a `^workflow ` prefix regex; verification against the source
killed it — real validation strings such as `duplicate node id: …`,
`unsupported workflow condition: …`, and `unknown workflow trigger config key(s)
for …` do not carry that prefix, and `workflow job timed out: …`
(`atlas/workflows.py:1827-1859`) does. `ClientAtlasError.kind` already carries
`"validation"` for every 400/422 (`src/lib/atlas-types.ts:93-103`), 5xx text is
already redacted to a fixed string by `toClientAtlasError`
(`src/lib/atlas-mappers.ts:149`), and timeouts get their own kind — so keying off
`kind === "validation"` is both complete and drift-proof.

## 4. Sequence and Definition of Done

```
D2b-1  trigger contract + DSL boundary in builder context   (atlas)   ← fixes the cause
D2b-2  _normalize_builder_draft for model output only       (atlas)   ← safety net, saves a call
D2b-3  fence-tolerant _json_from_text                       (atlas)   ← independent; droppable
D2b-4  ONE live retest with the purchase prompt             (atlas)   ← needs 1–3 + restart
D2b-5  friendly error headline + technical details          (flow-designer)

D2c-1  exclude gate-wait from max_minutes                   (atlas)   ┐ §8, PROPOSED —
D2c-2  age pending approvals -> outbound delivery           (atlas)   ┘ needs a go-ahead
```

D2c is listed for context only. It is not part of this plan's gate, must not be
folded into a D2b commit, and does not block D2b-4's live retest.

| Stage | Definition of Done | Check |
|---|---|---|
| **D2b-1** | `trigger_types` is a per-type contract map (config keys from `_TRIGGER_CONFIG_KEYS`; `manual`/`webhook` documented as open), `schedule_configs` folded in and removed, `trigger_item` example present, trigger rules present, `dsl_boundary` present with all eight rules of §F2 **including the capability-vs-vocabulary split**, `WORKFLOW_NODE_TYPES` / `WORKFLOW_CONDITION_TYPES` exported and used by both validator and context (§F1b), one added line in the `_builder_prompt` preamble. api-reference EN + TH updated. No `/api/*` path or response shape changed, so `openapi.yaml` needs no edit — state that in the PR body rather than inventing one. | extend `scripts/check_workflow_api.py` (§5 checks 1, 1b) |
| **D2b-2** | `_normalize_builder_draft` exists, is the only normalization point, is called before every `_validate_workflow_draft` on a builder reply (all three sites), drops non-dict trigger items with a quoting warning, leaves non-list `triggers` to the retry, and is never reachable from `_validate_workflow_payload`. api-reference EN + TH updated. | §5 checks 2, 3, 4 |
| **D2b-3** | `_json_from_text` parses a fenced reply on the first call; unfenced prose still raises `_BuilderReplyError`. Existing fenced assertion's comment corrected. api-reference EN + TH note it. Separate commit so it can be dropped without unpicking D2b-1/2. | §5 check 5 |
| **D2b-4** | Atlas restarted, **one** live run of `poc/try_ai_draft.py` with the verbatim purchase prompt, outcome recorded in §1's table. Pass = 200 with (a) amount branching via a classifier artifact, (b) `triggers: []` or valid trigger objects, (c) reminders/escalation/email present in `warnings`. Fail = record the raw error and the reply, add the class to the table, **stop** for a human decision. | live; no automation |
| **D2b-5** | `describeWorkflowDraftError` returns `{message, detail?, forbidden, needsBuilderSetup}`; dialog shows the headline and the raw Atlas text in a disclosure; `needsBuilderSetup` and `forbidden` behavior unchanged. `lint`, `typecheck`, `test`, `test:contract`, `scan:bundle`, `build` green; `docs/CHECKLIST.md` pass; guides EN/TH touched where they quote the error copy. | extend `tests/unit/workflow-ai-draft.test.ts` + a dialog test |

Each Atlas stage is its own commit with its own hermetic check, gated on
`./scripts/gate.sh` + `./scripts/lint.sh` green from a clean tree. No push unless
the human asks.

## 5. Checks to add (`scripts/check_workflow_api.py`)

All five go inside the existing builder-stub block (the one with `response`,
`response_queue`, `prompts`, `fail_job`). Every one must be mutation-tested:
break the code it covers and confirm the gate goes red.

1. **Context assertions.** `prompts[-1]` contains the trigger-shape fragments
   (`trigger_item`, `list of OBJECTS` or equivalent literal, `type, name, config,
   enabled`) and the boundary fragments (`never invent`, `no numeric comparison`,
   `classifies`, `warnings`, `never add interface`, and the capability rule —
   `available_roles` named inside `dsl_boundary`). Parse `Context JSON:` the way
   the existing role-grounding assertion does when checking structure rather than
   substrings.

1b. **Vocabulary set-equality (§F1b).** From the parsed `Context JSON:`, assert
   `set(context["node_types"]) == WORKFLOW_NODE_TYPES`,
   `set(context["condition_types"]) == WORKFLOW_CONDITION_TYPES`, and
   `set(context["trigger_types"]) == set(_TRIGGER_STATES)`. Substring assertions
   cannot catch the drift this exists for; only set equality can. Mutation test it
   by adding a fake type to the constant and confirming the gate goes red.
2. **Normalizer, and the money assertion.** Builder replies with
   `triggers=["พนักงานส่งคำขอจัดซื้อ", {"type": "manual"}]` → HTTP 200;
   `draft["triggers"] == [{"type": "manual"}]`; some warning contains
   `Ignored 1 trigger suggestion`; and `len(prompts) == calls_before + 1` —
   **no retry spent**. That last assertion is the one that protects the user's
   model budget; do not omit it.
3. **Boundary preserved.** The existing `triggers=None → 400 "triggers must be a
   list"` assertion stays exactly as-is and green.
4. **No leak into client input.** `POST /api/workflows` with
   `triggers: ["x"]` still returns 400 — full text
   `workflow draft trigger at index 0 must be an object`. This is the regression
   lock for §F3's third decision. (`PUT` is not a valid site for this assertion:
   it never validates `triggers` — §7.)
5. **Fence handling.** A fenced *valid* draft → `len(prompts) == calls_before + 1`
   and no `self-repair` warning. A prose reply
   (`Here is your workflow: ...`) → still `calls_before + 2`. Correct the stale
   comment above the existing fenced assertion.

## 6. Live-test discipline (this costs real money)

Builder jobs are real, metered, BYOK-billed model calls — up to two per draft.

- Restart Atlas after any `atlas/app.py` change, or the live test measures the old
  code (this bit the team during D1).
- Run the purchase prompt **once** per code state. Record the verbatim outcome.
- Do not loop live runs to "see if it passes this time." If a new failure class
  appears, it is a new row in §1's table and a decision for the human, not an
  invitation to keep spending.
- Hermetic checks are free and prove the mechanics; live runs only prove model
  behavior. Get the gate green first.

## 7. Adjacent gaps found while diagnosing — recorded, not scheduled

- **`interface` is not a draft field.** A prompt that describes required input
  fields (which the purchase prompt does, in detail) has nowhere structured to put
  them. D2b-1 tells the model not to emit `interface`; whether drafts *should*
  carry an interface is a feature decision, not a bug fix. Needs a human
  go-ahead before anyone builds it.
- **No SLA / reminder / escalation primitive.** The single most common ask in
  real approval workflows, and today it is warning text. Designed out in **§8**;
  the two proposed stages there need a human go-ahead before anyone builds them.
- **`PUT /api/workflows/{id}` silently ignores `triggers`.** The handler builds a
  filtered `validation_payload = {"graph": …, "policy": …}` (`atlas/app.py:793`)
  that never includes `triggers`, and `workflow_definitions` has no `triggers`
  column — so a client `PUT` carrying malformed triggers is neither validated nor
  persisted, it just vanishes. Harmless today (triggers live in their own table
  and are created explicitly), but it is an undocumented asymmetry with `POST`.
  Worth a line in `docs/specs/backlog.md`, not a fix in this plan.
- **Trigger items accept unknown top-level keys at draft time.**
  `validate_workflow_trigger_payload` (`atlas/workflows.py:2054-2070`) checks only
  `type` and `config`, so a drafted trigger carrying e.g. `description` passes
  draft validation and would be rejected later at trigger-create time. Low impact
  while triggers are display-only; note it in `docs/specs/backlog.md`.
- **`describeWorkflowDraftError` had no test for validation-class errors** before
  D2b-5 — the class that actually reached users was the untested one.

## 8. Reminder / escalation — design (proposed, needs a go-ahead)

The one requirement in §2 that no worker can absorb. Written out here because
"put it in `warnings`" is the right D2b-1 behavior but not an answer, and because
tracing it surfaced a defect larger than the reminder itself.

### 8.1 The requirement splits into three, and only one is missing

| # | Capability | Status today |
|---|---|---|
| 1 | a run **survives** a multi-day wait at a gate | ❌ **broken — see 8.2** |
| 2 | something **notices** a pending approval has aged | ✅ available: `GET /api/approvals?state=pending` (`atlas/app.py:1013-1017`), rows carry `created_at` (`atlas/db.py:1668-1691`) |
| 3 | something **acts** — remind, or escalate to the next manager | ✅ needs no engine concept: escalation is notifying a *different* person who also holds `approvals.decide`; the pending approval is the same row, and any permitted identity can decide it |

That reframes the work. This is not "build an SLA engine". Detection and action
already exist. **Only the waiting is broken**, and it is broken in a way that has
nothing to do with reminders.

### 8.2 The actual blocker: `max_minutes` bills human think-time as compute

`_workflow_deadline` (`atlas/workflows.py:2327-2332`) is
`started_at + max_minutes` — plain wall clock, with no notion of time parked at a
gate. `_check_deadline` runs inside the stepping loop
(`atlas/workflows.py:1132`, `1218`, `1240`, `1508`), including the steps that run
when `_continue_human_gate_decision` (`atlas/workflows.py:836`) resumes a run
after a human answers. Default `max_minutes` is 30 (`atlas/app.py:114`) and the
ceiling is 1440 (`atlas/workflows.py:344`).

**Consequence: an approval answered more than `max_minutes` after the run started
kills the run** with `workflow policy max_minutes exceeded`. The longest human
gate expressible in Atlas today is 24 hours. "2 business days" is not merely
un-remindable — it is un-waitable, and so is most of the real approval work this
product exists for.

**CONFIRMED empirically, 2026-08-12, against unmodified `56ae303`.** A gate
workflow with `max_minutes: 60`, parked at the gate, run backdated past the
deadline (the same trick `scripts/check_workflows.py:605-620` uses), then
approved:

```
state after approval : failed
error                : workflow policy max_minutes exceeded
jobs dispatched      : []
approval state       : approved
```

Note the last two lines — that is the part worth staring at. **Atlas records the
approval as `approved`, then fails the run, and the node the approval was
authorizing never dispatches.** The user's experience is "I approved the purchase
and the system threw it away." There is no error at approval time, no warning at
draft time, and nothing in the UI that would predict it. Any Atlas approval
workflow whose approver takes longer than `max_minutes` — default **30 minutes**
— already behaves this way today.

Reproduction lives in this plan's PR as `scripts/check_workflows.py` coverage
written **as part of D2c-1**, asserting the *fixed* behavior. Do not land a check
that asserts the current behavior; that would lock the defect in.

### 8.3 Who is allowed to notice? The auth direction decides the design

Before picking an option: **which side polls?** The current trust direction is
one-way and narrow, and reading it settles most of the design.

- **Atlas → thClaws** is the credentialed direction. Atlas stores each worker's
  token and dials out: `ThClawsClient(worker["base_url"], worker.get("token"))`
  (`atlas/app.py:637`).
- **thClaws → Atlas** has *no general API credential at all*. `ROLE_PERMISSIONS`
  (`atlas/app.py:138-143`) defines `admin` / `operator` / `viewer` / `auditor` —
  all human identities; there is no worker role. The only inbound path a worker
  has is a **per-job callback bearer**, which authorizes posting the result of
  the one job it was handed (`apply_worker_callback`, `atlas/jobs.py:950`).
- **Atlas already has an outbound channel**: the OB-1 delivery ledger
  (`atlas/outbound.py`, `atlas/db.py:550`) with an env allowlist, HMAC body
  signing (`sign_delivery_body`), retry, and a `deliveries` UI surface.

So an earlier draft of this section — "a sweeper worker calls
`GET /api/approvals?state=pending`" — would have minted a **new class of
credential**: a long-lived operator-grade Atlas token living on a worker host,
able to read the whole approvals queue and (one scope away) decide approvals. That
is not a scope tweak on something thClaws already holds; thClaws holds nothing of
the kind. Reject it.

**The correct direction is the one that already exists: Atlas notices, Atlas
delivers outward.** Atlas alone knows `approvals.created_at`; it already runs a
periodic scheduler for triggers (`next_fire_at`); it already has a signed,
retrying, audited outbound path. Reminders need no new inbound surface, no token
on thClaws, and no worker at all.

### 8.4 Options

| | Option | Solves | Cost | Verdict |
|---|---|---|---|---|
| **B** | **Stop the clock at a gate** — exclude time parked at a `human_gate` from the `max_minutes` deadline | 1 | small and local; no schema migration | **Do this first.** It is the enabler; every other option is theatre without it, and it fixes a live defect that exists with or without reminders |
| **E** | **Atlas-internal SLA sweep → outbound delivery** — the scheduler tick also ages pending approvals and emits a delivery event; the receiver (thClaws, email relay, LINE bridge) decides what to do with it | 2, 3 | moderate, and entirely inside the existing trust direction; reuses allowlist + HMAC + retry ledger | **The right shape.** No new credential, no new node type, no DSL change for the basic reminder |
| **C** | **Native per-gate SLA in the DSL** — `human_gate.reminder_after` / `escalate_after` / `escalate_to` | 1, 2, 3 | large: DSL, graph schema, pack export/import, flow-designer editor, docs | Only needed once thresholds must be **per gate**. E with one global threshold covers the common case; do C when a real workflow needs two different SLAs. **Not** D5/D6 — those are `/revise` + chat-refine and have nothing to do with this |
| **A** | **Sweeper as an Atlas workflow** whose worker polls `GET /api/approvals` | 2, 3 | needs the new inbound credential of §8.3 | **Reject** on the auth grounds above |
| **A′** | **Sweeper as a thClaws `schedule`** (`thclaws schedule add … --cron`) polling Atlas | 2, 3 | same credential problem, plus invisibility | **Reject as the answer**; see 8.6 for its one legitimate use |
| **D** | **Let the builder emit a two-workflow topology** for this | — | — | **Reject.** A model inventing cross-workflow topology is exactly the guessing that produced this plan's bug class |

### 8.5 Recommendation: B now, E next, C only when a real workflow needs it

**D2c-1 — exclude gate-wait from `max_minutes`.** `max_minutes` is a
runaway-*automation* guard; its siblings (`max_jobs`, `max_iterations`,
`max_attempts_per_node`, `max_budget_units`) all bound compute. A run parked at a
gate consumes nothing. Charging human think-time to a compute budget conflates
two unrelated things, which is why the symptom looks like a policy limit but is
really a bug.

Lazy implementation, no migration — `counters` is already a JSON blob on the run:

- when `_continue_human_gate_decision` resumes, add
  `now - approval["created_at"]` to `counters["human_wait_seconds"]`;
- `_workflow_deadline` adds `timedelta(seconds=counters.get("human_wait_seconds", 0))`.

Two things to state rather than hide:

- **Concurrent gates over-credit.** Two branches waiting at once contribute their
  waits twice, so the deadline stretches further than wall-clock idleness
  justifies. Deliberate: the error direction is leniency, never killing a run
  early, and compute stays bounded by the other four policy keys. If it ever
  matters, union the wait windows instead of summing them.
- **Runs can now live indefinitely.** Already true in practice — a gate nobody
  answers sits pending forever regardless. If a lifetime cap is wanted it belongs
  in its own policy key (`max_pending_days`), as its own decision. Do not smuggle
  it into this change.

**D2c-2 — age pending approvals in the scheduler tick, emit an outbound
delivery.** *(SHIPPED 2026-08-12; the §8.5.1 decisions were answered — per-workflow
routing over a global default, multi-level thresholds, no `decide_url`.)* No new
node type, no DSL change, no inbound credential:

- the periodic scheduler that already advances trigger `next_fire_at` also scans
  `approvals` where `state = 'pending'` and `created_at` is older than a
  threshold;
- first crossing emits an `approval_overdue` delivery through the existing OB-1
  path (allowlist + HMAC + retry + `deliveries` ledger), carrying approval id, run
  id, workflow name, label, and age;
- record what was emitted on the approval row so a reminder fires once per
  threshold rather than every tick — the single most important detail, because an
  SLA sweeper that re-notifies hourly gets muted by its recipients within a day
  and then the whole feature is decorative;
- **escalation is a receiver concern, not an Atlas concern.** Atlas states the
  fact ("this approval is 5 days old"); who gets told at which age is routing, and
  routing belongs to whatever consumes the delivery.

Threshold: start with one global value (policy key or env), not per-gate. Business
days, holidays, and time zones stay **out of Atlas** — the receiver applies the
calendar it knows. Atlas emitting a plain age in hours is a fact; Atlas deciding
what "2 business days" means for a Thai public holiday is a calendar it has no
business owning.

#### 8.5.1 The contract decisions, and how they were answered

*(Resolved 2026-08-12. Kept because the reasoning is the contract's rationale.)*

The outbound ledger turned out to be **run-completion-shaped**, not event-shaped.
`deliver_run` / `deliver_run_completion` (`atlas/outbound.py:304-350`) both
require `run["state"] in {"succeeded","failed"}` — a run parked at a gate is
neither — the delivery id is one-per-run (`_completion_delivery_id`), the row is
keyed by `run_id`, and the body is built from the finished run. An
`approval_overdue` event needs a different id (one per approval **per threshold
crossing**, or every tick re-notifies), a different body, and a URL that is not
necessarily the requester's `_meta.reply.callback_url`.

So D2c-2 is not a small reuse of an existing path — it defines a **new outbound
contract**, and three things about it are product decisions rather than
engineering ones:

1. **Where does a reminder go?** The requester's `_meta.reply.callback_url` is
   the only address Atlas holds today, and it is the *requester's* channel — the
   wrong place to nudge an *approver*. The alternative is a new operator-level
   webhook target (env or per-workflow), which is a new configuration surface.
2. **What is in the body?** Once a receiver parses it, it is a contract; getting
   it wrong is a breaking change later.
3. **What is the threshold, and whose clock?** One global env value is the lazy
   start; per-workflow or per-gate is option C.

Everything upstream of that — the sweep, the age computation, the once-per-
threshold record — is mechanical and cheap. The contract is the part that needed
a human answer, per this repo's own rule that an ambiguity touching an Atlas
contract stops and asks.

**Answers, as built:**

1. **Where.** `policy.approval_webhook_url` on the workflow definition, falling
   back to `ATLAS_APPROVAL_WEBHOOK_URL`. Per-workflow won over a single global
   because several departments share one Atlas and each should route its own
   reminders without an ops change — and the earlier cost estimate for
   "per-workflow" was wrong: `validate_workflow_policy` is an OPEN vocabulary, so
   a policy key costs two validation branches, no migration, no API change, and it
   rides the existing policy snapshot and pack export. It is safe because
   `resolve_outbound_target` still checks the operator's allowlist at send time:
   an author picks among approved hosts, never an arbitrary one. The **definition's
   current** policy is read rather than the run's `policy_snapshot`, so fixing a
   wrong URL revives approvals that are already waiting.
2. **What.** A self-sufficient body (workflow name, gate label, reason, choices,
   age, level, threshold) so the receiver never has to call Atlas back — needing a
   callback would put a long-lived read credential on a worker host, which is the
   thing §8.3 exists to avoid. `decide_url` was dropped: it would have required
   Atlas to learn the flow-designer base URL, and a receiver can compose the link
   from the approval id.
3. **Threshold.** A **list**, not a single value: Atlas must persist "already
   notified" either way, and storing an `int` level instead of a `bool` costs the
   same code while supporting remind-then-escalate. Business days stay out of
   Atlas — thresholds are wall-clock hours chosen with a weekend margin, and the
   receiver decides the exact send moment from `age_hours`.

### 8.6 Where thClaws's own scheduler fits

thClaws already has cron / `--in` / `--at` / `--watch` / heartbeat scheduling.
Useful, but it is the *receiving* side of this design, not the polling side:

- **Legitimate:** the receiver of an `approval_overdue` delivery is a thClaws
  agent that decides how to notify and to whom. `--resume-session` even gives it
  memory of who it already nudged.
- **Legitimate as a stopgap:** before D2c-2 exists, a `thclaws schedule add
  approval-sla --cron "0 * * * *"` job can do the sweep, **if and only if** the
  credential question of §8.3 is answered first — it still has to read Atlas
  somehow. Its cost is invisibility: no Atlas run history, no audit trail, no UI,
  and if the daemon stops nobody finds out until an approval is missed. Acceptable
  for a pilot that proves the notification copy and routing; not acceptable as the
  mechanism people rely on.
- **Not legitimate:** as the permanent home of the SLA rule. An SLA that lives on
  one laptop's crontab, invisible to the control plane that owns the approvals, is
  the kind of thing that works until the day it matters.

### 8.7 What D5 / D6 have to do about all this

Almost nothing, and that is worth stating explicitly because "D5/D6 territory"
appeared in an earlier draft of this section and was **wrong**. D5 is
`POST /api/workflows/{id}/revise` and D6 is the editor's chat-refine panel
(`ai-draft-authoring-plan.md:85-86, 101-102`) — draft authoring, not engine
semantics. D2c changes neither the draft schema nor the endpoint surface, so it
does not block, unblock, or reshape them.

Two real couplings, both cheap and both belonging to D5/D6 rather than here:

1. **D5 inherits D2b-1 for free.** `/revise` reuses `_builder_context` and the
   same deterministic validation, so the trigger contract, the capability rule,
   and the `dsl_boundary` all apply to revision prompts the moment D2b-1 lands.
   Nothing to re-implement; do not duplicate the boundary text into the revise
   prompt.
2. **`warnings` becomes load-bearing, so D6 must render it.** After D2b-1 the
   normal outcome for an ambitious prompt is a *partial* draft plus warnings, not
   a 400. A refine panel that shows the graph and hides the warnings would present
   an incomplete workflow as a complete one. D6's DoD should require warnings to
   be visible in the panel, and `/revise` should carry unresolved warnings forward
   rather than silently dropping them when the model returns a new draft.

**Sequence.** D2c-1 and D2c-2 are **proposed, not scheduled**. They are
independent of D2b-1…D2b-5 and must not be folded into them: D2b closes a
builder-context defect, D2c changes engine semantics, and mixing the two would
put an engine change behind a live model test. D2b-1's `warnings` copy is correct
whether or not D2c ever ships.

**One caveat on ordering that outranks this plan.** §8.2 is a live defect on its
own: approvals that take longer than 30 minutes are being recorded and then
discarded, in production, today, with no reminder feature anywhere in sight. That
is a bigger user-visible problem than the draft-context defect this whole plan was
written for. If only one thing gets built next, it should be D2c-1.
