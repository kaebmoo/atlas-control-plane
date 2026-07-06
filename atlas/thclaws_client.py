from __future__ import annotations

import hashlib
import http.client
import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator


# URLError reasons that PROVE the request never reached the worker: the kernel refused the
# connection (no listener) or the name never resolved. Everything else — timeout, reset,
# remote disconnect — can occur AFTER the request was delivered, so it stays ambiguous.
_NOT_ACCEPTED_REASONS = (ConnectionRefusedError, socket.gaierror)
# Cap on an HTTP ERROR body read in _request: an error response is a short message, so a
# larger body from a semi-trusted worker is a memory/thread-pin vector, not real content.
_ERROR_BODY_MAX_BYTES = 64 * 1024
# Cap for the small JSON control-plane bodies (healthz / agent info / model catalogue). The
# catalogue is the biggest legitimate payload (hundreds of models × a pricing block) — still
# well under 1 MiB; 8 MiB leaves generous headroom while bounding a hostile body.
_JSON_BODY_MAX_BYTES = 8 * 1024 * 1024
# Wall-clock bound on that read: a byte cap alone doesn't stop a slow-drip body (each byte
# resets the socket timeout), so read in chunks and stop at this deadline too.
_ERROR_BODY_READ_DEADLINE_SECONDS = 10.0


