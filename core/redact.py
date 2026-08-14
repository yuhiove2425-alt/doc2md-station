"""마크다운에서 자연인 정보를 찾아 치환한다.

이 모듈이 지키는 것 네 가지.

1. 지우지 않고 치환한다. `홍길동` → `〔자연인-01〕`.
   무엇이 빠졌는지 나중에도 알 수 있어야 한다.
2. 법인·기관은 남긴다. `대한상호저축은행`, `금융위원회`는 그대로 둔다.
3. 애매하면 확인필요(REVIEW)로 남긴다. 조용히 지우지 않는다.
4. 전부 로컬 정규식이다. 외부 API·LLM을 호출하지 않는다.

한국어 인명은 규칙만으로 정확히 잡히지 않는다. `안건명`처럼 성씨로 시작하는
평범한 낱말이 이름처럼 보이기 때문이다. 그래서 직위·호칭이 붙은 경우만
자동으로 처리하고, 나머지는 사람이 확인하게 남긴다.
"""

import hashlib
import json
import re
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path

ENGINE_VERSION = "redact/0.2"

AUTO = "AUTO"        # 오탐 가능성이 낮아 자동 치환
REVIEW = "REVIEW"    # 사람이 확인한 뒤에만 치환


# ---------------------------------------------------------------- 사전
# 법인·기관 접미사. 이 안에 들어가는 이름은 자연인이 아니다.
ORG_SUFFIX = (
    "은행|증권|보험|캐피탈|카드|저축은행|금고|신협|조합|중앙회|협회|공사|공단|재단|"
    "법인|주식회사|㈜|지주|홀딩스|자산운용|투자|파트너스|컨설팅|"
    "위원회|검찰청|경찰청|법원|시청|구청|도청|군청|부|처|청|원|국|과|팀"
)
ORG_RE = re.compile(r"[가-힣A-Za-z0-9]+(?:%s)" % ORG_SUFFIX)

# 치환하면 안 되는 구간 — 코드블록, 법령명, 링크 주소
SKIP_SPAN_RES = [
    re.compile(r"```.*?```", re.S),
    re.compile(r"`[^`\n]+`"),
    re.compile(r"「[^」\n]{1,60}」"),
    re.compile(r"\]\([^)\s]+\)"),
]

# 단성(單姓)
SURNAMES = set(
    "김이박최정강조윤장임한오서신권황안송류전홍고문양손배백허유남심노하곽성차주우구"
    "민진지엄채원천방공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국"
    "어은편용예봉夫皮"
)
# 복성(複姓)
COMPOUND_SURNAMES = ("남궁", "황보", "제갈", "선우", "사공", "서문", "독고", "동방", "망절")

# 정규식 문자 클래스로 쓸 성씨 목록. 부분 마스킹은 성을 남기고 가리는 표기라
# 첫 글자가 성씨인지로 이름 여부를 크게 좁힐 수 있다.
SURNAME_CHARS = "".join(sorted(SURNAMES))

# 이름에 흔히 쓰이는 음절. 이름 길이를 되돌릴 때 쓴다.
GIVEN_SYLLABLES = set(
    "가강건경계고광교구국권규근금기길나난남내노다단담대덕도돈동두라란래량려련렬령례"
    "로록루류리린마만매명모목무문미민바박반발방배백범법별병보복본봉부빈사산삼상새생"
    "서석선설섭성세소솔송수숙순술숭슬승시식신실심아안애야양어억언엄여연열영예오옥온"
    "완요용우욱운웅원월위유육윤율은음의이익인일임자작잔장재전정제조존종주준중지진찬"
    "창채천철청초총최추춘충치칠태택하학한해행향헌혁현형혜호홍화환효후훈휘흠희"
    "늘봄빛결담든람샘아름"
)

# 이름 앞뒤 항목명 — 표나 목록에서 가장 확실한 단서
NAME_LABELS = (
    "성명|이름|명의|대상자|조치대상자|조치대상|피조치자|위반자|행위자|"
    "당사자|피의자|피고인|원고|피고|청구인|피청구인|신청인|피신청인|참고인|증인|진술인|"
    "대표자|담당자|작성자|신고인|피신고인|수신인|발신인|보호자|대리인|성함"
)


