"""`.docx` → 공용 Document 구조.

python-docx 없이 표준 라이브러리(zipfile + ElementTree)만 쓴다. 이 프로그램의
다른 파서와 같은 방침으로, 네임스페이스를 무시하고 로컬 태그명으로만 매칭한다.

살리는 것: 제목 레벨, 굵게, 목록, 표(셀 병합 포함), 그림, 각주/미주.
버리는 것: 머리말·꼬리말·쪽번호(별도 파트라 애초에 읽지 않는다), 삭제된
추적 변경 내용(w:del), 글자 색·밑줄 같은 시각 서식.
"""

import re
import zipfile

from .hwpx import Block, Cell, Document, finalize
from .ooxml import (
    attr,
    bold_on,
    child,
    children,
    descend,
    ext_of,
    find_deep,
    iter_deep,
    ln,
    load_rels,
    read_xml,
    squash,
)

DOCUMENT_PART = "word/document.xml"
HEADING_NAME_RE = re.compile(r"^\s*(?:제목|개요|Heading|Outline)\s*(\d+)?", re.IGNORECASE)
TITLE_NAMES = {"title", "제목", "표제"}
SUBTITLE_NAMES = {"subtitle", "부제", "부제목"}
# 문단 안에서 순서대로 훑어야 하는 컨테이너들 (하이퍼링크·추적 변경 등)
RUN_CONTAINERS = {"hyperlink", "ins", "smartTag", "sdt", "sdtContent", "bookmarkStart"}


class DocxError(Exception):
    pass


class _Styles:
    """styles.xml 을 읽어 스타일 ID → 제목 레벨로 바꿔주는 표."""

    def __init__(self):
        self.by_id = {}
        self.list_ids = set()

    def load(self, root):
        if root is None:
            return
        for style in children(root, "style"):
            sid = attr(style, "styleId") or ""
            name_el = child(style, "name")
            name = (attr(name_el, "val") or "") if name_el is not None else ""
            outline = descend(style, "pPr", "outlineLvl")
            level = None
            if outline is not None:
                try:
                    level = int(attr(outline, "val", "0") or 0) + 1
                except ValueError:
                    level = None
            self.by_id[sid] = {"name": name, "outline": level}

            # 목록 서식은 문단이 아니라 스타일 쪽에 numPr 이 붙는 경우가 많다
            if descend(style, "pPr", "numPr") is not None:
                self.list_ids.add(sid)
            elif re.match(r"^(List|목록)", (name or sid).strip(), re.IGNORECASE):
                self.list_ids.add(sid)

    def heading_level(self, style_id):
        if not style_id:
            return None
        info = self.by_id.get(style_id, {})
        for candidate in (info.get("name", ""), style_id):
            level = _level_from_name(candidate)
            if level:
                return level
        return info.get("outline")


def _level_from_name(name):
    text = (name or "").strip()
    if not text:
        return None
    low = text.lower().replace(" ", "")
    if low in TITLE_NAMES:
        return 1
    if low in SUBTITLE_NAMES:
        return 2
    m = HEADING_NAME_RE.match(text)
    if not m:
        return None
    level = int(m.group(1)) if m.group(1) else 1
    return max(1, min(6, level))


