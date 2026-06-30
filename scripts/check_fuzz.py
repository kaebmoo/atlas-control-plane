"""Parser/validator fuzz (the INPUT-ROBUSTNESS axis at the trust boundary — distinct from the
concurrency axis in check_stress): throw random and malformed inputs at the SSE/CSV parsers and
the public validators. Parsers must never raise (only return / yield, or a controlled
ThClawsError); validators must reject bad input with ValueError only — any other exception is a
robustness bug (a 500 instead of a 400 at the HTTP boundary).

These are FINITE seeded samples: green is evidence that the common malformed shapes are handled,
NOT a proof that no input can crash them. Increase iterations / seeds to widen coverage."""

from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.thclaws_client import SseEvent, ThClawsError, extract_session_id, extract_text, iter_sse, parse_event_payload
from atlas.usage import usage_csv
from atlas.workflows import validate_workflow_graph, validate_workflow_policy, validate_workflow_trigger_payload

random.seed(20260630)

_ATOMS = [None, True, False, 0, 1, -1, 1.5, "", "x", "=1+1", "a\nb", "\t@", "ünïç"]


def _rand_value(depth: int = 0):
    if depth > 3:
        return random.choice(_ATOMS)
    roll = random.random()
    if roll < 0.55:
        return random.choice(_ATOMS)
    if roll < 0.75:
        return [_rand_value(depth + 1) for _ in range(random.randint(0, 4))]
    return {random.choice(["a", "id", "text", "type", "config", "graph", "nodes", "start", "key", "kind"]): _rand_value(depth + 1) for _ in range(random.randint(0, 4))}


def check_sse_parsers_never_crash() -> None:
    for _ in range(2000):
        data = random.choice(["[DONE]", "{}", "not json", "{\"id\":1}", "{\"text\":\"x\"}", "\x00\xff", str(_rand_value())])
        event = SseEvent(event=random.choice(["message", "text", "session", "delta", "done", ""]), data=data)
        assert extract_text(event) is None or isinstance(extract_text(event), str)
        assert extract_session_id(event) is None or isinstance(extract_session_id(event), str)
        assert isinstance(parse_event_payload(event), dict)
    # iter_sse over a random raw byte stream: only ThClawsError is allowed to escape.
    import io

    for _ in range(500):
        blob = b"".join(random.choice([b":ping\n", b"data: x\n", b"event: t\n", b"\n", b"data:\xff\xfe\n", b"garbage"]) for _ in range(random.randint(0, 20)))
        try:
            list(iter_sse(io.BytesIO(blob)))
        except ThClawsError:
            pass


def check_csv_never_crashes() -> None:
    for _ in range(1000):
        row = {k: _rand_value() for k in ("kind", "actor", "model", "status", "node_key", "metadata")}
        out = usage_csv([row])
        assert isinstance(out, str)


def check_validators_only_raise_valueerror() -> None:
    validators = [
        ("trigger", lambda v: validate_workflow_trigger_payload(v if isinstance(v, dict) else {"x": v})),
        ("graph", lambda v: validate_workflow_graph(v if isinstance(v, dict) else {}, {})),
        ("policy", lambda v: validate_workflow_policy(v if isinstance(v, dict) else {})),
    ]
    for _ in range(3000):
        value = _rand_value()
        for name, fn in validators:
            try:
                fn(value)
            except ValueError:
                pass
            except Exception as exc:  # noqa: BLE001 — the whole point is to catch the uncontrolled ones
                raise AssertionError(f"{name} validator raised {type(exc).__name__} (not ValueError) on {value!r}: {exc}") from exc


def main() -> None:
    check_sse_parsers_never_crash()
    check_csv_never_crashes()
    check_validators_only_raise_valueerror()
    print("fuzz check ok")


if __name__ == "__main__":
    main()
