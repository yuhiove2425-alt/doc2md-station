"""`.xlsx` → 공용 Document 구조.

시트 하나가 `##` 제목 + 표 하나가 된다. 첫 행은 머리글로 본다.

수식은 계산식이 아니라 **마지막으로 저장될 때 캐시된 값**을 쓴다. LLM에게
`=SUM(B2:B9)` 를 주는 건 의미가 없고, 사람이 화면에서 본 값이 문서의 내용이기
때문이다. 캐시 값이 없으면(한 번도 열지 않고 만든 파일) 빈 칸으로 남는다.

날짜는 엑셀 내부적으로 숫자라, 표시 서식이 날짜 계열인 칸만 날짜 문자열로 되돌린다.
"""

import datetime
import re
import zipfile

from .hwpx import Block, Cell, Document, finalize
from .ooxml import attr, child, children, find_deep, load_rels, read_xml

WORKBOOK_PART = "xl/workbook.xml"
CELL_REF_RE = re.compile(r"^([A-Z]+)(\d+)$")
# 엑셀 기본 서식 중 날짜/시간 계열 번호
BUILTIN_DATE_FORMATS = set(range(14, 23)) | set(range(45, 48)) | {27, 30, 36, 50, 57}
DATE_TOKEN_RE = re.compile(r"[dmyh]", re.IGNORECASE)
EXCEL_EPOCH = datetime.datetime(1899, 12, 30)


class XlsxError(Exception):
    pass


def _col_index(ref):
    m = CELL_REF_RE.match(ref or "")
    if not m:
        return None
    letters = m.group(1)
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


class _Parser:
    def __init__(self, zf, cfg):
        self.zf = zf
        self.cfg = cfg or {}
        self.doc = Document()
        self.shared = []
        self.date_styles = set()
        self.sheet_count = 0
        self.max_rows = int(self.cfg.get("sheet_max_rows", 2000) or 2000)

    def run(self):
        self._load_shared_strings()
        self._load_number_formats()

        sheets = self._sheets()
        if not sheets:
            raise XlsxError("시트를 찾지 못했습니다. 손상된 파일일 수 있습니다.")

        for name, part in sheets:
            self._sheet(name, part)

        if not self.doc.blocks:
            raise XlsxError("내용이 있는 시트가 없습니다.")

        finalize(self.doc)
        self.doc.stats["sheets"] = self.sheet_count
        return self.doc

    def _load_shared_strings(self):
        root = read_xml(self.zf, "xl/sharedStrings.xml")
        if root is None:
            return
        for si in children(root, "si"):
            parts = []
            for node in si.iter():
                if node.tag.rsplit("}", 1)[-1] == "t":
                    parts.append(node.text or "")
            self.shared.append("".join(parts))

    def _load_number_formats(self):
        root = read_xml(self.zf, "xl/styles.xml")
        if root is None:
            return

        custom_dates = set()
        fmts = find_deep(root, "numFmts")
        if fmts is not None:
            for f in children(fmts, "numFmt"):
                code = attr(f, "formatCode") or ""
                fid = attr(f, "numFmtId") or ""
                stripped = re.sub(r"\[[^\]]*\]|\"[^\"]*\"", "", code)
                if DATE_TOKEN_RE.search(stripped) and fid.isdigit():
                    custom_dates.add(int(fid))

        xfs = find_deep(root, "cellXfs")
        if xfs is None:
            return
        for index, xf in enumerate(children(xfs, "xf")):
            try:
                fid = int(attr(xf, "numFmtId", "0") or 0)
            except ValueError:
                continue
            if fid in BUILTIN_DATE_FORMATS or fid in custom_dates:
                self.date_styles.add(index)

    def _sheets(self):
        root = read_xml(self.zf, WORKBOOK_PART)
        if root is None:
            return []
        rels = load_rels(self.zf, WORKBOOK_PART)
        names = self.zf.namelist()

        out = []
        holder = find_deep(root, "sheets")
        for sheet in children(holder, "sheet") if holder is not None else []:
            if (attr(sheet, "state") or "").lower() in ("hidden", "veryhidden"):
                continue
            name = attr(sheet, "name") or f"시트{len(out) + 1}"
            rid = ""
            for key, value in sheet.attrib.items():
                if key.startswith("{") and key.endswith("}id") and "relationships" in key:
                    rid = value
            target = rels.get(rid, "")
            if target in names:
                out.append((name, target))
        return out

    def _sheet(self, name, part):
        root = read_xml(self.zf, part)
        if root is None:
            return
        data = find_deep(root, "sheetData")
        if data is None:
            return

        rows = []
        truncated = False
        for row in children(data, "row"):
            if len(rows) >= self.max_rows:
                truncated = True
                break
            values = self._row_values(row)
            if values is not None:
                rows.append(values)

        rows = _trim(rows)
        if not rows:
            return

        self.sheet_count += 1
        self.doc.blocks.append(Block("heading", text=name, level=2))
        width = max(len(r) for r in rows)
        table = [[Cell(text=v) for v in (r + [""] * (width - len(r)))] for r in rows]
        self.doc.blocks.append(Block("table", rows=table))
        if truncated:
            self.doc.blocks.append(
                Block("para", text=f"> 행이 많아 위 {self.max_rows}행까지만 옮겼습니다.")
            )
        self.doc.blocks.append(Block("blank"))

    def _row_values(self, row):
        values = []
        for c in children(row, "c"):
            index = _col_index(attr(c, "r") or "")
            text = self._cell_text(c)
            if index is None:
                values.append(text)
                continue
            while len(values) < index:
                values.append("")
            values.append(text)
        return values if any(v.strip() for v in values) else None

    def _cell_text(self, c):
        kind = (attr(c, "t") or "n").lower()

        if kind == "inlinestr":
            holder = child(c, "is")
            if holder is None:
                return ""
            return "".join(t.text or "" for t in holder.iter() if t.tag.rsplit("}", 1)[-1] == "t")

        v = child(c, "v")
        raw = (v.text or "").strip() if v is not None else ""
        if not raw:
            return ""

        if kind == "s":
            try:
                return self.shared[int(raw)]
            except (ValueError, IndexError):
                return ""
        if kind == "b":
            return "TRUE" if raw not in ("0", "") else "FALSE"
        if kind == "e":
            return raw  # #REF! 같은 오류 값은 그대로 남긴다

        try:
            style = int(attr(c, "s", "-1") or -1)
        except ValueError:
            style = -1
        if style in self.date_styles:
            date = _serial_to_date(raw)
            if date:
                return date

        return _clean_number(raw)


