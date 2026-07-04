# Draft: thClaws Ideas discussion post

> Target: https://github.com/thClaws/thClaws/discussions/categories/ideas
> Status: draft, not yet posted
>
> Note before posting: keep the auth wording neutral (as below). If the missing
> auth on `/workspace/sync/*` looks unintended rather than documented behavior,
> report that part privately via the repo Security tab first.

---

**Title:** Idea: authenticated, job-scoped artifact API for external orchestrators

## Use case

We are building a small control plane that dispatches jobs to multiple thClaws
workers over `/agent/run` (serve mode, Bearer `THCLAWS_API_TOKEN`). A common
pattern is a multi-agent pipeline: worker A (coder) produces files, worker B
(reviewer) needs those exact bytes — not a text description of them.

Today the only way to move files is the workspace sync surface
(`/workspace/sync/manifest`, `/export`, `/push`, ...). It works, but it was
clearly designed for workspace mirroring inside a trusted network, and an
external orchestrator hits a few gaps:

1. **Auth story.** `/agent/run` and `/v1/*` are protected by the Bearer token,
   while the sync routes are intended for trusted-network / tunnel / reverse-proxy
   deployments. For an orchestrator that only holds the API token, there is no
   supported way to use sync. We currently treat sync as disabled unless the
   operator asserts a tunnel or ForwardAuth is in place — which is a deployment
   assertion, not something we can verify.

2. **Job scoping.** Sync operates on the whole workspace. The orchestrator has
   to trust that the paths it requests are really the output of the job it just
   ran; there is no `job → files` association on the server side.

3. **Atomicity.** `manifest` then `export` are two requests; files can change
   in between. We re-hash after download and reject mismatches, but artifact
   IDs fixed at job completion would remove the race entirely.

## Proposal

**Tier 1 (small):** an opt-in flag to require the existing Bearer token on
`/workspace/sync/*`, e.g. `THCLAWS_SYNC_REQUIRE_AUTH=1`. This alone would let
orchestrators use `export`/`push` safely without a tunnel, and changes nothing
for existing deployments.

**Tier 2 (better long-term):** a job-scoped artifact API under the already
Bearer-protected `/v1` surface:

```
GET  /v1/jobs/{job_id}/artifacts                     # manifest: path, size, sha256
GET  /v1/jobs/{job_id}/artifacts/{artifact_id}       # bytes
POST /v1/jobs/{job_id}/inputs                        # stage files before/with dispatch
```

Artifacts would be snapshotted (or at least hashed) when the job completes, so
the manifest is stable. Which files count as artifacts could be declared in the
`/agent/run` request (e.g. `collect_files: ["reports/*.pdf"]`) or by the agent
itself at the end of the run.

Tier 2 solves auth, scoping, and the manifest/export race in one shape, and
gives least-privilege access (per-job, not whole-workspace).

Happy to provide more detail on the orchestrator side, test a branch, or help
with a PR if there is interest in either tier.
