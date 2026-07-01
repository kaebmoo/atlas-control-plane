# PoC — Permit Application web form → Atlas

A minimal, **stdlib-only** proof of concept for the
[Input Adapter Contract](../../docs/specs/input-adapter-contract.md): a web form
(a thin *adapter*) sends a permit-application request into Atlas, which runs a
governed workflow and streams the result back to the same page by **polling** —
no LINE OA, no ngrok, no OB-1 webhook/allowlist. Everything runs on localhost.

```
browser form ── /api/submit ──▶ Atlas  POST /api/workflow-runs   (envelope + _meta.source)
browser poll ── /api/status ──▶ Atlas  GET  /api/workflow-runs/{id} (+ artifacts, approval)
approver     ── /api/decide ──▶ Atlas  POST /api/approvals/{id}/approve|reject
```

Workflow: `intake` (worker) → `brief` (worker) → `approval` (**human gate**) →
`notice` (worker). The operator token stays server-side; the browser never sees it.

## Files

| File | What it is |
| --- | --- |
| `mock_worker.py` | Minimal thClaws-compatible worker (canned Thai text per `STEP=` marker) so you need no real thClaws. |
| `setup.py` | Registers the worker and creates/updates the `PoC Permit Application` workflow in a running Atlas. Idempotent by name. |
| `app.py` | The web PoC: serves the form and proxies submit / status / decide to Atlas. |

## Requirements

- **Python 3.11+** (Atlas itself imports `datetime.UTC`). The PoC scripts are 3.9+,
  but Atlas must run on 3.11+.
- The Atlas repo (this repo). No pip installs anywhere — stdlib only.

## Quick start (4 terminals, loopback no-auth — fastest)

```bash
# 1) Atlas (dev auth bypass on loopback; separate DB so you don't touch your real one)
cd <repo root>
ATLAS_LOOPBACK_NO_AUTH=true ATLAS_DB=./data/poc.sqlite \
  python3 -m atlas --host 127.0.0.1 --port 8787

# 2) mock worker
python3 poc/permit_web/mock_worker.py 4399

# 3) one-time setup (registers worker + creates workflow)
python3 poc/permit_web/setup.py

# 4) the PoC web app
python3 poc/permit_web/app.py
```

Open **http://127.0.0.1:8080**, submit the form, watch the run go
`running → waiting_for_human`, click **อนุมัติ (approve)**, and it finishes
`succeeded` with the drafted notice. Clicking **ปฏิเสธ (reject)** fails the run at
the gate (as designed).

## Create it manually in the Atlas web dashboard

`setup.py` does all of this for you through the API. If you'd rather build it by hand in
the dashboard (to learn the pieces), here is exactly what to enter and what each value
means. A machine-readable copy of the name/graph/policy/sample-input is in
[`workflow.dashboard.json`](workflow.dashboard.json).

> **Order matters:** register the worker **first**, then save the workflow. Saving a
> workflow whose node `role` has no matching worker fails with
> `workflow node intake role no matching worker: permit`.

### Step 1 — Add a worker (Fleet view)

Fleet → **Add worker**:

| Field | PoC value | Meaning |
| --- | --- | --- |
| Name | `permit-mock` | any label |
| Base URL | `http://127.0.0.1:4399` | the mock worker (or real thClaws `http://127.0.0.1:4317`) |
| Token | `mock-token` | the worker's `THCLAWS_API_TOKEN` (the mock accepts anything) |
| Role | `permit` | **must match** the `role` used in the workflow nodes below |
| Tags | `poc,permit` | optional; a tag `permit` also satisfies the role match |

Save → Atlas polls it. It must reach **online** to run a job (start the mock first:
`python3 poc/permit_web/mock_worker.py 4399`). A worker matches a node's `role` when its
**Role field equals the role, OR the role is one of its Tags** (case-insensitive).

### Step 2 — Create the workflow (Workflows view)

Workflows → **New**. There are separate boxes — put each JSON in the matching box.

**Name box:**

```
PoC Permit Application
```

**Graph box** (the `{start, nodes, edges}` object only):

