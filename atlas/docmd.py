"""Minimal markdown-to-HTML renderer for the operator docs served at /docs.

Scoped to exactly what docs/guides/web-user-guide-*.md use: headings (#/##/###),
paragraphs, **bold**, `inline code`, [links](url), fenced code blocks, blockquotes,
flat bullet lists, and pipe tables. Not a general-purpose markdown engine — stdlib
only, no new dependency, because the only content it will ever render is those two
files.
"""

from __future__ import annotations

import html
import re
import unicodedata

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
_TABLE_SEPARATOR = re.compile(r"[-: |]+")
_SLUG_SPACE = re.compile(r"\s+")


def _slugify(heading_text: str) -> str:
    # GitHub's own heading-anchor algorithm: lowercase, drop punctuation, spaces to
    # hyphens. The guide's own #section links were written expecting exactly this, so
    # matching it is what makes them resolve rather than an arbitrary choice.
    # \w alone drops Thai combining vowel/tone marks (Unicode category Mn — not "word"
    # characters to Python's re), which would silently corrupt every Thai heading's
    # anchor; keep any Mark category explicitly alongside alnum/space/hyphen.
    kept = [ch for ch in heading_text.lower() if ch.isalnum() or unicodedata.category(ch).startswith("M") or ch in " -"]
    return _SLUG_SPACE.sub("-", "".join(kept).strip())


def _link(label: str, href: str) -> str:
    if href.startswith(("http://", "https://")):
        return f'<a href="{html.escape(href, quote=True)}" target="_blank" rel="noopener">{label}</a>'
    if href.startswith("#"):
        return f'<a href="{html.escape(href, quote=True)}">{label}</a>'
    # A relative link to another doc file (e.g. ../specs/api-reference-en.md) — out of
    # this single-entry-point's scope (deeper docs stay repo-only, by design). Rendering
    # it as a live link would 404, or worse, silently fall through to the SPA's
    # catch-all and show the dashboard — so it renders as plain text instead.
    return label


def _inline(text: str) -> str:
    text = html.escape(text)
    codes: list[str] = []

    def stash(match: re.Match[str]) -> str:
        codes.append(match.group(1))
        return f"\x00{len(codes) - 1}\x00"

    # Stash code spans first so link/bold markup inside them isn't touched, then
    # restore them last — markdown code spans are always literal.
    text = _INLINE_CODE.sub(stash, text)
    text = _LINK.sub(lambda m: _link(m.group(1), m.group(2)), text)
    text = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{codes[int(m.group(1))]}</code>", text)


def _table_cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip("|").split("|")]


def _render_table(lines: list[str]) -> str:
    header = _table_cells(lines[0])
    body_lines = lines[2:] if len(lines) > 1 and _TABLE_SEPARATOR.fullmatch(lines[1]) else lines[1:]
    head_html = "".join(f"<th>{_inline(cell)}</th>" for cell in header)
    body_html = "".join(
        "<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in _table_cells(row)) + "</tr>"
        for row in body_lines
    )
    return f"<table><thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table>"


def render_markdown(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    used_slugs: dict[str, int] = {}

    def flush_paragraph() -> None:
        if paragraph:
            out.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def flush_list() -> None:
        if list_items:
            out.append("<ul>" + "".join(f"<li>{_inline(item)}</li>" for item in list_items) + "</ul>")
            list_items.clear()

    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        if stripped.startswith("```"):
            flush_paragraph()
            flush_list()
            i += 1
            code_lines: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            out.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
            i += 1
            continue

        heading = _HEADING.match(stripped)
        if heading:
            flush_paragraph()
            flush_list()
            level = len(heading.group(1))
            heading_text = heading.group(2)
            slug = _slugify(heading_text)
            if slug in used_slugs:
                used_slugs[slug] += 1
                slug = f"{slug}-{used_slugs[slug]}"
            else:
                used_slugs[slug] = 0
            out.append(f'<h{level} id="{html.escape(slug, quote=True)}">{_inline(heading_text)}</h{level}>')
            i += 1
            continue

        if stripped.startswith(">"):
            flush_paragraph()
            flush_list()
            quote_lines: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append(f"<blockquote><p>{_inline(' '.join(quote_lines))}</p></blockquote>")
            continue

        if stripped.startswith("- "):
            flush_paragraph()
            list_items.append(stripped[2:])
            i += 1
            continue

        if stripped.startswith("|") and stripped.endswith("|"):
            flush_paragraph()
            flush_list()
            table_lines: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            out.append(_render_table(table_lines))
            continue

        if not stripped:
            flush_paragraph()
            flush_list()
            i += 1
            continue

        paragraph.append(stripped)
        i += 1

    flush_paragraph()
    flush_list()
    return "\n".join(out)


def demo() -> None:
    sample = (
        "# Title\n\n"
        "Intro **bold** and `code` and a [link](https://example.com).\n\n"
        "> A quoted note\n> spanning two lines.\n\n"
        "## 9. Accounts: users and API tokens\n\n"
        "See [§9](#9-accounts-users-and-api-tokens) and the "
        "[API Reference](../specs/api-reference-en.md) for details.\n\n"
        "- one\n- two `x` **y**\n\n"
        "```\ncode line 1\n<escaped>\n```\n\n"
        "| A | B |\n| - | - |\n| 1 | 2 |\n"
    )
    rendered = render_markdown(sample)
    assert '<h1 id="title">Title</h1>' in rendered
    assert "<strong>bold</strong>" in rendered
    assert "<code>code</code>" in rendered
    assert '<a href="https://example.com" target="_blank" rel="noopener">link</a>' in rendered
    assert "<blockquote><p>A quoted note spanning two lines.</p></blockquote>" in rendered
    assert '<h2 id="9-accounts-users-and-api-tokens">9. Accounts: users and API tokens</h2>' in rendered
    # A same-page #anchor resolves to a real heading id; a relative link to another doc
    # file is out of scope for this single-entry-point page and renders as plain text,
    # not a link that would 404 or fall through to the SPA dashboard.
    assert '<a href="#9-accounts-users-and-api-tokens">§9</a>' in rendered
    assert "and the API Reference for details" in rendered and '<a href="../specs' not in rendered
    # Thai combining vowel/tone marks must survive slugification (they're Unicode
    # category Mn, which \w alone silently drops) or every Thai anchor 404s.
    assert _slugify("9. บัญชี: ผู้ใช้และ API token") == "9-บัญชี-ผู้ใช้และ-api-token"
    assert "<ul><li>one</li><li>two <code>x</code> <strong>y</strong></li></ul>" in rendered
    assert "<pre><code>code line 1\n&lt;escaped&gt;</code></pre>" in rendered
    assert "<table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table>" in rendered
    print("ok")


if __name__ == "__main__":
    demo()
