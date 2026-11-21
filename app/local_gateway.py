"""Loopback-only, zero-API-cost local AI gateway for PIN&PITCH.

Run with: python -m app.local_gateway
Do not expose this service to the public internet.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:4b")
PIPER_MODEL = os.getenv("PIPER_MODEL", "")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
MAX_AUDIO_BYTES = 20 * 1024 * 1024

app = FastAPI(title="PIN&PITCH Local AI Gateway", version="1.0.0")
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
    allow_headers=["Content-Type"],
)

_whisper_model = None
_whisper_device_actual: Optional[str] = None
_whisper_compute_actual: Optional[str] = None


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


@app.get("/health")
async def health():
    ollama = await _ollama_status()
    return {
        "service": "pinpitch-local-ai",
        "status": "ok",
        "network_scope": "loopback-only; do not expose publicly",
        "ollama": ollama,
        "whisper": {"installed": _whisper_model is not None, "configured_model": WHISPER_MODEL, "configured_device": WHISPER_DEVICE, "actual_device": _whisper_device_actual},
        "piper": {"configured": bool(PIPER_MODEL and Path(PIPER_MODEL).is_file())},
        "gpu": _gpu_info(),
    }


@app.post("/chat")
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
    messages.append({"role": "user", "content": data.message})
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={"model": OLLAMA_MODEL, "messages": messages, "stream": False, "options": {"temperature": 0.3}},
            )
            response.raise_for_status()
        answer = response.json().get("message", {}).get("content", "").strip()
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


@app.post("/transcribe")
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


@app.post("/speak")
async def speak(payload: dict):
    text = str(payload.get("text", "")).strip()
    if not text or len(text) > 4000:
        raise HTTPException(400, "Text must contain 1–4000 characters.")
    model_path = Path(PIPER_MODEL).expanduser() if PIPER_MODEL else None
    if not model_path or not model_path.is_file():
        raise HTTPException(503, "Piper voice model is not configured. Set PIPER_MODEL to a local .onnx voice file.")
    try:
        with tempfile.TemporaryDirectory() as folder:
            output_path = Path(folder) / "reply.wav"
            process = await asyncio.create_subprocess_exec(
                "piper", "--model", str(model_path), "--output_file", str(output_path),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(process.communicate(text.encode("utf-8")), timeout=60)
            if process.returncode != 0 or not output_path.is_file():
                detail = stderr.decode("utf-8", "ignore")[-300:]
                raise HTTPException(502, f"Piper synthesis failed: {detail or 'no audio returned'}")
            audio_bytes = output_path.read_bytes()
        return Response(content=audio_bytes, media_type="audio/wav", headers={"X-PINPITCH-VOICE": "piper-local"})
    except FileNotFoundError:
        raise HTTPException(503, "Piper CLI was not found. Install requirements-local.txt.")
    except asyncio.TimeoutError:
        raise HTTPException(504, "Piper synthesis timed out.")


if __name__ == "__main__":
    import uvicorn
    # Intentionally bind only to loopback. Never change this to 0.0.0.0.
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")
