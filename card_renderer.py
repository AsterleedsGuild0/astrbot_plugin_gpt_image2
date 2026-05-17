"""Self-contained Markdown card HTML for GPT Image2 text replies.

AstrBot's built-in remote T2I templates may depend on CDN-hosted JavaScript for
Markdown rendering. This module renders the Markdown subset we need on the
Python side, then wraps it in a self-contained HTML/CSS card so no external JS
or CSS is required during screenshot rendering.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CardRenderPayload:
    """Template payload for ``Star.html_render``."""

    template: str
    data: dict[str, str]
    options: dict[str, object]


CARD_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root {
      color-scheme: light;
      --bg: #eefafa;
      --card: #ffffff;
      --text: #182033;
      --muted: #637083;
      --line: #dce8ef;
      --accent: #38c7bd;
      --accent-2: #60a5fa;
      --soft: #eefbfa;
      --code: #f6f8fb;
      --danger: #fb7185;
      --shadow: 0 18px 45px rgba(15, 23, 42, 0.10);
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      padding: 32px;
      background: radial-gradient(circle at 18px 18px, rgba(56, 199, 189, 0.08) 1.5px, transparent 0) 0 0 / 18px 18px,
        linear-gradient(135deg, #edfafa 0%, #f8fcff 100%);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC",
        "Source Han Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif,
        "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji";
      font-size: 28px;
      line-height: 1.62;
    }

    .card {
      width: min(1180px, calc(100vw - 64px));
      margin: 0 auto;
      background: var(--card);
      border: 1px solid rgba(56, 199, 189, 0.18);
      border-radius: 18px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }

    .stripe {
      height: 10px;
      background: linear-gradient(90deg, var(--accent), var(--accent-2));
    }

    .content {
      padding: 36px 44px 34px;
    }

    h1, h2, h3, h4, h5, h6 {
      margin: 0 0 18px;
      line-height: 1.28;
      font-weight: 800;
      letter-spacing: -0.02em;
    }

    h1 { font-size: 52px; }
    h2 { font-size: 46px; }
    h3 { font-size: 34px; margin-top: 30px; }
    h4 { font-size: 30px; margin-top: 26px; }

    h1::before, h2::before {
      content: "";
      display: inline-block;
      width: 10px;
      height: 0.78em;
      margin-right: 14px;
      border-radius: 999px;
      background: linear-gradient(180deg, var(--accent), var(--accent-2));
      vertical-align: -0.08em;
    }

    p { margin: 16px 0; }
    strong { color: #0f948c; }

    ul, ol {
      margin: 14px 0 20px;
      padding-left: 1.4em;
    }

    li { margin: 7px 0; }
    li::marker { color: var(--accent); }

    code {
      padding: 0.13em 0.42em;
      border-radius: 7px;
      background: var(--code);
      color: #0f766e;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
      font-size: 0.84em;
      word-break: break-word;
    }

    pre {
      margin: 20px 0;
      padding: 20px 24px;
      border-radius: 14px;
      background: #101828;
      color: #e5edf6;
      overflow-wrap: anywhere;
      white-space: pre-wrap;
      border: 1px solid rgba(96, 165, 250, 0.35);
    }

    pre code {
      padding: 0;
      background: transparent;
      color: inherit;
      font-size: 0.80em;
    }

    blockquote {
      margin: 22px 0;
      padding: 18px 22px;
      border-left: 8px solid var(--accent);
      border-radius: 0 14px 14px 0;
      background: var(--soft);
      color: #334155;
    }

    blockquote p { margin: 8px 0; }

    table {
      width: 100%;
      margin: 20px 0 24px;
      border-collapse: collapse;
      border: 1px solid var(--line);
      border-radius: 14px;
      overflow: hidden;
      font-size: 0.82em;
    }

    th, td {
      padding: 13px 16px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }

    th {
      background: linear-gradient(90deg, rgba(56, 199, 189, 0.18), rgba(96, 165, 250, 0.12));
      color: #0f766e;
      font-weight: 800;
    }

    tr:last-child td { border-bottom: 0; }
    tr:nth-child(even) td { background: #fbfdff; }

    hr {
      border: 0;
      border-top: 1px solid var(--line);
      margin: 28px 0;
    }

    .footer {
      padding: 8px 44px 18px;
      color: var(--muted);
      font-size: 18px;
      text-align: right;
    }
  </style>
</head>
<body>
  <main class="card">
    <div class="stripe"></div>
    <article class="content">{{ body | safe }}</article>
    <footer class="footer">GPT Image2 · Markdown Card</footer>
  </main>
</body>
</html>
"""


