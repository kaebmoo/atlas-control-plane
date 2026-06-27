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

Terminal 3: start Atlas.

```bash
cd /Users/seal/Documents/GitHub/atlas-control-plane
python3 -m atlas --host 127.0.0.1 --port 8787
```

Open:

```text
http://127.0.0.1:8787
```

## Demo 1: Worker Control Plane

1. Add worker `Reporter`.
2. Add worker `Anchor`.
3. Poll both workers.
4. Show status and route metadata in the sidebar.

Expected result:

- workers show `online`
- dashboard has worker and workspace inventory

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
- output streams into the Live Stream panel

## Demo 3: Handoff

Steps:

1. Select reporter worker.
2. Enter the reporter prompt from Demo 2.
3. Enable `After success`.
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

1. In Workflows, click `New`.
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

8. Click `Run Workflow`.

Expected result:

- workflow run is created
- runtime nodes are visible in run detail
- `completed_nodes` contains `reporter` and `anchor`
- timeline shows node and run lifecycle events
- artifacts show `notes` and `script` with decoded JSON where applicable

## Demo 5: Trigger

Steps:

1. Select the `News Desk` workflow.
2. Create a trigger:

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

Optional webhook dedupe check:

1. Create another trigger with type `webhook`.
2. Call `/api/workflow-triggers/{id}/fire` twice with the same `dedupe_key`.
3. Confirm the second call returns an `ignored` event with
   `duplicate dedupe_key`.

## Demo 6: Manual Artifact

Use a run id from Demo 4:

```bash
curl -sS -X POST http://127.0.0.1:8787/api/artifacts \
  -H 'content-type: application/json' \
  -d '{"run_id":"wfr_xxx","key":"demo_note","kind":"json","content":{"ok":true}}'
```

Select the run again. The Artifacts panel shows decoded content and metadata.

## Close Demo

Stop Atlas with `Ctrl+C`.

Stop thClaws worker terminals with `Ctrl+C`.
