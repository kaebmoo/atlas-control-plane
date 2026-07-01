"""Shared HTTP helpers for the hermetic check scripts.

Each check remains its own process with its own temp DB/port; only the tiny
request plumbing is shared so it is written (and fixed) in exactly one place.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


def request(
    base_url: str,
    method: str,
    path: str,
    payload: dict | None = None,
    token: str | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base_url + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def request_json(
    base_url: str,
    method: str,
    path: str,
    payload: dict | None = None,
    token: str | None = None,
) -> tuple[int, dict, dict[str, str]]:
    status, body, headers = request(base_url, method, path, payload, token)
    return status, json.loads(body or b"{}"), headers
