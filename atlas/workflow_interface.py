"""Bounded, standard-library validator for the optional `workflow.interface` v1
contract (docs/adr/0002-workflow-interface-contract.md). A profile, not a JSON Schema
engine: a small allowlisted keyword set, hard bounds on size/depth/count, and a
graph-aware cross-check that a declared schema doesn't make a workflow's own start
prompt impossible to satisfy. This is the SINGLE validator reused by the workflow API,
the run-start path, pack import, and the hermetic checks — never duplicate this logic.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

INTERFACE_SCHEMA_VERSION = 1
INPUT_SCHEMA_URI = "https://atlas.local/schemas/workflow-interface-input-v1.schema.json"

# Byte caps, measured via canonical_bytes() below.
INTERFACE_MAX_BYTES = 65536
SAMPLE_MAX_BYTES = 65536
EFFECTIVE_INPUT_MAX_BYTES = 1048576

# Schema/instance bounds (docs/adr/0002-workflow-interface-contract.md §3).
MAX_SCHEMA_DEPTH = 16
MAX_PROPERTIES = 256
MAX_LIST_ENTRIES = 256
MAX_OUTPUTS = 256
MAX_TRAVERSAL_NODES = 10000
MAX_TITLE_CODEPOINTS = 256
MAX_DESCRIPTION_CODEPOINTS = 2048

# Reserved top-level run-input keys excluded from business-schema validation. Exactly
# these two, never every underscore-prefixed key — that would be a validation bypass.
RESERVED_INPUT_FIELDS = ("_meta", "_trigger_chain")

_PRIMITIVE_TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})
_SCHEMA_KEYWORDS = frozenset(
    {
        "type", "properties", "required", "additionalProperties", "items", "enum", "const",
        "minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems",
        "title", "description", "default", "examples", "$schema",
    }
)
_INTERFACE_TOP_KEYS = frozenset({"schema_version", "input_schema", "sample_input", "outputs", "primary_output"})
_OUTPUT_ENTRY_KEYS = frozenset({"key", "kind", "title", "description"})
_OUTPUT_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_PROMPT_INPUT_PATH_RE = re.compile(r"{(input(?:\.[A-Za-z_]\w*)+)}")


def canonical_bytes(value: Any) -> bytes:
    """The one serialization every byte cap is measured against. `allow_nan=False`
    doubles as the "reject non-finite numbers" rule for any value that flows through
    this function (interface, sample_input, and run input all do)."""
    try:
        return json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except ValueError as exc:
        raise ValueError(f"value contains a non-finite number: {exc}") from exc


def business_projection(input_value: dict[str, Any]) -> dict[str, Any]:
    """Business-input validation target: `input` minus exactly the reserved keys. The
    complete input (including reserved keys) is still what gets persisted/size-capped —
    this projection narrows only what gets validated against input_schema."""
    return {key: value for key, value in input_value.items() if key not in RESERVED_INPUT_FIELDS}


def check_effective_input_size(input_value: Any) -> None:
    if len(canonical_bytes(input_value)) > EFFECTIVE_INPUT_MAX_BYTES:
        raise ValueError(f"effective input exceeds {EFFECTIVE_INPUT_MAX_BYTES} bytes")


def _is_nonneg_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _schema_types(schema: dict[str, Any]) -> tuple[str, ...]:
    node_type = schema.get("type")
    if node_type is None:
        return ()
    if isinstance(node_type, str):
        return (node_type,)
    if isinstance(node_type, list):
        return tuple(node_type)
    return ()


def _is_exactly_object(schema: Any) -> bool:
    return isinstance(schema, dict) and _schema_types(schema) == ("object",)


class _PropertyBudget:
    """Cumulative declared-property count across the WHOLE schema document (not
    per-object) — mutated by reference through the recursive walk."""

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0


def _validate_schema_node(node: Any, path: str, depth: int, budget: _PropertyBudget, *, is_root: bool) -> None:
    if depth > MAX_SCHEMA_DEPTH:
        raise ValueError(f"{path}: schema nesting exceeds {MAX_SCHEMA_DEPTH} levels")
    if not isinstance(node, dict):
        raise ValueError(f"{path}: schema node must be an object")
    unknown = sorted(set(node) - _SCHEMA_KEYWORDS)
    if unknown:
        raise ValueError(f"{path}: unsupported schema keyword(s): {', '.join(unknown)}")

    if "$schema" in node:
        if not is_root:
            raise ValueError(f"{path}.$schema: only allowed at the schema root")
        if node["$schema"] != INPUT_SCHEMA_URI:
            raise ValueError(f"{path}.$schema: must be exactly {INPUT_SCHEMA_URI!r}")

    types: tuple[str, ...] = ()
    if "type" in node:
        raw_type = node["type"]
        if isinstance(raw_type, str):
            types = (raw_type,)
        elif (
            isinstance(raw_type, list)
            and raw_type
            and all(isinstance(entry, str) for entry in raw_type)  # before set(): a dict/list entry is unhashable
            and len(raw_type) == len(set(raw_type))
        ):
            types = tuple(raw_type)
        else:
            raise ValueError(f"{path}.type: must be a primitive type string or a unique list of them")
        for one_type in types:
            if one_type not in _PRIMITIVE_TYPES:
                raise ValueError(f"{path}.type: unsupported type {one_type!r}")

    if is_root and types != ("object",):
        raise ValueError(f"{path}: root input_schema must declare exactly type \"object\"")

    for key, cap in (("title", MAX_TITLE_CODEPOINTS), ("description", MAX_DESCRIPTION_CODEPOINTS)):
        if key not in node:
            continue
        if not isinstance(node[key], str):
            raise ValueError(f"{path}.{key}: must be a string")
        if len(node[key]) > cap:
            raise ValueError(f"{path}.{key}: exceeds {cap} Unicode code points")

    if "examples" in node and not isinstance(node["examples"], list):
        raise ValueError(f"{path}.examples: must be a list")

    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in node and not _is_nonneg_int(node[key]):
            raise ValueError(f"{path}.{key}: must be a non-negative integer")
    for key in ("minimum", "maximum"):
        if key in node and not _is_finite_number(node[key]):
            raise ValueError(f"{path}.{key}: must be a finite number")

    if "additionalProperties" in node and not isinstance(node["additionalProperties"], bool):
        raise ValueError(f"{path}.additionalProperties: must be a boolean")

    if "required" in node:
        required = node["required"]
        if not isinstance(required, list) or not all(isinstance(item, str) and item for item in required):
            raise ValueError(f"{path}.required: must be a list of non-empty strings")
        if len(required) > MAX_LIST_ENTRIES:
            raise ValueError(f"{path}.required: exceeds {MAX_LIST_ENTRIES} entries")
        if len(required) != len(set(required)):
            raise ValueError(f"{path}.required: entries must be unique")

    if "enum" in node:
        enum = node["enum"]
        if not isinstance(enum, list) or not enum:
            raise ValueError(f"{path}.enum: must be a non-empty list")
        if len(enum) > MAX_LIST_ENTRIES:
            raise ValueError(f"{path}.enum: exceeds {MAX_LIST_ENTRIES} entries")

    if "properties" in node:
        properties = node["properties"]
        if not isinstance(properties, dict):
            raise ValueError(f"{path}.properties: must be an object")
        budget.count += len(properties)
        if budget.count > MAX_PROPERTIES:
            raise ValueError(f"{path}.properties: total declared properties exceed {MAX_PROPERTIES}")
        for key, subschema in properties.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"{path}.properties: property names must be non-empty strings")
            _validate_schema_node(subschema, f"{path}.properties.{key}", depth + 1, budget, is_root=False)

    if "items" in node:
        _validate_schema_node(node["items"], f"{path}.items", depth + 1, budget, is_root=False)


def validate_input_schema(input_schema: Any) -> None:
    """Validate a bare `input_schema` document (root must be exactly `type: "object"`)
    against the bounded profile. Raises path-aware `ValueError` on any violation."""
    _validate_schema_node(input_schema, "$.input_schema", 1, _PropertyBudget(), is_root=True)


def _json_equal(a: Any, b: Any) -> bool:
    """Equality with JSON type fidelity: bool is never equal to a number (`True != 1`),
    used for `enum`/`const` comparison."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_json_equal(value, b[key]) for key, value in a.items())
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return type(a) is type(b) and a == b


