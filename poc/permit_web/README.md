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
