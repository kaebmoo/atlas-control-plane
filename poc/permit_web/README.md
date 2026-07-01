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

Point setup at a running `thclaws --serve` instead of the mock, then re-run it:

```bash
MOCK_WORKER_URL='http://127.0.0.1:4317' MOCK_WORKER_TOKEN='<thclaws-token>' \
  python3 poc/permit_web/setup.py
```

The node prompts start with a harmless `STEP=intake|summary|notice` marker (the mock
keys off it; a real model just treats it as a label) followed by the real instruction,
so the same workflow works unchanged against a real worker.

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
