from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator


class ThClawsError(RuntimeError):
    pass


@dataclass(frozen=True)
class SseEvent:
    event: str
    data: str

    def json_data(self) -> Any:
        try:
            return json.loads(self.data)
        except json.JSONDecodeError:
            return self.data


class ThClawsClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: Any = None, timeout: float | None = None) -> urllib.response.addinfourl:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(f"{self.base_url}{path}", data=body, headers=headers, method=method)
        try:
            return urllib.request.urlopen(request, timeout=self.timeout if timeout is None else timeout)  # nosec B310
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise ThClawsError(f"thClaws HTTP {exc.code}: {details}") from exc
        except urllib.error.URLError as exc:
            raise ThClawsError(f"thClaws connection error: {exc.reason}") from exc

    def get_json(self, path: str) -> Any:
        with self._request("GET", path) as response:
            body = response.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body or "{}")
        except json.JSONDecodeError as exc:
            raise ThClawsError(f"Invalid JSON from {path}: {body[:200]}") from exc

    def get_text(self, path: str) -> tuple[int, str]:
        with self._request("GET", path) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = response.getcode() or 0
        return status, body

    def health(self) -> dict[str, Any]:
        status, body = self.get_text("/healthz")
        text = body.strip()
        if not text:
            return {"ok": 200 <= status < 300, "status": status}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"ok": 200 <= status < 300, "status": status, "body": text}
        if isinstance(parsed, dict):
            parsed.setdefault("ok", 200 <= status < 300)
            parsed.setdefault("status", status)
            return parsed
        return {"ok": 200 <= status < 300, "status": status, "body": parsed}


    def agent_info(self) -> dict[str, Any]:
        return self.get_json("/v1/agent/info")

    def run_agent_stream(
        self,
        *,
        prompt: str,
        workspace_dir: str | None = None,
        system: str | None = None,
        model: str | None = None,
        session_id: str | None = None,
        max_tokens: int | None = None,
        x_callback: str | None = None,
        stream_deadline: float | None = None,
        max_total_bytes: int | None = None,
    ) -> Iterator[SseEvent]:
        payload: dict[str, Any] = {
            "prompt": prompt,
            "stream": True,
        }
        if workspace_dir:
            payload["workspace_dir"] = workspace_dir
        if system:
            payload["system"] = system
        if model:
            payload["model"] = model
        if session_id:
            payload["session_id"] = session_id
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if x_callback:
            payload["x_callback"] = x_callback

        response = self._request("POST", "/agent/run", payload=payload, timeout=None)
        try:
            yield from iter_sse(response, stream_deadline=stream_deadline, max_total_bytes=max_total_bytes)
        finally:
            response.close()


def iter_sse(
    response: urllib.response.addinfourl,
    max_event_bytes: int = 32 * 1024 * 1024,
    stream_deadline: float | None = None,
    max_total_bytes: int | None = None,
) -> Iterator[SseEvent]:
    """Parse SSE frames from a CHUNKED read (not line iteration). Line iteration blocks in
    readline() until a newline, so a worker dripping bytes with no newline — or only `: ping`
    heartbeats — would pin the thread forever, evading a per-line/per-event deadline check.
    Reading bounded chunks and checking the deadline per chunk bounds both: a drip delivers
    data (chunk returns, deadline checked), a total stall hits the socket timeout (caught as a
    deadline tick). The socket timeout comes from the client's request_timeout.

    `max_total_bytes` caps the CUMULATIVE raw bytes read across the whole stream. Enforced here,
    at the byte source, it bounds EVERY wire byte — data, `event:`/`data:` framing and its
    whitespace padding, comment/heartbeat lines, and data-less frames that never yield an event —
    so a semi-trusted worker can't push traffic past ATLAS_MAX_JOB_OUTPUT_BYTES no matter how it
    hides the volume. The per-event `max_event_bytes` cap still bounds a single unterminated frame."""
    event = "message"
    data_lines: list[str] = []
    buffered = 0
    total = 0  # cumulative raw bytes read from the worker across the whole stream
    pending = b""
    read = getattr(response, "read1", None) or response.read

    def _expired() -> bool:
        return stream_deadline is not None and time.monotonic() > stream_deadline

    while True:
        if _expired():
            raise ThClawsError("worker stream exceeded its deadline without completing")
        try:
            chunk = read(65536)
        except TimeoutError:
            # Socket read timed out (a quiet stream): just a tick to re-check the deadline.
            continue
        if not chunk:
            break  # EOF
        total += len(chunk)
        if max_total_bytes is not None and total > max_total_bytes:
            # Every wire byte counts here (comments, padding, framing, data-less frames), so no
            # frame shape can smuggle volume past the caller's total-output cap.
            raise ThClawsError(f"worker output exceeded {max_total_bytes} bytes")
        buffered += len(chunk)
        if buffered > max_event_bytes:
            # A single event that never hits its blank-line terminator would otherwise
            # accumulate the whole stream in memory before the caller ever sees a frame.
            raise ThClawsError(f"SSE event exceeded {max_event_bytes} bytes without terminating")
        pending += chunk
        while b"\n" in pending:
            raw, pending = pending.split(b"\n", 1)
            line = raw.decode("utf-8", errors="replace").rstrip("\r")
            if line == "":
                if data_lines:
                    yield SseEvent(event=event, data="\n".join(data_lines))
                event = "message"
                data_lines = []
                buffered = len(pending)
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line.removeprefix("event:").strip() or "message"
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").lstrip())
    if data_lines:
        yield SseEvent(event=event, data="\n".join(data_lines))


