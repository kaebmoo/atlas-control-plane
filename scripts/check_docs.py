"""Docs-drift gate (ga-completion-plan.md §5).

Fails if:
  1. a relative link in docs/README.md points to a file that is not committed (would 404 on
     a fresh clone — e.g. a doc that exists locally but is .gitignored), or
  2. an exact /api route in atlas/app.py — including templated subroutes like
     /api/jobs/{id}/cancel — is absent from openapi.yaml, api-reference-en.md, OR
     api-reference-th.md. Path params are normalized to a "{}" marker BY POSITION, so a
     collection route (/api/users) and a detail route (/api/users/{id}) are distinct, while
     param-name differences ({id} vs {job_id}) don't matter.

HTTP-method-level coverage is intentionally not enforced: mapping the hand-rolled router's
methods to OpenAPI operations is brittle. Path-level (with positional param markers) catches
the drift that matters — a route or subroute vanishing from any of the three docs.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def _tracked_files() -> set[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return set(out.splitlines())


def check_readme_links(tracked: set[str]) -> list[str]:
    readme = DOCS / "README.md"
    problems = []
    for target in re.findall(r"\]\(([^)]+)\)", readme.read_text(encoding="utf-8")):
        target = target.split("#", 1)[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        resolved = (readme.parent / target).resolve()
        try:
            rel = resolved.relative_to(ROOT).as_posix()
        except ValueError:
            problems.append(f"README link escapes the repo: {target}")
            continue
        if target.endswith("/") or resolved.is_dir():
            # Directory link: pass if any committed file lives under it.
            if not any(f == rel or f.startswith(rel + "/") for f in tracked):
                problems.append(f"README links a directory with no committed files: {target}")
        elif rel not in tracked:
            problems.append(f"README links a file that is not committed (404 on fresh clone): {target}")
    return problems


def _route_sig(path: str) -> tuple[str, ...]:
    """Path -> positional signature: each {param} segment becomes the marker '{}'."""
    return tuple("{}" if seg.startswith("{") else seg for seg in path.strip("/").split("/"))


def _doc_route_sigs(text: str) -> set[tuple[str, ...]]:
    """Every /api/... path mentioned in a doc (OpenAPI keys or reference prose), as positional
    signatures with params normalized to '{}'."""
    return {
        _route_sig(m.group(0))
        for m in re.finditer(r"/api/[a-z0-9-]+(?:/(?:\{[a-z_]+\}|[a-z0-9-]+))*", text)
    }


def _app_route_sigs() -> set[tuple[str, ...]]:
    """Extract exact route signatures from the hand-rolled router in _handle_api, with path
    params as positional '{}' markers (so collection vs detail vs subroute stay distinct)."""
    src = (ROOT / "atlas" / "app.py").read_text(encoding="utf-8")
    start = src.index("def _handle_api(")
    end = src.index("def _handle_static(", start) if "def _handle_static(" in src[start:] else len(src)
    body = src[start:end]

    sigs: set[tuple[str, ...]] = set()
    current: str | None = None
    alias: str | None = None  # a local var bound to parts[3], e.g. `action = parts[3]`
    for line in body.splitlines():
        m_full = re.search(r"parts == (\[[^\]]+\])", line)
        m_pref = re.search(r'parts\[:2\] == \["api", "([a-z0-9-]+)"\]', line)
        if m_full or m_pref:
            alias = None  # new routing condition -> any prior parts[3] alias is out of scope
        m_alias = re.search(r"(\w+) = parts\[3\]", line)
        if m_alias:
            alias = m_alias.group(1)
        # A subroute action is written either inline (parts[3] == "x") or via the alias
        # (action = parts[3]; if action == "x") — match both forms.
        m_sub = re.search(r'parts\[3\] == "([a-z0-9-]+)"', line)
        if not m_sub and alias:
            m_sub = re.search(rf'\b{re.escape(alias)} == "([a-z0-9-]+)"', line)
        if m_full:  # full static path literal, e.g. ["api","workflows","draft"]
            try:
                segs = tuple(ast.literal_eval(m_full.group(1)))
            except (ValueError, SyntaxError):
                segs = ()
            if segs and segs[0] == "api":
                sigs.add(segs)
                current = segs[1] if len(segs) > 1 else current
        if m_pref:  # /api/X/{id} (detail) or the prefix of an /api/X/{id}/<action> block
            current = m_pref.group(1)
            if m_sub:
                sigs.add(("api", current, "{}", m_sub.group(1)))
            elif "len(parts) == 3" in line:
                sigs.add(("api", current, "{}"))
        elif m_sub and current:  # nested parts[3]/alias == "<action>" under an /api/X/{id}/... block
            sigs.add(("api", current, "{}", m_sub.group(1)))
    assert sigs, "no API routes discovered in app.py (regex drift?)"
    # Sanity floor: these tricky-pattern routes MUST be discovered, so a future regex regression
    # (e.g. a new alias form) fails loudly here instead of silently shrinking coverage.
    expected = {
        ("api", "jobs", "{}", "cancel"),               # parts[3] == "..."
        ("api", "approvals", "{}", "approve"),         # nested parts[3] == "..."
        ("api", "packs", "{}", "export"),
        ("api", "workflow-triggers", "{}", "fire"),
        ("api", "workflow-runs", "{}", "pause"),        # action = parts[3]; if action == "..."
        ("api", "workflow-runs", "{}", "resume"),
        ("api", "workflow-runs", "{}", "cancel"),
    }
    missing = sorted(expected - sigs)
    assert not missing, f"route extractor regressed; did not discover: {['/' + '/'.join(s) for s in missing]}"
    return sigs


def check_routes() -> list[str]:
    app_sigs = _app_route_sigs()
    docs = {
        "openapi.yaml": _doc_route_sigs((DOCS / "specs" / "openapi.yaml").read_text(encoding="utf-8")),
        "api-reference-en.md": _doc_route_sigs((DOCS / "specs" / "api-reference-en.md").read_text(encoding="utf-8")),
        "api-reference-th.md": _doc_route_sigs((DOCS / "specs" / "api-reference-th.md").read_text(encoding="utf-8")),
    }
    problems = []
    for sig in sorted(app_sigs):
        path = "/" + "/".join(sig)
        for label, doc_sigs in docs.items():
            if sig not in doc_sigs:
                problems.append(f"route {path} (atlas/app.py) is missing from {label}")
    return problems


def main() -> None:
    tracked = _tracked_files()
    problems = check_readme_links(tracked) + check_routes()
    if problems:
        print("docs check FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)
    print("docs check ok")


if __name__ == "__main__":
    main()
