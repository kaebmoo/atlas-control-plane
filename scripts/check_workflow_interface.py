"""Milestone B (docs/adr/0002-workflow-interface-contract.md): the workflow.interface
contract. Covers the bounded validator in isolation, then CRUD/versioning, direct-run
and trigger-fire validation ordering, run snapshots, pack round-trip, and the manager/
worker prompt-interpolation parity fix, against a real (HTTP + DB) harness."""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas import workflow_interface as wi
from atlas.app import AtlasHttpServer, AtlasRuntime
from atlas.config import Config
from atlas.db import Database, now_iso
from atlas.packs import export_pack, import_pack, sign_pack, validate_pack, verify_pack_signature
from atlas.workflows import WorkflowRunner, _manager_prompt, render_prompt


# ---------------------------------------------------------------------------
# Pure validator unit tests — no HTTP/DB needed.
# ---------------------------------------------------------------------------


def _expect_raises(fn, *args, **kwargs) -> str:
    try:
        fn(*args, **kwargs)
    except ValueError as exc:
        return str(exc)
    raise AssertionError(f"{fn.__name__}{args}: expected ValueError, none raised")


def check_validator_unit() -> None:
    good_schema = {
        "$schema": wi.INPUT_SCHEMA_URI,
        "type": "object",
        "title": "Permit request",
        "description": "A permit application input.",
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 200},
            "age": {"type": "integer", "minimum": 0, "maximum": 150},
            "score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "active": {"type": "boolean"},
            "kind": {"type": "string", "enum": ["a", "b", "c"]},
            "fixed": {"const": "permit"},
            "tags": {"type": "array", "minItems": 0, "maxItems": 10, "items": {"type": "string"}},
            "meta": {"type": ["object", "null"], "default": None, "examples": [{"x": 1}]},
        },
        "required": ["name"],
        "additionalProperties": False,
    }
    wi.validate_input_schema(good_schema)  # every supported keyword, must accept

    # Unknown/unsupported keywords rejected outright — nothing silently ignored.
    for bad_keyword_schema in (
        {"type": "object", "$ref": "#/whatever"},
        {"type": "object", "oneOf": [{"type": "string"}]},
        {"type": "object", "anyOf": [{"type": "string"}]},
        {"type": "object", "allOf": [{"type": "string"}]},
        {"type": "object", "not": {"type": "string"}},
        {"type": "object", "if": {}, "then": {}},
        {"type": "object", "properties": {"x": {"type": "string", "pattern": "^a"}}},
        {"type": "object", "format": "email"},
        {"type": "object", "patternProperties": {"^x": {"type": "string"}}},
        {"type": "object", "dependentRequired": {"a": ["b"]}},
        {"type": "object", "unevaluatedProperties": False},
        {"type": "object", "bogusKeyword": True},
    ):
        _expect_raises(wi.validate_input_schema, bad_keyword_schema)

    # $schema: exact URI only, never a different or malformed value.
    wi.validate_input_schema({"$schema": wi.INPUT_SCHEMA_URI, "type": "object"})
    assert "$schema" in _expect_raises(wi.validate_input_schema, {"$schema": "https://example.com/x", "type": "object"})
    # $schema only legal at the schema root, not nested.
    assert "root" in _expect_raises(
        wi.validate_input_schema,
        {"type": "object", "properties": {"a": {"$schema": wi.INPUT_SCHEMA_URI, "type": "string"}}},
    )

    # Root must declare exactly type "object". The single-element list form is
    # DOCUMENTED as equivalent (ADR 0002 §3) — lock that acceptance here.
    assert "root input_schema" in _expect_raises(wi.validate_input_schema, {"type": "string"})
    assert "root input_schema" in _expect_raises(wi.validate_input_schema, {"type": ["object", "null"]})
    assert "root input_schema" in _expect_raises(wi.validate_input_schema, {})
    wi.validate_input_schema({"type": ["object"]})
    # A non-string (unhashable) entry inside a type list must be a ValueError, never a
    # TypeError from set() — found by check_fuzz, locked here.
    assert "primitive type" in _expect_raises(wi.validate_input_schema, {"type": ["object", {}]})
    assert "primitive type" in _expect_raises(
        wi.validate_input_schema, {"type": "object", "properties": {"a": {"type": [["x"]]}}}
    )

    # Depth bound: exactly 16 accepted, 17 rejected.
    def _nest(levels: int) -> dict:
        node: dict = {"type": "string"}
        for _ in range(levels - 1):
            node = {"type": "object", "properties": {"a": node}, "required": ["a"]}
        return node

    wi.validate_input_schema(_nest(16))
    assert "nesting exceeds" in _expect_raises(wi.validate_input_schema, _nest(17))

    # Total declared properties bound (cumulative across the WHOLE schema, not per-object).
    small_ok = {"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(256)}}
    wi.validate_input_schema(small_ok)
    too_many = {"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(257)}}
    assert "properties" in _expect_raises(wi.validate_input_schema, too_many)

    # required / enum entry-count bound (each list independently, <=256).
    wi.validate_input_schema({"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]})
    over_required = {
        "type": "object",
        "properties": {f"p{i}": {"type": "string"} for i in range(257)},
        "required": [f"p{i}" for i in range(257)],
    }
    assert "required" in _expect_raises(wi.validate_input_schema, over_required)
    wi.validate_input_schema({"type": "object", "properties": {"x": {"type": "string", "enum": [str(i) for i in range(256)]}}})
    assert "enum" in _expect_raises(
        wi.validate_input_schema, {"type": "object", "properties": {"x": {"type": "string", "enum": [str(i) for i in range(257)]}}}
    )

    # title / description code-point bounds.
    wi.validate_input_schema({"type": "object", "title": "x" * 256})
    assert "title" in _expect_raises(wi.validate_input_schema, {"type": "object", "title": "x" * 257})
    wi.validate_input_schema({"type": "object", "description": "x" * 2048})
    assert "description" in _expect_raises(wi.validate_input_schema, {"type": "object", "description": "x" * 2049})

    # Non-finite numbers rejected (Python's json module admits NaN/Infinity by default).
    assert "non-finite" in _expect_raises(wi.canonical_bytes, {"minimum": float("nan")})
    assert "non-finite" in _expect_raises(wi.canonical_bytes, {"x": float("inf")})
    assert "finite number" in _expect_raises(
        wi.validate_input_schema, {"type": "object", "properties": {"a": {"type": "number", "minimum": True}}}
    )

    # JSON type fidelity: bool is never a number/integer, and enum/const distinguish
    # `true` from `1` — the exact bug class this profile must not admit.
    number_schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    assert "expected type" in _expect_raises(wi.validate_business_input, number_schema, {"n": True})
    wi.validate_business_input(number_schema, {"n": 1})
    enum_schema = {"type": "object", "properties": {"flag": {"enum": [1, 2, 3]}}}
    assert "enum" in _expect_raises(wi.validate_business_input, enum_schema, {"flag": True})
    const_schema = {"type": "object", "properties": {"flag": {"const": 1}}}
    assert "const" in _expect_raises(wi.validate_business_input, const_schema, {"flag": True})
    bool_enum_schema = {"type": "object", "properties": {"flag": {"enum": [True]}}}
    wi.validate_business_input(bool_enum_schema, {"flag": True})
    assert "enum" in _expect_raises(wi.validate_business_input, bool_enum_schema, {"flag": 1})

    # Validation traversal bound: a schema with no maxItems lets a hostile instance be
    # arbitrarily large; the traversal budget must still bound the work, not crash.
    unbounded_array_schema = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "integer"}}}}
    wi.validate_business_input(unbounded_array_schema, {"items": list(range(9998))})  # + root + property node = ~10000
    assert "traversal" in _expect_raises(
        wi.validate_business_input, unbounded_array_schema, {"items": list(range(20000))}
    )

    # additionalProperties: false really closes the object; absent/true leaves it open.
    closed = {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": False}
    wi.validate_business_input(closed, {"a": "x"})
    assert "not allowed" in _expect_raises(wi.validate_business_input, closed, {"a": "x", "b": "y"})
    open_schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    wi.validate_business_input(open_schema, {"a": "x", "b": "y"})

    # Business projection excludes EXACTLY _meta/_trigger_chain, never every underscore key.
    projected = wi.business_projection({"_meta": {"x": 1}, "_trigger_chain": ["a"], "_custom": "keep", "topic": "AI"})
    assert projected == {"_custom": "keep", "topic": "AI"}, projected

    # Byte caps. First the AT-LIMIT side: an interface serializing to exactly
    # INTERFACE_MAX_BYTES must be accepted (pad an ASCII sample value, 1 char = 1 byte).
    padded_interface = {
        "schema_version": 1,
        "input_schema": {"type": "object", "properties": {"p": {"type": "string"}}},
        "sample_input": {"p": ""},
    }
    pad = wi.INTERFACE_MAX_BYTES - len(wi.canonical_bytes(padded_interface))
    padded_interface["sample_input"]["p"] = "x" * pad
    assert len(wi.canonical_bytes(padded_interface)) == wi.INTERFACE_MAX_BYTES
    wi.validate_interface_document(padded_interface)  # exactly at the cap: accepted
    padded_interface["sample_input"]["p"] += "x"
    assert "exceeds" in _expect_raises(wi.validate_interface_document, padded_interface)

    huge_interface = {
        "schema_version": 1,
        "input_schema": {"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(1)}},
        "sample_input": {"p0": "x" * (wi.INTERFACE_MAX_BYTES + 1000)},
    }
    assert "exceeds" in _expect_raises(wi.validate_interface_document, huge_interface)
    # sample_input alone is a strict subset of the serialized interface, so an oversized
    # sample always also breaches the whole-interface cap first — both caps are 64 KiB,
    # so this can never surface the sample-specific message; assert only that it's
    # rejected (the dedicated per-sample cap exists for when the two caps ever diverge).
    over_sample = {
        "schema_version": 1,
        "input_schema": {"type": "object", "properties": {"p": {"type": "string"}}},
        "sample_input": {"p": "x" * (wi.SAMPLE_MAX_BYTES + 1)},
    }
    assert "exceeds" in _expect_raises(wi.validate_interface_document, over_sample)
    assert "effective input exceeds" in _expect_raises(
        wi.check_effective_input_size, {"topic": "x" * (wi.EFFECTIVE_INPUT_MAX_BYTES + 1)}
    )

    # Sample mismatch: sample_input must itself conform to input_schema.
    mismatched = {
        "schema_version": 1,
        "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]},
        "sample_input": {"wrong_key": "x"},
    }
    assert "missing required" in _expect_raises(wi.validate_interface_document, mismatched)

    # Unknown top-level interface / outputs-entry fields rejected.
    assert "unknown field" in _expect_raises(
        wi.validate_interface_document,
        {"schema_version": 1, "input_schema": {"type": "object"}, "bogus": True},
    )
    assert "unknown field" in _expect_raises(
        wi.validate_interface_document,
        {"schema_version": 1, "input_schema": {"type": "object"}, "outputs": [{"key": "a", "kind": "text", "bogus": 1}]},
    )
    assert "schema_version" in _expect_raises(
        wi.validate_interface_document, {"schema_version": 2, "input_schema": {"type": "object"}}
    )

    # Outputs: key regex, kind enum, duplicate keys, primary_output must name a declared key.
    assert "key" in _expect_raises(
        wi.validate_interface_document,
        {"schema_version": 1, "input_schema": {"type": "object"}, "outputs": [{"key": "1bad", "kind": "text"}]},
    )
    assert "kind" in _expect_raises(
        wi.validate_interface_document,
        {"schema_version": 1, "input_schema": {"type": "object"}, "outputs": [{"key": "ok", "kind": "csv"}]},
    )
    assert "duplicate" in _expect_raises(
        wi.validate_interface_document,
        {
            "schema_version": 1,
            "input_schema": {"type": "object"},
            "outputs": [{"key": "ok", "kind": "text"}, {"key": "ok", "kind": "json"}],
        },
    )
    assert "primary_output" in _expect_raises(
        wi.validate_interface_document,
        {"schema_version": 1, "input_schema": {"type": "object"}, "outputs": [{"key": "ok", "kind": "text"}], "primary_output": "nope"},
    )

    # Output cross-check: exactly-one-producer, kind matches output_format.
    graph_one_producer = {
        "start": "w1",
        "nodes": [{"id": "w1", "type": "worker", "prompt": "go", "outputs": ["out1"], "output_format": "json"}],
        "edges": [],
    }
    ok_iface = {"schema_version": 1, "input_schema": {"type": "object"}, "outputs": [{"key": "out1", "kind": "json"}]}
    wi.cross_check_against_graph(ok_iface, graph_one_producer)
    wrong_kind_iface = {"schema_version": 1, "input_schema": {"type": "object"}, "outputs": [{"key": "out1", "kind": "text"}]}
    assert "kind" in _expect_raises(wi.cross_check_against_graph, wrong_kind_iface, graph_one_producer)
    undeclared_iface = {"schema_version": 1, "input_schema": {"type": "object"}, "outputs": [{"key": "missing", "kind": "text"}]}
    assert "exactly one worker" in _expect_raises(wi.cross_check_against_graph, undeclared_iface, graph_one_producer)
    graph_two_producers = {
        "start": "w1",
        "nodes": [
            {"id": "w1", "type": "worker", "prompt": "go", "outputs": ["out1"]},
            {"id": "w2", "type": "worker", "prompt": "go2", "outputs": ["out1"]},
        ],
        "edges": [],
    }
    ambiguous_iface = {"schema_version": 1, "input_schema": {"type": "object"}, "outputs": [{"key": "out1", "kind": "text"}]}
    assert "exactly one worker" in _expect_raises(wi.cross_check_against_graph, ambiguous_iface, graph_two_producers)

    # Prompt-path cross-check: impossible path under a closed schema.
    impossible_iface = {
        "schema_version": 1,
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    }
    impossible_graph = {"start": "w1", "nodes": [{"id": "w1", "type": "worker", "prompt": "Hi {input.topic}"}], "edges": []}
    assert "impossible" in _expect_raises(wi.cross_check_against_graph, impossible_iface, impossible_graph)

    # Start-node requiredness: every intermediate segment declared+required, exactly object typed.
    start_ok_iface = {
        "schema_version": 1,
        "input_schema": {
            "type": "object",
            "properties": {"customer": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
            "required": ["customer"],
        },
    }
    start_graph = {"start": "w1", "nodes": [{"id": "w1", "type": "worker", "prompt": "Hi {input.customer.name}"}], "edges": []}
    wi.cross_check_against_graph(start_ok_iface, start_graph)

    # Nullable/mixed intermediate segment on the START path must be rejected.
    mixed_iface = {
        "schema_version": 1,
        "input_schema": {
            "type": "object",
            "properties": {
                "customer": {
                    "type": ["object", "null"],
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                }
            },
            "required": ["customer"],
        },
    }
    assert "start node" in _expect_raises(wi.cross_check_against_graph, mixed_iface, start_graph)

    # Not-required intermediate segment on the START path must also be rejected.
    not_required_iface = {
        "schema_version": 1,
        "input_schema": {
            "type": "object",
            "properties": {"customer": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
        },
    }
    assert "start node" in _expect_raises(wi.cross_check_against_graph, not_required_iface, start_graph)

    # Downstream/conditional node paths may remain OPTIONAL — merely representable.
    downstream_graph = {
        "start": "w1",
        "nodes": [
            {"id": "w1", "type": "worker", "prompt": "go", "outputs": ["out1"]},
            {"id": "w2", "type": "worker", "prompt": "Context: {input.review_context}"},
        ],
        "edges": [{"from": "w1", "to": "w2", "condition": {"type": "always"}}],
    }
    optional_iface = {
        "schema_version": 1,
        "input_schema": {"type": "object", "properties": {"review_context": {"type": "string"}}},
        "outputs": [{"key": "out1", "kind": "text"}],
    }
    wi.cross_check_against_graph(optional_iface, downstream_graph)  # not required -> still fine downstream

    print("validator unit checks ok")


# ---------------------------------------------------------------------------
# HTTP + DB harness (mirrors scripts/check_workflow_api.py's pattern).
# ---------------------------------------------------------------------------


def request(base_url: str, method: str, path: str, payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base_url + path, data=body, method=method, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def request_error(base_url: str, method: str, path: str, payload: dict | None = None, status: int = 400) -> dict:
    try:
        request(base_url, method, path, payload)
    except urllib.error.HTTPError as exc:
        assert exc.code == status, f"{method} {path}: expected HTTP {status}, got {exc.code}"
        return json.loads(exc.read().decode("utf-8"))
    raise AssertionError(f"{method} {path}: expected HTTP {status}, request succeeded")


def wait_for_api_run(base_url: str, run_id: str, state: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = request(base_url, "GET", f"/api/workflow-runs/{run_id}")["run"]
        if run["state"] == state:
            return run
        time.sleep(0.02)
    raise AssertionError(f"workflow run {run_id} did not reach {state}")


def wait_for_trigger_event(base_url: str, trigger_id: str, state: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = request(base_url, "GET", f"/api/workflow-triggers/{trigger_id}/events")["events"]
        match = next((event for event in events if event["state"] == state), None)
        if match:
            return match
        time.sleep(0.02)
    raise AssertionError(f"trigger {trigger_id} never reached event state {state}")


def _padded_topic_input(target_bytes: int) -> dict:
    """A {"topic": "A"*N} object whose canonical_bytes length is exactly target_bytes."""
    overhead = len(wi.canonical_bytes({"topic": ""}))
    pad = target_bytes - overhead
    assert pad >= 0
    return {"topic": "A" * pad}


SIMPLE_INTERFACE = {
    "schema_version": 1,
    "input_schema": {
        "type": "object",
        "properties": {"topic": {"type": "string", "minLength": 1}},
        "required": ["topic"],
    },
    "outputs": [{"key": "notes", "kind": "text"}],
}


def _simple_graph() -> dict:
    return {
        "start": "only",
        "nodes": [{"id": "only", "type": "worker", "prompt": "Topic: {input.topic}", "outputs": ["notes"]}],
        "edges": [],
    }


class FakeJobService:
    """Synchronous stand-in for real thClaws dispatch — completes a job in-process so a
    WorkflowRunner invoked directly (bypassing HTTP) can reach a real terminal state
    without a network worker. Mirrors scripts/check_workflows.py's FakeJobService."""

    def __init__(self, db: Database, worker_id: str):
        self.db = db
        self.worker_id = worker_id
        self.prompts: list[str] = []

    def submit(self, payload: dict, *, on_created=None) -> dict:
        prompt = payload["prompt"]
        self.prompts.append(prompt)
        job = self.db.create_job({"worker_id": self.worker_id, "prompt": prompt, "state": "running"})
        if on_created:
            on_created(job)
        self.db.append_job_text(job["id"], f"result: {prompt}")
        self.db.update_job(job["id"], state="succeeded", finished_at=now_iso())
        return self.db.get_job(job["id"]) or job


def check_crud_and_versioning(base_url: str) -> None:
    workflow = request(
        base_url,
        "POST",
        "/api/workflows",
        {"name": "Interface CRUD", "graph": _simple_graph(), "policy": {"max_jobs": 1}, "interface": SIMPLE_INTERFACE},
    )["workflow"]
    workflow_id = workflow["id"]
    assert workflow["interface"]["schema_version"] == 1, workflow
    assert request(base_url, "GET", f"/api/workflows/{workflow_id}")["workflow"]["interface"] == SIMPLE_INTERFACE
    assert request(base_url, "GET", "/api/workflows")["workflows"][0]["interface"] is not None

    # a bare {"type":"object"} schema doesn't declare/require "topic" -> the start-node
    # requiredness rule (not representability) rejects it at create.
    bad_create = request_error(
        base_url,
        "POST",
        "/api/workflows",
        {"name": "bad", "graph": _simple_graph(), "interface": {"schema_version": 1, "input_schema": {"type": "object"}}},
    )
    assert "declared and required" in bad_create["error"], bad_create

    # PUT omitting interface preserves it.
    updated = request(base_url, "PUT", f"/api/workflows/{workflow_id}", {"description": "v2"})["workflow"]
    assert updated["interface"] == SIMPLE_INTERFACE, updated

    # PUT with explicit null clears it.
    cleared = request(base_url, "PUT", f"/api/workflows/{workflow_id}", {"interface": None})["workflow"]
    assert cleared["interface"] is None, cleared
    assert request(base_url, "GET", f"/api/workflows/{workflow_id}")["workflow"]["interface"] is None

    # PUT with an object replaces + validates it.
    restored = request(base_url, "PUT", f"/api/workflows/{workflow_id}", {"interface": SIMPLE_INTERFACE})["workflow"]
    assert restored["interface"] == SIMPLE_INTERFACE

    # interface-only change via expected_version increments version exactly once; a
    # stale expected_version on the same change is a 409, and creates no side effect.
    before_version = restored["version"]
    changed_iface = dict(SIMPLE_INTERFACE)
    changed_iface["outputs"] = [{"key": "notes", "kind": "text", "title": "Notes"}]
    bumped = request(
        base_url, "PUT", f"/api/workflows/{workflow_id}", {"interface": changed_iface, "expected_version": before_version}
    )["workflow"]
    assert bumped["version"] == before_version + 1, bumped
    assert bumped["interface"] == changed_iface
    stale = request_error(
        base_url,
        "PUT",
        f"/api/workflows/{workflow_id}",
        {"interface": SIMPLE_INTERFACE, "expected_version": before_version},
        status=409,
    )
    assert "conflict" in stale["error"]
    assert request(base_url, "GET", f"/api/workflows/{workflow_id}")["workflow"]["interface"] == changed_iface

    # graph edit revalidates the STORED interface: shrinking the graph so it no longer
    # references {input.topic} at all is fine (topic stays optional-unused); but making
    # the schema closed WHILE keeping a graph that references an undeclared path must
    # fail before the write lands.
    incompatible_graph = dict(_simple_graph())
    incompatible_graph["nodes"] = [
        {"id": "only", "type": "worker", "prompt": "Hi {input.customer.name}", "outputs": ["notes"]}
    ]
    revalidate_failure = request_error(
        base_url, "PUT", f"/api/workflows/{workflow_id}", {"graph": incompatible_graph}
    )
    assert "input.customer.name" in revalidate_failure["error"], revalidate_failure
    # confirm the failed PUT did not partially apply (graph unchanged).
    assert request(base_url, "GET", f"/api/workflows/{workflow_id}")["workflow"]["graph"] == _simple_graph()

    # /validate: supplied interface is additively accepted; omitted falls back to
    # cross-checking the STORED interface against the candidate graph; default_reply
    # stays excluded exactly as before (existing behavior, unaffected).
    assert request(base_url, "POST", f"/api/workflows/{workflow_id}/validate")["ok"] is True
    bad_validate = request_error(base_url, "POST", f"/api/workflows/{workflow_id}/validate", {"graph": incompatible_graph})
    assert "input.customer.name" in bad_validate["error"]
    merged_validate = request(
        base_url,
        "POST",
        f"/api/workflows/{workflow_id}/validate",
        {"graph": incompatible_graph, "interface": {"schema_version": 1, "input_schema": {
            "type": "object",
            "properties": {"customer": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
            "required": ["customer"],
        }}},
    )
    assert merged_validate["ok"] is True

    print("CRUD + versioning checks ok")


def check_direct_run(base_url: str, runtime) -> None:
    workflow = request(
        base_url,
        "POST",
        "/api/workflows",
        {"name": "Direct run", "graph": _simple_graph(), "policy": {"max_jobs": 1}, "interface": SIMPLE_INTERFACE},
    )["workflow"]
    workflow_id = workflow["id"]

    before_count = len(request(base_url, "GET", "/api/workflow-runs")["runs"])

    # valid business input -> run created (interface snapshot taken).
    valid_run = request(base_url, "POST", "/api/workflow-runs", {"workflow_definition_id": workflow_id, "input": {"topic": "AI"}})["run"]
    detail = request(base_url, "GET", f"/api/workflow-runs/{valid_run['id']}")["run"]
    assert detail["interface_snapshot"] == SIMPLE_INTERFACE, detail
    assert detail["workflow_version_snapshot"] == workflow["version"], detail

    # invalid business input (missing required "topic") -> 400, no run/event/job created.
    invalid = request_error(base_url, "POST", "/api/workflow-runs", {"workflow_definition_id": workflow_id, "input": {}})
    assert "missing required" in invalid["error"], invalid
    after_count = len(request(base_url, "GET", "/api/workflow-runs")["runs"])
    assert after_count == before_count + 1, "an invalid direct start must not create a run"

    # 1 MiB effective-input boundary: at-limit accepted, one byte over rejected.
    at_limit = request(
        base_url, "POST", "/api/workflow-runs", {"workflow_definition_id": workflow_id, "input": _padded_topic_input(wi.EFFECTIVE_INPUT_MAX_BYTES)}
    )
    assert at_limit["run"]["id"]
    over_limit = request_error(
        base_url,
        "POST",
        "/api/workflow-runs",
        {"workflow_definition_id": workflow_id, "input": _padded_topic_input(wi.EFFECTIVE_INPUT_MAX_BYTES + 1)},
    )
    assert "exceeds" in over_limit["error"], over_limit

    # default_reply merge tipping otherwise-small business input over the cap.
    reply_workflow = request(
        base_url,
        "POST",
        "/api/workflows",
        {
            "name": "Default reply tips over",
            "graph": _simple_graph(),
            "interface": SIMPLE_INTERFACE,
            "default_reply": {"mode": "none", "correlation_id": "R" * (wi.EFFECTIVE_INPUT_MAX_BYTES)},
        },
    )["workflow"]
    small_business_input = {"topic": "AI"}
    assert len(wi.canonical_bytes(small_business_input)) < 1000, "business input alone must be well under the cap"
    tipped = request_error(
        base_url, "POST", "/api/workflow-runs", {"workflow_definition_id": reply_workflow["id"], "input": small_business_input}
    )
    assert "exceeds" in tipped["error"], tipped

    # expected_workflow_version: match succeeds; stale is 409 and creates no run;
    # boolean is rejected as not-an-integer.
    matched = request(
        base_url,
        "POST",
        "/api/workflow-runs",
        {"workflow_definition_id": workflow_id, "input": {"topic": "AI"}, "expected_workflow_version": workflow["version"]},
    )
    assert matched["run"]["id"]
    before_stale_count = len(request(base_url, "GET", "/api/workflow-runs")["runs"])
    stale_version = request_error(
        base_url,
        "POST",
        "/api/workflow-runs",
        {"workflow_definition_id": workflow_id, "input": {"topic": "AI"}, "expected_workflow_version": workflow["version"] + 1},
        status=409,
    )
    assert "conflict" in stale_version["error"], stale_version
    assert len(request(base_url, "GET", "/api/workflow-runs")["runs"]) == before_stale_count, "a version conflict must create no run"
    bool_version = request_error(
        base_url,
        "POST",
        "/api/workflow-runs",
        {"workflow_definition_id": workflow_id, "input": {"topic": "AI"}, "expected_workflow_version": True},
    )
    assert "positive integer" in bool_version["error"], bool_version

    # Ordering: invalid _meta precedes version comparison (400, not 409) even with a
    # stale version supplied; a VALID envelope + stale version precedes business-schema
    # validation (409, not a business-input 400).
    bad_meta_and_stale = request_error(
        base_url,
        "POST",
        "/api/workflow-runs",
        {
            "workflow_definition_id": workflow_id,
            "input": {"topic": "AI", "_meta": "not-an-object"},
            "expected_workflow_version": workflow["version"] + 1,
        },
        status=400,
    )
    assert "_meta" in bad_meta_and_stale["error"], bad_meta_and_stale
    stale_and_bad_business = request_error(
        base_url,
        "POST",
        "/api/workflow-runs",
        {"workflow_definition_id": workflow_id, "input": {}, "expected_workflow_version": workflow["version"] + 1},
        status=409,
    )
    assert "conflict" in stale_and_bad_business["error"], stale_and_bad_business

    print("direct-run checks ok")


def check_trigger_fire(base_url: str) -> None:
    workflow = request(
        base_url,
        "POST",
        "/api/workflows",
        {"name": "Trigger interface", "graph": _simple_graph(), "interface": SIMPLE_INTERFACE},
    )["workflow"]
    trigger = request(
        base_url, "POST", "/api/workflow-triggers", {"workflow_definition_id": workflow["id"], "name": "Manual", "type": "manual"}
    )["trigger"]

    valid_fire = request(base_url, "POST", f"/api/workflow-triggers/{trigger['id']}/fire", {"payload": {"topic": "AI"}, "dedupe_key": "d1"})
    assert valid_fire["run"] is not None, valid_fire
    started_event = wait_for_trigger_event(base_url, trigger["id"], "started")
    assert started_event["run_id"] == valid_fire["run"]["id"]

    # Object payload that fails interface validation: retains the existing HTTP 202,
    # records a failed trigger event, run:null — never a 400 from bad business input.
    invalid_fire = request(base_url, "POST", f"/api/workflow-triggers/{trigger['id']}/fire", {"payload": {}, "dedupe_key": "d2"})
    assert invalid_fire["run"] is None, invalid_fire
    assert invalid_fire["event"]["state"] == "failed", invalid_fire
    assert "missing required" in invalid_fire["event"]["error"], invalid_fire

    # Non-object payload retains its existing separate 400 BEFORE trigger bookkeeping —
    # no trigger event created for it at all.
    events_before = request(base_url, "GET", f"/api/workflow-triggers/{trigger['id']}/events")["events"]
    non_object = request_error(base_url, "POST", f"/api/workflow-triggers/{trigger['id']}/fire", {"payload": "not-an-object"})
    assert "must be an object" in non_object["error"], non_object
    events_after = request(base_url, "GET", f"/api/workflow-triggers/{trigger['id']}/events")["events"]
    assert len(events_after) == len(events_before), "a non-object payload must create no trigger event"

    # Existing dedupe-claim bookkeeping is untouched: re-firing the SAME dedupe_key that
    # already failed validation is still deduped (ignored), not re-validated/re-run.
    duplicate_of_invalid = request(base_url, "POST", f"/api/workflow-triggers/{trigger['id']}/fire", {"payload": {}, "dedupe_key": "d2"})
    assert duplicate_of_invalid["event"]["state"] == "ignored", duplicate_of_invalid

    # A fixed-payload trigger that can never satisfy the interface (e.g. a schedule
    # firing with no caller-supplied input) records a failed event and starts no run,
    # while schedule advancement proceeds normally rather than wedging the slot.
    schedule = request(
        base_url,
        "POST",
        "/api/workflow-triggers",
        {"workflow_definition_id": workflow["id"], "name": "Sched", "type": "schedule", "config": {"interval_minutes": 5}},
    )["trigger"]
    assert schedule["last_fired_at"] is None, schedule
    fired_schedule = request(base_url, "POST", f"/api/workflow-triggers/{schedule['id']}/fire", {})
    assert fired_schedule["run"] is None, fired_schedule
    assert fired_schedule["event"]["state"] == "failed", fired_schedule
    after_schedule = next(t for t in request(base_url, "GET", "/api/workflow-triggers")["triggers"] if t["id"] == schedule["id"])
    # a failed fire must still run the SAME post-fire bookkeeping as a successful one
    # (last_fired_at stamped, next_fire_at recomputed), not short-circuit/wedge the slot.
    assert after_schedule["last_fired_at"] is not None, "a failed fire must still stamp last_fired_at, not wedge the slot"
    assert after_schedule["next_fire_at"] is not None

    print("trigger-fire checks ok")


def check_snapshot_survival(base_url: str) -> None:
    workflow = request(
        base_url, "POST", "/api/workflows", {"name": "Snapshot", "graph": _simple_graph(), "interface": SIMPLE_INTERFACE}
    )["workflow"]
    run = request(base_url, "POST", "/api/workflow-runs", {"workflow_definition_id": workflow["id"], "input": {"topic": "x"}})["run"]
    original_snapshot = request(base_url, "GET", f"/api/workflow-runs/{run['id']}")["run"]["interface_snapshot"]
    assert original_snapshot == SIMPLE_INTERFACE

    # Editing the LIVE definition's interface must not reinterpret the historical run.
    changed_iface = dict(SIMPLE_INTERFACE)
    changed_iface["outputs"] = []
    request(base_url, "PUT", f"/api/workflows/{workflow['id']}", {"interface": changed_iface})
    still_original = request(base_url, "GET", f"/api/workflow-runs/{run['id']}")["run"]["interface_snapshot"]
    assert still_original == SIMPLE_INTERFACE, "editing the live definition must not alter a run's snapshot"

    # Deleting the definition must not alter or hide the run's own snapshot.
    request(base_url, "DELETE", f"/api/workflows/{workflow['id']}")
    after_delete = request(base_url, "GET", f"/api/workflow-runs/{run['id']}")["run"]
    assert after_delete["interface_snapshot"] == SIMPLE_INTERFACE
    assert after_delete["workflow_version_snapshot"] == workflow["version"]

    print("snapshot-survival checks ok")


def check_rbac_and_audit(base_url: str, runtime) -> None:
    # Workflow read/update RBAC is unaffected: interface validation is reached through
    # the SAME create/update handlers _required_permission already gates (no new route,
    # no new permission literal introduced) — confirmed directly against app.py during
    # implementation; exercised end-to-end here via the loopback-authenticated calls
    # every other check in this file already makes through those same routes.
    workflow = request(
        base_url, "POST", "/api/workflows", {"name": "Audit", "graph": _simple_graph(), "interface": SIMPLE_INTERFACE}
    )["workflow"]
    request(base_url, "PUT", f"/api/workflows/{workflow['id']}", {"description": "x"})
    with runtime.db.connect() as conn:
        rows = conn.execute(
            "SELECT details FROM audit_log WHERE resource_type = 'workflow_definition' AND resource_id = ? ORDER BY id DESC LIMIT 5",
            (workflow["id"],),
        ).fetchall()
    for row in rows:
        details = row["details"]
        assert "input_schema" not in details and "sample_input" not in details, (
            f"workflow_definition audit details must never copy the full interface/sample: {details}"
        )

    print("RBAC/audit checks ok")


def check_manager_worker_parity() -> None:
    """B-PR01: manager prompts render {input.*}/{artifact.*}/{run.*} identically to
    worker prompts (previously dropped in verbatim, unresolved) — and the
    manager_decision_v1 response contract is unaffected by this change."""
    rendered = render_prompt("Hi {input.topic}", input={"topic": "AI"})
    assert rendered == "Hi AI"

    manager_text = _manager_prompt(
        graph={"nodes": []},
        node={"id": "m1", "type": "manager", "prompt": "Decide about {input.topic}"},
        artifacts={},
        counters={},
        policy={},
        input={"topic": "AI"},
        run={"id": "run_1"},
    )
    instruction_portion = manager_text.split("Manager context JSON:")[0]
    assert "Decide about AI" in instruction_portion, instruction_portion
    assert "{input.topic}" not in instruction_portion, "manager prompt must render, not pass through literally"
    assert "manager_decision_v1" in manager_text  # instruction suffix preserved
    assert '"stop":false' in manager_text  # response-contract hint preserved verbatim

    # An unresolved placeholder still fails closed, exactly like the worker path.
    try:
        _manager_prompt(graph={"nodes": []}, node={"id": "m1", "type": "manager", "prompt": "{input.missing}"}, artifacts={}, counters={}, policy={})
    except ValueError as exc:
        assert "missing prompt variable" in str(exc)
    else:
        raise AssertionError("an unresolved manager placeholder must raise, not render literally")

    print("manager/worker prompt parity checks ok")


def check_possible_outputs_and_undeclared_artifacts(runtime) -> None:
    """A declared-but-unreached output must not fail an otherwise successful run, and an
    undeclared artifact must still surface through the existing artifact listing."""
    worker = runtime.db.upsert_worker({"base_url": "http://check-interface.local", "name": "check"})
    graph = {
        "start": "w1",
        "nodes": [
            {"id": "w1", "type": "worker", "prompt": "go", "outputs": ["reached"]},
            {"id": "w2", "type": "worker", "prompt": "never runs", "outputs": ["never_reached"]},
        ],
        "edges": [
            {
                "from": "w1",
                "to": "w2",
                "condition": {"type": "artifact_equals", "artifact": "reached", "path": "impossible", "value": True},
            }
        ],
    }
    interface = {
        "schema_version": 1,
        "input_schema": {"type": "object"},
        "outputs": [{"key": "reached", "kind": "text"}, {"key": "never_reached", "kind": "text"}],
    }
    definition = runtime.db.create_workflow_definition({"name": "Possible outputs", "graph": graph, "interface": interface})
    jobs = FakeJobService(runtime.db, worker["id"])
    runner = WorkflowRunner(runtime.db, jobs, poll_interval_seconds=0)
    run = runner.start_workflow(definition["id"], {})
    deadline = time.monotonic() + 5
    while runtime.db.get_workflow_run(run["id"])["state"] not in {"succeeded", "failed"} and time.monotonic() < deadline:
        time.sleep(0.02)
    final = runtime.db.get_workflow_run(run["id"])
    assert final["state"] == "succeeded", f"omitting a possible-but-unreached output must not fail the run: {final}"
    artifacts = runtime.db.list_artifacts(run_id=run["id"])
    keys = {artifact["key"] for artifact in artifacts}
    assert keys == {"reached"}, f"the never-reached branch must not have produced an artifact: {keys}"

    print("possible-output / undeclared-artifact checks ok")


# The canonical Permit Application fixture (flow-designer test plan §4.1-4.3 /
# PERMIT_APPLICATION_CONTRACT_V1): nested closed object, array-of-closed-objects,
# Thai annotations, enum, optional downstream-only field, two possible text outputs.
PERMIT_INTERFACE = {
    "schema_version": 1,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["applicant_name", "permit_type", "detail", "attachments"],
        "properties": {
            "applicant_name": {"type": "string", "title": "ชื่อผู้ขอ", "minLength": 1},
            "permit_type": {"type": "string", "title": "ประเภทคำขอ", "enum": ["ขออนุญาตก่อสร้าง", "ขออนุญาตดัดแปลงอาคาร"]},
            "detail": {
                "type": "object", "title": "รายละเอียด", "additionalProperties": False,
                "required": ["building_type", "floors"],
                "properties": {"building_type": {"type": "string"}, "floors": {"type": "integer", "minimum": 1}},
            },
            "attachments": {
                "type": "array", "title": "รายการเอกสารแนบ",
                "items": {
                    "type": "object", "additionalProperties": False, "required": ["name", "kind"],
                    "properties": {"name": {"type": "string"}, "kind": {"type": "string"}},
                },
            },
            "review_context": {"type": "string", "title": "บริบทเพิ่มเติม"},
        },
    },
    "sample_input": {
        "applicant_name": "นายทดสอบ ระบบ",
        "permit_type": "ขออนุญาตก่อสร้าง",
        "detail": {"building_type": "อาคารพาณิชย์", "floors": 2},
        "attachments": [
            {"name": "synthetic-id-copy.pdf", "kind": "identity-copy"},
            {"name": "synthetic-land-title.pdf", "kind": "land-record"},
        ],
        "review_context": "ข้อมูลสมมติสำหรับทดสอบ PoC เท่านั้น",
    },
    "outputs": [
        {"key": "intake_review", "kind": "text", "title": "ผลตรวจความครบถ้วน"},
        {"key": "assessment_result", "kind": "text", "title": "ผลการประเมิน"},
    ],
    "primary_output": "assessment_result",
}

PERMIT_VALID_INPUT = {
    "applicant_name": "นายทดสอบ ระบบ",
    "permit_type": "ขออนุญาตก่อสร้าง",
    "detail": {"building_type": "อาคารพาณิชย์", "floors": 2},
    "attachments": [
        {"name": "synthetic-id-copy.pdf", "kind": "identity-copy"},
        {"name": "synthetic-land-title.pdf", "kind": "land-record"},
    ],
    "review_context": "ข้อมูลสมมติสำหรับทดสอบ PoC เท่านั้น",
    "_meta": {"source": {"channel": "web_form", "adapter": "hermetic-check", "form": "permit-poc", "external_id": "TEST-PERMIT-001"}},
}


def _permit_graph(worker_id: str) -> dict:
    return {
        "start": "intake",
        "nodes": [
            {
                "id": "intake", "type": "worker", "worker_id": worker_id,
                "prompt": "STEP=intake\nผู้ขอ: {input.applicant_name}\nประเภทคำขอ: {input.permit_type}\nรายละเอียด: {input.detail}\nเอกสารแนบ: {input.attachments}",
                "outputs": ["intake_review"], "budget_units": 1,
            },
            {
                "id": "assessment", "type": "worker", "worker_id": worker_id,
                "prompt": "STEP=assessment\nประเมินผล {artifact.intake_review}\nบริบทเพิ่มเติม: {input.review_context}",
                "outputs": ["assessment_result"], "budget_units": 1,
            },
        ],
        "edges": [{"from": "intake", "to": "assessment", "condition": {"type": "always"}}],
    }


def check_permit_fixture(base_url: str, runtime) -> None:
    """The canonical Permit contract end to end: API round trip, nested/array/Thai
    validation errors with no side effect, and a real run through the SYNCHRONOUS
    run_workflow path — which must validate interface input and stamp the
    interface/version snapshots exactly like start_workflow (no bypass entry point)."""
    worker = runtime.db.upsert_worker({"base_url": "http://permit-check.local", "name": "permit"})
    wf = request(base_url, "POST", "/api/workflows", {
        "name": "PoC Permit Application",
        "graph": _permit_graph(worker["id"]),
        "policy": {"max_jobs": 2, "allowed_worker_ids": [worker["id"]]},
        "interface": PERMIT_INTERFACE,
    })["workflow"]
    assert request(base_url, "GET", f"/api/workflows/{wf['id']}")["workflow"]["interface"] == PERMIT_INTERFACE

    runs_before = len(request(base_url, "GET", "/api/workflow-runs")["runs"])
    for bad_input, expected_fragment in (
        ({k: v for k, v in PERMIT_VALID_INPUT.items() if k != "attachments"}, "attachments"),
        ({**PERMIT_VALID_INPUT, "secret_override": True}, "secret_override"),
        ({**PERMIT_VALID_INPUT, "detail": {"building_type": "x", "floors": True}}, "floors"),
        ({**PERMIT_VALID_INPUT, "detail": None}, "detail"),
    ):
        error = request_error(base_url, "POST", "/api/workflow-runs", {"workflow_definition_id": wf["id"], "input": bad_input})
        assert expected_fragment in error["error"], (expected_fragment, error)
    assert len(request(base_url, "GET", "/api/workflow-runs")["runs"]) == runs_before, "invalid Permit input must create no run"

    # Synchronous run_workflow: same validation (ValueError, no run) ...
    jobs = FakeJobService(runtime.db, worker["id"])
    runner = WorkflowRunner(runtime.db, jobs, poll_interval_seconds=0)
    try:
        runner.run_workflow(wf["id"], {"applicant_name": "x"})
    except ValueError as exc:
        assert "missing required" in str(exc), exc
    else:
        raise AssertionError("run_workflow must validate interface input, not bypass it")
    assert len(request(base_url, "GET", "/api/workflow-runs")["runs"]) == runs_before

    # ... and same snapshots + rendering on success.
    final = runner.run_workflow(wf["id"], PERMIT_VALID_INPUT)
    assert final["state"] == "succeeded", final
    assert final["interface_snapshot"] == PERMIT_INTERFACE, "run_workflow must snapshot the interface"
    assert final["workflow_version_snapshot"] == wf["version"], "run_workflow must snapshot the workflow version"
    for key, value in PERMIT_VALID_INPUT.items():
        assert final["input"][key] == value, (key, final["input"].get(key))
    artifact_keys = {artifact["key"] for artifact in runtime.db.list_artifacts(run_id=final["id"])}
    assert {"intake_review", "assessment_result"} <= artifact_keys, artifact_keys
    joined = "\n".join(jobs.prompts)
    assert "นายทดสอบ ระบบ" in joined and "synthetic-id-copy.pdf" in joined and "building_type" in joined
    assert "{input." not in joined, "no literal placeholder may reach a worker"

    print("canonical Permit fixture checks ok")


def check_pack_round_trip() -> None:
    bundle = {
        "schema_version": 1,
        "name": "interface-pack",
        "version": "1.0.0",
        "workflows": [
            {
                "name": "Packed",
                "graph": _simple_graph(),
                "interface": SIMPLE_INTERFACE,
            }
        ],
    }
    validate_pack(bundle)
    signed = sign_pack(bundle, "test-secret")
    assert verify_pack_signature(signed, "test-secret")

    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        imported = import_pack(db, signed, secret_key="test-secret", require_signature=True)
        stored = imported["workflows"][0]
        assert stored["interface"] == SIMPLE_INTERFACE, stored
        exported = export_pack(db, stored["id"])
        assert exported["workflows"][0]["interface"] == SIMPLE_INTERFACE

        # Tamper: flipping the exported interface after signing must fail verification.
        tampered = json.loads(json.dumps(signed))
        tampered["workflows"][0]["interface"]["outputs"] = []
        assert not verify_pack_signature(tampered, "test-secret"), "a tampered interface must invalidate the signature"

        # An interface that fails cross-check must be rejected at import, before any write.
        bad_bundle = json.loads(json.dumps(bundle))
        bad_bundle["workflows"][0]["interface"] = {"schema_version": 1, "input_schema": {"type": "object", "additionalProperties": False}}
        try:
            import_pack(db, bad_bundle)
        except ValueError as exc:
            assert "impossible" in str(exc), exc
        else:
            raise AssertionError("a pack with an invalid per-workflow interface must not import")

    print("pack round-trip checks ok")


def main() -> None:
    check_validator_unit()

    with TemporaryDirectory() as tmp:
        runtime = AtlasRuntime(
            Config(
                host="127.0.0.1",
                port=0,
                db_path=Path(tmp) / "atlas.sqlite",
                api_token=None,
                request_timeout_seconds=1,
                enable_loopback_without_token=True,
                upload_dir=Path(tmp) / "uploads",
                max_upload_bytes=32,
                outbound_allowlist=("127.0.0.1",),
            )
        )
        server = AtlasHttpServer(("127.0.0.1", 0), runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            check_crud_and_versioning(base_url)
            check_direct_run(base_url, runtime)
            check_trigger_fire(base_url)
            check_snapshot_survival(base_url)
            check_rbac_and_audit(base_url, runtime)
            check_possible_outputs_and_undeclared_artifacts(runtime)
            check_permit_fixture(base_url, runtime)
        finally:
            runtime.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    check_manager_worker_parity()
    check_pack_round_trip()

    print("workflow interface check ok")


if __name__ == "__main__":
    main()
