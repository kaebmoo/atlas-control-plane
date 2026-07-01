# Workflow Patterns in Atlas

How common multi-agent patterns map to the Atlas workflow engine — what it
expresses directly today, and the one capability gap (dynamic fan-out) that
bounds the rest. This summarizes an analysis of the "six workflow patterns"
(classify-and-act, fan-out-and-synthesize, adversarial verification,
generate-and-filter, tournament, loop-until-done) against the current engine.

> Engine reference: [Concepts](concepts-en.md) · [Architecture](architecture.md) ·
> [Workflow Examples](workflow-examples.md)

## Pattern coverage

The dividing line is not the pattern — it is whether the number of parallel
branches (**N** = how many agents a step fans out to) is **fixed when you design
the graph**, or **only known at run time**.

| Pattern | Fixed N (declared in the graph) | Dynamic N (size known only at run time) |
| --- | --- | --- |
| Classify-and-act (route to 1 of N) | ✅ `manager` node, or `artifact_equals` / `artifact_in` edges | ✅ picks one of a known set — not a fan-out |
| Fan-out and synthesize | ✅ N edges → `join: all` → synthesize node | ❌ needs a map primitive |
| Adversarial verification | ✅ fan-out → `join: quorum` (majority vote) | ❌ needs a map primitive |
| Generate and filter | ✅ N generators → `join: all` → filter node | ❌ needs a map primitive |
| Tournament (bracket) | ⚠️ fixed-size bracket only, wired by hand | ❌ needs map + list artifacts + dynamic join + multi-round |
| Loop until done | ✅ guarded loop (a loop, not a fan-out) | ✅ iteration count may be runtime; guards bound it |

Atlas expresses **5 of 6 directly at fixed N** (tournament needs a hand-wired
fixed bracket). The fan-out patterns only become impossible when N must grow at
run time. Classify-and-act and loop-until-done are unaffected, because neither
fans out over a runtime collection — one selects a single target from a known
set, the other re-runs one node in a guarded loop.

## Two cross-cutting limits

**1. The executor is sequential, not parallel.** Fan-out queues every branch but
runs one job at a time — the engine submits a node, then waits for its job before
starting the next (`workflows.py`; see [Architecture](architecture.md)). Results
are correct (a `join` still waits for all branches) but there is no concurrency
speedup, and the branch count is also bounded by policy (`max_jobs`,
`max_budget_units`). Parallel execution is a documented current limitation, not a
planned feature.

**2. There is no dynamic fan-out ("map") primitive.** The graph is static: nodes
and edges are declared ahead of time, a node writes to one fixed artifact key,
`join` waits on a fixed declared set of upstreams, and a `manager` node may only
select among already-declared nodes. Fanning out over a runtime-sized collection
would need three things the engine does not have:

| Missing piece | What it is |
| --- | --- |
| Map / foreach node | Spawn one node instance per element of a runtime list |
| Per-element list artifacts | So each instance writes its own slot and downstream can pair / aggregate them |
| Runtime-arity join / reduce | Join or reduce over a set whose size is known only at run time |

The foundational one is the **map node**; multi-round shapes (e.g. a tournament's
log₂N rounds) are then just a guarded loop around map + reduce. The same map gap
is what blocks the dynamic-N column for every fan-out pattern above — it is not
tournament-specific.

## Working with dynamic N today (without map)

Most runtime-sized work has a cheaper path that needs no engine change:

| Workaround | Use when | Mechanism |
| --- | --- | --- |
| One run per item | items are processed independently (no cross-item aggregation) | fire the workflow once per item via a manual/webhook trigger with a stable `dedupe_key`; each run is a fixed graph |
| One worker iterates the list | per-item work is light | a single thClaws job loops over the list inside its own prompt; N collapses to one node |

Example: "process 17 complaints" → fire the workflow 17 times (one run each), no
map needed. This covers most batch, per-account, and per-worker cases.

## When to build the map primitive

Build dynamic fan-out only when a real case needs **all three at once** — the
point where the workarounds above stop working:

1. N is known only at run time, **and**
2. it must be **one coordinated run** (not N independent runs), **and**
3. there is an **aggregation / join across all N**, with per-item retry, budget,
   and observability inside that run.

Example that fails the workarounds: *"verify N documents in one run, then emit a
single report that must wait for all N, with each document retried and metered
separately."*

Until such a case is concrete, this is **deliberately not built** (YAGNI). Adding
map without parallel execution yields correctness but not speed, so the two are
best scoped together as one engine milestone.