class _Parser:
    def __init__(self, zf):
        self.zf = zf
        self.doc = Document()
        self.styles = _Styles()
        self.rels = {}
        self.notes = {}
        self._img_index = 0
        self._note_index = 0
        self._note_seen = {}

    def run(self):
        root = read_xml(self.zf, DOCUMENT_PART)
        if root is None:
            raise DocxError("word/document.xml 을 읽지 못했습니다. 손상된 파일일 수 있습니다.")

        self.styles.load(read_xml(self.zf, "word/styles.xml"))
        self.rels = load_rels(self.zf, DOCUMENT_PART)
        self._load_notes()

        body = find_deep(root, "body")
        self._walk(body if body is not None else root)
        finalize(self.doc)
        return self.doc

    # ---------- 본문 ----------

    def _walk(self, el):
        for node in el:
            name = ln(node.tag)
            if name == "p":
                self._paragraph(node)
            elif name == "tbl":
                self._table(node)
            elif name in ("sdt", "sdtContent"):
                self._walk(node)
            elif name == "sectPr":
                continue

    def _paragraph(self, p):
        style_id = None
        style_el = descend(p, "pPr", "pStyle")
        if style_el is not None:
            style_id = attr(style_el, "val")

        text = self._runs_text(p)
        if not text.strip():
            self.doc.blocks.append(Block("blank"))
            return

        level = self.styles.heading_level(style_id)
        if level is None:
            outline = descend(p, "pPr", "outlineLvl")
            if outline is not None:
                try:
                    level = int(attr(outline, "val", "0") or 0) + 1
                except ValueError:
                    level = None

        if level:
            # 제목 줄에서는 굵게 표시가 중복이라 지운다
            clean = squash(text.replace("**", ""))
            self.doc.blocks.append(Block("heading", text=clean, level=max(1, min(6, level))))
            return

        prefix = self._list_prefix(p, style_id)
        self.doc.blocks.append(Block("para", text=prefix + text.strip()))

    def _list_prefix(self, p, style_id=None):
        numpr = descend(p, "pPr", "numPr")
        if numpr is None:
            return "- " if style_id in self.styles.list_ids else ""
        ilvl = child(numpr, "ilvl")
        try:
            depth = int(attr(ilvl, "val", "0") or 0) if ilvl is not None else 0
        except ValueError:
            depth = 0
        return "  " * max(0, min(5, depth)) + "- "

    def _runs_text(self, el):
        parts = []
        self._collect(el, parts)
        text = "".join(parts)
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip()

    def _collect(self, el, parts):
        for node in el:
            name = ln(node.tag)
            if name == "del":
                continue  # 추적 변경에서 지워진 내용은 버린다
            if name == "r":
                parts.append(self._run(node))
            elif name in RUN_CONTAINERS or name in ("fldSimple", "pPr"):
                if name == "pPr":
                    continue
                self._collect(node, parts)

    def _run(self, r):
        rpr = child(r, "rPr")
        bold = bold_on(rpr, "b") or bold_on(rpr, "bCs")

        pieces = []
        for node in r.iter():
            name = ln(node.tag)
            if name == "t":
                pieces.append(node.text or "")
            elif name == "tab":
                pieces.append(" ")
            elif name in ("br", "cr"):
                pieces.append("\n")
            elif name == "noBreakHyphen":
                pieces.append("-")
            elif name in ("footnoteReference", "endnoteReference"):
                pieces.append(self._note_ref(node, name))
            elif name in ("drawing", "pict", "object"):
                pieces.append(self._image(node))

        text = "".join(pieces)
        if bold and text.strip():
            lead = text[: len(text) - len(text.lstrip())]
            tail = text[len(text.rstrip()) :]
            return f"{lead}**{text.strip()}**{tail}"
        return text

    # ---------- 표 ----------

    def _table(self, tbl):
        rows = []
        open_cells = {}  # 세로 병합이 진행 중인 열 → 위쪽 Cell
        for tr in children(tbl, "tr"):
            row = []
            col = 0
            for tc in children(tr, "tc"):
                tcpr = child(tc, "tcPr")
                span = 1
                if tcpr is not None:
                    grid = child(tcpr, "gridSpan")
                    if grid is not None:
                        try:
                            span = max(1, int(attr(grid, "val", "1") or 1))
                        except ValueError:
                            span = 1

                vmerge = child(tcpr, "vMerge") if tcpr is not None else None
                merged_continue = False
                if vmerge is not None:
                    val = (attr(vmerge, "val") or "continue").lower()
                    merged_continue = val in ("continue", "")

                if merged_continue and col in open_cells:
                    open_cells[col].row_span += 1
                    col += span
                    continue

                text = self._cell_text(tc)
                cell = Cell(text=text, col_span=span)
                row.append(cell)
                if vmerge is not None:
                    open_cells[col] = cell
                else:
                    open_cells.pop(col, None)
                col += span

            if row:
                rows.append(row)

        if rows:
            self.doc.blocks.append(Block("table", rows=rows))

    def _cell_text(self, tc):
        lines = []
        for node in tc:
            name = ln(node.tag)
            if name == "p":
                text = self._runs_text(node)
                if text.strip():
                    lines.append(text.strip())
            elif name == "tbl":
                # 중첩 표는 셀 안에서 줄바꿈으로 펼친다
                for tr in children(node, "tr"):
                    cells = [self._cell_text(c) for c in children(tr, "tc")]
                    lines.append(" / ".join(x for x in cells if x))
        return "\n".join(lines)

    # ---------- 그림 ----------

    def _image(self, node):
        rid = ""
        blip = find_deep(node, "blip")
        if blip is not None:
            rid = attr(blip, "embed") or attr(blip, "link") or ""
        if not rid:
            data = find_deep(node, "imagedata")
            if data is not None:
                rid = attr(data, "id") or ""
        member = self.rels.get(rid, "")
        if not member or not member.startswith("word/"):
            return ""

        self._img_index += 1
        label = f"img{self._img_index}"
        self.doc.images.append((label, member))
        return f"\n![{label}]({{ASSETS}}/{label}{ext_of(member)})\n"

    # ---------- 각주·미주 ----------

    def _load_notes(self):
        for part, tag in (("word/footnotes.xml", "footnote"), ("word/endnotes.xml", "endnote")):
            root = read_xml(self.zf, part)
            if root is None:
                continue
            for note in children(root, tag):
                nid = attr(note, "id")
                kind = (attr(note, "type") or "").lower()
                if nid is None or kind in ("separator", "continuationseparator", "continuationnotice"):
                    continue
                texts = [t.text or "" for t in iter_deep(note, "t")]
                self.notes[(tag, nid)] = squash(" ".join(texts))

    def _note_ref(self, node, ref_name):
        tag = "footnote" if ref_name.startswith("footnote") else "endnote"
        nid = attr(node, "id")
        key = (tag, nid)
        if key in self._note_seen:
            return f"[^{self._note_seen[key]}]"
        body = self.notes.get(key)
        if not body:
            return ""
        self._note_index += 1
        self._note_seen[key] = self._note_index
        self.doc.footnotes.append((self._note_index, body))
        return f"[^{self._note_index}]"


def parse(path):
    try:
        with zipfile.ZipFile(path) as zf:
            if DOCUMENT_PART not in zf.namelist():
                raise DocxError(
                    "Word 문서 구조가 아닙니다. 97-2003 형식(.doc)이면 .docx로 저장한 뒤 넣어주세요."
                )
            return _Parser(zf).run()
    except zipfile.BadZipFile as exc:
        raise DocxError("파일이 손상되었거나 .docx 형식이 아닙니다.") from exc
