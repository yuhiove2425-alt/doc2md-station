import json
import re
import socket
import webbrowser
from pathlib import Path
from threading import Timer

from flask import Flask, jsonify, render_template, request, send_from_directory

from core import redact as redactor
from core import registry
from core.pipeline import Pipeline

INVALID_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_filename(raw_name):
    """윈도우에서 안전한 파일명으로 정리하되 한글 파일명은 그대로 둔다."""
    name = Path(raw_name).name.strip(" .")
    name = INVALID_NAME_CHARS.sub("_", name)
    return name or "제목없음"


def unique_target(directory, name):
    stem, suffix = Path(name).stem, Path(name).suffix
    target = directory / name
    for i in range(1, 1000):
        if not target.exists():
            return target
        target = directory / f"{stem}_{i}{suffix}"
    return target


BASE = Path(__file__).parent
CONFIG_PATH = BASE / "config.json"

DEFAULTS = {
    "root": str(BASE / "작업폴더"),
    "inbox_name": "작업전",
    "done_name": "작업완료",
    "failed_name": "변환실패",
    "output_name": "마크다운",
    "redact_name": "치환기록",
    "poll_seconds": 2,
    "stable_seconds": 3,
    "bridge_timeout": 180,
    "extract_images": True,
    "include_speaker_notes": True,
    "pdf_page_marks": True,
    "pdf_headings": True,
    "sheet_max_rows": 2000,
    "hwp_to_hwpx_command": "",
    "port": 7800,
}


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


config = load_config()
save_config(config)
pipeline = Pipeline(config)

# 사용자 사전 — 규칙이 놓치는 이름과 잘못 잡는 낱말을 팀이 직접 채운다.
DICT_DIR = BASE / "사전"
DICT_TEMPLATES = {
    "이름.txt": (
        "# 무조건 치환할 이름을 한 줄에 하나씩 적습니다.\n"
        "# 규칙이 놓치는 이름(특이한 성씨, 외자 이름 등)을 여기에 넣으세요.\n"
        "# 이 파일에는 실제 이름이 들어갑니다. 공유·커밋하지 마세요.\n"
    ),
    "제외.txt": (
        "# 이름으로 잘못 잡히는 낱말을 한 줄에 하나씩 적습니다.\n"
        "# 예: 사내 용어, 제품명, 부서명\n"
    ),
}


def ensure_dicts():
    DICT_DIR.mkdir(parents=True, exist_ok=True)
    for name, header in DICT_TEMPLATES.items():
        path = DICT_DIR / name
        if not path.exists():
            path.write_text(header, encoding="utf-8")
    redactor.load_user_dicts(DICT_DIR)


ensure_dicts()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200MB


@app.errorhandler(413)
def too_large(_exc):
    return jsonify({"ok": False, "error": "파일이 너무 큽니다 (최대 200MB)."}), 413


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/state")
def state():
    data = pipeline.state()
    data["config"] = config
    # 지원 형식은 core/registry.py 한 곳에서만 정의하고, 화면 안내도 여기서 내려간다
    data["formats"] = registry.catalog()
    data["accept"] = registry.accept_attribute()
    return jsonify(data)


@app.post("/api/watch")
def watch():
    on = bool(request.json.get("on"))
    pipeline.start() if on else pipeline.stop()
    return jsonify({"watching": pipeline.watching})


@app.post("/api/scan")
def scan():
    pipeline.log("info", "지금 변환을 실행합니다.")
    pipeline.scan_once()
    return jsonify({"ok": True})


@app.post("/api/config")
def update_config():
    payload = request.json or {}
    for key in (
        "hwp_to_hwpx_command",
        "root",
        "stable_seconds",
        "poll_seconds",
        "extract_images",
        "include_speaker_notes",
        "pdf_page_marks",
        "pdf_headings",
        "sheet_max_rows",
        "redact_include_review",
    ):
        if key in payload:
            config[key] = payload[key]
    save_config(config)
    pipeline.cfg = config
    pipeline._ensure_dirs()
    pipeline.log("info", "설정을 저장했습니다.")
    return jsonify({"ok": True, "config": config})


@app.post("/api/upload")
def upload():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"ok": False, "error": "파일이 없습니다."}), 400

    pipeline.inbox.mkdir(parents=True, exist_ok=True)
    saved, rejected = [], []
    for f in files:
        if not f.filename:
            continue
        name = safe_filename(f.filename)
        fmt = registry.lookup(name)
        if fmt is None:
            rejected.append({"name": name, "reason": "지원하지 않는 형식입니다."})
            continue
        ok, why = registry.availability(fmt)
        if not ok:
            rejected.append({"name": name, "reason": why})
            continue
        target = unique_target(pipeline.inbox, name)
        f.save(target)
        saved.append(target.name)

    if saved:
        pipeline.log("info", f"{len(saved)}개 파일을 끌어다 놓았습니다: " + ", ".join(saved))
    for item in rejected:
        pipeline.log("error", f"{item['name']}: {item['reason']}")

    return jsonify({"ok": True, "saved": saved, "rejected": rejected})