def _serial_to_date(raw):
    try:
        serial = float(raw)
    except ValueError:
        return ""
    if serial <= 0 or serial > 2958465:  # 1900-01-01 ~ 9999-12-31 범위 밖
        return ""
    moment = EXCEL_EPOCH + datetime.timedelta(days=serial)
    if abs(serial - int(serial)) < 1e-6:
        return moment.strftime("%Y-%m-%d")
    return moment.strftime("%Y-%m-%d %H:%M")


def _clean_number(raw):
    try:
        number = float(raw)
    except ValueError:
        return raw
    if number.is_integer():
        return str(int(number))
    return f"{number:.10g}"


def _trim(rows):
    """오른쪽·아래쪽의 빈 줄과 빈 열을 잘라낸다."""
    while rows and not any(v.strip() for v in rows[-1]):
        rows.pop()
    if not rows:
        return rows
    width = max(len(r) for r in rows)
    keep = [i for i in range(width) if any(i < len(r) and r[i].strip() for r in rows)]
    if not keep:
        return []
    last = keep[-1]
    return [r[: last + 1] for r in rows]


def parse(path, cfg=None):
    try:
        with zipfile.ZipFile(path) as zf:
            if WORKBOOK_PART not in zf.namelist():
                raise XlsxError(
                    "Excel 구조가 아닙니다. 97-2003 형식(.xls)이면 .xlsx로 저장한 뒤 넣어주세요."
                )
            return _Parser(zf, cfg).run()
    except zipfile.BadZipFile as exc:
        raise XlsxError("파일이 손상되었거나 .xlsx 형식이 아닙니다.") from exc
