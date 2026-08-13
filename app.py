import json
import re
import socket
import webbrowser
from pathlib import Path
from threading import Timer

from flask import Flask, jsonify, render_template, request, send_from_directory

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


@app.get("/api/preview")
def preview():
    name = request.args.get("name", "")
    # 이 서버는 0.0.0.0 으로 열려 있어서 같은 네트워크의 누구나 부를 수 있다.
    # name 을 그대로 이어붙이면 ../ 로 폴더 밖 파일까지 읽힌다.
    base = pipeline.output.resolve()
    try:
        target = (base / name).resolve()
        target.relative_to(base)
    except (ValueError, OSError):
        return jsonify({"error": "파일을 찾을 수 없습니다."}), 404
    if not target.is_file() or target.suffix != ".md":
        return jsonify({"error": "파일을 찾을 수 없습니다."}), 404
    return jsonify({"name": target.name, "text": target.read_text(encoding="utf-8")})


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