def _spaced(word):
    """글자 사이에 공백이 끼어도 잡히게 만든다.

    공문서는 `신 청 인`, `성  명`처럼 항목명을 벌려 쓰는 일이 잦다. 변형을 하나씩
    적어두면 빠뜨린 것만 조용히 놓치므로, 항목명 하나로 두고 여기서 넓힌다.
    """
    return r"[\s\u00a0]*".join(re.escape(c) for c in word)


# 긴 항목명을 앞에 둬야 `피신청인`이 `신청인`으로 잘려 잡히지 않는다.
NAME_LABEL_ALT = "|".join(
    _spaced(w) for w in sorted(NAME_LABELS.split("|"), key=len, reverse=True)
)

TITLE_BEFORE = (
    "대표이사|사내이사|사외이사|이사장|이사|감사위원|감사|본부장|지점장|부서장|실장|"
    "부장|차장|과장|팀장|대리|주임|사원|임원|대표자|대표|사장|부사장|전무|상무|"
    "원고|피고|청구인|피청구인|신청인|피신청인|참고인|진술인|증인|대리인|변호사|"
    "위반자|행위자|담당자|신고인|피신고인|고소인|피고소인|매도인|매수인|채권자|채무자"
)
TITLE_AFTER = (
    "대표이사|이사장|이사|감사|본부장|지점장|실장|부장|차장|과장|팀장|대리|주임|"
    "사장|부사장|전무|상무|회장|부회장|위원장|교수|박사|변호사|씨|님|귀하"
)
# `군`·`양`은 호칭에서 뺐다. `양 당사자`, `과다매매 지양`, `군 단위`처럼 평범한
# 낱말과 구분되지 않아, 붙여 쓴 경우로 좁혀도 `지양`이 그대로 걸린다. 이 표기를
# 쓰는 문서라면 사전/이름.txt 에 넣는 편이 확실하다.
TITLE_WORDS = set(TITLE_BEFORE.split("|")) | set(TITLE_AFTER.split("|"))

# 이름 뒤에 올 수 있는 조사·어미의 '전체 형태'.
# 글자를 하나씩 깎지 않고, 이름 뒤에 남은 꼬리가 이 목록에 있는지로 판정한다.
# 깎는 방식은 '심재서'의 서, '이상도'의 도처럼 이름 글자를 조사로 오해한다.
JOSA_TAILS = {
    "", "은", "는", "이", "가", "을", "를", "의", "와", "과", "도", "만", "께", "에",
    "랑", "이나", "이라", "이라고", "이라는", "이며", "이고", "으로", "로",
    "에게", "에게서", "께서", "에서", "으로부터", "부터", "까지", "처럼", "같이",
    "이든지", "에도", "에는", "과의", "와의", "에게도", "에게는", "이라며",
    "님", "씨", "군", "양", "님은", "님이", "씨는", "씨가", "씨의", "씨와", "씨도",
}
# 동사 어미('~하였다', '~하고')는 넣지 않는다. 넣으면 '서명하였다'의 '서명하'가
# 이름으로 잡힌다. 조사는 명사 뒤에만 붙는다는 성질을 그대로 쓴다.

# 이름 마지막 글자가 이 글자이면서 뒤에 조사가 하나도 없으면 판단을 보류한다.
# '김하은'처럼 실제 이름일 수도, '진술은'처럼 명사+조사일 수도 있기 때문이다.
AMBIGUOUS_TAIL = set("은는이가을를의와과도만에")
# 용언 활용형과 겹치는 끝글자('진술한', '참석해')도 같은 이유로 보류한다.
AMBIGUOUS_TAIL |= set("한하해함되된될적게며고여")

# 이름으로 자주 오인되는 낱말 — 앵커 없는 추정에서만 적용한다
NAME_STOPWORDS = {
    "김포시", "이천시", "고양시", "여주시", "광주시", "남양주", "의정부", "강남구",
    "대한민국", "금융위원", "감독원장", "위원장님", "관계자는", "안건명", "조치의",
    "조치내용", "조치이유", "결정문", "처분청", "재결청", "심판원", "관계인",
    "이해관", "조사관", "심사역", "신청서", "위반행", "은행법", "보험업", "자본시",
    "개인정", "전자금", "신용정", "여신전", "금융소", "지배구", "특정금", "명의로",
    "이해관계", "당해연도", "고객확인", "내부통제", "이사회", "주주총", "정기주",
    "준법감", "감사회", "임원회", "운영위", "심의위",
}

