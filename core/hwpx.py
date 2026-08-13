import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

SKIP_CONTAINERS = {"header", "footer", "pageNum", "autoNum", "newNum", "pageHiding", "pageOddEven"}


class HwpxError(Exception):
    pass


HEADING_STYLE_RE = re.compile(r"^(제목|개요|Heading|Outline)\s*(\d+)?")


def ln(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def attr(el, name, default=None):
    if name in el.attrib:
        return el.attrib[name]
    for k, v in el.attrib.items():
        if ln(k) == name:
            return v
    return default


def children(el, name):
    return [c for c in el if ln(c.tag) == name]


def find_deep(el, name):
    for c in el.iter():
        if ln(c.tag) == name:
            return c
    return None


@dataclass
class Cell:
    text: str = ""
    col_span: int = 1
    row_span: int = 1


@dataclass
class Block:
    kind: str
    text: str = ""
    level: int = 0
    rows: list = field(default_factory=list)
    ref: str = ""


@dataclass
class Document:
    blocks: list = field(default_factory=list)
    footnotes: list = field(default_factory=list)
    images: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)


class StyleTable:
    def __init__(self):
        self.char = {}
        self.para = {}
        self.style = {}
        self.bin_items = {}

    def load(self, root):
        for el in root.iter():
            name = ln(el.tag)
            if name == "charPr":
                cid = attr(el, "id")
                if cid is None:
                    continue
                kids = {ln(c.tag) for c in el}
                self.char[cid] = {
                    "bold": "bold" in kids,
                    "italic": "italic" in kids,
                    "height": int(attr(el, "height", "1000") or 1000),
                }
            elif name == "paraPr":
                pid = attr(el, "id")
                if pid is None:
                    continue
                level = None
                heading = find_deep(el, "heading")
                if heading is not None and (attr(heading, "type") or "").upper() == "OUTLINE":
                    try:
                        level = int(attr(heading, "level", "0") or 0)
                    except ValueError:
                        level = None
                self.para[pid] = {"outline": level}
            elif name == "style":
                sid = attr(el, "id")
                if sid is None:
                    continue
                self.style[sid] = {
                    "name": attr(el, "name", "") or "",
                    "para_ref": attr(el, "paraPrIDRef"),
                }
            elif name == "binItem":
                bid = attr(el, "id") or attr(el, "binaryItemIDRef")
                path = attr(el, "href") or attr(el, "binaryItemIDRef") or ""
                if bid:
                    self.bin_items[bid] = path

    def heading_level(self, style_ref, para_ref):
        info = self.style.get(style_ref)
        if info:
            m = HEADING_STYLE_RE.match(info["name"].strip())
            if m:
                return int(m.group(2)) if m.group(2) else 1
            if info["name"].strip() in ("본문", "바탕글", "Normal", "Body"):
                return None
        pinfo = self.para.get(para_ref)
        if pinfo and pinfo["outline"] is not None:
            return pinfo["outline"] + 1
        return None


