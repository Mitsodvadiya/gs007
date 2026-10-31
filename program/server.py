"""
GastroAGI Chunk Uploader - the local program behind the Chrome extension. The work happens in engine.py; this takes PDFs
from the extension, reports progress, and keeps each job on disk so a queue survives a restart.

    python server.py
"""

import json
import logging
import re
import secrets
import sys
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, jsonify, request

BASE_DIR = Path(__file__).resolve().parent
CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
sys.path.insert(0, str(BASE_DIR))

from chatgpt_controller import ChromeLink  # noqa: E402

from engine import Engine  # noqa: E402

JOBS_DIR = BASE_DIR / "jobs"
MAX_PDF_BYTES = 100 * 1024 * 1024
RESUMED = ("queued", "chatgpt", "ready", "filling", "review", "uploading")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_PDF_BYTES
jobs: dict[str, dict] = {}
engine: Engine = None


def save(job: dict) -> None:
    state = {key: job[key] for key in ("id", "name", "upload_name", "status", "auto", "solo")}
    (job["dir"] / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def new_job(job_id: str, name: str, upload_name: str, folder: Path, auto: bool) -> dict:
    job = {"id": job_id, "name": name, "upload_name": upload_name, "dir": folder, "auto": auto, "status": "queued",
           "step": "", "error": "", "warnings": [], "problems": [], "chunks": [], "decision": None, "solo": False,
           "tries": 0, "result": None}
    jobs[job_id] = job
    return job


def resume_jobs() -> None:
    """Put unfinished work from the last run back in the queue. Chunks already made are not asked for again."""
    if not JOBS_DIR.exists():
        return
    for state_file in sorted(JOBS_DIR.glob("*/state.json")):
        state = json.loads(state_file.read_text(encoding="utf-8"))
        if state["status"] not in RESUMED:
            continue
        job = new_job(state["id"], state["name"], state["upload_name"], state_file.parent, state["auto"])
        job["solo"] = state["solo"]
        chunks_file = state_file.parent / "chunks.json"
        result = json.loads(chunks_file.read_text(encoding="utf-8")) if chunks_file.exists() else None
        if result and not result["problems"]:
            job.update(result=result, status="ready", warnings=result["warnings"],
                       chunks=[{"section": c["section"], "words": c["words"]} for c in result["chunks"]])
            engine.ready.append(job)
        else:
            engine.pending.append(job)
    if jobs:
        print(f"Picked up {len(jobs)} unfinished PDFs from the last run.", flush=True)


@app.before_request
def extension_only():
    """Any website could otherwise POST to this port from the user's browser and start an upload."""
    if request.host.split(":")[0] not in ("127.0.0.1", "localhost"):
        abort(403)
    if request.method == "POST" and not request.headers.get("Origin", "").startswith("chrome-extension://"):
        abort(403)


def public(job: dict) -> dict:
    return {key: job[key] for key in ("id", "name", "status", "step", "error", "warnings", "chunks")}


@app.get("/jobs")
def list_jobs():
    return jsonify(jobs=[public(j) for j in reversed(list(jobs.values()))], notice=engine.notice,
                   accounts=[a.public() for a in engine.accounts])


@app.post("/jobs")
def add_job():
    name = Path(request.args.get("name") or "document.pdf").name
    data = request.get_data()
    if not data.startswith(b"%PDF"):
        return jsonify(error="That file is not a PDF."), 400
    token = secrets.token_hex(3)
    slug = re.sub(r"[^a-z0-9]+", "-", Path(name).stem.lower()).strip("-")[:40] or "document"
    job_id = f"{datetime.now():%Y%m%d_%H%M%S}_{token}_{slug}"
    upload_name = f"{slug}-{token}.pdf"     # unique, so ChatGPT's answer can be matched to this PDF
    folder = JOBS_DIR / job_id
    folder.mkdir(parents=True)
    (folder / upload_name).write_bytes(data)
    job = new_job(job_id, name, upload_name, folder, request.args.get("auto") == "1")
    save(job)
    engine.pending.append(job)
    return jsonify(id=job_id)


@app.post("/jobs/<job_id>/<action>")
def decide(job_id: str, action: str):
    job = jobs.get(job_id)
    if not job or action not in ("upload", "discard", "retry"):
        abort(404)
    if action == "retry":
        if job["status"] == "failed":
            engine.retry(job)
        return jsonify(ok=True)
    if job["status"] in ("queued", "ready") and action == "discard":
        job["status"] = "discarded"
        save(job)
    job["decision"] = action
    return jsonify(ok=True)


def main() -> None:
    global engine
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    engine = Engine(ChromeLink(lambda text: print(text, flush=True)), CONFIG, BASE_DIR / "prompt.md", save)
    resume_jobs()
    threading.Thread(target=engine.run, daemon=True).start()
    print(f"GastroAGI Chunk Uploader is running. Keep this window open. (port {CONFIG['port']})", flush=True)
    logging.getLogger("werkzeug").disabled = True
    app.run(host="127.0.0.1", port=CONFIG["port"], threaded=True)


if __name__ == "__main__":
    main()
