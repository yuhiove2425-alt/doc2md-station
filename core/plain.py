"""텍스트 계열(`.txt` `.md` `.csv` `.tsv` `.html` `.json`) → 공용 Document 구조.

한국에서 받은 파일은 UTF-8이 아니라 CP949(euc-kr)인 경우가 잦아서, 인코딩을
차례로 시도해 본 뒤 가장 그럴듯한 것으로 읽는다. 여기서 실패하면 뒤 단계가
전부 깨진 글자를 물고 가기 때문에 인코딩 판정을 제일 먼저 한다.
"""

import csv
import io
import json
import re
from html.parser import HTMLParser

from .hwpx import Block, Cell, Document, finalize

ENCODINGS = ("utf-8-sig", "utf-8", "cp949", "euc-kr", "utf-16")
FRONTMATTER_RE = re.compile(r"^---\r?\n.*?\r?\n---\r?\n", re.DOTALL)


class PlainError(Exception):
    pass


def read_text(path):
    """인코딩을 추정해 문자열로 읽는다. (텍스트, 인코딩 이름)"""
    data = path.read_bytes()
    if not data.strip():
        raise PlainError("빈 파일입니다.")
    for encoding in ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace"), "utf-8(일부 손실)"


# ---------- .txt ----------


def parse_text(path, cfg=None):
    text, encoding = read_text(path)
    doc = Document()
    for chunk in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        body = chunk.strip("\n")
        if not body.strip():
            continue
        doc.blocks.append(Block("para", text=body.strip()))
        doc.blocks.append(Block("blank"))
    if not doc.blocks:
        raise PlainError("내용이 없습니다.")
    finalize(doc)
    doc.stats["encoding"] = encoding
    return doc


# ---------- .md ----------


def parse_markdown(path, cfg=None):
    """이미 마크다운이면 본문은 그대로 두고 frontmatter만 새로 붙인다.

    원본에 frontmatter가 있으면 지운다. 두 개가 겹치면 뒤 단계에서 어느 쪽이
    진짜 출처인지 알 수 없게 되기 때문이다.
    """
    text, encoding = read_text(path)
    body = FRONTMATTER_RE.sub("", text.replace("\r\n", "\n"), count=1).strip()
    if not body:
        raise PlainError("내용이 없습니다.")

    doc = Document()
    doc.blocks.append(Block("raw", text=body))
    finalize(doc)
    doc.stats.update(
        {
            "headings": len(re.findall(r"(?m)^#{1,6}\s+\S", body)),
            "paragraphs": len([p for p in re.split(r"\n\s*\n", body) if p.strip()]),
            "tables": len(re.findall(r"(?m)^\|.+\|\s*$\n^\|[-: |]+\|\s*$", body)),
            "images": len(re.findall(r"!\[[^\]]*\]\([^)]+\)", body)),
            "encoding": encoding,
        }
    )
    return doc


# ---------- .csv / .tsv ----------


def parse_csv(path, cfg=None):
    text, encoding = read_text(path)
    delimiter = "\t" if path.suffix.lower() == ".tsv" else _sniff(text)

    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter) if any(c.strip() for c in r)]
    if not rows:
        raise PlainError("내용이 없습니다.")

    limit = int((cfg or {}).get("sheet_max_rows", 2000) or 2000)
    truncated = len(rows) > limit
    rows = rows[:limit]

    width = max(len(r) for r in rows)
    table = [[Cell(text=" ".join(v.split())) for v in (r + [""] * (width - len(r)))] for r in rows]

    doc = Document()
    doc.blocks.append(Block("table", rows=table))
    if truncated:
        doc.blocks.append(Block("para", text=f"> 행이 많아 위 {limit}행까지만 옮겼습니다."))
    finalize(doc)
    doc.stats["encoding"] = encoding
    doc.stats["rows"] = len(rows)
    return doc


def _sniff(text):
    sample = "\n".join(text.splitlines()[:20])
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


# ---------- .json ----------