def parse_event_payload(event: SseEvent) -> dict[str, Any]:
    data = event.json_data()
    if isinstance(data, dict):
        payload = dict(data)
    else:
        payload = {"data": data}
    payload.setdefault("event", event.event)
    return payload


# Event names whose payload carries assistant-visible text. Everything else — thinking, tool_*,
# skill_*, user_message_injected, usage, result, error, session, and any unknown named event —
# is a structured frame that falls through to append_job_event, never into assistant_text. The
# scoping matters because a `thinking` frame is {"delta": …} and `user_message_injected` is
# {"text": …}: those key shapes are indistinguishable from assistant text, so only the event
# NAME can separate them. "message" (and the unnamed default, normalized to "message") is the
# legacy assistant-text frame older workers stream without an event name; "delta"/"content" are
# legacy OpenAI-compat text-frame names kept for backward compatibility (thinking /
# user_message_injected use different names, so they stay excluded).
_ASSISTANT_TEXT_EVENTS = {"text", "message", "delta", "content"}
# Bare-string frames are assistant text only under these explicit text names (unchanged from the
# pre-T2 parser); an unnamed `message` bare string still falls through to append_job_event.
_BARE_STRING_TEXT_EVENTS = {"text", "delta", "content"}


def extract_text(event: SseEvent) -> str | None:
    if event.data == "[DONE]":
        return None
    data = event.json_data()
    if isinstance(data, str):
        return data if event.event in _BARE_STRING_TEXT_EVENTS else None
    if (event.event or "message") not in _ASSISTANT_TEXT_EVENTS:
        return None
    if isinstance(data, dict):
        for key in ("text", "content", "delta"):
            value = data.get(key)
            if isinstance(value, str):
                return value
        message = data.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                return delta["content"]
    return None


# Tool/skill events carry `input`/`output` that can hold secrets or BYOK keys Atlas can never
# reliably detect (they live outside Atlas by design). These are projected to structural
# metadata ONLY before storage — the raw payload never reaches SQLite. A Skill call is a tool
# call renamed at emit time, so skill_* shares the tool payload shape.
_TOOL_SKILL_EVENTS = {
    "tool_use_start",
    "tool_use_result",
    "tool_use_denied",
    "skill_invoked",
    "skill_invoked_result",
}


def _canonical_bytes(value: Any) -> bytes:
    """Deterministic byte encoding of a JSON value for size/hash — never stored, only measured."""
    if isinstance(value, str):
        return value.encode("utf-8")
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _tool_status(event_type: str, payload: dict[str, Any]) -> str:
    if event_type == "tool_use_denied":
        return "denied"
    # Honor the worker's own status first — thClaws results carry {"status": "error"|"ok"}, so
    # deriving from absent is_error/error keys would misclassify real failures as ok. Length-cap
    # it: status is enum-like but worker-controlled. Fall back to the is_error/error heuristic,
    # then a type-derived default. Kept tolerant until T0 pins the exact contract.
    status = payload.get("status")
    if isinstance(status, str) and status:
        return status[:32]
    if event_type in {"tool_use_result", "skill_invoked_result"}:
        return "error" if (payload.get("is_error") or payload.get("error")) else "ok"
    return "started"


