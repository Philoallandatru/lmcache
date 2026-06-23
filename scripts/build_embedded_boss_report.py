#!/usr/bin/env python3
"""Build a self-contained HTML version of AI_SSD_BOSS_REPORT.md.

The generated HTML embeds PNG figures as base64 data URIs and replaces the two
Mermaid diagrams in the Markdown source with static inline SVG diagrams. This
keeps the report portable for email or browser sharing.
"""

from __future__ import annotations

import base64
import html
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "AI_SSD_BOSS_REPORT.md"
OUTPUT = ROOT / "AI_SSD_BOSS_REPORT_EMBEDDED.html"


def inline_markup(text: str) -> str:
    text = html.escape(text.strip())
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    return text


def image_html(alt: str, rel_path: str) -> str:
    img_path = (ROOT / rel_path).resolve()
    data = base64.b64encode(img_path.read_bytes()).decode("ascii")
    return (
        '<figure class="figure">'
        f'<img src="data:image/png;base64,{data}" alt="{html.escape(alt)}" />'
        "</figure>"
    )


def static_svg(kind: int) -> str:
    if kind == 1:
        return """
<figure class="figure svg-figure">
<svg viewBox="0 0 980 250" role="img" aria-label="KV Cache 分层示意">
  <defs>
    <marker id="arrow1" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto">
      <path d="M0,0 L0,6 L9,3 z" fill="#334155"/>
    </marker>
  </defs>
  <rect x="25" y="78" width="120" height="72" rx="10" fill="#e0f2fe" stroke="#0284c7"/>
  <text x="85" y="108" text-anchor="middle" font-size="18" font-weight="700">用户请求</text>
  <text x="85" y="132" text-anchor="middle" font-size="13">Prompt</text>
  <rect x="200" y="78" width="130" height="72" rx="10" fill="#f0fdf4" stroke="#16a34a"/>
  <text x="265" y="108" text-anchor="middle" font-size="18" font-weight="700">模型推理</text>
  <text x="265" y="132" text-anchor="middle" font-size="13">Prefill / Decode</text>
  <rect x="385" y="78" width="125" height="72" rx="10" fill="#fef3c7" stroke="#d97706"/>
  <text x="448" y="108" text-anchor="middle" font-size="18" font-weight="700">KV Cache</text>
  <text x="448" y="132" text-anchor="middle" font-size="13">中间状态</text>
  <rect x="590" y="20" width="155" height="58" rx="10" fill="#fee2e2" stroke="#dc2626"/>
  <text x="668" y="44" text-anchor="middle" font-size="16" font-weight="700">GPU 显存</text>
  <text x="668" y="64" text-anchor="middle" font-size="12">最快 / 容量最小</text>
  <rect x="590" y="96" width="155" height="58" rx="10" fill="#ede9fe" stroke="#7c3aed"/>
  <text x="668" y="120" text-anchor="middle" font-size="16" font-weight="700">主机内存</text>
  <text x="668" y="140" text-anchor="middle" font-size="12">较快 / 容量更大</text>
  <rect x="590" y="172" width="155" height="58" rx="10" fill="#f1f5f9" stroke="#475569"/>
  <text x="668" y="196" text-anchor="middle" font-size="16" font-weight="700">SSD / AI SSD</text>
  <text x="668" y="216" text-anchor="middle" font-size="12">容量最大 / 延迟最高</text>
  <rect x="805" y="172" width="145" height="58" rx="10" fill="#ecfeff" stroke="#0891b2"/>
  <text x="878" y="196" text-anchor="middle" font-size="16" font-weight="700">L3 reload</text>
  <text x="878" y="216" text-anchor="middle" font-size="12">缓存回盘读取</text>
  <line x1="145" y1="114" x2="195" y2="114" stroke="#334155" stroke-width="2" marker-end="url(#arrow1)"/>
  <line x1="330" y1="114" x2="380" y2="114" stroke="#334155" stroke-width="2" marker-end="url(#arrow1)"/>
  <line x1="510" y1="114" x2="585" y2="48" stroke="#334155" stroke-width="2" marker-end="url(#arrow1)"/>
  <line x1="510" y1="114" x2="585" y2="125" stroke="#334155" stroke-width="2" marker-end="url(#arrow1)"/>
  <line x1="510" y1="114" x2="585" y2="201" stroke="#334155" stroke-width="2" marker-end="url(#arrow1)"/>
  <line x1="745" y1="201" x2="800" y2="201" stroke="#334155" stroke-width="2" marker-end="url(#arrow1)"/>
</svg>
</figure>
"""
    return """
<figure class="figure svg-figure">
<svg viewBox="0 0 980 310" role="img" aria-label="多 prompt 回放测试流程">
  <defs>
    <marker id="arrow2" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto">
      <path d="M0,0 L0,6 L9,3 z" fill="#334155"/>
    </marker>
  </defs>
  <rect x="40" y="40" width="190" height="70" rx="10" fill="#e0f2fe" stroke="#0284c7"/>
  <text x="135" y="68" text-anchor="middle" font-size="16" font-weight="700">普通缓存命中测试</text>
  <text x="135" y="92" text-anchor="middle" font-size="13">多数请求命中内存</text>
  <polygon points="315,40 410,75 315,110 220,75" fill="#fef3c7" stroke="#d97706"/>
  <text x="315" y="70" text-anchor="middle" font-size="15" font-weight="700">能看出盘差?</text>
  <text x="315" y="91" text-anchor="middle" font-size="13">通常不能</text>
  <rect x="500" y="40" width="210" height="70" rx="10" fill="#fee2e2" stroke="#dc2626"/>
  <text x="605" y="68" text-anchor="middle" font-size="16" font-weight="700">L2 / 主机内存命中</text>
  <text x="605" y="92" text-anchor="middle" font-size="13">盘差被遮住</text>
  <rect x="40" y="190" width="190" height="70" rx="10" fill="#f0fdf4" stroke="#16a34a"/>
  <text x="135" y="218" text-anchor="middle" font-size="16" font-weight="700">20 个不同 prompt</text>
  <text x="135" y="242" text-anchor="middle" font-size="13">先把缓存塞满</text>
  <rect x="285" y="190" width="155" height="70" rx="10" fill="#ede9fe" stroke="#7c3aed"/>
  <text x="363" y="218" text-anchor="middle" font-size="16" font-weight="700">回放 p0</text>
  <text x="363" y="242" text-anchor="middle" font-size="13">最早的请求</text>
  <rect x="500" y="190" width="130" height="70" rx="10" fill="#fef3c7" stroke="#d97706"/>
  <text x="565" y="218" text-anchor="middle" font-size="16" font-weight="700">L2 miss</text>
  <text x="565" y="242" text-anchor="middle" font-size="13">内存放不下</text>
  <rect x="690" y="190" width="210" height="70" rx="10" fill="#ecfeff" stroke="#0891b2"/>
  <text x="795" y="218" text-anchor="middle" font-size="16" font-weight="700">从 SSD 读回 KV Cache</text>
  <text x="795" y="242" text-anchor="middle" font-size="13">暴露真实 L3 reload 延迟</text>
  <line x1="230" y1="75" x2="220" y2="75" stroke="#334155" stroke-width="2" marker-end="url(#arrow2)"/>
  <line x1="410" y1="75" x2="495" y2="75" stroke="#334155" stroke-width="2" marker-end="url(#arrow2)"/>
  <line x1="230" y1="225" x2="280" y2="225" stroke="#334155" stroke-width="2" marker-end="url(#arrow2)"/>
  <line x1="440" y1="225" x2="495" y2="225" stroke="#334155" stroke-width="2" marker-end="url(#arrow2)"/>
  <line x1="630" y1="225" x2="685" y2="225" stroke="#334155" stroke-width="2" marker-end="url(#arrow2)"/>
</svg>
</figure>
"""


