"""`.pptx` → 공용 Document 구조.

슬라이드 하나를 `##` 제목 한 덩어리로 만든다. 제목 개체틀이 있으면 그 문구를,
없으면 `슬라이드 N`을 제목으로 쓴다. 슬라이드 번호를 항상 남기는 이유는
발표 자료에서 인용할 때 "몇 번째 장"이 사실상 유일한 출처 표시이기 때문이다.

살리는 것: 슬라이드 순서, 제목, 글머리 기호 단계, 굵게, 표, 그림, 발표자 노트.
버리는 것: 도형 위치·색·애니메이션, 마스터/레이아웃의 장식 문구.
"""

import re
import zipfile

from .hwpx import Block, Cell, Document, finalize
from .ooxml import (
    attr,
    child,
    children,
    ext_of,
    find_deep,
    iter_deep,
    ln,
    load_rels,
    read_xml,
    squash,
)

PRESENTATION_PART = "ppt/presentation.xml"
SLIDE_NAME_RE = re.compile(r"slide(\d+)\.xml$")


class PptxError(Exception):
    pass


class _Parser:
    def __init__(self, zf, cfg):
        self.zf = zf
        self.cfg = cfg or {}
        self.doc = Document()
        self._img_index = 0
        self.slide_count = 0

    def run(self):
        slides = self._slide_order()
        if not slides:
            raise PptxError("슬라이드를 찾지 못했습니다. 손상된 파일일 수 있습니다.")

        for number, part in enumerate(slides, start=1):
            self._slide(part, number)

        finalize(self.doc)
        self.doc.stats["slides"] = self.slide_count
        return self.doc

    def _slide_order(self):
        """presentation.xml 의 sldIdLst 순서를 따른다. 없으면 파일명 숫자순."""
        root = read_xml(self.zf, PRESENTATION_PART)
        rels = load_rels(self.zf, PRESENTATION_PART)
        ordered = []
        if root is not None:
            lst = find_deep(root, "sldIdLst")
            if lst is not None:
                for sld in children(lst, "sldId"):
                    # sldId 에는 id(숫자)와 r:id(관계) 두 개가 있고 로컬명이 똑같이 "id"라
                    # 네임스페이스를 봐야 구분된다.
                    rid = ""
                    for key, value in sld.attrib.items():
                        if key.startswith("{") and key.endswith("}id") and "relationships" in key:
                            rid = value
                    target = rels.get(rid)
                    if target and target in self.zf.namelist():
                        ordered.append(target)
        if ordered:
            return ordered

        names = [n for n in self.zf.namelist() if SLIDE_NAME_RE.search(n) and "/slides/" in n]
        return sorted(names, key=lambda n: int(SLIDE_NAME_RE.search(n).group(1)))

    # ---------- 슬라이드 ----------

    def _slide(self, part, number):
        root = read_xml(self.zf, part)
        if root is None:
            return
        self.slide_count += 1
        rels = load_rels(self.zf, part)

        tree = find_deep(root, "spTree")
        shapes = list(tree) if tree is not None else []

        title_shape, title_text = self._title(shapes)
        heading = title_text or f"슬라이드 {number}"
        if title_text:
            heading = f"{number}. {heading}"
        self.doc.blocks.append(Block("heading", text=squash(heading), level=2))

        for shape in shapes:
            if shape is title_shape:
                continue
            self._shape(shape, rels)

        if self.cfg.get("include_speaker_notes", True):
            self._notes(part, rels)

        self.doc.blocks.append(Block("blank"))

    def _title(self, shapes):
        for shape in shapes:
            if ln(shape.tag) != "sp":
                continue
            ph = find_deep(shape, "ph")
            if ph is None:
                continue
            kind = (attr(ph, "type") or "").lower()
            if kind in ("title", "ctrtitle"):
                text = squash(" ".join(self._plain_texts(shape)))
                return shape, text.replace("**", "")
        return None, ""

    def _shape(self, shape, rels):
        name = ln(shape.tag)
        if name == "sp":
            self._text_frame(shape)
        elif name == "pic":
            self._image(shape, rels)
        elif name == "graphicFrame":
            table = find_deep(shape, "tbl")
            if table is not None:
                self._table(table)
        elif name == "grpSp":
            for inner in shape:
                if ln(inner.tag) in ("sp", "pic", "graphicFrame", "grpSp"):
                    self._shape(inner, rels)

    def _text_frame(self, shape):
        body = find_deep(shape, "txBody")
        if body is None:
            return
        for para in children(body, "p"):
            text = self._paragraph_text(para)
            if not text.strip():
                continue
            depth = self._depth(para)
            self.doc.blocks.append(Block("para", text="  " * depth + "- " + text.strip()))

    def _depth(self, para):
        ppr = child(para, "pPr")
        if ppr is None:
            return 0
        try:
            return max(0, min(5, int(attr(ppr, "lvl", "0") or 0)))
        except ValueError:
            return 0

    def _paragraph_text(self, para):
        parts = []
        for node in para:
            name = ln(node.tag)
            if name == "r":
                parts.append(self._run(node))
            elif name == "br":
                parts.append(" ")
            elif name == "fld":
                t = child(node, "t")
                if t is not None:
                    parts.append(t.text or "")
        return squash("".join(parts))

    def _run(self, r):
        t = child(r, "t")
        text = t.text or "" if t is not None else ""
        if not text.strip():
            return text
        # DrawingML은 굵게를 자식 태그가 아니라 a:rPr 의 b="1" 속성으로 표시한다
        rpr = child(r, "rPr")
        bold = rpr is not None and (attr(rpr, "b") or "").lower() in ("1", "true")
        if bold:
            return f"**{text.strip()}**"
        return text

    def _plain_texts(self, el):
        return [t.text or "" for t in iter_deep(el, "t")]

    # ---------- 표 ----------

    def _table(self, tbl):
        rows = []
        open_cells = {}
        for tr in children(tbl, "tr"):
            row = []
            col = 0
            for tc in children(tr, "tc"):
                span = _int_attr(tc, "gridSpan", 1)
                row_span = _int_attr(tc, "rowSpan", 1)
                if (attr(tc, "hMerge") or "").lower() in ("1", "true"):
                    col += 1
                    continue
                if (attr(tc, "vMerge") or "").lower() in ("1", "true"):
                    if col in open_cells:
                        open_cells[col].row_span += 1
                    col += span
                    continue

                text = squash(" ".join(self._plain_texts(tc)))
                cell = Cell(text=text, col_span=span, row_span=row_span)
                row.append(cell)
                if row_span > 1:
                    open_cells[col] = cell
                else:
                    open_cells.pop(col, None)
                col += span
            if row:
                rows.append(row)

        if rows:
            self.doc.blocks.append(Block("table", rows=rows))

    # ---------- 그림 ----------

    def _image(self, shape, rels):
        blip = find_deep(shape, "blip")
        if blip is None:
            return
        rid = attr(blip, "embed") or attr(blip, "link") or ""
        member = rels.get(rid, "")
        if not member or not member.startswith("ppt/"):
            return
        self._img_index += 1
        label = f"img{self._img_index}"
        self.doc.images.append((label, member))
        alt = ""
        name_el = find_deep(shape, "cNvPr")
        if name_el is not None:
            alt = squash(attr(name_el, "descr") or "")
        caption = alt or label
        self.doc.blocks.append(
            Block("para", text=f"![{caption}]({{ASSETS}}/{label}{ext_of(member)})")
        )

    # ---------- 발표자 노트 ----------

    def _notes(self, part, rels):
        note_part = ""
        for rid, target in rels.items():
            if "/notesSlides/" in target:
                note_part = target
                break
        if not note_part:
            return
        root = read_xml(self.zf, note_part)
        if root is None:
            return

        lines = []
        for body in iter_deep(root, "txBody"):
            for para in children(body, "p"):
                text = squash(" ".join(self._plain_texts(para)))
                if text and not text.isdigit():
                    lines.append(text)
        if not lines:
            return
        self.doc.blocks.append(Block("para", text="> **발표자 노트** — " + " ".join(lines)))


def _int_attr(el, name, default):
    try:
        return max(1, int(attr(el, name, str(default)) or default))
    except ValueError:
        return default


def parse(path, cfg=None):
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if PRESENTATION_PART not in names and not any("/slides/slide" in n for n in names):
                raise PptxError(
                    "PowerPoint 구조가 아닙니다. 97-2003 형식(.ppt)이면 .pptx로 저장한 뒤 넣어주세요."
                )
            return _Parser(zf, cfg).run()
    except zipfile.BadZipFile as exc:
        raise PptxError("파일이 손상되었거나 .pptx 형식이 아닙니다.") from exc