```json
{
  "start": "intake",
  "nodes": [
    {"id": "intake", "type": "worker", "role": "permit",
     "prompt": "STEP=intake\nตรวจความครบถ้วนของคำขออนุญาตต่อไปนี้ และระบุสิ่งที่ขาด:\nผู้ขอ: {input.applicant_name}\nประเภทคำขอ: {input.permit_type}\nรายละเอียด: {input.detail}\nเอกสารแนบ: {input.attachments}",
     "outputs": ["review"]},
    {"id": "brief", "type": "worker", "role": "permit",
     "prompt": "STEP=summary\nจากผลตรวจต่อไปนี้ เขียนบันทึกสรุปเสนอผู้พิจารณาพร้อมข้อเสนอแนะ:\n{artifact.review}",
     "outputs": ["brief"]},
    {"id": "approval", "type": "human_gate",
     "label": "อนุมัติคำขออนุญาต", "reason": "ตรวจบันทึกสรุปก่อนตัดสินใจอนุมัติหรือปฏิเสธ"},
    {"id": "notice", "type": "worker", "role": "permit",
     "prompt": "STEP=notice\nร่างหนังสือแจ้งผลการอนุมัติตามบันทึกสรุปนี้:\n{artifact.brief}",
     "outputs": ["notice"]}
  ],
  "edges": [
    {"from": "intake", "to": "brief", "condition": {"type": "always"}},
    {"from": "brief", "to": "approval", "condition": {"type": "always"}},
    {"from": "approval", "to": "notice", "condition": {"type": "always"}}
  ]
}
```

**Policy box:**

```json
{"max_jobs": 10, "max_iterations": 3, "max_attempts_per_node": 2, "max_budget_units": 10}
```

Click **Save** (optionally **Validate** first). You get a `wfd_…` in `draft` status —
draft workflows run fine.

### Step 3 — Run it

Open the workflow → paste this into the **run Input** box → **Run**:

```json
{
  "applicant_name": "นายทดสอบ ระบบ",
  "national_id": "1234567890123",
  "permit_type": "ขออนุญาตก่อสร้าง",
  "detail": "ขออนุญาตก่อสร้างอาคารพาณิชย์ 2 ชั้น บนที่ดินของตนเอง",
  "attachments": "สำเนาบัตรประชาชน, โฉนดที่ดิน, แบบแปลน",
  "_meta": {"source": {"channel": "web_form", "adapter": "manual-dashboard", "form": "permit_request", "external_id": "manual-001"}}
}
```

Watch `running → waiting_for_human`; open **Monitor**, **Approve** → `succeeded` with the
`notice` artifact (Reject → run fails at the gate).

### Field reference (what each value does)

**Worker node** (`intake`, `brief`, `notice`):

- `id` — unique node id, referenced by edges.
- `type` — `worker` (runs a job on a worker), `human_gate`, `join`, or `manager`.
- `role` — routes to any online worker advertising this role. Alternative: `worker_id`
  (an exact `wrk_…`) — set that **instead of** `role` and the save-time role check is skipped.
- `prompt` — the instruction. `{input.X}` pulls a submitted field; `{artifact.KEY}` pulls a
  prior node's output. (The leading `STEP=…` line only helps the mock choose canned text;
  a real model ignores it.)
- `outputs` — names the artifact this node writes (the **first** key is used). Omit it and
  the next node's `{artifact.KEY}` has nothing to read. Add `"output_format": "json"` to
  parse the answer as JSON (then conditions/prompts can use dot-paths).

**Human gate** (`approval`): `label` + `reason` are shown to the approver. Approve resumes
the outgoing edges; Reject fails the run. For a multi-choice gate add
`"choices": [{"id","label"}]` and use `human_selected` edges.

**Edges** — `{from, to, condition}`. Here `condition.type` is `always`; other types:
`artifact_equals`, `artifact_in`, `human_selected`, `manager_selected`,
`max_iterations_below`. Two edges out of one node are OR (independent), not AND.

**Policy keys** used here:

- `max_jobs` — max worker jobs per run.
- `max_iterations` — max total node executions (loop guard).
- `max_attempts_per_node` — retries allowed per node.
- `max_budget_units` — total abstract cost cap (each worker node costs `budget_units`,
  default `1`). This is **not** money or tokens.

Also available: `max_minutes`, `requires_human_after_iterations`, `allowed_worker_ids`,
`allowed_workspace_ids`, `stop_on_first_failure` (default `true`).

**Run input (the envelope):** business fields at the top level (read via `{input.*}`) plus
the reserved `_meta.source` for provenance. `channel` must be one of
`line, email, web_form, api, schedule, other` (an unknown channel is rejected pre-run).

### Common dashboard errors

| Message | Cause / fix |
| --- | --- |
| `… role no matching worker: permit` | No worker with role/tag `permit` exists yet → add it in Fleet **first** (Step 1). |
| run stuck in `running`, never finishes | The matching worker is **offline** → start the mock / thClaws and confirm it shows online. |
| `missing prompt variable {artifact.review}` | A prior node didn't declare `outputs` (so no artifact) → add `outputs` to that node. |
| `unknown prompt variable …` | `{input.X}` names a field you didn't include in the run input. |

