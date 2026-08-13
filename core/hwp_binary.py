import re
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .hwpx import Block, Cell, Document, _ext, finalize

DEFAULT_SIZE = 10.0
CHARSHAPE_RULE_RE = re.compile(r"span\.charshape-(\d+)(?:\.[\w-]+)?\s*\{([^}]*)\}")
FONT_SIZE_RE = re.compile(r"font-size:\s*([\d.]+)pt")


class BridgeError(Exception):
    pass


def available():
    try:
        import hwp5.hwp5html  # noqa: F401
    except ImportError:
        return False
    return True


def convert(src, out_dir, cfg):
    src = Path(src)
    with tempfile.TemporaryDirectory() as tmp:
        html_dir = Path(tmp) / "html"
        _run_hwp5html(src, html_dir, cfg.get("bridge_timeout", 180))

        index = html_dir / "index.xhtml"
        if not index.exists():
            raise BridgeError("hwp5html이 결과 파일을 만들지 않았습니다.")

        css_text = ""
        css_path = html_dir / "styles.css"
        if css_path.exists():
            css_text = css_path.read_text(encoding="utf-8")
        bold_ids, size_map = _parse_css(css_text)

        try:
            doc = _Parser(bold_ids, size_map).run(index)
        except ET.ParseError as exc:
            raise BridgeError(f"hwp5html 출력(HTML)을 읽지 못했습니다: {exc}") from exc

        if not doc.blocks:
            raise BridgeError("추출된 본문이 없습니다. 지원하지 않는 구조일 수 있습니다.")

        target = _unique(out_dir / f"{src.stem}.md")

        assets_rel = ""
        if cfg.get("extract_images", True) and doc.images:
            assets_rel = f"assets/{target.stem}"
            _extract_images(html_dir, doc, out_dir / assets_rel)

        from . import markdown

        text = markdown.render(doc, src.name, assets_rel)
        target.write_text(text, encoding="utf-8")
        return target, doc.stats


def _run_hwp5html(src, out_dir, timeout):
    try:
        proc = subprocess.run(
            [sys.executable, "-c", "from hwp5.hwp5html import main; main()",
             "--output", str(out_dir), str(src)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise BridgeError("변환기가 응답하지 않아 중단했습니다.") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"종료 코드 {proc.returncode}"
        raise BridgeError(f"hwp5html 변환 실패: {tail}")


def _parse_css(css_text):
    bold_ids = set()
    size_map = {}
    for m in CHARSHAPE_RULE_RE.finditer(css_text):
        cid, body = m.group(1), m.group(2)
        if "font-weight" in body and "bold" in body:
            bold_ids.add(cid)
        sm = FONT_SIZE_RE.search(body)
        if sm:
            size = float(sm.group(1))
            size_map[cid] = max(size_map.get(cid, 0.0), size)
    return bold_ids, size_map


def ln(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


class _Parser:
    def __init__(self, bold_ids, size_map):
        self.bold_ids = bold_ids
        self.size_map = size_map
        self.doc = Document()
        self._img_index = 0
        self._headings = []  # (block_index, text, size)

    def run(self, index_path):
        root = ET.parse(str(index_path)).getroot()
        body = next((el for el in root.iter() if ln(el.tag) == "body"), None)
        if body is None:
            raise BridgeError("HTML 본문을 찾지 못했습니다.")
        self._walk(body)
        self._classify_headings()
        finalize(self.doc)
        return self.doc

    def _walk(self, el):
        for child in el:
            name = ln(child.tag)
            if name == "table":
                self.doc.blocks.append(self._table(child))
            elif name == "p":
                self._paragraph(child)
            elif self._is_leaf_content(child):
                # Floating shapes/images sometimes sit as bare <div> siblings
                # of <p>, outside any paragraph. Treat the whole subtree as
                # one paragraph-like block so images aren't silently dropped.
                self._paragraph(child)
            else:
                self._walk(child)

    def _is_leaf_content(self, el):
        for d in el.iter():
            if d is el:
                continue
            if ln(d.tag) in ("p", "table"):
                return False
        has_img = any(ln(d.tag) == "img" for d in el.iter())
        has_text = any((d.text or "").strip() or (d.tail or "").strip() for d in el.iter())
        return has_img or has_text

    def _paragraph(self, p):
        parts = []
        sizes = []
        self._collect(p, parts, sizes)
        text = "".join(parts).replace("\r", "")
        text = re.sub(r"[ \t]{2,}", " ", text).strip()

        if not text:
            self.doc.blocks.append(Block("blank"))
            return

        index = len(self.doc.blocks)
        self.doc.blocks.append(Block("para", text=text))
        self._headings.append((index, text, max(sizes) if sizes else DEFAULT_SIZE))

    def _collect(self, el, parts, sizes):
        name = ln(el.tag)
        if name == "img":
            ref = el.get("src", "")
            if ref:
                self._img_index += 1
                label = f"img{self._img_index}"
                self.doc.images.append((label, ref))
                parts.append(f"\n![{label}]({{ASSETS}}/{label}{_ext(ref)})\n")
            return

        bold = False
        size = None
        if name == "span":
            classes = (el.get("class") or "").split()
            cid = next((c.split("-", 1)[1] for c in classes if c.startswith("charshape-")), None)
            if cid:
                bold = cid in self.bold_ids
                size = self.size_map.get(cid)

        own_text = el.text or ""
        if own_text.strip():
            sizes.append(size if size is not None else DEFAULT_SIZE)
            parts.append(_decorate(own_text, bold))
        elif own_text:
            parts.append(own_text)

        for child in el:
            self._collect(child, parts, sizes)
            if child.tail:
                parts.append(child.tail)

    def _table(self, tbl):
        rows = []
        for tr in tbl:
            if ln(tr.tag) != "tr":
                continue
            row = []
            for td in tr:
                if ln(td.tag) not in ("td", "th"):
                    continue
                col_span = int(td.get("colspan", "1") or 1)
                row_span = int(td.get("rowspan", "1") or 1)
                text = " ".join("".join(td.itertext()).split())
                row.append(Cell(text, col_span, row_span))
            if row:
                rows.append(row)
        return Block("table", rows=rows)

    def _classify_headings(self):
        if not self._headings:
            return
        baseline = statistics.mode(size for _, _, size in self._headings)
        if not baseline:
            return
        for index, text, size in self._headings:
            if len(text) > 100:
                continue
            ratio = size / baseline
            if ratio >= 1.6:
                level = 1
            elif ratio >= 1.3:
                level = 2
            elif ratio >= 1.15:
                level = 3
            else:
                continue
            block = self.doc.blocks[index]
            block.kind = "heading"
            block.level = level
            block.text = text.replace("**", "").strip()


def _decorate(text, bold):
    if not bold or not text.strip():
        return text
    lead = len(text) - len(text.lstrip())
    trail = len(text) - len(text.rstrip())
    core = text.strip()
    return text[:lead] + "**" + core + "**" + (text[len(text) - trail:] if trail else "")


def _extract_images(html_dir, doc, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    for label, rel_src in doc.images:
        src_path = html_dir / rel_src
        if not src_path.exists():
            continue
        target = out_dir / f"{label}{_ext(rel_src)}"
        target.write_bytes(src_path.read_bytes())


def _unique(path):
    if not path.exists():
        return path
    for i in range(1, 1000):
        candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise BridgeError("같은 이름의 결과 파일이 너무 많습니다.")
