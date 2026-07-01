from __future__ import annotations

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
            yield from iter_sse(response, stream_deadline=stream_deadline)
        finally:
            response.close()


def iter_sse(
    response: urllib.response.addinfourl,
    max_event_bytes: int = 32 * 1024 * 1024,
    stream_deadline: float | None = None,
) -> Iterator[SseEvent]:
    """Parse SSE frames from a CHUNKED read (not line iteration). Line iteration blocks in
    readline() until a newline, so a worker dripping bytes with no newline — or only `: ping`
    heartbeats — would pin the thread forever, evading a per-line/per-event deadline check.
    Reading bounded chunks and checking the deadline per chunk bounds both: a drip delivers
    data (chunk returns, deadline checked), a total stall hits the socket timeout (caught as a
    deadline tick). The socket timeout comes from the client's request_timeout."""
    event = "message"
    data_lines: list[str] = []
    buffered = 0
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


def extract_text(event: SseEvent) -> str | None:
    if event.data == "[DONE]":
        return None
    data = event.json_data()
    if event.event in {"text", "delta", "content"} and isinstance(data, str):
        return data
    if isinstance(data, str):
        return data if event.event == "text" else None
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
