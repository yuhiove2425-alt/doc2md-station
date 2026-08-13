"""`.pdf` → 공용 Document 구조.

PDF는 "글자가 어디에 찍혀 있는가"만 있고 문단·제목 같은 구조가 없다. 그래서
여기서 하는 일은 복원이 아니라 **추정**이고, 다음 세 가지만 규칙으로 정리한다.

1. 쪽마다 반복되는 머리말·꼬리말·쪽번호를 걷어낸다.
   (여러 쪽의 같은 자리에 같은 문구가 반복되면 본문이 아니라고 본다.
    이게 청크에 섞이면 검색 결과가 오염되는 게 이 프로그램의 원래 문제의식이다.)
2. 줄바꿈으로 잘린 한 문단을 다시 한 줄로 잇는다.
3. `제3조`, `2.1` 같은 번호 형식의 짧은 줄만 제목으로 올린다.

표는 복원하지 않는다. PDF의 표는 좌표만 남아 있어서 잘못 맞추면 숫자가 엉뚱한
행에 붙는데, 그건 틀린 걸 모른 채 쓰게 되므로 줄글로 두는 편이 낫다.
쪽 경계는 `<!-- page N -->` 주석으로 남겨서, 나중에 출처를 쪽 단위로 인용할 수 있다.
"""

import re

from .hwpx import Block, Document, finalize

HEADING_RULES = [
    (re.compile(r"^제\s*\d+\s*편(\s|\.|$)"), 1),
    (re.compile(r"^제\s*\d+\s*장(\s|\.|$)"), 2),
    (re.compile(r"^제\s*\d+\s*[절관](\s|\.|$)"), 3),
    (re.compile(r"^제\s*\d+\s*조(의\s*\d+)?(\s|\(|\.|$)"), 4),
    (re.compile(r"^\d+\.\d+\.\d+\.?\s+\S"), 4),
    (re.compile(r"^\d+\.\d+\.?\s+\S"), 3),
    (re.compile(r"^\d+\.\s+\S"), 2),
    (re.compile(r"^[IVX]+\.\s+\S"), 2),
]
PAGE_NUMBER_RE = re.compile(r"^[-–—\s]*(?:\d{1,4}|[ivxlcIVXLC]{1,7})[-–—\s]*$")
BULLET_RE = re.compile(r"^\s*(?:[-•○●▪·*]|\(\d+\)|\d+\)|[가-힣]\.)\s+")
SENTENCE_END = ("다.", "다,", ".", "요.", "함.", "음.", ":", ";", "!", "?", "”", '"', ")")


class PdfError(Exception):
    pass


def available():
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return False
    return True


def parse(path, cfg=None):
    cfg = cfg or {}
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise PdfError(
            "PDF를 읽으려면 pypdf가 필요합니다. pip install -r requirements.txt 를 실행해 주세요."
        ) from exc

    try:
        reader = PdfReader(str(path))
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")  # 빈 암호로 잠긴 배포용 PDF가 흔하다
            except Exception as exc:
                raise PdfError("암호가 걸린 PDF입니다. 암호를 푼 뒤 다시 넣어주세요.") from exc
        pages = [(page.extract_text() or "") for page in reader.pages]
    except PdfError:
        raise
    except Exception as exc:
        raise PdfError(f"PDF를 읽지 못했습니다: {exc}") from exc

    if not pages:
        raise PdfError("쪽이 없는 PDF입니다.")

    page_lines = [_clean_lines(text) for text in pages]
    boilerplate = _repeated_edges(page_lines)

    doc = Document()
    total_chars = 0
    mark_pages = cfg.get("pdf_page_marks", True)
    find_headings = cfg.get("pdf_headings", True)

    for number, lines in enumerate(page_lines, start=1):
        body = [l for l in lines if l not in boilerplate and not PAGE_NUMBER_RE.match(l)]
        total_chars += sum(len(l) for l in body)
        if not body:
            continue
        if mark_pages:
            doc.blocks.append(Block("para", text=f"<!-- page {number} -->"))
        for block in _blocks(body, find_headings):
            doc.blocks.append(block)
        doc.blocks.append(Block("blank"))

    if total_chars < 20:
        raise PdfError(
            "글자를 거의 찾지 못했습니다. 스캔(이미지)으로 만든 PDF로 보입니다 — "
            "OCR로 글자를 넣은 뒤 다시 시도해 주세요."
        )

    finalize(doc)
    doc.stats["pages"] = len(pages)
    return doc


def _clean_lines(text):
    lines = []
    for raw in text.splitlines():
        line = " ".join(raw.replace("\xa0", " ").split())
        if line:
            lines.append(line)
    return lines


def _repeated_edges(page_lines):
    """여러 쪽의 위·아래 가장자리에서 똑같이 반복되는 줄 = 머리말·꼬리말."""
    pages = [p for p in page_lines if p]
    if len(pages) < 3:
        return set()

    counts = {}
    for lines in pages:
        edge = set(lines[:2]) | set(lines[-2:])
        for line in edge:
            if len(line) > 90:
                continue
            # 쪽 첫 줄이 제목인 문서(장마다 같은 번호 체계)에서 제목을 지워버리지
            # 않도록, 제목·글머리 기호 모양의 줄은 머리말 후보에서 제외한다.
            if _heading_level(line) or BULLET_RE.match(line):
                continue
            counts[line] = counts.get(line, 0) + 1

    threshold = max(3, int(len(pages) * 0.6))
    return {line for line, n in counts.items() if n >= threshold}


def _blocks(lines, find_headings):
    out = []
    buffer = []

    def flush():
        if buffer:
            out.append(Block("para", text=" ".join(buffer)))
            buffer.clear()

    for line in lines:
        level = _heading_level(line) if find_headings else None
        if level:
            flush()
            out.append(Block("heading", text=line, level=level))
            continue

        if BULLET_RE.match(line):
            flush()
            out.append(Block("para", text=BULLET_RE.sub("- ", line, count=1)))
            continue

        if buffer and _ends_sentence(buffer[-1]):
            flush()
        buffer.append(line)

    flush()
    return out


def _heading_level(line):
    if len(line) > 60:
        return None
    for pattern, level in HEADING_RULES:
        if pattern.match(line):
            return level
    return None


def _ends_sentence(line):
    return line.endswith(SENTENCE_END)