def output_file(name):
    """결과 폴더 안의 마크다운 파일만 돌려준다. 아니면 None.

    이 서버는 0.0.0.0 으로 열려 있어 같은 네트워크의 누구나 부를 수 있다. 요청으로
    받은 이름을 폴더 경로에 그냥 이어붙이면 `../`를 섞어 폴더 밖 파일까지 읽힌다.
    파일을 읽는 경로가 셋(미리보기·치환 탐지·치환 실행)이라 검사도 한 곳에 둔다.
    """
    if not name:
        return None
    base = pipeline.output.resolve()
    try:
        target = (base / name).resolve()
        target.relative_to(base)
    except (ValueError, OSError):
        return None
    if not target.is_file() or target.suffix != ".md":
        return None
    return target


@app.get("/api/preview")
def preview():
    target = output_file(request.args.get("name", ""))
    if target is None:
        return jsonify({"error": "파일을 찾을 수 없습니다."}), 404
    return jsonify({"name": target.name, "text": target.read_text(encoding="utf-8")})


@app.get("/api/redact/scan")
def redact_scan():
    """탐지만 한다. 파일은 만들지 않는다.

    미리보기를 열 때 호출되어 "자동 N · 확인필요 M"을 보여주는 용도다.
    문맥(context)은 이 화면에서만 쓰고 저장 파일에는 넣지 않는다.
    """
    name = request.args.get("name", "")
    target = output_file(name)
    if target is None:
        return jsonify({"error": "파일을 찾을 수 없습니다."}), 404

    redactor.load_user_dicts(DICT_DIR)
    if redactor.is_masked_name(target.name):
        return jsonify({
            "name": target.name, "already_masked": True,
            "summary": {"auto": 0, "review": 0}, "findings": [],
        })

    result = redactor.scan_file(target)
    return jsonify({
        "name": target.name,
        "already_masked": False,
        "summary": {"auto": result.auto_count, "review": result.review_count,
                    "names": result.name_count},
        "findings": [f.public(include_context=True) for f in result.findings],
    })


@app.post("/api/redact/apply")
def redact_apply():
    """치환본을 만든다. 원본 마크다운은 그대로 둔다."""
    payload = request.json or {}
    name = payload.get("name", "")
    target = output_file(name)
    if target is None:
        return jsonify({"ok": False, "error": "파일을 찾을 수 없습니다."}), 404

    redactor.load_user_dicts(DICT_DIR)
    if redactor.is_masked_name(target.name):
        return jsonify({"ok": False, "error": "이미 치환된 파일입니다."}), 400

    include_review = bool(payload.get("include_review",
                                      config.get("redact_include_review", False)))
    try:
        result, masked_path = redactor.redact_file(
            target, pipeline.output, pipeline.redact_dir, include_review=include_review)
    except OSError as exc:
        pipeline.log("error", f"{name}: 치환본을 저장하지 못했습니다 ({exc})")
        return jsonify({"ok": False, "error": "치환본을 저장하지 못했습니다."}), 500

    left = 0 if include_review else result.review_count
    pipeline.log(
        "ok",
        f"{name} → {masked_path.name} (치환 {result.applied_count}건"
        + (f" · 확인필요 {left}건 남김" if left else "") + ")",
    )
    return jsonify({
        "ok": True,
        "name": name,
        "masked": masked_path.name,
        "applied": result.applied_count,
        "review_left": left,
        "text": result.masked_text,
    })


@app.get("/download/<path:name>")
def download(name):
    return send_from_directory(pipeline.output, name, as_attachment=True)


def open_browser(port):
    webbrowser.open(f"http://127.0.0.1:{port}")


def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


if __name__ == "__main__":
    port = int(config.get("port", 7800))
    lan_ip = get_lan_ip()

    print("=" * 50)
    print(f"내 컴퓨터에서 접속:  http://127.0.0.1:{port}")
    if lan_ip:
        print(f"팀원과 공유할 주소:  http://{lan_ip}:{port}")
        print("(같은 사무실/와이파이 네트워크에 있는 팀원만 접속 가능합니다)")
    else:
        print("네트워크 IP를 찾지 못했습니다. 같은 네트워크에서만 공유할 수 있습니다.")
    print("=" * 50)

    Timer(1.0, open_browser, args=[port]).start()
    app.run(host="0.0.0.0", port=port, debug=False)