def project_structured_event(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Project a tool/skill event to structural metadata ONLY —
    {id, name, status, input_bytes, output_bytes, input_sha256, output_sha256}. Whitelist by
    construction (a fresh dict of allowed keys), so `input`/`output` and any other field are
    dropped entirely: the "never store tokens or model keys" invariant holds even for secrets
    Atlas has never seen. Hashes still let T5 correlate collected artifacts without content.
    Non-tool/skill events pass through unchanged."""
    if event_type not in _TOOL_SKILL_EVENTS:
        return payload
    projected: dict[str, Any] = {
        "id": payload.get("id"),
        "name": payload.get("name"),
        "status": _tool_status(event_type, payload),
    }
    for field in ("input", "output"):
        value = payload.get(field)
        if value is not None:
            raw = _canonical_bytes(value)
            projected[f"{field}_bytes"] = len(raw)
            projected[f"{field}_sha256"] = hashlib.sha256(raw).hexdigest()
    return projected


# The only keys a projected tool/skill row may carry. A stored row with ANYTHING else is a
# legacy raw row (input/output/error/event/…) and must be re-projected before it leaves the
# server; a row whose keys are all structural is already projected and passes through untouched.
_TOOL_STRUCTURAL_KEYS = {"id", "name", "status", "input_bytes", "output_bytes", "input_sha256", "output_sha256"}


def redact_tool_payload_for_read(event_type: str, payload: Any) -> Any:
    """Sanitize a STORED job_event payload before it leaves the server. Rows written before the
    write-time projection (legacy DBs) can still hold ANY raw tool/skill field — `input`,
    `output`, `error`, the echoed `event` name, etc.; project them on read so no raw payload
    ever reaches a client, keeping the no-payload-preview invariant even for old data. A row
    whose keys are all structural is already projected and passes through unchanged (so the
    already-computed byte/hash metadata is never dropped); non-tool events pass through too."""
    if event_type in _TOOL_SKILL_EVENTS and isinstance(payload, dict) and not set(payload).issubset(_TOOL_STRUCTURAL_KEYS):
        return project_structured_event(event_type, payload)
    return payload


_USAGE_TOKEN_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "reasoning_output_tokens",
)


def extract_usage(event: SseEvent) -> dict[str, int] | None:
    """Token counts from a worker `usage` SSE event (thClaws >= v0.85.0). Tolerant by design,
    like extract_session_id: only well-formed non-negative integer counts are kept; anything
    else (non-usage events, non-dict payloads, missing keys, strings, bools, negatives,
    ints beyond SQLite's signed 64-bit range) yields None rather than an exception, so a
    usage frame can never fail a job or drop its usage ledger row."""
    if event.event != "usage":
        return None
    data = event.json_data()
    if not isinstance(data, dict):
        return None
    usage: dict[str, int] = {}
    for key in _USAGE_TOKEN_KEYS:
        value = data.get(key)
        # Upper bound: SQLite stores signed 64-bit integers; a larger JSON int would raise
        # OverflowError at insert time and drop the job's entire usage ledger row.
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**63 - 1:
            usage[key] = value
    return usage or None


def extract_session_id(event: SseEvent) -> str | None:
    data = event.json_data()
    if isinstance(data, dict):
        # Only explicit session keys. A bare "id" is NOT a session id — most worker events
        # carry a per-message/tool id, and treating that as the session would (a) repoint the
        # conversation's session binding to garbage and (b) make the caller skip the frame's text.
        for key in ("session_id", "sessionId"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        # A worker may send the session id under "id" ONLY on an explicit session event.
        if event.event == "session":
            value = data.get("id")
            if isinstance(value, str) and value:
                return value
    if event.event == "session" and isinstance(data, str) and data:
        return data
    return None
