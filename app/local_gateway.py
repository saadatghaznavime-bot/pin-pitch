"""Loopback-only, zero-API-cost local AI gateway for PIN&PITCH.

Run with: python -m app.local_gateway
Do not expose this service to the public internet.
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import subprocess
import time
from collections import defaultdict, deque
import tempfile
import io
import wave
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
KOKORO_MODEL_PATH = Path(os.getenv("KOKORO_MODEL_PATH", str(Path.home() / ".local" / "share" / "kokoro" / "kokoro-v1.0.onnx"))).expanduser()
KOKORO_VOICES_PATH = Path(os.getenv("KOKORO_VOICES_PATH", str(Path.home() / ".local" / "share" / "kokoro" / "voices-v1.0.bin"))).expanduser()
DEFAULT_KOKORO_VOICE = os.getenv("KOKORO_DEFAULT_VOICE", "af_heart")

KOKORO_VOICE_CATALOG = {
    "af_heart": {"name": "Heart", "gender": "female", "accent": "American English", "style": "Warm & expressive"},
    "af_jessica": {"name": "Jessica", "gender": "female", "accent": "American English", "style": "Conversational"},
    "am_eric": {"name": "Eric", "gender": "male", "accent": "American English", "style": "Friendly & confident"},
}
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
MAX_AUDIO_BYTES = 20 * 1024 * 1024

app = FastAPI(title="PIN&PITCH Local AI Gateway", version="1.1.0", docs_url=None, redoc_url=None, openapi_url=None)
allowed_origins = [
    item.strip()
    for item in os.getenv(
        "LOCAL_AI_ALLOWED_ORIGINS",
        "http://127.0.0.1:8000,http://localhost:8000",
    ).split(",")
    if item.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

_whisper_model = None
_kokoro_engine = None
_whisper_device_actual: Optional[str] = None
_whisper_compute_actual: Optional[str] = None
API_TOKEN = os.getenv("LOCAL_AI_API_TOKEN", "").strip()
_RATE_BUCKETS: dict[str, deque] = defaultdict(deque)

async def _security_guard(request: Request, authorization: Optional[str] = Header(default=None)):
    host = (request.headers.get("host") or "").split(":")[0].strip("[]").lower()
    if host not in {"127.0.0.1", "localhost"}:
        raise HTTPException(403, "Local AI gateway accepts localhost connections only.")
    origin = request.headers.get("origin")
    if origin and origin not in allowed_origins:
        raise HTTPException(403, "This website is not allowed to access the local AI gateway.")
    if not API_TOKEN or len(API_TOKEN) < 32:
        raise HTTPException(503, "Local gateway is locked: configure a random LOCAL_AI_API_TOKEN of at least 32 characters.")
    if not authorization or not secrets.compare_digest(authorization, "Bearer " + API_TOKEN):
        raise HTTPException(401, "Local AI gateway token is missing or invalid.")
    now = time.monotonic()
    key = request.client.host if request.client else "unknown"
    bucket = _RATE_BUCKETS[key]
    while bucket and now - bucket[0] > 60.0:
        bucket.popleft()
    limit = 12 if request.url.path == "/transcribe" else 60
    if len(bucket) >= limit:
        raise HTTPException(429, "Local gateway rate limit reached. Please wait a moment.")
    bucket.append(now)

def _public_answer(raw: str) -> str:
    """Return only customer-facing text, even when Qwen omits the opening think tag."""
    answer = raw or ""
    # Standard tagged reasoning: keep text after the closing tag.
    if re.search(r"</think\s*>", answer, flags=re.IGNORECASE):
        answer = re.split(r"</think\s*>", answer, maxsplit=1, flags=re.IGNORECASE)[-1]
    else:
        # If an opening tag exists without a close, discard the unfinished reasoning.
        answer = re.sub(r"<think[^>]*>.*$", "", answer, flags=re.IGNORECASE | re.DOTALL)
    answer = re.sub(r"</?think[^>]*>", "", answer, flags=re.IGNORECASE)
    return answer.strip()


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    instructions: str = Field(default="You are a helpful business receptionist. Be concise, honest, and never invent business facts.", max_length=8000)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=20)


def _gpu_info() -> dict:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4, check=True,
        )
        rows = []
        for line in result.stdout.strip().splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) == 4:
                rows.append({"name": parts[0], "vram_total_mib": int(parts[1]), "vram_used_mib": int(parts[2]), "gpu_utilization_percent": int(parts[3])})
        return {"available": bool(rows), "devices": rows}
    except Exception:
        return {"available": False, "devices": []}


async def _ollama_status() -> dict:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"{OLLAMA_URL}/api/tags")
            response.raise_for_status()
            models = response.json().get("models", [])
        names = [model.get("name", "") for model in models]
        return {"reachable": True, "model": OLLAMA_MODEL, "model_available": any(name == OLLAMA_MODEL or name.startswith(OLLAMA_MODEL + ":") for name in names), "installed_models": names}
    except Exception as exc:
        return {"reachable": False, "model": OLLAMA_MODEL, "model_available": False, "error": type(exc).__name__}


@app.get("/health", dependencies=[Depends(_security_guard)])
async def health():
    ollama = await _ollama_status()
    return {
        "service": "pinpitch-local-ai",
        "status": "ok",
        "network_scope": "loopback-only; do not expose publicly",
        "ollama": ollama,
        "whisper": {"installed": _whisper_model is not None, "configured_model": WHISPER_MODEL, "configured_device": WHISPER_DEVICE, "actual_device": _whisper_device_actual},
        "piper": {"configured": _kokoro_files_available(), "engine": "kokoro-onnx", "default_voice": DEFAULT_KOKORO_VOICE, "voices": _kokoro_voice_status()},
        "gpu": _gpu_info(),
    }


@app.post("/chat", dependencies=[Depends(_security_guard)])
async def chat(data: ChatIn):
    ollama = await _ollama_status()
    if not ollama["reachable"] or not ollama["model_available"]:
        raise HTTPException(503, "Local Ollama model is unavailable. Start Ollama and pull the configured model.")
    messages = [{"role": "system", "content": data.instructions}]
    for item in data.history[-20:]:
        role = item.get("role")
        content = item.get("content", "")
        if role in ("user", "assistant") and isinstance(content, str):
            messages.append({"role": role, "content": content[:8000]})
    messages.append({"role": "user", "content": data.message.rstrip() + " /no_think"})
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": OLLAMA_MODEL,
                    "messages": messages,
                    "stream": False,
                    "think": False,
                    "options": {"temperature": 0.2, "num_predict": 160},
                },
            )
            response.raise_for_status()
        answer = _public_answer(response.json().get("message", {}).get("content", ""))
        if not answer:
            raise HTTPException(502, "Ollama returned an empty response.")
        return {"reply": answer, "provider": "ollama-local", "model": OLLAMA_MODEL, "simulated_external_action": False}
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Local Ollama request failed: {type(exc).__name__}")


def _load_whisper():
    global _whisper_model, _whisper_device_actual, _whisper_compute_actual
    if _whisper_model is not None:
        return _whisper_model
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise HTTPException(503, "faster-whisper is not installed. Install requirements-local.txt.")
    try:
        _whisper_model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE)
        _whisper_device_actual, _whisper_compute_actual = WHISPER_DEVICE, WHISPER_COMPUTE_TYPE
    except Exception:
        if WHISPER_DEVICE.lower() == "cpu":
            raise HTTPException(503, "Could not initialize the local Whisper model.")
        try:
            _whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
            _whisper_device_actual, _whisper_compute_actual = "cpu", "int8"
        except Exception as exc:
            raise HTTPException(503, f"Could not initialize Whisper on GPU or CPU: {type(exc).__name__}")
    return _whisper_model


def _transcribe_file(model, file_path: str):
    segments, info = model.transcribe(file_path, beam_size=5)
    transcript = " ".join(segment.text.strip() for segment in segments).strip()
    return transcript, getattr(info, "language", None)


@app.post("/transcribe", dependencies=[Depends(_security_guard)])
async def transcribe(audio: UploadFile = File(...)):
    raw = await audio.read(MAX_AUDIO_BYTES + 1)
    if not raw or len(raw) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "Audio must be between 1 byte and 20 MB.")
    suffix = Path(audio.filename or "recording.wav").suffix[:10] or ".wav"
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
            handle.write(raw)
            temp_path = handle.name
        model = await asyncio.to_thread(_load_whisper)
        transcript, language = await asyncio.to_thread(_transcribe_file, model, temp_path)
        return {"text": transcript, "language": language, "device": _whisper_device_actual, "compute_type": _whisper_compute_actual, "provider": "faster-whisper-local"}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, f"Could not transcribe audio: {type(exc).__name__}")
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _kokoro_files_available() -> bool:
    return KOKORO_MODEL_PATH.is_file() and KOKORO_VOICES_PATH.is_file()


def _kokoro_voice_status() -> list[dict]:
    ready = _kokoro_files_available()
    return [{**details, "id": voice_id, "available": ready} for voice_id, details in KOKORO_VOICE_CATALOG.items()]


def _load_kokoro():
    global _kokoro_engine
    if _kokoro_engine is not None:
        return _kokoro_engine
    if not _kokoro_files_available():
        raise HTTPException(
            503,
            "Kokoro model files are missing. Install kokoro-v1.0.onnx and voices-v1.0.bin in " + str(KOKORO_MODEL_PATH.parent) + ".",
        )
    try:
        from kokoro_onnx import Kokoro
    except ImportError:
        raise HTTPException(503, "Kokoro is not installed. Run pip install -r requirements-local.txt.")
    try:
        _kokoro_engine = Kokoro(str(KOKORO_MODEL_PATH), str(KOKORO_VOICES_PATH))
        return _kokoro_engine
    except Exception as exc:
        raise HTTPException(503, f"Could not load Kokoro model: {type(exc).__name__}")


def _synthesize_kokoro_wav(text: str, voice_id: str) -> bytes:
    import io
    import soundfile as sf

    engine = _load_kokoro()
    samples, sample_rate = engine.create(text, voice=voice_id, speed=1.0, lang="en-us")
    buffer = io.BytesIO()
    sf.write(buffer, samples, sample_rate, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


@app.get("/voices", dependencies=[Depends(_security_guard)])
async def list_voices():
    return {
        "engine": "kokoro-onnx",
        "default_voice": DEFAULT_KOKORO_VOICE,
        "voices": _kokoro_voice_status(),
        "model_path": str(KOKORO_MODEL_PATH),
        "voice_data_path": str(KOKORO_VOICES_PATH),
    }


@app.post("/speak", dependencies=[Depends(_security_guard)])
async def speak(payload: dict):
    text = str(payload.get("text", "")).strip()
    if not text or len(text) > 4000:
        raise HTTPException(400, "Text must contain 1–4000 characters.")
    voice_id = str(payload.get("voice", DEFAULT_KOKORO_VOICE))
    if voice_id not in KOKORO_VOICE_CATALOG:
        raise HTTPException(400, "Unsupported Kokoro voice.")
    try:
        audio_bytes = await asyncio.wait_for(
            asyncio.to_thread(_synthesize_kokoro_wav, text, voice_id),
            timeout=90,
        )
        return Response(content=audio_bytes, media_type="audio/wav", headers={"X-PINPITCH-VOICE": voice_id, "Cache-Control": "no-store"})
    except asyncio.TimeoutError:
        raise HTTPException(504, "Kokoro synthesis timed out.")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Kokoro synthesis failed: {type(exc).__name__}: {str(exc)[:240]}")


if __name__ == "__main__":
    import uvicorn
    # Intentionally bind only to loopback. Never change this to 0.0.0.0.
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")