# 이름 형태를 갖췄지만 이름이 아닌 낱말. 끝글자로 막으면 '이상호'·'심재서'처럼
# 흔한 이름이 통째로 빠지므로, 낱말 단위로만 제외한다.
NON_NAME_WORDS = {
    # 문서·행정
    "신청서", "신고서", "제출본", "결정문", "의견서", "확인서", "동의서", "위임장",
    "명세서", "계약서", "합의서", "진술서", "소명서", "보고서", "첨부물", "별첨본",
    "이용자", "이해도", "이자율", "이행기", "이사회", "임원진", "임대인", "임차인",
    "관계인", "당사자", "명의자", "예금주", "양수인", "양도인", "수취인", "수령인",
    # 금융·감독
    "은행법", "은행권", "금융권", "금융업", "보험업", "증권사", "여신액", "수신액",
    "차입금", "대출금", "예치금", "송금액", "손실액", "이익금", "수수료", "과태료",
    "제재안", "조치안", "조치문", "심의회", "심판원", "심사역", "조사관", "감독원",
    "주주총", "정기주", "정관상", "장부상", "전산상", "서면상", "사실상", "형식상",
    # 시간·수량
    "당해년", "전년도", "당년도", "반기말", "분기말", "연간액", "월평균", "일평균",
    "백만원", "천만원", "억원대", "최고가", "최저가", "최종안", "추정치", "예상액",
    # 기타 자주 나오는 낱말
    "고의성", "정당성", "성실성", "위험도", "중요도", "우선순", "유의점", "문제점",
    "가능성", "필요성", "타당성", "적정성", "투명성", "신뢰도", "만족도", "참고로",
    "대한민", "우리나", "관련하", "포함하", "제외하", "해당하", "명의로", "실질적",
    # 당사자 항목명 바로 뒤에 오는 낱말. "신청인 주장", "피신청인 답변"처럼
    # 소제목으로 쓰이는 형태가 많아, 이름 자리로 오해하면 제목이 통째로 사라진다.
    "주장", "의견", "진술", "답변", "소명", "요지", "입장", "반박", "청구", "신청",
    "제출", "확인", "자격", "지위", "책임", "과실", "손해", "배상", "계약", "약관",
    "해지", "해제", "취소", "철회", "동의", "승낙", "통지", "고지", "설명", "판단",
    "결정", "조정", "심의", "의결", "검토", "조사", "처분", "제재", "위반", "준수",
    # 서식·표에서 항목명 뒤에 오는 낱말. "신청인 성명", "대리인 기준", "감사 진행"
    # 처럼 앵커 바로 뒤가 이름이 아닌 경우가 공문서에 흔하다.
    "성명", "서명", "날인", "기준", "진행", "서류", "명단", "목록", "정보", "자료",
    "내용", "사항", "여부", "구분", "항목", "금액", "일자", "기간", "번호", "종류",
}

# 항목명·직위로 쓰이는 낱말은 이름 값이 될 수 없다. "신청인 : 신청인" 같은 표를
# 이름으로 읽지 않도록 이름 판정에서 통째로 제외한다.
NAME_LABEL_WORDS = set(NAME_LABELS.split("|"))

# ---------------------------------------------------------------- 사용자 사전
# 규칙이 놓치는 이름과, 반대로 이름으로 잘못 잡히는 낱말을 팀이 직접 채운다.
# 파일 한 줄에 하나씩. `#`으로 시작하는 줄은 설명으로 본다.
USER_NAMES = set()      # 사전/이름.txt  — 무조건 치환
USER_STOPWORDS = set()  # 사전/제외.txt  — 절대 치환하지 않음
_DICT_STAMP = {}


def load_user_dicts(dict_dir):
    """사용자 사전을 읽는다. 파일이 없으면 조용히 넘어간다."""
    dict_dir = Path(dict_dir)
    for fname, target in (("이름.txt", USER_NAMES), ("제외.txt", USER_STOPWORDS)):
        path = dict_dir / fname
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        if _DICT_STAMP.get(str(path)) == stamp:
            continue
        words = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                words.add(line)
        target.clear()
        target.update(words)
        _DICT_STAMP[str(path)] = stamp