class Parser:
    def __init__(self, zf):
        self.zf = zf
        self.styles = StyleTable()
        self.doc = Document()
        self._fn_index = 0
        self._img_index = 0
        self._bin_names = [n for n in zf.namelist() if n.lower().startswith("bindata/")]

    def run(self):
        header = self._read_first(["Contents/header.xml", "contents/header.xml"])
        if header is not None:
            self.styles.load(header)
        for name in self._section_names():
            root = self._read(name)
            if root is None:
                continue
            sec = find_deep(root, "sec")
            container = sec if sec is not None else root
            for el in container:
                if ln(el.tag) == "p":
                    self._paragraph(el)
        self._finish()
        return self.doc

    def _section_names(self):
        names = [n for n in self.zf.namelist() if re.search(r"section\d+\.xml$", n, re.I)]
        return sorted(names, key=lambda n: int(re.search(r"section(\d+)", n, re.I).group(1)))

    def _read(self, name):
        try:
            return ET.fromstring(self.zf.read(name))
        except (KeyError, ET.ParseError):
            return None

    def _read_first(self, candidates):
        lowered = {n.lower(): n for n in self.zf.namelist()}
        for c in candidates:
            real = lowered.get(c.lower())
            if real:
                root = self._read(real)
                if root is not None:
                    return root
        return None

    def _paragraph(self, p):
        level = self.styles.heading_level(attr(p, "styleIDRef"), attr(p, "paraPrIDRef"))
        parts = []
        for run in children(p, "run"):
            parts.extend(self._run(run))

        text = "".join(x for x in parts if isinstance(x, str))
        text = re.sub(r"[ \t]{2,}", " ", text).strip()
        tables = [x for x in parts if isinstance(x, Block)]

        if text:
            if level:
                plain = text.replace("**", "").strip()
                self.doc.blocks.append(Block("heading", text=plain, level=min(level, 6)))
            else:
                self.doc.blocks.append(Block("para", text=text))
        elif not tables:
            self.doc.blocks.append(Block("blank"))

        for t in tables:
            self.doc.blocks.append(t)

    def _run(self, run):
        style = self.styles.char.get(attr(run, "charPrIDRef"), {})
        out = []
        for el in run:
            name = ln(el.tag)
            if name in SKIP_CONTAINERS:
                continue
            if name == "t":
                out.append(self._decorate(self._text_of(el), style))
            elif name == "tbl":
                out.append(self._table(el))
            elif name == "equation":
                out.append(self._equation(el))
            elif name in ("pic", "container"):
                out.append(self._image(el))
            elif name == "footNote":
                out.append(self._footnote(el))
            elif name == "ctrl":
                for sub in el:
                    if ln(sub.tag) not in SKIP_CONTAINERS:
                        out.extend(self._run(sub))
            elif name == "lineBreak":
                out.append("\n")
            elif name == "tab":
                out.append(" ")
        return out

    def _text_of(self, el):
        buf = []
        if el.text:
            buf.append(el.text)
        for c in el:
            cname = ln(c.tag)
            if cname == "tab":
                buf.append(" ")
            elif cname == "lineBreak":
                buf.append("\n")
            else:
                buf.append(self._text_of(c))
            if c.tail:
                buf.append(c.tail)
        return "".join(buf)

    def _decorate(self, text, style):
        if not text.strip():
            return text
        if style.get("bold"):
            lead = len(text) - len(text.lstrip())
            trail = len(text) - len(text.rstrip())
            core = text.strip()
            return text[:lead] + "**" + core + "**" + (text[len(text) - trail:] if trail else "")
        return text

    def _table(self, tbl):
        rows = []
        for tr in children(tbl, "tr"):
            row = []
            for tc in children(tr, "tc"):
                span = find_deep(tc, "cellSpan")
                col_span = int(attr(span, "colSpan", "1") or 1) if span is not None else 1
                row_span = int(attr(span, "rowSpan", "1") or 1) if span is not None else 1
                sub = find_deep(tc, "subList")
                texts = []
                if sub is not None:
                    for cp in sub.iter():
                        if ln(cp.tag) == "t":
                            texts.append(self._text_of(cp))
                cell_text = " ".join(" ".join(texts).split())
                row.append(Cell(cell_text, col_span, row_span))
            if row:
                rows.append(row)
        return Block("table", rows=rows)

    def _equation(self, eq):
        script = find_deep(eq, "script")
        raw = (self._text_of(script) if script is not None else "").strip()
        return " $" + hwp_script_to_latex(raw) + "$ " if raw else ""

    def _image(self, el):
        img = find_deep(el, "img")
        if img is None:
            return ""
        ref = attr(img, "binaryItemIDRef") or ""
        target = self.styles.bin_items.get(ref, ref)
        path = self._resolve_bin(target, ref)
        self._img_index += 1
        label = f"img{self._img_index}"
        if path:
            self.doc.images.append((label, path))
            return f"\n![{label}]({{ASSETS}}/{label}{_ext(path)})\n"
        return f"\n[그림 {self._img_index}]\n"

    def _resolve_bin(self, target, ref):
        stem = target.rsplit("/", 1)[-1].split(".")[0].lower()
        for n in self._bin_names:
            base = n.rsplit("/", 1)[-1]
            if base.lower() == target.rsplit("/", 1)[-1].lower():
                return n
            if base.split(".")[0].lower() in (stem, ref.lower()):
                return n
        if 0 <= self._img_index < len(self._bin_names):
            return self._bin_names[self._img_index]
        return ""

    def _footnote(self, fn):
        self._fn_index += 1
        texts = []
        for c in fn.iter():
            if ln(c.tag) == "t":
                texts.append(self._text_of(c))
        body = " ".join(" ".join(texts).split())
        self.doc.footnotes.append((self._fn_index, body))
        return f"[^{self._fn_index}]"

    def _finish(self):
        finalize(self.doc)


