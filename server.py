"""
Speakly Voice Cloner - XTTS-v2 microservice.

Runs Coqui XTTS-v2 (zero-shot multilingual voice cloning) behind a small
FastAPI server. Works two ways:

  * Next.js dev/local  -> proxied through  POST /api/clone
  * Direct browser call -> the Hugging Face Space URL (CORS enabled)

Cloning is run as an async job so long generations never hold one HTTP
request open (Cloudflare tunnels cap requests at ~100s; serverless functions
have their own limits). The client POSTs once, gets a job id, then polls.

Endpoints
    GET  /                 -> info page
    GET  /health           -> engine status
    GET  /languages        -> supported languages
    GET  /clone            -> health payload (same as /health)
    GET  /clone?job=<id>   -> clone job status
    POST /clone            -> multipart (text, language, speaker_wav) -> {jobId}

Run
    .\\run.ps1            (local)
    docker run -p 7860:7860  (Space / container)

Env
    CLONE_HOST (default 127.0.0.1)   CLONE_PORT (default 8770)
    CLONE_WARMUP=1                    COQUI_TOS_AGREED=1 (set automatically)
"""

import os
import tempfile
import threading
import time
import uuid

# XTTS-v2 ships under the Coqui CPML license; auto-agree to skip the prompt.
os.environ.setdefault("COQUI_TOS_AGREED", "1")

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

HOST = os.environ.get("CLONE_HOST", "127.0.0.1")
PORT = int(os.environ.get("CLONE_PORT") or os.environ.get("PORT", "8770"))
MODEL_NAME = os.environ.get(
    "CLONE_MODEL", "tts_models/multilingual/multi-dataset/xtts_v2"
)
MAX_CHARS = int(os.environ.get("CLONE_MAX_CHARS", "5000"))

LANGUAGES = [
    "en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl",
    "cs", "ar", "zh-cn", "hu", "ko", "hi", "ja",
]

app = FastAPI(title="Speakly Voice Cloner")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

_tts = None
_model_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_JOB_TTL = 30 * 60


def get_tts():
    """Lazily load (and, on first run, download) the XTTS-v2 model."""
    global _tts
    if _tts is None:
        with _model_lock:
            if _tts is None:
                from TTS.api import TTS

                model = TTS(MODEL_NAME)
                try:
                    model.to("cpu")
                except Exception:  # noqa: BLE001
                    pass
                _tts = model
    return _tts


def _warmup():
    try:
        get_tts()
        print("[clone-service] XTTS-v2 model ready.", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[clone-service] warmup failed: {exc}", flush=True)


@app.on_event("startup")
def _startup():
    if os.environ.get("CLONE_WARMUP", "1") == "1":
        threading.Thread(target=_warmup, daemon=True).start()


def _health() -> dict:
    return {
        "available": True,
        "ok": True,
        "engine": "xtts-v2",
        "model": MODEL_NAME,
        "loaded": _tts is not None,
    }


def _sweep():
    cutoff = time.time() - _JOB_TTL
    with _jobs_lock:
        for jid in [k for k, j in _jobs.items() if j.get("status") != "pending" and j.get("createdAt", 0) < cutoff]:
            _jobs.pop(jid, None)


def _run_clone(job_id: str, text: str, language: str, ref_bytes: bytes):
    job = _jobs[job_id]
    ref_path = None
    out_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
            fh.write(ref_bytes)
            ref_path = fh.name
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
            out_path = fh.name

        model = get_tts()
        model.tts_to_file(
            text=text, speaker_wav=ref_path, language=language, file_path=out_path
        )
        with open(out_path, "rb") as fh:
            audio = fh.read()
        if not audio:
            job.update(status="error", error="Cloning produced no audio.")
            return
        import base64

        job.update(status="done", mime="audio/wav", audio=base64.b64encode(audio).decode("ascii"))
    except Exception as exc:  # noqa: BLE001
        job.update(status="error", error=f"Cloning failed: {exc}")
    finally:
        for path in (ref_path, out_path):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


@app.get("/", response_class=HTMLResponse)
def index():
    return (
        "<!doctype html><html><head><title>Speakly Clone Engine</title></head>"
        "<body style='font-family:system-ui;background:#020b16;color:#e7eefc;padding:2rem'>"
        "<h1>Speakly clone engine</h1>"
        f"<p>XTTS-v2 &middot; model <code>{MODEL_NAME}</code></p>"
        f"<p>Health: <a style='color:#f7c451' href='/health'>/health</a></p>"
        "</body></html>"
    )


@app.get("/health")
def health():
    return _health()


@app.get("/languages")
def languages():
    return {"languages": LANGUAGES}


@app.get("/clone")
def clone_status(job: str | None = None):
    # No job id => act as a health endpoint (used by the studio's status chip).
    if not job:
        return _health()
    with _jobs_lock:
        entry = _jobs.get(job)
    if entry is None:
        return JSONResponse({"status": "error", "error": "Job not found."}, status_code=404)
    if entry["status"] == "done":
        with _jobs_lock:
            _jobs.pop(job, None)
        return {"status": "done", "mime": entry.get("mime", "audio/wav"), "audio": entry["audio"]}
    return {"status": entry["status"], "error": entry.get("error")}


@app.post("/clone")
async def clone_start(
    text: str = Form(...),
    language: str = Form("en"),
    speaker_wav: UploadFile = File(...),
):
    text = (text or "").strip()
    if not text:
        return JSONResponse({"error": "No text supplied."}, status_code=400)
    if len(text) > MAX_CHARS:
        return JSONResponse(
            {"error": f"Text is too long. Maximum is {MAX_CHARS} characters."},
            status_code=413,
        )
    if language not in LANGUAGES:
        language = "en"

    ref_bytes = await speaker_wav.read()
    if not ref_bytes:
        return JSONResponse({"error": "Empty voice sample."}, status_code=400)

    _sweep()
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "pending", "createdAt": time.time()}

    threading.Thread(
        target=_run_clone, args=(job_id, text, language, ref_bytes), daemon=True
    ).start()
    return {"jobId": job_id}


def main():
    import uvicorn

    print(f"[clone-service] listening on http://{HOST}:{PORT}", flush=True)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