# ---------------------------------------------------------------- 규칙
# (종류, 이름, 정규식, 등급, 캡처그룹)
RULES = [
    # 식별번호 — 오탐이 거의 없다
    ("RRN", "주민등록번호", re.compile(r"\d{6}\s*[-–—]\s*[1-4]\d{6}"), AUTO, 0),
    ("FOREIGNER", "외국인등록번호", re.compile(r"\d{6}\s*[-–]\s*[5-8]\d{6}"), AUTO, 0),
    ("PASSPORT", "여권번호", re.compile(r"\b[MSRO]\d{8}\b"), AUTO, 0),
    ("DRIVER", "운전면허번호", re.compile(r"\b\d{2}-\d{2}-\d{6}-\d{2}\b"), AUTO, 0),

    # 연락처 — 계좌번호보다 먼저 봐야 휴대폰이 계좌로 잡히지 않는다
    ("MOBILE", "휴대전화", re.compile(r"\b01[016789][-\s.]?\d{3,4}[-\s.]?\d{4}\b"), AUTO, 0),
    ("PHONE", "전화번호", re.compile(r"\b0\d{1,2}[-\s.]\d{3,4}[-\s.]\d{4}\b"), AUTO, 0),
    ("EMAIL", "이메일", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"), AUTO, 0),

    ("CARD", "카드번호", re.compile(r"\b\d{4}-\d{4}-\d{4}-\d{4}\b"), AUTO, 0),
    ("ACCOUNT", "계좌번호",
     re.compile(r"\b(?!01[016789]-)\d{3,6}-\d{2,6}-\d{4,7}\b"), AUTO, 0),

    # ── 이름 1단계: 앵커 ────────────────────────────────────────────
    # 항목명 뒤 — "성명: 홍길동", "| 대상자 | 홍길동 |"
    ("NAME_LABELED", "성명(항목명)",
     re.compile(r"(?:%s)\s*[:：|\t]\s*([가-힣]{2,6})" % NAME_LABEL_ALT), AUTO, 1),
    # 직위 앞 — "대표이사 홍길동"
    ("NAME_TITLED", "성명(직위 동반)",
     re.compile(r"(?:%s)\s+([가-힣]{2,6})" % TITLE_BEFORE), AUTO, 1),
    # 직위 뒤 — "홍길동 부장", "홍길동님"
    ("NAME_TITLED", "성명(직위 동반)",
     re.compile(r"(?<![가-힣])([가-힣]{2,4})\s*(?:%s)(?![가-힣])" % TITLE_AFTER), AUTO, 1),
    # 성 + 호칭 — "홍씨", "김모씨". 뒤에 조사가 붙어도 놓치지 않는다.
    ("NAME_SURNAME", "성(호칭 동반)",
     re.compile(r"(?<![가-힣])[%s]\s?모?\s?씨"
                r"(?=$|[^가-힣]|[은는이가와과의도를에])" % SURNAME_CHARS), AUTO, 0),

    # 부분 마스킹 — 기관이 공표할 때 쓰는 형태
    # 첫 글자는 성씨여야 한다. 부분 마스킹은 성을 남기고 이름만 가리는 표기라
    # (`홍○○`, `김○`), 성씨가 아닌 글자로 시작하면 이름이 아니다. 이 조건이 없으면
    # 표에서 칸 표시로 쓰인 `행위○20%`의 `행위○`가 이름으로 잡힌다.
    #
    # 별표와 중점은 마스킹 문자에서 뺀다. 입력이 마크다운이라 `**주 문**`의 끝 별표가
    # 마스킹으로 읽혀 `문**`을 이름으로 잡고, 표 안에서는 `비율*위반`처럼 강조 기호가
    # 글자 사이에 끼어 `율*위`까지 잡힌다. 중점도 `심의·의결`처럼 낱말을 잇는 자리에
    # 훨씬 자주 쓰인다. 셋 다 문서마다 수십 번 나오는 표기라, 오탐 하나가 본문을
    # 통째로 망가뜨린다. 별표로 가린 이름(`홍*동`)은 사용자 사전으로 받는다.
    ("NAME_MASKED", "성명(부분마스킹)",
     re.compile(r"(?<![가-힣])[%s][○OoΟ×●]{1,3}(?=$|[^가-힣]|[은는이가와과의도를을에])"
                % SURNAME_CHARS), AUTO, 0),
    ("NAME_MASKED", "성명(부분마스킹)",
     re.compile(r"(?<![가-힣])[%s][가-힣][○OoΟ×●](?=$|[^가-힣]|[은는이가와과의도를을에])"
                % SURNAME_CHARS), AUTO, 0),
    ("NAME_INITIAL", "성명(이니셜)",
     re.compile(r"\b[A-Z](?:씨|\s?대표|\s?이사|\s?부장|\s?과장)"), AUTO, 0),

    ("BIRTH", "생년월일",
     re.compile(r"\b(?:19|20)\d{2}\s*[.\-/년]\s*\d{1,2}\s*[.\-/월]\s*\d{1,2}\s*일?생"), AUTO, 0),

    # ── 이름 3단계: 앵커 없는 추정 ──────────────────────────────────
    # 한글이 이어진 덩어리를 통째로 잡는다. 조사를 뗀 나머지가 정확히
    # 이름 길이여야 인정한다. 긴 낱말의 앞부분을 이름으로 오려내지 않기 위해서다.
    ("NAME_BARE", "성명(추정)",
     re.compile(r"(?<![가-힣])([가-힣]{2,12})(?![가-힣])"), AUTO, 1),

    ("ADDRESS", "상세주소",
     re.compile(r"[가-힣]+(?:시|도)\s*[가-힣]+(?:시|군|구)\s*[가-힣0-9]+(?:동|읍|면|로|길)"
                r"\s*[0-9-]+(?:번지|호)?"), REVIEW, 0),
]

PLACEHOLDER_PREFIX = {
    "RRN": "주민번호", "FOREIGNER": "외국인번호", "PASSPORT": "여권번호",
    "DRIVER": "면허번호", "ACCOUNT": "계좌번호", "CARD": "카드번호",
    "MOBILE": "연락처", "PHONE": "연락처", "EMAIL": "이메일",
    "NAME_MASKED": "자연인", "NAME_INITIAL": "자연인", "NAME_LABELED": "자연인",
    "NAME_TITLED": "자연인", "NAME_SURNAME": "자연인", "NAME_BARE": "자연인",
    "NAME_ECHO": "자연인", "NAME_USER": "자연인",
    "BIRTH": "생년월일", "ADDRESS": "주소",
}

NAME_KINDS = {"NAME_MASKED", "NAME_INITIAL", "NAME_LABELED", "NAME_TITLED",
              "NAME_SURNAME", "NAME_BARE", "NAME_ECHO", "NAME_USER"}
# 앵커가 있어 이름이라고 믿을 수 있는 종류 — 문서 전체 전파의 출발점
ANCHOR_KINDS = {"NAME_LABELED", "NAME_TITLED", "NAME_MASKED", "NAME_USER"}


# ---------------------------------------------------------------- 이름 판정
ADDRESS_LEAD_RE = re.compile(r"[가-힣]{2,}(?:특별시|광역시|시|군|구|읍|면|동|리|로|길)[ \t]*$")


def _in_address_context(text, start):
    """앞말이 행정구역이면 지명이지 사람 이름이 아니다. '김포시 장기동'.

    줄이 바뀌면 앞말로 보지 않는다. 주소는 한 줄에 이어 적고, 줄바꿈까지 허용하면
    `홍길동` 다음 줄에 오는 이름이 `동`으로 끝났다는 이유만으로 통째로 빠진다.
    """
    return bool(ADDRESS_LEAD_RE.search(text[max(0, start - 14):start]))


def _valid_name_chars(name):
    """성씨 + 이름 음절 구성인가."""
    if name.startswith(COMPOUND_SURNAMES):
        return len(name) == 4 and all(c in GIVEN_SYLLABLES for c in name[2:])
    if len(name) not in (2, 3):
        return False
    if name[0] not in SURNAMES:
        return False
    return all(c in GIVEN_SYLLABLES for c in name[1:])


def _split_name(run, allow_two=False):
    """한글 덩어리에서 이름과 꼬리를 가른다.

    글자를 깎아내지 않고, 앞쪽을 이름으로 뒀을 때 **남은 꼬리가 조사인지**를 본다.
    깎는 방식은 `심재서`의 `서`나 `이상도`의 `도`를 조사로 오해해 이름을 잘라낸다.
    """
    if not run:
        return None
    lengths = (4, 3, 2) if allow_two else (4, 3)
    for n in lengths:
        if len(run) < n:
            continue
        cand, tail = run[:n], run[n:]
        if not _valid_name_chars(cand):
            continue
        if tail in JOSA_TAILS:
            return cand
    return None


def _trim_name(raw):
    """앵커가 있는 자리에서 쓴다. 두 글자 이름까지 인정한다."""
    name = _split_name(raw, allow_two=True)
    if name:
        return name
    # 사전에 없는 음절이 섞인 이름 — 앵커가 확실하므로 꼬리만 떼고 인정한다
    for n in (4, 3, 2):
        if len(raw) >= n and raw[n:] in JOSA_TAILS:
            head = raw[:n]
            if head[0] in SURNAMES or head.startswith(COMPOUND_SURNAMES):
                return head
    return None


def _bare_name(run):
    """앵커가 없는 자리에서 쓴다. 세 글자(복성 네 글자)만 인정한다.

    돌려주는 값은 (이름, 확실한가). 확실하지 않으면 확인필요로 남긴다.
    """
    name = _split_name(run, allow_two=False)
    if not name:
        return None, False
    if name in NON_NAME_WORDS or name in USER_STOPWORDS:
        return None, False
    # 뒤에 조사가 붙지 않은 채 끝났고 마지막 글자가 조사와 겹치면 보류한다.
    # '김하은'일 수도 있고 '진술은'일 수도 있어 규칙만으로는 가릴 수 없다.
    sure = not (run == name and name[-1] in AMBIGUOUS_TAIL)
    return name, sure


def _looks_like_name(name, strict):
    """이름다운가. strict=True 면 앵커 없는 추정이라 기준을 높인다."""
    if not name or not (2 <= len(name) <= 4):
        return False
    if name in TITLE_WORDS or name in NAME_LABEL_WORDS or name in USER_STOPWORDS:
        return False
    # 앵커가 있어도 일반명사는 거른다. "신청인 주장"의 주장처럼, 항목명 뒤에
    # 이름이 아니라 낱말이 오는 소제목이 공문서에 흔하다.
    if name in NON_NAME_WORDS:
        return False
    if not strict:
        return True
    if name in NAME_STOPWORDS:
        return False
    return _valid_name_chars(name) and (len(name) >= 3)


# ---------------------------------------------------------------- 자료구조
@dataclass
class Finding:
    kind: str
    label: str
    original: str      # 원문 값 — 공유용 리포트에서는 빠진다
    placeholder: str
    confidence: str
    line: int
    start: int
    end: int
    applied: bool = False
    context: str = ""  # 앞뒤 문맥 — 로컬 검토 화면에서만 쓴다

    def public(self, include_context=False):
        """공유 가능한 형태. 원문은 물론 문맥도 기본으로 뺀다.

        문맥에는 앞뒤 문장이 그대로 들어가 다른 개인정보가 묻어날 수 있다.
        """
        d = asdict(self)
        d.pop("original", None)
        if not include_context:
            d.pop("context", None)
        return d


@dataclass
class RedactResult:
    source: str
    masked_text: str
    findings: list = field(default_factory=list)
    engine_version: str = ENGINE_VERSION
    created_at: str = ""

    @property
    def auto_count(self):
        return sum(1 for f in self.findings if f.confidence == AUTO)

    @property
    def review_count(self):
        return sum(1 for f in self.findings if f.confidence == REVIEW)

    @property
    def applied_count(self):
        return sum(1 for f in self.findings if f.applied)

    @property
    def name_count(self):
        return sum(1 for f in self.findings if f.kind in NAME_KINDS)


# ---------------------------------------------------------------- 보조
def _skip_spans(text):
    spans = []
    for rx in SKIP_SPAN_RES:
        for m in rx.finditer(text):
            spans.append((m.start(), m.end()))
    return sorted(spans)


def _in_spans(pos, spans):
    for s, e in spans:
        if s <= pos < e:
            return True
        if pos < s:
            break
    return False


def _is_org(text, start, end):
    """매치 주변이 법인·기관명이면 자연인이 아니다."""
    left, right = max(0, start - 12), min(len(text), end + 12)
    window = text[left:right]
    for m in ORG_RE.finditer(window):
        if left + m.start() <= start and end <= left + m.end():
            return True
    return False


def _context(text, start, end, width=18):
    left = text[max(0, start - width):start].replace("\n", " ")
    right = text[end:end + width].replace("\n", " ")
    return ("%s⟦…⟧%s" % (left, right)).strip()


def _dedupe(findings):
    """겹치는 매치는 더 긴 것, 길이가 같으면 자동 등급을 남긴다."""
    order = sorted(findings,
                   key=lambda f: (f.start, -(f.end - f.start), 0 if f.confidence == AUTO else 1))
    kept, last_end = [], -1
    for f in order:
        if f.start < last_end:
            continue
        kept.append(f)
        last_end = f.end
    return kept


def _assign_placeholders(findings):
    """같은 값에는 같은 번호를 준다. 중복 제거 뒤에 매겨 빈 번호가 없게 한다."""
    counters, assigned = {}, {}
    for f in findings:
        prefix = PLACEHOLDER_PREFIX.get(f.kind, "비식별")
        key = (prefix, f.original)
        if key not in assigned:
            counters[prefix] = counters.get(prefix, 0) + 1
            assigned[key] = "〔%s-%02d〕" % (prefix, counters[prefix])
        f.placeholder = assigned[key]
    return findings


# ---------------------------------------------------------------- 탐지
def _make(kind, label, value, conf, text, start):
    return Finding(
        kind=kind, label=label, original=value, placeholder="", confidence=conf,
        line=text.count("\n", 0, start) + 1,
        start=start, end=start + len(value),
        context=_context(text, start, start + len(value)),
    )


def detect(text):
    """규칙을 적용해 후보를 찾는다. 치환은 하지 않는다."""
    spans = _skip_spans(text)
    found = []

    # 사용자 사전에 등록된 이름은 조건 없이 찾는다
    for name in sorted(USER_NAMES, key=len, reverse=True):
        if len(name) < 2:
            continue
        for m in re.finditer(r"(?<![가-힣])%s(?![가-힣])" % re.escape(name), text):
            if _in_spans(m.start(), spans):
                continue
            found.append(_make("NAME_USER", "성명(사용자 사전)", name, AUTO, text, m.start()))

    for kind, label, rx, conf, group in RULES:
        for m in rx.finditer(text):
            start = m.start(group)
            raw = m.group(group)
            if not raw or _in_spans(start, spans):
                continue

            if kind not in NAME_KINDS:
                found.append(_make(kind, label, raw, conf, text, start))
                continue

            # ---- 이름 계열은 길이를 되돌린 뒤 판정한다
            if kind in ("NAME_MASKED", "NAME_INITIAL", "NAME_SURNAME"):
                name = raw
            elif kind == "NAME_BARE":
                name = _bare_name(raw)[0]
            else:
                name = _trim_name(raw)
            if not name:
                continue

            if kind == "NAME_SURNAME":
                grade = AUTO
            else:
                if not _looks_like_name(name, kind == "NAME_BARE"):
                    continue
                # 앵커 없는 추정은 자동으로 올리지 않는다. 성씨로 시작하는 세 글자는
                # '이유로'·'하나의'·'구매자'처럼 평범한 낱말에도 그대로 들어맞아서,
                # 끝 글자만 보고 확신을 매기면 본문을 〔자연인-01〕로 덮어쓴다.
                # 진짜 이름은 앵커가 한 번이라도 붙은 자리에서 확정되고, 나머지
                # 등장은 _echo 가 같은 이름으로 메운다.
                grade = REVIEW if kind == "NAME_BARE" else AUTO

            if _is_org(text, start, start + len(name)):
                continue
            if kind == "NAME_BARE" and _in_address_context(text, start):
                continue
            found.append(_make(kind, label, name, grade, text, start))

    found = _echo(text, found, spans)
    return _assign_placeholders(_dedupe(found))


def _echo(text, findings, spans):
    """한 번 확정된 이름을 문서 전체에서 다시 찾는다.

    "대표이사 홍길동"으로 확정했다면 뒤에서 혼자 나오는 "홍길동"도,
    "홍길동은"도 같은 사람이다. 앵커가 있는 자리에서만 이름을 확정하고
    나머지는 이 단계가 메우기 때문에, 규칙을 느슨하게 풀지 않고도
    놓치는 이름이 크게 줄어든다.
    """
    confirmed = {f.original for f in findings
                 if f.confidence == AUTO and len(f.original) >= 2
                 and f.kind in ANCHOR_KINDS}
    if not confirmed:
        return findings

    # 자리를 이미 차지한 것이 확인필요 후보라면 비켜준다. 앵커로 확정된 이름이
    # 뒤에서 혼자 나오면 그 자리는 '성명(추정)'이 먼저 잡아두는데, 이걸 그대로
    # 두면 확정된 이름인데도 확인필요로 남아 치환되지 않는다. 겹치는 둘 중
    # 무엇을 남길지는 _dedupe 가 자동 등급을 우선해 정리한다.
    taken = {f.start for f in findings if f.confidence == AUTO}
    extra = []
    for name in confirmed:
        # 이름 뒤에 붙은 한글을 함께 잡아 그 꼬리가 조사인지 확인한다. 뒤에 한글이
        # 오면 무조건 건너뛰면 `홍길동은`·`김철수가`처럼 조사가 붙은 자리를 전부
        # 놓치는데, 한국어 문장에서 이름은 거의 항상 조사를 달고 나온다.
        for m in re.finditer(r"(?<![가-힣])%s([가-힣]*)" % re.escape(name), text):
            if m.group(1) not in JOSA_TAILS:
                continue
            if m.start() in taken or _in_spans(m.start(), spans):
                continue
            if _is_org(text, m.start(), m.start() + len(name)):
                continue
            extra.append(_make("NAME_ECHO", "성명(같은 문서 재등장)", name, AUTO, text, m.start()))
    return findings + extra


def apply_findings(text, findings, include_review=False):
    """뒤에서부터 치환해 앞쪽 위치가 밀리지 않게 한다."""
    targets = [f for f in findings
               if f.confidence == AUTO or (include_review and f.confidence == REVIEW)]
    for f in targets:
        f.applied = True
    out = text
    for f in sorted(targets, key=lambda x: x.start, reverse=True):
        out = out[:f.start] + f.placeholder + out[f.end:]
    return out


def redact(text, source="", include_review=False):
    findings = detect(text)
    return RedactResult(
        source=source,
        masked_text=apply_findings(text, findings, include_review),
        findings=findings,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )


def scan_file(md_path):
    """파일을 읽어 탐지만 한다. 아무것도 쓰지 않는다."""
    md_path = Path(md_path)
    return redact(md_path.read_text(encoding="utf-8"), source=md_path.name)


def redact_file(md_path, output_dir, report_dir, include_review=False, write_mapping=True):
    """치환본을 만든다. 원본 마크다운은 건드리지 않는다.

    생성물
      <output_dir>/<이름>.masked.md    치환본 — 공유 가능
      <report_dir>/<이름>.redact.json  탐지 내역 — 원문·문맥 없음, 공유 가능
      <report_dir>/<이름>.mapping.json 원문 ↔ 치환기호 — 공유 금지
    """
    md_path, output_dir, report_dir = Path(md_path), Path(output_dir), Path(report_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    text = md_path.read_text(encoding="utf-8")
    result = redact(text, source=md_path.name, include_review=include_review)
    stem = md_path.stem

    masked_path = output_dir / ("%s.masked.md" % stem)
    masked_path.write_text(result.masked_text, encoding="utf-8")

    report = {
        "source": result.source,
        "engine_version": result.engine_version,
        "created_at": result.created_at,
        "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "include_review": include_review,
        "summary": {
            "auto": result.auto_count,
            "review": result.review_count,
            "applied": result.applied_count,
            "names": result.name_count,
        },
        "findings": [f.public() for f in result.findings],
    }
    (report_dir / ("%s.redact.json" % stem)).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if write_mapping:
        mapping = {f.placeholder: f.original for f in result.findings if f.applied}
        (report_dir / ("%s.mapping.json" % stem)).write_text(
            json.dumps({"_warning": "원문 개인정보가 들어 있습니다. 공유·커밋 금지.",
                        "source": result.source, "map": mapping},
                       ensure_ascii=False, indent=2), encoding="utf-8")

    return result, masked_path


def is_masked_name(name):
    """이미 치환된 결과물인지. 두 번 치환하는 것을 막는다."""
    return str(name).endswith(".masked.md")