def parse_json(path, cfg=None):
    text, encoding = read_text(path)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlainError(f"JSON 형식이 아닙니다 ({exc.lineno}행): {exc.msg}") from exc

    pretty = json.dumps(data, ensure_ascii=False, indent=2)
    doc = Document()
    doc.blocks.append(Block("raw", text="```json\n" + pretty + "\n```"))
    finalize(doc)
    doc.stats["encoding"] = encoding
    return doc


# ---------- .html ----------

SKIP_TAGS = {"script", "style", "head", "noscript", "svg", "template"}
HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
BLOCK_TAGS = {"p", "div", "section", "article", "blockquote", "pre", "figcaption", "dd", "dt"}


class _HtmlReader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.doc = Document()
        self._skip = 0
        self._buf = []
        self._heading = None
        self._bold = 0
        self._list_depth = 0
        self._table = None
        self._row = None
        self._cell = None

    # 텍스트 모으기 -------------------------------------------------

    def handle_data(self, data):
        if self._skip:
            return
        # 앞뒤 공백을 통째로 버리면 "본문 <b>강조</b> 입니다"가 붙어버린다.
        # 안쪽 공백만 하나로 줄이고 경계 공백은 남긴다.
        text = data if self._cell is not None else re.sub(r"\s+", " ", data)
        if not text.strip():
            if self._buf and not self._buf[-1].endswith(" "):
                self._buf.append(" ")
            return
        self._buf.append(text)

    def _text(self):
        text = " ".join("".join(self._buf).split())
        self._buf = []
        return text

    def _flush(self, prefix=""):
        text = self._text()
        if not text:
            return
        if self._cell is not None:
            self._cell.append(text)
            return
        if self._heading:
            self.doc.blocks.append(Block("heading", text=text.replace("**", ""), level=self._heading))
        else:
            self.doc.blocks.append(Block("para", text=prefix + text))

    # 태그 ---------------------------------------------------------

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        attrs = dict(attrs)

        if tag in HEADING_TAGS:
            self._flush()
            self._heading = HEADING_TAGS[tag]
        elif tag in BLOCK_TAGS:
            self._flush()
        elif tag == "br":
            self._buf.append(" ")
        elif tag in ("b", "strong"):
            self._bold += 1
            self._buf.append("**")
        elif tag in ("ul", "ol"):
            self._flush()
            self._list_depth += 1
        elif tag == "li":
            self._flush()
        elif tag == "table":
            self._flush()
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            self._cell_span = (_int(attrs.get("colspan"), 1), _int(attrs.get("rowspan"), 1))
        elif tag == "img":
            src = attrs.get("src", "")
            alt = attrs.get("alt", "그림")
            if src:
                self._buf.append(f" ![{alt}]({src}) ")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return

        if tag in HEADING_TAGS:
            self._flush()
            self._heading = None
        elif tag in ("b", "strong"):
            if self._bold:
                self._bold -= 1
                self._buf.append("**")
        elif tag == "li":
            prefix = "  " * max(0, self._list_depth - 1) + "- "
            self._flush(prefix)
        elif tag in ("ul", "ol"):
            self._flush()
            self._list_depth = max(0, self._list_depth - 1)
        elif tag in ("td", "th") and self._cell is not None:
            text = " ".join(("".join(self._buf) + " " + " ".join(self._cell)).split())
            self._buf = []
            span, rspan = getattr(self, "_cell_span", (1, 1))
            self._row.append(Cell(text=text, col_span=span, row_span=rspan))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.doc.blocks.append(Block("table", rows=self._table))
            self._table = None
        elif tag in BLOCK_TAGS:
            self._flush()

    def close(self):
        super().close()
        self._flush()
        return self.doc


def _int(value, default):
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def parse_html(path, cfg=None):
    text, encoding = read_text(path)
    reader = _HtmlReader()
    reader.feed(text)
    doc = reader.close()
    if not doc.blocks:
        raise PlainError("본문을 찾지 못했습니다.")
    finalize(doc)
    doc.stats["encoding"] = encoding
    return doc
