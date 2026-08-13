"""지원 형식을 한 곳에서만 정의한다.

변환기·폴더 감시·업로드 검사·화면 안내가 각자 확장자 목록을 들고 있으면
형식을 하나 추가할 때 네 군데를 고쳐야 하고, 그 중 하나를 빠뜨리면 "화면에는
된다고 적혀 있는데 아무 일도 안 일어나는" 상태가 된다. 그래서 이 표가 유일한
출처이고, 화면 안내 문구도 여기서 API로 내려간다.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Format:
    ext: str  # 소문자 확장자 (".docx")
    label: str  # 화면에 보일 이름
    group: str  # 화면에서 묶을 분류
    handler: str  # convert.py 가 고를 처리기 이름
    note: str = ""  # 이 형식에서 미리 알아둘 점
    hint: str = ""  # handler 가 "unsupported" 일 때 안내할 방법


FORMATS = [
    Format(".hwpx", "한글 HWPX", "한글", "hwpx", "구조를 직접 읽어 가장 정확합니다."),
    Format(".hwp", "한글 HWP", "한글", "hwp", "pyhwp를 거칩니다. 각주는 원본에서 빠집니다."),
    Format(".docx", "워드", "오피스", "docx", "제목·표·각주·그림까지 옮깁니다."),
    Format(".pptx", "파워포인트", "오피스", "pptx", "슬라이드마다 제목을 만들고 발표자 노트도 남깁니다."),
    Format(".xlsx", "엑셀", "오피스", "xlsx", "시트마다 표 하나. 수식은 저장된 값으로 옮깁니다."),
    Format(".pdf", "PDF", "PDF", "pdf", "머리말·쪽번호를 걷어냅니다. 표는 줄글이 됩니다."),
    Format(".txt", "텍스트", "텍스트", "txt", "빈 줄을 문단 경계로 봅니다."),
    Format(".md", "마크다운", "텍스트", "md", "본문은 두고 frontmatter만 새로 붙입니다."),
    Format(".markdown", "마크다운", "텍스트", "md"),
    Format(".csv", "CSV 표", "텍스트", "csv", "첫 줄을 머리글로 봅니다. CP949도 읽습니다."),
    Format(".tsv", "TSV 표", "텍스트", "csv"),
    Format(".html", "HTML", "텍스트", "html", "제목·표·목록만 남기고 스크립트는 버립니다."),
    Format(".htm", "HTML", "텍스트", "html"),
    Format(".json", "JSON", "텍스트", "json", "코드 블록으로 감싸 보기 좋게 정렬합니다."),
    # 옛 바이너리 형식: 조용히 무시하면 왜 안 되는지 알 수 없으므로,
    # 받아들이되 방법을 알려주며 실패시킨다.
    Format(".doc", "워드 97-2003", "옛 형식", "unsupported", hint="워드에서 .docx로 저장한 뒤 넣어주세요."),
    Format(".ppt", "파워포인트 97-2003", "옛 형식", "unsupported", hint="파워포인트에서 .pptx로 저장한 뒤 넣어주세요."),
    Format(".xls", "엑셀 97-2003", "옛 형식", "unsupported", hint="엑셀에서 .xlsx로 저장한 뒤 넣어주세요."),
]

BY_EXT = {f.ext: f for f in FORMATS}
EXTENSIONS = set(BY_EXT)


def lookup(name):
    """파일명이나 경로에서 형식을 찾는다. 모르면 None."""
    text = str(name)
    dot = text.rfind(".")
    return BY_EXT.get(text[dot:].lower()) if dot >= 0 else None


def supported(name):
    return lookup(name) is not None


def availability(fmt):
    """지금 이 컴퓨터에서 실제로 변환되는지. (가능 여부, 이유)"""
    if fmt.handler == "unsupported":
        return False, fmt.hint
    if fmt.handler == "pdf":
        from . import pdf

        if not pdf.available():
            return False, "pypdf가 없습니다. pip install -r requirements.txt 를 실행하세요."
    if fmt.handler == "hwp":
        from . import hwp_binary

        if not hwp_binary.available():
            return False, "pyhwp가 없습니다. pip install -r requirements.txt 를 실행하세요."
    return True, ""


def catalog():
    """화면에 내려줄 형식 목록. 분류 순서는 FORMATS 등장 순서를 따른다."""
    seen = {}
    for fmt in FORMATS:
        ok, reason = availability(fmt)
        entry = seen.setdefault(
            (fmt.group, fmt.label),
            {
                "group": fmt.group,
                "label": fmt.label,
                "extensions": [],
                "available": ok,
                "reason": reason,
                "note": fmt.note,
            },
        )
        entry["extensions"].append(fmt.ext)
        if fmt.note and not entry["note"]:
            entry["note"] = fmt.note
    return list(seen.values())


def accept_attribute():
    """<input type=file accept="..."> 에 넣을 문자열.

    지금 실제로 변환되는 형식만 넣는다. 파일 선택 창에서 고를 수 있는데 넣자마자
    실패하는 건 고르기 전에 막는 것보다 나쁘다. (끌어다 놓는 경로에서는 그대로
    받아서 이유를 알려준다.)
    """
    return ",".join(sorted(f.ext for f in FORMATS if availability(f)[0]))