def table_to_html(lines: list[str]) -> str:
    rows = []
    for line in lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
            continue
        rows.append(cells)
    if not rows:
        return ""
    header, body = rows[0], rows[1:]
    out = ["<table>", "<thead><tr>"]
    out.extend(f"<th>{inline_markup(c)}</th>" for c in header)
    out.append("</tr></thead>")
    out.append("<tbody>")
    for row in body:
        out.append("<tr>")
        out.extend(f"<td>{inline_markup(c)}</td>" for c in row)
        out.append("</tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


def markdown_to_html(md: str) -> str:
    html_parts: list[str] = []
    table_lines: list[str] = []
    list_kind: str | None = None
    mermaid: list[str] | None = None
    mermaid_count = 0

    def flush_table() -> None:
        nonlocal table_lines
        if table_lines:
            html_parts.append(table_to_html(table_lines))
            table_lines = []

    def close_list() -> None:
        nonlocal list_kind
        if list_kind:
            html_parts.append(f"</{list_kind}>")
            list_kind = None

    for raw in md.splitlines():
        line = raw.rstrip()

        if mermaid is not None:
            if line.startswith("```"):
                mermaid_count += 1
                html_parts.append(static_svg(mermaid_count))
                mermaid = None
            else:
                mermaid.append(line)
            continue

        if line.startswith("```mermaid"):
            flush_table()
            close_list()
            mermaid = []
            continue

        if not line.strip():
            flush_table()
            close_list()
            continue

        if line.startswith("|"):
            close_list()
            table_lines.append(line)
            continue

        flush_table()

        image_match = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", line.strip())
        if image_match:
            close_list()
            alt, rel_path = image_match.groups()
            html_parts.append(image_html(alt, rel_path))
            continue

        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            close_list()
            level = len(heading.group(1))
            text = inline_markup(heading.group(2))
            html_parts.append(f"<h{level}>{text}</h{level}>")
            continue

        if line.startswith("> "):
            close_list()
            html_parts.append(f"<blockquote>{inline_markup(line[2:])}</blockquote>")
            continue

        ordered = re.match(r"^\d+\.\s+(.+)$", line)
        unordered = re.match(r"^-\s+(.+)$", line)
        if ordered or unordered:
            kind = "ol" if ordered else "ul"
            if list_kind != kind:
                close_list()
                html_parts.append(f"<{kind}>")
                list_kind = kind
            item = ordered.group(1) if ordered else unordered.group(1)
            html_parts.append(f"<li>{inline_markup(item)}</li>")
            continue

        close_list()
        html_parts.append(f"<p>{inline_markup(line)}</p>")

    flush_table()
    close_list()
    return "\n".join(html_parts)


def main() -> None:
    body = markdown_to_html(SOURCE.read_text())
    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AI SSD 预研简报</title>
  <style>
    :root {{
      color-scheme: light;
      --text: #172033;
      --muted: #5b667a;
      --line: #d9e0ea;
      --panel: #f7f9fc;
      --accent: #0f766e;
    }}
    body {{
      margin: 0;
      background: #ffffff;
      color: var(--text);
      font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC", "Microsoft YaHei", Arial, sans-serif;
    }}
    main {{
      max-width: 980px;
      margin: 0 auto;
      padding: 36px 28px 72px;
    }}
    h1 {{
      font-size: 34px;
      line-height: 1.2;
      margin: 0 0 18px;
      letter-spacing: 0;
    }}
    h2 {{
      margin: 40px 0 14px;
      padding-top: 12px;
      border-top: 1px solid var(--line);
      font-size: 24px;
    }}
    h3 {{
      margin: 28px 0 12px;
      font-size: 19px;
    }}
    p, li {{ color: var(--text); }}
    blockquote {{
      margin: 0 0 22px;
      padding: 12px 16px;
      background: var(--panel);
      border-left: 4px solid var(--accent);
      color: var(--muted);
    }}
    .figure {{
      margin: 24px 0 10px;
      padding: 14px;
      border: 1px solid var(--line);
      background: #fff;
      border-radius: 10px;
      overflow: hidden;
    }}
    .figure img, .figure svg {{
      display: block;
      width: 100%;
      height: auto;
    }}
    .svg-figure {{
      background: #fbfdff;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin: 14px 0 22px;
      font-size: 14px;
    }}
    th, td {{
      border: 1px solid var(--line);
      padding: 8px 10px;
      text-align: left;
      vertical-align: top;
    }}
    th {{
      background: var(--panel);
      font-weight: 700;
    }}
    code {{
      background: #eef2f7;
      padding: 1px 5px;
      border-radius: 4px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.92em;
    }}
    strong {{ font-weight: 750; }}
    @media print {{
      main {{ max-width: none; padding: 20mm 16mm; }}
      h2 {{ break-after: avoid; }}
      .figure {{ break-inside: avoid; }}
    }}
  </style>
</head>
<body>
<main>
{body}
</main>
</body>
</html>
"""
    OUTPUT.write_text(html_doc)
    print(f"Wrote {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
