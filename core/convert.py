"""변환 오케스트레이션.

파일 한 개를 받아 어떤 파서로 보낼지 고르고, 결과 Document를 마크다운 한 벌로
떨어뜨린다. 파서는 전부 같은 Document 구조를 돌려주기 때문에, 형식이 늘어나도
아래 _EMIT 흐름(그림 추출 → 직렬화 → 파일 쓰기)은 그대로 재사용된다.
"""

import shlex
import subprocess
import tempfile
from pathlib import Path

from . import hwp_binary, hwpx, markdown, ooxml, registry


class ConversionError(Exception):
    pass


def convert(src, out_dir, cfg):
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    fmt = registry.lookup(src.name)
    if fmt is None:
        raise ConversionError(f"지원하지 않는 형식입니다: {src.suffix or '확장자 없음'}")

    handler = fmt.handler
    if handler == "unsupported":
        raise ConversionError(f"{fmt.label}은 바로 읽을 수 없습니다. {fmt.hint}")

    if handler == "hwp":
        return _hwp(src, out_dir, cfg)
    if handler == "hwpx":
        return _emit(src, src.name, out_dir, cfg, hwpx.parse_document, hwpx.extract_images)

    parse, extract = _PARSERS[handler]
    return _emit(src, src.name, out_dir, cfg, parse, extract)


# ---------- 형식별 파서 연결 ----------


def _docx(path, cfg):
    from . import docx

    return docx.parse(path)


def _pptx(path, cfg):
    from . import pptx

    return pptx.parse(path, cfg)


def _xlsx(path, cfg):
    from . import xlsx

    return xlsx.parse(path, cfg)


def _pdf(path, cfg):
    from . import pdf

    return pdf.parse(path, cfg)


def _plain(name):
    def run(path, cfg):
        from . import plain

        return getattr(plain, name)(path, cfg)

    return run


_PARSERS = {
    "docx": (_docx, ooxml.extract_images),
    "pptx": (_pptx, ooxml.extract_images),
    "xlsx": (_xlsx, None),
    "pdf": (_pdf, None),
    "txt": (_plain("parse_text"), None),
    "md": (_plain("parse_markdown"), None),
    "csv": (_plain("parse_csv"), None),
    "json": (_plain("parse_json"), None),
    "html": (_plain("parse_html"), None),
}


# ---------- 공통 출력 ----------


def _emit(path, source_name, out_dir, cfg, parse, extract_images):
    """파서를 돌리고 마크다운 파일 한 개를 쓴다. (결과 경로, 통계)"""
    try:
        doc = parse(path, cfg)
    except ConversionError:
        raise
    except Exception as exc:
        raise ConversionError(_reason(exc)) from exc

    if not doc.blocks:
        raise ConversionError("추출된 본문이 없습니다. 지원하지 않는 구조일 수 있습니다.")

    # 결과 파일명을 먼저 확정한다. 그림 폴더가 원본 이름을 따라가면 보고서.docx와
    # 보고서.hwpx가 같은 assets/보고서 로 들어가 서로 그림을 덮어쓴다.
    target = _unique(out_dir / f"{Path(source_name).stem}.md")

    assets_rel = ""
    if extract_images and cfg.get("extract_images", True) and doc.images:
        assets_rel = f"assets/{target.stem}"
        try:
            extract_images(path, doc, out_dir / assets_rel)
        except OSError:
            assets_rel = ""

    text = markdown.render(doc, source_name, assets_rel)
    target.write_text(text, encoding="utf-8")
    return target, doc.stats


def _reason(exc):
    """파서가 올린 예외를 사람이 읽을 문장으로."""
    message = str(exc).strip()
    if message and exc.__class__.__name__.endswith("Error"):
        return message
    return f"파일을 읽지 못했습니다: {message or exc.__class__.__name__}"


# ---------- .hwp (외부 명령 또는 pyhwp) ----------


def _hwp(src, out_dir, cfg):
    if (cfg.get("hwp_to_hwpx_command") or "").strip():
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / (src.stem + ".hwpx")
            _bridge(src, staged, cfg)
            if not staged.exists():
                raise ConversionError("변환기가 HWPX 파일을 만들지 않았습니다. 명령 설정을 확인하세요.")
            return _emit(staged, src.name, out_dir, cfg, hwpx.parse_document, hwpx.extract_images)

    if hwp_binary.available():
        try:
            return hwp_binary.convert(src, out_dir, cfg)
        except hwp_binary.BridgeError as exc:
            raise ConversionError(str(exc)) from exc

    raise ConversionError(
        ".hwp를 변환할 방법이 없습니다. pip install -r requirements.txt 를 실행하거나 "
        "설정에서 변환 명령을 지정하세요."
    )


def _bridge(src, dst, cfg):
    template = (cfg.get("hwp_to_hwpx_command") or "").strip()
    cmd = template.replace("{input}", str(src)).replace("{output}", str(dst))
    try:
        proc = subprocess.run(
            shlex.split(cmd, posix=False),
            capture_output=True,
            text=True,
            timeout=cfg.get("bridge_timeout", 180),
        )
    except FileNotFoundError as exc:
        raise ConversionError(f"변환기 실행 파일을 찾지 못했습니다: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ConversionError("변환기가 응답하지 않아 중단했습니다.") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"종료 코드 {proc.returncode}"
        raise ConversionError(f"변환기 오류: {tail}")


def _unique(path):
    if not path.exists():
        return path
    for i in range(1, 1000):
        candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise ConversionError("같은 이름의 결과 파일이 너무 많습니다.")