def _instance_matches_type(instance: Any, type_name: str) -> bool:
    if type_name == "object":
        return isinstance(instance, dict)
    if type_name == "array":
        return isinstance(instance, list)
    if type_name == "string":
        return isinstance(instance, str)
    if type_name == "boolean":
        return isinstance(instance, bool)
    if type_name == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if type_name == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if type_name == "null":
        return instance is None
    return False


class _TraversalBudget:
    __slots__ = ("remaining",)

    def __init__(self, remaining: int) -> None:
        self.remaining = remaining

    def spend(self, path: str) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise ValueError(f"{path}: input exceeds the {MAX_TRAVERSAL_NODES}-node validation traversal bound")


def _validate_instance_node(schema: dict[str, Any], instance: Any, path: str, budget: _TraversalBudget) -> None:
    budget.spend(path)
    types = _schema_types(schema)
    if types and not any(_instance_matches_type(instance, one_type) for one_type in types):
        raise ValueError(f"{path}: expected type {'/'.join(types)}")
    if "const" in schema and not _json_equal(instance, schema["const"]):
        raise ValueError(f"{path}: does not match const")
    if "enum" in schema and not any(_json_equal(instance, item) for item in schema["enum"]):
        raise ValueError(f"{path}: does not match enum")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise ValueError(f"{path}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            raise ValueError(f"{path}: longer than maxLength {schema['maxLength']}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise ValueError(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            raise ValueError(f"{path}: above maximum {schema['maximum']}")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise ValueError(f"{path}: fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            raise ValueError(f"{path}: more than maxItems {schema['maxItems']}")
        if "items" in schema:
            for index, item in enumerate(instance):
                _validate_instance_node(schema["items"], item, f"{path}[{index}]", budget)
    if isinstance(instance, dict):
        properties = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in instance:
                raise ValueError(f"{path}: missing required property {key!r}")
        additional = schema.get("additionalProperties")
        for key, value in instance.items():
            if key in properties:
                _validate_instance_node(properties[key], value, f"{path}.{key}", budget)
            elif additional is False:
                raise ValueError(f"{path}.{key}: additional property not allowed")


def validate_business_input(input_schema: dict[str, Any], instance: Any) -> None:
    if not isinstance(instance, dict):
        raise ValueError("$: business input must be an object")
    _validate_instance_node(input_schema, instance, "$", _TraversalBudget(MAX_TRAVERSAL_NODES))


def validate_interface_document(interface: Any) -> None:
    """Structural validation of an `interface` object on its own: schema_version,
    input_schema profile, sample_input conformance, outputs shape. Does not touch a
    workflow graph — see cross_check_against_graph / validate_interface for that."""
    if interface is None:
        return
    if not isinstance(interface, dict):
        raise ValueError("interface: must be an object or null")
    unknown = sorted(set(interface) - _INTERFACE_TOP_KEYS)
    if unknown:
        raise ValueError(f"interface: unknown field(s): {', '.join(unknown)}")
    if interface.get("schema_version") != INTERFACE_SCHEMA_VERSION:
        raise ValueError(f"interface.schema_version must be {INTERFACE_SCHEMA_VERSION}")
    if "input_schema" not in interface:
        raise ValueError("interface.input_schema is required")
    if len(canonical_bytes(interface)) > INTERFACE_MAX_BYTES:
        raise ValueError(f"interface exceeds {INTERFACE_MAX_BYTES} bytes")

    input_schema = interface["input_schema"]
    validate_input_schema(input_schema)

    if "sample_input" in interface and interface["sample_input"] is not None:
        sample = interface["sample_input"]
        if not isinstance(sample, dict):
            raise ValueError("interface.sample_input: must be an object")
        if len(canonical_bytes(sample)) > SAMPLE_MAX_BYTES:
            raise ValueError(f"interface.sample_input exceeds {SAMPLE_MAX_BYTES} bytes")
        validate_business_input(input_schema, sample)

    outputs = interface.get("outputs") or []
    if not isinstance(outputs, list):
        raise ValueError("interface.outputs: must be a list")
    if len(outputs) > MAX_OUTPUTS:
        raise ValueError(f"interface.outputs: exceeds {MAX_OUTPUTS} entries")
    seen_keys: set[str] = set()
    for index, entry in enumerate(outputs):
        if not isinstance(entry, dict):
            raise ValueError(f"interface.outputs[{index}]: must be an object")
        unknown_entry = sorted(set(entry) - _OUTPUT_ENTRY_KEYS)
        if unknown_entry:
            raise ValueError(f"interface.outputs[{index}]: unknown field(s): {', '.join(unknown_entry)}")
        key = entry.get("key")
        if not isinstance(key, str) or not _OUTPUT_KEY_RE.match(key):
            raise ValueError(f"interface.outputs[{index}].key: must match {_OUTPUT_KEY_RE.pattern}")
        if key in seen_keys:
            raise ValueError(f"interface.outputs[{index}].key: duplicate key {key!r}")
        seen_keys.add(key)
        if entry.get("kind") not in ("text", "json"):
            raise ValueError(f"interface.outputs[{index}].kind: must be 'text' or 'json'")
        for field, cap in (("title", MAX_TITLE_CODEPOINTS), ("description", MAX_DESCRIPTION_CODEPOINTS)):
            if field not in entry:
                continue
            if not isinstance(entry[field], str) or len(entry[field]) > cap:
                raise ValueError(f"interface.outputs[{index}].{field}: invalid or exceeds {cap} code points")

    primary_output = interface.get("primary_output")
    if primary_output is not None and (not isinstance(primary_output, str) or primary_output not in seen_keys):
        raise ValueError("interface.primary_output: must name a declared outputs[].key")


def _path_representable(schema: Any, parts: list[str]) -> bool:
    """True if some value could exist at this dotted `{input.*}` path under `schema` —
    i.e. a closed (additionalProperties:false) schema hasn't made the path impossible."""
    if not parts:
        return True
    if not isinstance(schema, dict):
        return False
    types = _schema_types(schema)
    if types and "object" not in types:
        return False
    properties = schema.get("properties") or {}
    key = parts[0]
    if key in properties:
        return _path_representable(properties[key], parts[1:])
    return schema.get("additionalProperties") is not False


def _path_required_and_typed(schema: dict[str, Any], parts: list[str]) -> bool:
    """True if every INTERMEDIATE segment of `parts` is declared+required under a
    schema that is exactly `type: "object"` (no nullable/mixed union) — the start-node
    requiredness rule. The final (leaf) segment's own type is unconstrained."""
    current = schema
    for key in parts:
        if not _is_exactly_object(current):
            return False
        properties = current.get("properties") or {}
        required = current.get("required") or []
        if key not in properties or key not in required:
            return False
        current = properties[key]
    return True


def _extract_input_paths(text: Any) -> list[list[str]]:
    if not isinstance(text, str):
        return []
    return [match.group(1).split(".")[1:] for match in _PROMPT_INPUT_PATH_RE.finditer(text)]


def cross_check_against_graph(interface: Any, graph: dict[str, Any]) -> None:
    """Graph-dependent half of interface validation: each declared output is produced
    by exactly one worker node with a matching kind/output_format, and every
    `{input.*}` path referenced by a node's prompt is representable under the schema —
    required (and object-typed at every intermediate segment) when that node is the
    graph's start node, merely representable (not impossible) otherwise."""
    if interface is None:
        return
    input_schema = interface["input_schema"]
    nodes = {node["id"]: node for node in graph.get("nodes") or [] if isinstance(node, dict) and node.get("id")}
    start_id = graph.get("start")

    producers: dict[str, list[dict[str, Any]]] = {}
    for node in nodes.values():
        if node.get("type") != "worker":
            continue
        for key in node.get("outputs") or []:
            producers.setdefault(key, []).append(node)
    for index, entry in enumerate(interface.get("outputs") or []):
        key = entry["key"]
        matches = producers.get(key) or []
        if len(matches) != 1:
            raise ValueError(
                f"interface.outputs[{index}].key {key!r} must be produced by exactly one worker node "
                f"(found {len(matches)})"
            )
        expected_kind = "json" if matches[0].get("output_format") == "json" else "text"
        if entry["kind"] != expected_kind:
            raise ValueError(
                f"interface.outputs[{index}].kind must be {expected_kind!r} to match "
                f"node {matches[0]['id']!r}'s output_format"
            )

    for node in nodes.values():
        if node.get("type") not in {"worker", "manager"}:
            continue
        is_start = node.get("id") == start_id
        for parts in _extract_input_paths(node.get("prompt")):
            dotted = ".".join(parts)
            if not _path_representable(input_schema, parts):
                raise ValueError(
                    f"workflow node {node['id']!r} prompt references input.{dotted}, "
                    "which is impossible under interface.input_schema"
                )
            if is_start and not _path_required_and_typed(input_schema, parts):
                raise ValueError(
                    f"workflow node {node['id']!r} is the graph start node; input.{dotted} must be "
                    "declared and required at every object segment (each intermediate segment "
                    "exactly type \"object\", no nullable/mixed union)"
                )


def validate_interface(interface: Any, graph: dict[str, Any]) -> None:
    """The one entry point most callers need: structural validation followed by the
    graph cross-check. A no-op when interface is None (legacy/no-contract workflow)."""
    validate_interface_document(interface)
    cross_check_against_graph(interface, graph)


def validate_run_input(interface: dict[str, Any] | None, complete_input: dict[str, Any]) -> None:
    """Direct-run / trigger-fire business-input validation: the 1 MiB effective-input
    cap on the COMPLETE input (before projection, so a large _meta/_trigger_chain can't
    dodge it), then business-schema validation of the projection. A no-op when the
    workflow has no interface — exact legacy behavior."""
    if interface is None:
        return
    check_effective_input_size(complete_input)
    validate_business_input(interface["input_schema"], business_projection(complete_input))
