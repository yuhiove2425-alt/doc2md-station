"""docx / pptx / xlsx 가 공유하는 OOXML 헬퍼.

세 포맷 모두 "zip 안에 XML" 구조라 표준 라이브러리만으로 읽는다.
hwpx.py와 같은 방침으로 네임스페이스를 무시하고 로컬 태그명으로만 매칭해서,
Office 버전이나 생성 도구가 달라도 잘 깨지지 않게 한다.
"""

import posixpath
from xml.etree import ElementTree as ET


def ln(tag):
    """`{...}tbl` 처럼 네임스페이스가 붙은 태그에서 로컬명만 뽑는다."""
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


def child(el, name):
    for c in el:
        if ln(c.tag) == name:
            return c
    return None


def find_deep(el, name):
    for c in el.iter():
        if ln(c.tag) == name:
            return c
    return None


def iter_deep(el, name):
    for c in el.iter():
        if ln(c.tag) == name:
            yield c


def descend(el, *names):
    """`descend(p, "pPr", "pStyle")` 처럼 자식 경로를 따라 내려간다."""
    cur = el
    for name in names:
        cur = child(cur, name)
        if cur is None:
            return None
    return cur


def read_xml(zf, name):
    """zip 안의 XML 파트를 파싱한다. 없으면 None."""
    try:
        data = zf.read(name)
    except KeyError:
        return None
    if not data.strip():
        return None
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return None


def load_rels(zf, part):
    """파트의 관계 파일을 읽어 {rId: zip 내부 경로} 로 돌려준다.

    관계 파일의 Target은 파트 기준 상대 경로라서 zip 경로로 정규화해 준다.
    외부 링크(TargetMode="External")는 경로가 아니므로 원문 그대로 둔다.
    """
    folder, name = posixpath.split(part)
    rels_path = posixpath.join(folder, "_rels", name + ".rels")
    root = read_xml(zf, rels_path)
    if root is None:
        return {}

    out = {}
    for rel in root:
        rid = attr(rel, "Id")
        target = attr(rel, "Target") or ""
        if not rid or not target:
            continue
        if (attr(rel, "TargetMode") or "").lower() == "external":
            out[rid] = target
            continue
        out[rid] = posixpath.normpath(posixpath.join(folder, target)).lstrip("/")
    return out


def rel_type(zf, part, rid):
    """관계 Type 문자열(끝부분)을 돌려준다. 예: notesSlide, image."""
    folder, name = posixpath.split(part)
    root = read_xml(zf, posixpath.join(folder, "_rels", name + ".rels"))
    if root is None:
        return ""
    for rel in root:
        if attr(rel, "Id") == rid:
            return (attr(rel, "Type") or "").rsplit("/", 1)[-1]
    return ""


def ext_of(member):
    """zip 멤버 경로에서 확장자를 뽑는다. 없으면 .bin."""
    tail = member.rsplit("/", 1)[-1]
    return "." + tail.rsplit(".", 1)[-1].lower() if "." in tail else ".bin"


def extract_images(path, doc, out_dir):
    """doc.images 에 모인 (라벨, zip 내부 경로)를 실제 파일로 꺼낸다.

    hwpx.extract_images 와 시그니처를 맞춰서 convert.py 가 구분 없이 호출한다.
    """
    import zipfile

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
            target = out_dir / f"{label}{ext_of(member)}"
            target.write_bytes(data)
            written.append(target.name)
    return written


def bold_on(rpr, tag="b"):
    """rPr 안의 굵게 지정이 실제로 켜져 있는지. val="0"/"false" 는 해제로 본다."""
    if rpr is None:
        return False
    el = child(rpr, tag)
    if el is None:
        return False
    val = (attr(el, "val") or "").strip().lower()
    return val not in ("0", "false", "none", "off")


def squash(text):
    return " ".join((text or "").split())
