# Atlas User Guide

This guide covers the current local Atlas workflow MVP.

## 1. Start Atlas

```bash
cd /Users/seal/Documents/GitHub/atlas-control-plane
python3 -m atlas --host 127.0.0.1 --port 8787
```

If port `8787` is busy, use another port:

```bash
python3 -m atlas --host 127.0.0.1 --port 8788
```

Open the dashboard:

```text
http://127.0.0.1:8787
```

Stop Atlas with `Ctrl+C` in the terminal.

## 2. Start thClaws Workers

Start one or more thClaws servers on separate ports.

```bash
cd /Users/seal/Documents/GitHub/thClaws

THCLAWS_API_TOKEN="dev-token-1" \
thclaws --serve --bind 127.0.0.1 --port 4317
```

Optional second worker:

```bash
cd /Users/seal/Documents/GitHub/thClaws

THCLAWS_API_TOKEN="dev-token-2" \
thclaws --serve --bind 127.0.0.1 --port 4318
```

## 3. Add Workers

In the sidebar, click `Add` under Workers.

Example reporter worker:

```text
Name:     Reporter
Base URL: http://127.0.0.1:4317
Token:    dev-token-1
Role:     reporter
Tags:     local,news,reporter
```

Example anchor worker:

```text
Name:     Anchor
Base URL: http://127.0.0.1:4318
Token:    dev-token-2
Role:     anchor
Tags:     local,news,anchor
```

After saving, Atlas polls the worker. A reachable worker becomes `online`.

## 4. Add Workspaces

Click `Add` under Workspaces.

```text
Worker:    Reporter
Key:       thclaws
Directory: /Users/seal/Documents/GitHub/thClaws
Company:   Personal
Tags:      local,news
```

`Directory` is resolved on the worker machine, not the Atlas machine.

## 5. Run A Simple Job

In the Command panel:

1. Enter a prompt.
2. Choose a worker or leave routing on auto.
3. Click `Run`.

The Jobs list shows state, route reason, prompt, and stream output.

## 6. Run A Handoff

Use this when job B should start after job A succeeds.

1. Select the reporter worker.
2. Enter a reporter prompt.
3. Enable `After success`.
4. Select the anchor worker.
5. Keep or edit the handoff prompt.
6. Click `Run`.

Atlas creates a child job when the reporter job succeeds.

## 7. Create A Workflow

In the Workflows panel:

1. Click `New`.
2. Set a name.
3. Paste graph JSON.
4. Set policy JSON.
5. Click `Save`.
6. Click `Validate`.

Minimal graph:

```json
{
  "start": "reporter",
  "nodes": [
    {
      "id": "reporter",
      "type": "worker",
      "role": "reporter",
      "prompt": "Find facts about: {input.topic}",
      "outputs": ["notes"]
    },
    {
      "id": "anchor",
      "type": "worker",
      "role": "anchor",
      "prompt": "Turn these notes into a short script: {artifact.notes}",
      "outputs": ["script"]
    }
  ],
  "edges": [
    {"from": "reporter", "to": "anchor", "condition": {"type": "always"}}
  ]
}
```

Policy:

```json
{
  "max_jobs": 5,
  "max_iterations": 5
}
```

## 8. Run A Workflow

In `Run Input JSON`, enter:

```json
{
  "topic": "technology news"
}
```

Click `Run Workflow`.

Inspect:

- Runs list
- Run detail JSON
- Artifacts box
- Related jobs in the Jobs panel

## 9. Draft A Workflow With A Builder Worker

Add a worker with role or tag `workflow_builder`.

Then use the Draft box:

```text
Create a reporter -> fact_checker -> anchor workflow. If fact check says
needs_more_sources, send it back to reporter up to 2 times.
```

Atlas sends available workers, workspaces, condition types, trigger types, and
templates to the builder worker. The returned JSON is validated before display.

## 10. Create And Fire A Trigger

Select a saved workflow, then fill Triggers:

Manual trigger:

```json
{}
```

Schedule trigger every 15 minutes:

```json
{
  "interval_minutes": 15
}
```

Daily local-time schedule:

```json
{
  "daily_time": "09:30"
}
```

Click `Create Trigger`. Use `Fire` to start the workflow manually from that
trigger. Click a trigger to inspect recent trigger events.

## Troubleshooting

- `No workers registered`: add and poll at least one worker.
- `role has no matching worker`: add a worker with that role or tag.
- `unknown worker_id`: the workflow references a deleted or wrong worker id.
- `missing prompt variable`: the prompt uses an input/artifact path that does
  not exist.
- `output_format=json` fails: the worker did not return valid JSON.
