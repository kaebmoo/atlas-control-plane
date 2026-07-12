# Atlas Demo Script

Use this for a short local demo of the current MVP.

## Setup

Terminal 1: start a reporter worker.

```bash
cd /Users/seal/Documents/GitHub/thClaws

THCLAWS_API_TOKEN="dev-token-1" \
thclaws --serve --bind 127.0.0.1 --port 4317
```

Terminal 2: start an anchor worker.

```bash
cd /Users/seal/Documents/GitHub/thClaws

THCLAWS_API_TOKEN="dev-token-2" \
thclaws --serve --bind 127.0.0.1 --port 4318
```

Terminal 3: start Atlas. Atlas requires an authenticated session (or Bearer
token) for essentially every endpoint, so pick one of these two bootstrap
paths first:

- **Browser login (recommended):** start Atlas normally, then seed the first
  admin user once:

  ```bash
  cd /Users/seal/Documents/GitHub/atlas-control-plane
  python3 -m atlas.admin create-admin admin
  python3 -m atlas --host 127.0.0.1 --port 8787
  ```

  `create-admin` prompts for a password and prints a one-time API token. Log
  in at `http://127.0.0.1:8787` with that username/password.

- **Loopback demo mode (no login, curl-friendly):** skip user creation and
  start Atlas with loopback auth disabled. This also makes the Demo 5/6 curl
  examples below work exactly as written, with no `Authorization` header:

  ```bash
  cd /Users/seal/Documents/GitHub/atlas-control-plane
  ATLAS_LOOPBACK_NO_AUTH=true python3 -m atlas --host 127.0.0.1 --port 8787
  ```

Open:

```text
http://127.0.0.1:8787
```

## Demo 1: Worker Control Plane

1. Add worker `Reporter`.
2. Add worker `Anchor`.
3. Poll both workers.
4. Open the **Fleet** view to show worker status and inventory.

Expected result:

- workers show `online`
- the Fleet view lists worker and workspace inventory

## Demo 2: Routed Job

Prompt:

```text
Find one concise technology news item and summarize the facts.
```

Steps:

1. Select the reporter worker.
2. Click `Run`.
3. Show stream output and job events.

Expected result:

- job moves through queued/running/succeeded
- output streams into the live stream in the **Jobs** view

## Demo 3: Handoff

Steps:

1. Select reporter worker.
2. Enter the reporter prompt from Demo 2.
3. Enable `Hand off after success`.
4. Select anchor worker.
5. Use this handoff prompt:

```text
You are a news anchor.
Turn this report into a short broadcast script.
Do not add facts that are not in the report.

{result}
```

6. Click `Run`.

Expected result:

- reporter job succeeds
- Atlas creates child anchor job
- Jobs list shows parent/child relationship

## Demo 4: Workflow

Steps:

1. In the **Workflows** view, click `New`.
2. Name it `News Desk`.
3. Paste the Reporter To Anchor graph from `docs/workflow-examples.md`.
4. Paste the policy from the same example.
5. Click `Save`.
6. Click `Validate`.
7. Set run input:

```json
{
  "topic": "technology news"
}
```

8. Click `Run Workflow`. Atlas opens the **Monitor** view.

Expected result:

- workflow run is created
- runtime nodes are visible in run detail
- `completed_nodes` contains `reporter` and `anchor`
- timeline shows node and run lifecycle events
- artifacts show `notes` and `script` with decoded JSON where applicable

## Demo 5: Trigger

Steps:

1. Select the `News Desk` workflow, then open the **Monitor** view.
2. In the Triggers card, create a trigger:

```text
Name: Manual news run
Type: manual
Config JSON: {}
Enabled: checked
```

3. Click `Create Trigger`.
4. Click `Fire`.
5. Click the trigger row.

Expected result:

- trigger creates a workflow run
- trigger events show `received` and `started`
- trigger card shows its latest state/error

Optional webhook dedupe check (if Atlas was started with
`ATLAS_LOOPBACK_NO_AUTH=true`, calls from `127.0.0.1` need no
`Authorization` header; otherwise add `-H 'Authorization: Bearer <token>'`
using a token from `python3 -m atlas.admin create-token <username>`):

1. Create another trigger with type `webhook`.
2. Call `/api/workflow-triggers/{id}/fire` twice with the same `dedupe_key`.
3. Confirm the second call returns an `ignored` event with
   `duplicate dedupe_key`.

## Demo 6: Manual Artifact

Use a run id from Demo 4. This example assumes Atlas was started with
`ATLAS_LOOPBACK_NO_AUTH=true` (see Setup); otherwise add
`-H 'Authorization: Bearer <token>'` with a token from
`python3 -m atlas.admin create-token <username>`:

```bash
curl -sS -X POST http://127.0.0.1:8787/api/artifacts \
  -H 'content-type: application/json' \
  -d '{"run_id":"wfr_xxx","key":"demo_note","kind":"json","content":{"ok":true}}'
```

Select the run again in the **Monitor** view. The Artifacts card shows decoded content and metadata.

## Demo 7: Human Approval Gate

1. Create a workflow from `Human Gate Before Publish` in
   `docs/workflow-examples.md`.
2. Run it with `{"topic":"technology news"}`.
3. After the reporter succeeds, select the run in the **Monitor** view.
4. Confirm the run is `waiting_for_human`, the approval is pending, and no
   anchor job exists yet.
5. Click `Approve`.
6. Confirm the run succeeds and the anchor runs once.
7. Start it again and click `Reject`.

Expected result:

- the gate itself creates no worker job
- approve continues from the gate without duplicate execution
- reject fails the run
- the timeline records approval creation and the final decision

## Close Demo

Stop Atlas with `Ctrl+C`.

Stop thClaws worker terminals with `Ctrl+C`.