def finalize(doc):
    blocks = []
    for b in doc.blocks:
        if b.kind == "blank" and blocks and blocks[-1].kind == "blank":
            continue
        blocks.append(b)
    while blocks and blocks[0].kind == "blank":
        blocks.pop(0)
    while blocks and blocks[-1].kind == "blank":
        blocks.pop()
    doc.blocks = blocks
    doc.stats = {
        "headings": sum(1 for b in blocks if b.kind == "heading"),
        "paragraphs": sum(1 for b in blocks if b.kind == "para"),
        "tables": sum(1 for b in blocks if b.kind == "table"),
        "images": len(doc.images),
        "footnotes": len(doc.footnotes),
    }
    return doc


def _ext(path):
    tail = path.rsplit("/", 1)[-1]
    return "." + tail.rsplit(".", 1)[-1] if "." in tail else ".bin"


LATEX_MAP = [
    (r"\bover\b", "\\over"),
    (r"\bsqrt\b", "\\sqrt"),
    (r"\bsum\b", "\\sum"),
    (r"\bint\b", "\\int"),
    (r"\btimes\b", "\\times"),
    (r"\bdiv\b", "\\div"),
    (r"\bleq\b", "\\leq"),
    (r"\bgeq\b", "\\geq"),
    (r"\bneq\b", "\\neq"),
    (r"\binf\b", "\\infty"),
    (r"\balpha\b", "\\alpha"),
    (r"\bbeta\b", "\\beta"),
    (r"\bgamma\b", "\\gamma"),
    (r"\bdelta\b", "\\delta"),
    (r"\btheta\b", "\\theta"),
    (r"\blambda\b", "\\lambda"),
    (r"\bpi\b", "\\pi"),
    (r"\bsigma\b", "\\sigma"),
    (r"\bomega\b", "\\omega"),
]


def hwp_script_to_latex(script):
    out = " ".join(script.split())
    for pattern, repl in LATEX_MAP:
        out = re.sub(pattern, repl.replace("\\", "\\\\"), out)
    out = re.sub(r"\{([^{}]*)\}\s*\\over\s*\{([^{}]*)\}", r"\\frac{\1}{\2}", out)
    return out


def parse(path):
    with zipfile.ZipFile(path) as zf:
        return Parser(zf).run(), zf.namelist()


def parse_document(path, cfg=None):
    """convert.py 의 공통 흐름이 부르는 형태 — 다른 파서들과 시그니처를 맞춘다."""
    try:
        doc, _ = parse(path)
    except zipfile.BadZipFile as exc:
        raise HwpxError("파일이 손상되었거나 .hwpx 형식이 아닙니다.") from exc
    except Exception as exc:
        raise HwpxError(f"HWPX 구조를 읽지 못했습니다: {exc}") from exc
    return doc


def extract_images(path, doc, out_dir):
    if not doc.images:
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    with zipfile.ZipFile(path) as zf:
        for label, member in doc.images:
            try:
                data = zf.read(member)
            except KeyError:
                continue
            target = out_dir / f"{label}{_ext(member)}"
            target.write_bytes(data)
            written.append(target.name)
    return written
