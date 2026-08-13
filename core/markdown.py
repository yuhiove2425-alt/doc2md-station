"""구조(Document) → 마크다운 문자열.

어떤 형식에서 왔든 이 파일 하나만 거치기 때문에, 결과물의 생김새는 항상 같다.
frontmatter에 `format`을 남기는 이유는, 뒤에서 결과를 쓰는 쪽이 "이 문서는 PDF에서
왔으니 표가 줄글로 풀려 있을 수 있다" 같은 판단을 할 수 있어야 하기 때문이다.
"""

import html
import re
from datetime import datetime
from pathlib import Path

LIST_ITEM_RE = re.compile(r"^\s*(?:-|\d+\.)\s+\S")

# frontmatter에 넣을 통계 — 없는 항목은 건너뛴다 (형식마다 셀 수 있는 게 다르다)
STAT_KEYS = [
    ("headings", "headings"),
    ("paragraphs", "paragraphs"),
    ("tables", "tables"),
    ("images", "images"),
    ("footnotes", "footnotes"),
    ("pages", "pages"),
    ("slides", "slides"),
    ("sheets", "sheets"),
    ("rows", "rows"),
]


def render(doc, source_name, assets_dir=""):
    body = _body(doc, assets_dir)
    meta = _frontmatter(doc, source_name, body)
    return meta + "\n" + body


def _frontmatter(doc, source_name, body):
    stats = doc.stats or {}
    suffix = Path(source_name).suffix.lower().lstrip(".")

    lines = [
        "---",
        f'source: "{_yaml_escape(source_name)}"',
        f"format: {suffix or 'unknown'}",
        f"converted_at: {datetime.now().isoformat(timespec='seconds')}",
    ]
    for key, label in STAT_KEYS:
        if key in stats:
            lines.append(f"{label}: {stats[key]}")
    if stats.get("encoding"):
        lines.append(f"encoding: {stats['encoding']}")
    lines.append(f"chars: {len(body)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _yaml_escape(text):
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _body(doc, assets_dir):
    out = []
    for b in doc.blocks:
        if b.kind == "heading":
            out.append("#" * b.level + " " + b.text)
            out.append("")
        elif b.kind == "para":
            text = b.text.replace("{ASSETS}", assets_dir or ".")
            # 목록 항목이 이어질 때 사이에 빈 줄이 들어가면 하위 항목의 들여쓰기가
            # 목록으로 안 읽히고 인용문처럼 보인다. 붙어 있을 때만 붙여 쓴다.
            if LIST_ITEM_RE.match(text) and len(out) >= 2 and out[-1] == "" and LIST_ITEM_RE.match(out[-2] or ""):
                out.pop()
            out.append(text)
            out.append("")
        elif b.kind == "raw":
            # 이미 마크다운인 내용 (원본 .md, 코드 블록) — 손대지 않는다
            out.append(b.text)
            out.append("")
        elif b.kind == "table":
            out.append(_table(b.rows))
            out.append("")
        elif b.kind == "blank":
            if out and out[-1] != "":
                out.append("")

    if doc.footnotes:
        out.append("")
        out.append("---")
        out.append("")
        for num, text in doc.footnotes:
            out.append(f"[^{num}]: {text}")
        out.append("")

    text = "\n".join(out)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip() + "\n"


def _table(rows):
    if not rows:
        return ""
    if _is_simple(rows):
        return _pipe_table(rows)
    return _html_table(rows)


def _is_simple(rows):
    widths = set()
    for row in rows:
        if any(c.col_span > 1 or c.row_span > 1 for c in row):
            return False
        widths.add(len(row))
    return len(widths) == 1


def _pipe_table(rows):
    head = rows[0]
    lines = ["| " + " | ".join(_esc(c.text) for c in head) + " |"]
    lines.append("| " + " | ".join("---" for _ in head) + " |")
    for row in rows[1:]:
        lines.append("| " + " | ".join(_esc(c.text) for c in row) + " |")
    return "\n".join(lines)


def _html_table(rows):
    lines = ["<table>"]
    for row in rows:
        lines.append("  <tr>")
        for c in row:
            attrs = ""
            if c.col_span > 1:
                attrs += f' colspan="{c.col_span}"'
            if c.row_span > 1:
                attrs += f' rowspan="{c.row_span}"'
            lines.append(f"    <td{attrs}>{html.escape(c.text)}</td>")
        lines.append("  </tr>")
    lines.append("</table>")
    return "\n".join(lines)


def _esc(text):
    return text.replace("|", "\\|").replace("\n", " ")
