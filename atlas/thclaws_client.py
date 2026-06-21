from __future__ import annotations

import json
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
            return urllib.request.urlopen(request, timeout=self.timeout if timeout is None else timeout)
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
            status = response.getcode()
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
        model: str | None = None,
        session_id: str | None = None,
        max_tokens: int | None = None,
        x_callback: str | None = None,
    ) -> Iterator[SseEvent]:
        payload: dict[str, Any] = {
            "prompt": prompt,
            "stream": True,
        }
        if workspace_dir:
            payload["workspace_dir"] = workspace_dir
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
            yield from iter_sse(response)
        finally:
            response.close()


def iter_sse(response: urllib.response.addinfourl) -> Iterator[SseEvent]:
    event = "message"
    data_lines: list[str] = []
    for raw in response:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            if data_lines:
                yield SseEvent(event=event, data="\n".join(data_lines))
            event = "message"
            data_lines = []
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
        for key in ("session_id", "sessionId", "id"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    if event.event == "session" and isinstance(data, str) and data:
        return data
    return None