def build_markdown_card(markdown_text: str) -> CardRenderPayload:
    """Build a self-contained HTML card payload for AstrBot custom rendering."""
    return CardRenderPayload(
        template=CARD_TEMPLATE,
        data={"body": markdown_to_html(markdown_text)},
        options={"full_page": True, "type": "png", "quality": 90},
    )


def markdown_to_html(text: str) -> str:
    """Render a pragmatic Markdown subset used by plugin replies."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    html_parts: list[str] = []
    paragraph: list[str] = []
    i = 0

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            html_parts.append(f"<p>{_render_inline(' '.join(paragraph))}</p>")
            paragraph = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            flush_paragraph()
            i += 1
            continue

        if stripped.startswith("```"):
            flush_paragraph()
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            code = html.escape("\n".join(code_lines))
            html_parts.append(f"<pre><code>{code}</code></pre>")
            continue

        if _is_table_start(lines, i):
            flush_paragraph()
            table_html, next_index = _render_table(lines, i)
            html_parts.append(table_html)
            i = next_index
            continue

        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            html_parts.append(
                f"<h{level}>{_render_inline(heading.group(2))}</h{level}>"
            )
            i += 1
            continue

        if stripped in {"---", "***", "___"}:
            flush_paragraph()
            html_parts.append("<hr />")
            i += 1
            continue

        if stripped.startswith(">"):
            flush_paragraph()
            quote_lines: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip()[1:].strip())
                i += 1
            quote_body = "<br />".join(_render_inline(item) for item in quote_lines)
            html_parts.append(f"<blockquote><p>{quote_body}</p></blockquote>")
            continue

        list_match = re.match(r"^([-*+])\s+(.+)$", stripped)
        if list_match:
            flush_paragraph()
            items: list[str] = []
            while i < len(lines):
                item_match = re.match(r"^([-*+])\s+(.+)$", lines[i].strip())
                if not item_match:
                    break
                items.append(f"<li>{_render_inline(item_match.group(2))}</li>")
                i += 1
            html_parts.append(f"<ul>{''.join(items)}</ul>")
            continue

        ordered_match = re.match(r"^\d+[.)]\s+(.+)$", stripped)
        if ordered_match:
            flush_paragraph()
            items = []
            while i < len(lines):
                item_match = re.match(r"^\d+[.)]\s+(.+)$", lines[i].strip())
                if not item_match:
                    break
                items.append(f"<li>{_render_inline(item_match.group(1))}</li>")
                i += 1
            html_parts.append(f"<ol>{''.join(items)}</ol>")
            continue

        paragraph.append(stripped)
        i += 1

    flush_paragraph()
    return "\n".join(html_parts) or "<p></p>"


def _render_inline(text: str) -> str:
    tokens: list[str] = []

    def stash_code(match: re.Match[str]) -> str:
        tokens.append(f"<code>{html.escape(match.group(1))}</code>")
        return f"\x00{len(tokens) - 1}\x00"

    escaped = re.sub(r"`([^`]+)`", stash_code, text)
    escaped = html.escape(escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", escaped)

    for index, token in enumerate(tokens):
        escaped = escaped.replace(f"\x00{index}\x00", token)
    return escaped


def _is_table_start(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    header = lines[index].strip()
    separator = lines[index + 1].strip()
    return "|" in header and bool(
        re.match(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$", separator)
    )


def _render_table(lines: list[str], index: int) -> tuple[str, int]:
    headers = _split_table_row(lines[index])
    i = index + 2
    rows: list[list[str]] = []
    while i < len(lines) and "|" in lines[i].strip() and lines[i].strip():
        rows.append(_split_table_row(lines[i]))
        i += 1

    head = "".join(f"<th>{_render_inline(cell)}</th>" for cell in headers)
    body_rows: list[str] = []
    for row in rows:
        padded = row + [""] * max(0, len(headers) - len(row))
        cells = "".join(
            f"<td>{_render_inline(cell)}</td>" for cell in padded[: len(headers)]
        )
        body_rows.append(f"<tr>{cells}</tr>")

    html_table = (
        "<table>"
        f"<thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table>"
    )
    return html_table, i


def _split_table_row(row: str) -> list[str]:
    stripped = row.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]