The dashboard version above uses `role: "permit"` (any online permit worker can serve it);
`setup.py` instead pins each node to a specific `worker_id`. Use one or the other — don't
keep two workflows with the same name.

## With a real API token (closer to production)

Start Atlas normally (no bypass), create an operator token, then point the PoC at it:

```bash
# Atlas prints the token once:
python3 -m atlas.admin create-admin admin
# start Atlas without the loopback bypass, behind your usual proxy/VPN, then:

export ATLAS_TOKEN='<operator-or-admin-token>'
python3 poc/permit_web/setup.py      # uses ATLAS_TOKEN
ATLAS_TOKEN="$ATLAS_TOKEN" python3 poc/permit_web/app.py
```

## Swap in a real thClaws worker (instead of the mock)

The only difference from the mock is that a real worker actually calls a model, so it
needs a **model key (BYOK)** — Atlas never holds it; thClaws does.

**1. Start thClaws as a server, with its provider key in the environment:**

```bash
cd <thClaws repo>
# BYOK: give thClaws the key for whatever model it uses (example: Anthropic)
ANTHROPIC_API_KEY='sk-…' THCLAWS_API_TOKEN='dev-token-1' \
  thclaws --serve --bind 127.0.0.1 --port 4317
# (or, if not installed: cargo run --features gui --bin thclaws -- \
#     --serve --bind 127.0.0.1 --port 4317)

# sanity check:
curl http://127.0.0.1:4317/healthz
```

**2. Point the PoC at it and re-run setup** (`setup.py` upserts the worker by URL, so this
repoints the workflow to thClaws automatically — the mock worker can stay registered):

```bash
MOCK_WORKER_URL='http://127.0.0.1:4317' MOCK_WORKER_TOKEN='dev-token-1' \
  python3 poc/permit_web/setup.py
# expect: worker permit-mock (wrk_…) -> http://127.0.0.1:4317  [status: online]
```

If it prints `[status: online]`, restart `app.py` (or just submit again) and the same
form now runs against the real model.

**Notes for the real worker:**

- The node prompts begin with a harmless `STEP=intake|summary|notice` label followed by
  the real instruction; a real model simply answers the instruction (the label is ignored).
  You will get genuine Thai text instead of the mock's canned blocks.
- No workspace is required for these text tasks — thClaws runs in its `--serve` directory.
  If a task needs project files, add a workspace in Atlas and set `workspace_id` on the node.
- Alternative to putting the key in thClaws's shell env: use Atlas's write-only injector
  `python3 -m atlas.byok …` (see [BYOK Key Injection](../../docs/specs/byok-key-injection.md)) —
  it writes the key into the worker's env/config and audits it, without Atlas ever storing it.
- If the worker shows offline: check the token matches `THCLAWS_API_TOKEN`, the port is right,
  and Atlas can reach the host; then re-run `setup.py` to re-poll.

## Configuration (env)

| Var | Default | Used by |
| --- | --- | --- |
| `ATLAS_BASE` | `http://127.0.0.1:8787` | setup, app |
| `ATLAS_TOKEN` | *(empty)* | setup, app |
| `MOCK_WORKER_URL` | `http://127.0.0.1:4399` | setup |
| `MOCK_WORKER_TOKEN` | `mock-token` | setup |
| `WORKFLOW_NAME` | `PoC Permit Application` | setup, app |
| `WORKFLOW_ID` | *(auto-discovered by name)* | app |
| `PORT` | `8080` | app |

## What it demonstrates

- **IA-1 in action:** the form posts the envelope with `_meta.source.channel="web_form"`;
  Atlas records that provenance in the audit log (grep the audit for `web_form`).
- **Governance by design:** every submission flows through the same control plane —
  routing, the **human approval gate**, artifacts, and audit — regardless of channel.
- **Poll return path:** the page re-reads the run it created; no public callback URL or
  outbound allowlist needed. (Swapping to OB-1 push is a later step.)

## Notes & troubleshooting

- **Worker stays offline:** make sure `mock_worker.py` is running *before* `setup.py`
  (setup polls it on register); re-run `setup.py` to re-poll.
- **This is a demo**, not production: the mock returns canned text, and loopback-no-auth
  must never be used off localhost. Keep the PoC behind localhost/VPN.
- Isolated from Atlas core: nothing here imports or changes `atlas/`, and it is not part
  of the completion gate.
