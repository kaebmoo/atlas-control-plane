#!/usr/bin/env python3
"""UI/UX regression gate for the NT dashboard redesign.

Hermetic: reads the static frontend files directly (no server, no DB) and asserts
the markers that encode three fixed behaviours, so a future edit cannot silently
regress them:

  1. Job stream header stays in sync on every poll — render() calls
     updateStreamHeader() AFTER applyRoleGate() (so Cancel is not re-enabled on
     finished jobs), and the blinking cursor is gated to live jobs only.
  2. The mobile sidebar collapses to a hamburger drawer (#navToggle + .nav-open),
     and picking a nav item closes it.
  3. Closing a modal returns keyboard focus to the control that opened it
     (lastModalTrigger captured on open, restored in closeModals()).

It also pins a few load-bearing anchors of the redesign (NT @font-face, brand
token, logo, Overview view) so the reskin itself can't quietly disappear.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "atlas" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")

problems: list[str] = []


def need(cond: bool, msg: str) -> None:
    if not cond:
        problems.append(msg)


def func_body(src: str, name: str) -> str:
    """Return the source of a top-level `function name(...)` (closing brace at col 0)."""
    match = re.search(r"\n(?:async )?function " + re.escape(name) + r"\s*\(", src)
    if not match:
        return ""
    end = src.find("\n}\n", match.start())
    return src[match.start(): end if end != -1 else len(src)]


# --- 1. Job terminal-state sync -------------------------------------------------
render = func_body(JS, "render")
need(bool(render), "render() not found in app.js")
need("updateStreamHeader()" in render,
     "render() must call updateStreamHeader() so the selected job header syncs on poll")
need("applyRoleGate()" in render and 0 <= render.find("applyRoleGate()") < render.find("updateStreamHeader()"),
     "updateStreamHeader() must run AFTER applyRoleGate() (else Cancel re-enables on finished jobs)")
ush = func_body(JS, "updateStreamHeader")
need('"stream-live"' in ush, "updateStreamHeader() must toggle the .stream-live cursor class")
need("operator" in ush, "updateStreamHeader() Cancel state must respect the operator role")
need(".stream-output.stream-live" in CSS,
     "the live cursor must be gated on .stream-live so it stops when the job is terminal")

# --- 2. Mobile hamburger navigation --------------------------------------------
need('id="navToggle"' in HTML, "mobile hamburger button #navToggle missing from index.html")
need('classList.toggle("nav-open")' in JS, "navToggle must toggle .nav-open on the sidebar")
need('classList.remove("nav-open")' in JS, "picking a nav item must collapse the mobile drawer")
need(".sidebar.nav-open .nav" in CSS, ".nav-open drawer rule missing from styles.css")
need(".nav-toggle { display: none;" in CSS, "the hamburger must be hidden on desktop (.nav-toggle display:none)")

# --- 3. Modal focus restoration -------------------------------------------------
opener = func_body(JS, "openWorkerModal")
closer = func_body(JS, "closeModals")
need("lastModalTrigger = document.activeElement" in opener,
     "openWorkerModal() must capture the opening control in lastModalTrigger")
need("lastModalTrigger" in closer and ".focus" in closer,
     "closeModals() must return focus to the opener (lastModalTrigger)")

# --- redesign anchors -----------------------------------------------------------
need("@font-face" in CSS and "NT-Brand.ttf" in CSS, "NT brand @font-face missing from styles.css")
need("--nt-yellow-500: #ffd100" in CSS, "NT brand-yellow token missing from styles.css")
need("nt-logo.png" in HTML, "NT logo reference missing from index.html")
need('data-view="overview"' in HTML, "Overview view missing from index.html")

if problems:
    print("ui/ux check FAILED:", file=sys.stderr)
    for item in problems:
        print("  - " + item, file=sys.stderr)
    sys.exit(1)
print("ui/ux check ok")