class ThClawsError(RuntimeError):
    """Worker call failure, classified for callers that must know whether the request might
    still be executing (T3 callback dispatch). http_status is set ONLY when the worker itself
    answered with an HTTP error — a definitive rejection. request_not_accepted is True only
    when the request provably never reached the worker (connection refused / DNS failure).
    Neither set means AMBIGUOUS: the request may have been accepted and still be running."""

    def __init__(self, message: str, http_status: int | None = None, request_not_accepted: bool = False):
        super().__init__(message)
        self.http_status = http_status
        self.request_not_accepted = request_not_accepted


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

    def _request(
        self,
        method: str,
        path: str,
        payload: Any = None,
        timeout: float | None = None,
        raw_body: bytes | None = None,
        content_type: str | None = None,
    ) -> urllib.response.addinfourl:
        body = None
        headers = {"Accept": "application/json"}
        if raw_body is not None:
            # Raw bytes (a T6 push tar), not JSON — the caller owns the content type.
            body = raw_body
            headers["Content-Type"] = content_type or "application/octet-stream"
        elif payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(f"{self.base_url}{path}", data=body, headers=headers, method=method)
        try:
            return urllib.request.urlopen(request, timeout=self.timeout if timeout is None else timeout)  # nosec B310
        except urllib.error.HTTPError as exc:
            # BOUND the error-body read in BOTH bytes and time: a semi-trusted worker answering
            # an error with a huge or slow-dripped body would otherwise let an unbounded read
            # exhaust memory or pin the thread (each byte resets the socket timeout). Best-effort
            # (already in an error path) — read chunks under a deadline, truncate, never raise here.
            details = _read_error_body(exc)
            raise ThClawsError(f"thClaws HTTP {exc.code}: {details}", http_status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise ThClawsError(
                f"thClaws connection error: {exc.reason}",
                request_not_accepted=isinstance(exc.reason, _NOT_ACCEPTED_REASONS),
            ) from exc

    def get_json(self, path: str, timeout: float | None = None) -> Any:
        # Bounded like every other worker read (same class fix as sync/ACK/error bodies): a
        # bare response.read() has NO size cap and NO wall-clock bound — each drip-fed chunk
        # resets the socket timeout, so a semi-trusted worker could pin a poll thread or
        # exhaust memory from /healthz//v1/agent/info//v1/models.
        effective_timeout = self.timeout if timeout is None else timeout
        with self._request("GET", path, timeout=effective_timeout) as response:
            body = _read_bounded(
                response, _JSON_BODY_MAX_BYTES, time.monotonic() + effective_timeout
            ).decode("utf-8", errors="replace")
        try:
            return json.loads(body or "{}")
        except json.JSONDecodeError as exc:
            raise ThClawsError(f"Invalid JSON from {path}: {body[:200]}") from exc

    def get_text(self, path: str) -> tuple[int, str]:
        with self._request("GET", path) as response:
            body = _read_bounded(
                response, _JSON_BODY_MAX_BYTES, time.monotonic() + self.timeout
            ).decode("utf-8", errors="replace")
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

    def list_models(self) -> list[dict[str, Any]]:
        """Return the worker's OpenAI-compatible model catalogue.

        Keep validation at the client boundary: a malformed response is a poll failure, not
        a poisoned pricing cache that can later produce a misleading estimate.
        """
        payload = self.get_json("/v1/models", timeout=min(self.timeout, 5.0))
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ThClawsError("Invalid model list from /v1/models")
        return [row for row in payload["data"] if isinstance(row, dict)]

    def sync_stat(self) -> dict[str, Any]:
        """Advisory process-wide `busy` snapshot from `GET /workspace/sync/stat`.

        Short timeout — an advisory routing signal must never pace the poll. NOTE `/workspace/
        sync/*` is NOT protected by the worker Bearer (docs/specs/thclaws-worker-contract.md):
        only call this on a worker whose operator-asserted `sync_mode` is an approved shape
        (tunnel / forward_auth). A malformed response is a probe failure, not a false signal.
        """
        payload = self.get_json("/workspace/sync/stat", timeout=min(self.timeout, 5.0))
        if not isinstance(payload, dict):
            raise ThClawsError("Invalid sync stat from /workspace/sync/stat")
        return payload

    def sync_export(
        self,
        paths: list[str],
        *,
        deadline: float,
        max_bytes: int,
        retry_409_max: int = 4,
        retry_409_delay: float = 0.5,
    ) -> bytes:
        """Collect an EXPLICIT path list from the worker via `POST /workspace/sync/export`
        (JSON path array in, gzip tar of just those paths out). Returns the raw tar bytes,
        bounded in BOTH size (`max_bytes`) and wall-clock (`deadline`, a `time.monotonic()`
        value) — a semi-trusted worker must not be able to pin the collection thread or exhaust
        memory. NOT `/sync/pull`, which tars the whole workspace.

        Export returns 409 Conflict while an agent turn is active (`workspace busy`). Collection
        runs AFTER the worker stream terminates, so contention is transient — retry a bounded
        number of times with a fixed delay, but never past `deadline`. Any other error (or a
        persistent 409) propagates as a ThClawsError for the caller's failure isolation.

        Like `sync_stat`, `/workspace/sync/*` is NOT Bearer-protected: only call this on a worker
        whose operator-asserted `sync_mode` is an approved shape (docs/specs/thclaws-worker-contract.md)."""
        return self._call_with_409_retry(
            lambda: self._sync_export_once(paths, deadline=deadline, max_bytes=max_bytes),
            deadline=deadline,
            retry_max=retry_409_max,
            retry_delay=retry_409_delay,
        )

    def sync_push(
        self,
        tar_bytes: bytes,
        *,
        deadline: float,
        max_ack_bytes: int = 64 * 1024,
        retry_409_max: int = 4,
        retry_409_delay: float = 0.5,
    ) -> dict[str, Any]:
        """Push a gzip tar of ADDITIVE files into the target worker's workspace via
        `POST /workspace/sync/push` (T6). Atlas builds the arcnames as
        `incoming/<run_id>/<node_key>/…`, so a push can never clobber the worker's own files;
        Atlas never sends any replace/trash option. Bounded by `deadline` (a `time.monotonic()`
        value) with a bounded 409-`workspace busy` retry. The ACK body is read bounded. Same
        sync-auth caveat as `sync_export` — only call on a `tunnel`/`forward_auth` worker."""
        return self._call_with_409_retry(
            lambda: self._sync_push_once(tar_bytes, deadline=deadline, max_ack_bytes=max_ack_bytes),
            deadline=deadline,
            retry_max=retry_409_max,
            retry_delay=retry_409_delay,
        )

    def _call_with_409_retry(self, once: Any, *, deadline: float, retry_max: int, retry_delay: float) -> Any:
        # 409 = the worker is mid-turn; sync collection/push follows stream termination so it
        # clears quickly. Retry a bounded number of times, but only while enough of the deadline
        # remains for both the delay and a subsequent attempt. Any other error, or a persistent
        # 409, propagates to the caller. Shared by sync_export and sync_push (one source).
        attempts_left = max(0, retry_max)
        while True:
            try:
                return once()
            except ThClawsError as exc:
                if exc.http_status == 409 and attempts_left > 0 and (deadline - time.monotonic()) > retry_delay:
                    attempts_left -= 1
                    time.sleep(retry_delay)
                    continue
                raise

    def _sync_export_once(self, paths: list[str], *, deadline: float, max_bytes: int) -> bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ThClawsError("sync export exceeded its deadline")
        response = self._request(
            "POST", "/workspace/sync/export", payload=list(paths), timeout=min(self.timeout, remaining)
        )
        try:
            return _read_bounded(response, max_bytes, deadline)
        finally:
            response.close()

    def _sync_push_once(self, tar_bytes: bytes, *, deadline: float, max_ack_bytes: int) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ThClawsError("sync push exceeded its deadline")
        response = self._request(
            "POST",
            "/workspace/sync/push",
            timeout=min(self.timeout, remaining),
            raw_body=tar_bytes,
            content_type="application/gzip",
        )
        try:
            body = _read_bounded(response, max_ack_bytes, deadline).decode("utf-8", errors="replace")
        finally:
            response.close()
        if not body.strip():
            return {}
        try:
            ack = json.loads(body)
        except json.JSONDecodeError:
            return {}  # a non-JSON 2xx ack is fine — the status already means accepted
        return ack if isinstance(ack, dict) else {}

    def run_agent_stream(
        self,
        *,
        prompt: str,
        workspace_dir: str | None = None,
        system: str | None = None,
        model: str | None = None,
        session_id: str | None = None,
        max_tokens: int | None = None,
        stream_deadline: float | None = None,
        max_total_bytes: int | None = None,
    ) -> Iterator[SseEvent]:
        payload = self._agent_run_payload(
            prompt=prompt,
            workspace_dir=workspace_dir,
            system=system,
            model=model,
            session_id=session_id,
            max_tokens=max_tokens,
        )
        payload["stream"] = True
        response = self._request("POST", "/agent/run", payload=payload, timeout=None)
        try:
            yield from iter_sse(response, stream_deadline=stream_deadline, max_total_bytes=max_total_bytes)
        finally:
            response.close()

    def run_agent_async(
        self,
        *,
        prompt: str,
        callback_url: str,
        callback_api_key: str,
        run_id: str,
        workspace_dir: str | None = None,
        system: str | None = None,
        model: str | None = None,
        session_id: str | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Fire-and-forget dispatch via thClaws's `x_callback` extension. The envelope is an
        OBJECT — {url, api_key, run_id} (idempotency_key defaults to run_id upstream, and Atlas
        uses run_id = job id = idempotency key, so it is never sent). thClaws 202-ACKs
        immediately with {run_id, session_id, status:"accepted", ...} and later POSTs the
        terminal payload to callback_url (Bearer api_key; 3 attempts at ~0/10/60s backoff,
        gives up on any non-429 4xx)."""
        payload = self._agent_run_payload(
            prompt=prompt,
            workspace_dir=workspace_dir,
            system=system,
            model=model,
            session_id=session_id,
            max_tokens=max_tokens,
        )
        payload["x_callback"] = {"url": callback_url, "api_key": callback_api_key, "run_id": run_id}
        # Cap the socket timeout at the ACK read deadline: a single blocking read otherwise
        # runs up to self.timeout, so a request_timeout above the deadline would let a silent
        # worker pin this dispatch thread past the promised wall-clock bound. min() keeps a
        # short request_timeout short and a long one bounded by the deadline.
        dispatch_timeout = min(self.timeout, _ACK_READ_DEADLINE_SECONDS)
        try:
            with self._request("POST", "/agent/run", payload=payload, timeout=dispatch_timeout) as response:
                status_code = response.getcode() or 0
                if status_code != 202:
                    # The x_callback contract's ONLY acceptance signal is a 202 ACK. Any other
                    # 2xx (an incompatible worker running the request synchronously, a proxy
                    # answering 200) proves no async run+callback was scheduled — definitive,
                    # so the caller fails fast instead of parking the job until the reaper.
                    raise ThClawsError(
                        f"x_callback dispatch expected a 202 ACK, got HTTP {status_code}",
                        request_not_accepted=True,
                    )
                # The ACK is a tiny JSON object; a semi-trusted worker must not be able to pin
                # this dispatch thread (slow drip) or exhaust memory (giant 2xx body) — the
                # stream path is bounded by iter_sse, so bound this read too.
                body = _read_bounded(response, _ACK_MAX_BYTES, time.monotonic() + _ACK_READ_DEADLINE_SECONDS).decode(
                    "utf-8", errors="replace"
                )
        except ThClawsError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            # A failure while READING the 202 ACK — a socket timeout/reset (OSError) OR a
            # protocol error like IncompleteRead / BadStatusLine (HTTPException, NOT an
            # OSError). Both happen AFTER the request was delivered and (for the read case)
            # after the 202 was seen, so this is the AMBIGUOUS shape (neither http_status nor
            # request_not_accepted): the run may be executing. A plain ThClawsError routes the
            # caller to keep the job callback-pending, not fail it.
            raise ThClawsError(f"thClaws connection error while reading the x_callback ACK: {exc}") from exc
        # Post-202 problems stay AMBIGUOUS (plain ThClawsError): the status code already said
        # the run was accepted, so a malformed/mismatched ACK body must not undo that — the
        # caller keeps the job callback-pending and the callback or reaper resolves it.
        try:
            ack = json.loads(body or "{}")
        except json.JSONDecodeError as exc:
            raise ThClawsError(f"Invalid x_callback ACK from /agent/run: {body[:200]}") from exc
        if not isinstance(ack, dict):
            raise ThClawsError(f"x_callback ACK must be a JSON object, got: {body[:200]}")
        if ack.get("status") != "accepted" or ack.get("run_id") != run_id:
            # A genuine thClaws ACK always echoes status:"accepted" and OUR run_id; anything
            # else is a non-conforming intermediary and must not be recorded as a clean
            # dispatch (no session binding from an untrusted echo).
            raise ThClawsError(
                f"x_callback ACK mismatch: status={ack.get('status')!r}, run_id={ack.get('run_id')!r}"
            )
        return ack

    @staticmethod
    def _agent_run_payload(
        *,
        prompt: str,
        workspace_dir: str | None,
        system: str | None,
        model: str | None,
        session_id: str | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"prompt": prompt}
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
        return payload


# x_callback ACK bounds: the ACK is ~200 bytes of JSON, so 64 KiB is ample headroom, and the
# wall-clock deadline stops a slow-drip body from pinning the dispatch thread (the per-recv
# socket timeout resets on every byte, so it bounds stalls, not total duration).
_ACK_MAX_BYTES = 64 * 1024
_ACK_READ_DEADLINE_SECONDS = 60.0


def _response_socket(response: Any) -> Any:
    """Best-effort reach for the underlying socket of a urllib response so a per-read timeout
    can be set. Guarded: returns None if the (CPython) attribute path isn't present, in which
    case callers fall back to the inherited socket timeout — finite, just looser."""
    try:
        return response.fp.fp.raw._sock
    except AttributeError:
        return None


def _bound_recv(response_socket: Any, remaining: float) -> None:
    """Cap the next recv at the remaining wall-clock budget: without this, ONE blocking read
    inherits the request timeout (up to 60s on callback dispatch) and can overrun a tighter
    read deadline. No-op if the socket wasn't reachable."""
    if response_socket is not None:
        try:
            response_socket.settimeout(max(0.05, remaining))
        except OSError:
            pass


def _read_error_body(response: Any) -> str:
    """Best-effort read of an HTTP error body, bounded in bytes AND wall-clock time. Each read
    tick is capped at the remaining deadline (so a withheld body can't overrun it by a whole
    socket timeout), stops at the byte cap or the deadline, and NEVER raises — we are already
    handling an error and only want a short, safe diagnostic string. Marks truncation."""
    deadline = time.monotonic() + _ERROR_BODY_READ_DEADLINE_SECONDS
    read = getattr(response, "read1", None) or response.read
    sock = _response_socket(response)
    chunks = bytearray()
    truncated = False
    while len(chunks) <= _ERROR_BODY_MAX_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            truncated = True
            break
        _bound_recv(sock, remaining)
        try:
            chunk = read(min(8192, _ERROR_BODY_MAX_BYTES + 1 - len(chunks)))
        except (TimeoutError, OSError, http.client.HTTPException):
            # http.client.IncompleteRead (a truncated chunked body) is an HTTPException, NOT an
            # OSError — it must not escape this best-effort reader (docstring promises it never
            # raises), or it would propagate out of _request past the intended classification.
            truncated = True
            break
        if not chunk:
            break
        chunks += chunk
    if len(chunks) > _ERROR_BODY_MAX_BYTES:
        chunks = chunks[:_ERROR_BODY_MAX_BYTES]
        truncated = True
    text = bytes(chunks).decode("utf-8", errors="replace")
    return text + "…[truncated]" if truncated else text


def _read_bounded(response: urllib.response.addinfourl, max_bytes: int, deadline: float) -> bytes:
    """Read an HTTP body with BOTH a byte cap and a wall-clock deadline (chunked, like
    iter_sse). Exceeding either raises a plain ThClawsError — deliberately ambiguous for the
    T3 dispatch (the 2xx status means the worker accepted the run; a garbage ACK body does not
    undo that)."""
    chunks = bytearray()
    read = getattr(response, "read1", None) or response.read
    sock = _response_socket(response)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ThClawsError("response body read exceeded its deadline")
        # Cap the recv at the remaining deadline so a single withheld read can't overrun it by
        # a whole (larger) request timeout.
        _bound_recv(sock, remaining)
        try:
            chunk = read(8192)
        except TimeoutError:
            continue  # per-recv stall: just a tick to re-check the deadline
        if not chunk:
            return bytes(chunks)
        chunks += chunk
        if len(chunks) > max_bytes:
            raise ThClawsError(f"response body exceeded {max_bytes} bytes")


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


def extract_effective_model(event: SseEvent) -> str | None:
    """Worker-reported model from a usage or terminal result frame."""
    if event.event not in {"usage", "result"}:
        return None
    data = event.json_data()
    if not isinstance(data, dict):
        return None
    model = data.get("model")
    return model if isinstance(model, str) and model.strip() else None


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
