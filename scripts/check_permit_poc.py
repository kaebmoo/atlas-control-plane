"""Permit PoC hardening check (poc/permit_web).

The operator dashboard renders worker- and server-supplied fields into innerHTML. Every such
field must pass through esc() first, or a malicious artifact executes inline JavaScript in the
operator's browser against the same-origin /api/decide endpoint (stored XSS on the governance
path). Locks two things: no untrusted field is interpolated raw, and esc() actually neutralizes
angle brackets (verified by running the shipped implementation through node).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "poc" / "permit_web" / "app.py").read_text(encoding="utf-8")

# Untrusted fields that MUST NOT appear interpolated raw (i.e. without an esc() wrapper).
RAW_FORBIDDEN = ("+j.state+", "+runId+", "+a.key+", "+a.kind+", "+(j.approval.reason", "+(j.error", "+err.message+")
# The escaped forms that MUST be present (removing esc() around any of them reintroduces the sink).
ESCAPED_REQUIRED = (
    "esc(j.state)", "esc(runId)", "esc(a.key)", "esc(a.kind)",
    "esc(typeof a.content", "esc(j.approval.reason", "esc(j.error", "esc(err.message)",
)


def main() -> None:
    problems: list[str] = []
    if "const esc =" not in PAGE:
        problems.append("esc() helper removed from the permit dashboard")
    for raw in RAW_FORBIDDEN:
        if raw in PAGE:
            problems.append(f"unescaped interpolation reintroduced into innerHTML: {raw}")
    for wrapped in ESCAPED_REQUIRED:
        if wrapped not in PAGE:
            problems.append(f"expected escaped interpolation missing: {wrapped}")

    # Run the shipped esc() against an XSS payload: no angle brackets may survive.
    match = re.search(r"const esc =.*", PAGE)  # whole line; entity ';' inside would trip a lazy match
    harness = (match.group(0) if match else "const esc=x=>x;") + (
        "\nconst out = esc('<img src=x onerror=alert(1)>');"
        "\nprocess.stdout.write(/[<>]/.test(out) ? 'LIVE' : 'SAFE');"
    )
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
    if result.stdout.strip() != "SAFE":
        problems.append(f"esc() failed to neutralize angle brackets: out={result.stdout!r} err={result.stderr!r}")

    if problems:
        print("permit poc check FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)
    print("permit poc check ok")


if __name__ == "__main__":
    main()
