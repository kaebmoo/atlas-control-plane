# Draft: thClaws Ideas discussion post

> Target: https://github.com/thClaws/thClaws/discussions/categories/ideas
> Status: posted as [thClaws discussion #179](https://github.com/thClaws/thClaws/discussions/179)

**Title:** Idea: workspace-scoped capabilities and a worker protocol version

## Use case

An external orchestrator can discover useful daemon information through
`GET /v1/agent/info`, but it cannot tell whether that information applies to
the `workspace_dir` it is about to send to `/agent/run`. Skills are discovered
from the daemon environment, and the response has no protocol/schema version
that a client can use for compatibility gating.

## Proposal

Add a Bearer-protected endpoint scoped the same way as `/agent/run`:

```http
GET /v1/capabilities?workspace_dir=/workspace/project-a
```

The response could include the skills/tools/policies available for that
workspace plus explicit compatibility fields, for example:

```json
{
  "protocol_version": "1",
  "engine_version": "0.85.0",
  "workspace_dir": "/workspace/project-a",
  "skills": [],
  "features": {
    "agent_run": true,
    "x_callback": true,
    "structured_events": true
  }
}
```

If a new endpoint is too large initially, adding `protocol_version` (or a
schema version) to `/v1/agent/info` would still let orchestrators distinguish
an understood contract from a newer unknown one. `engine_version` alone is not
enough because clients otherwise have to infer wire compatibility from product
SemVer.

This would let a control plane use workspace capabilities as advisory routing
signals while keeping operator-assigned roles/tags as the routing contract.
