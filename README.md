# PIN&PITCH — zero-cost communications demo

PIN&PITCH is a workspace-scoped communications demo. It includes account/workspace authentication, AI agent setup, fictional browser call simulations, a unified demo inbox, approved business FAQs, and a mobile-first PWA.

## Zero-cost and traffic boundaries

- The hosted application works without a local AI server.
- Simulated calls, conversations and replies are PIN&PITCH database records. They do not place calls or deliver messages.
- Outbound SMS, WhatsApp, social messaging, OpenAI Realtime and paid voice providers are disabled in the demo backend.
- Inbox replies are saved with the status `saved_demo_only`; they are not sent externally.
- Optional local AI runs on the Windows PC using Ollama, faster-whisper and Piper. It does not require paid API keys.
- Do not enter real customer data, payment details or sensitive information in this demo.

## Run the web application locally

Python 3.11+ is recommended.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Set a unique `JWT_SECRET` in `.env`, then start the app:

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. API documentation is at http://127.0.0.1:8000/docs. SQLite is created in the project directory by default.

## Optional local AI gateway (Windows)

The local gateway is a separate process. It binds to `127.0.0.1:8765` only. Do not expose it with port forwarding, a public tunnel, or `0.0.0.0`.

### 1. Install Ollama and Qwen3

Install Ollama for Windows from https://ollama.com/download/windows. Then run:

```powershell
ollama --version
ollama pull qwen3:4b
ollama run qwen3:4b
```

Exit the model prompt with `/bye`. Ollama should remain running in the background.

### 2. Install optional local speech packages

In the activated project environment:

```powershell
pip install -r requirements-local.txt
```

This installs faster-whisper, Piper TTS and multipart upload support. Download a Piper voice model from the official voice repository and note the full path to its `.onnx` file. Check the model's licence before redistribution.

### 3. Start the local gateway

```powershell
$env:OLLAMA_URL = "http://127.0.0.1:11434"
$env:OLLAMA_MODEL = "qwen3:4b"
$env:WHISPER_MODEL = "small"
$env:WHISPER_DEVICE = "cpu"
$env:WHISPER_COMPUTE_TYPE = "int8"
$env:PIPER_MODEL = "C:\full\path\to\your\voice.onnx"
$env:LOCAL_AI_ALLOWED_ORIGINS = "http://127.0.0.1:8000,http://localhost:8000"
python -m app.local_gateway
```

The gateway health endpoint is http://127.0.0.1:8765/health.

Available endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /health` | Ollama, model, speech and NVIDIA GPU diagnostics |
| `POST /chat` | Local Qwen chat via Ollama |
| `POST /transcribe` | Local faster-whisper transcription |
| `POST /speak` | Local Piper WAV generation |

The first Whisper transcription downloads the selected model. Start with CPU transcription to avoid CUDA setup and GPU memory contention. For GPU transcription on Windows, faster-whisper requires compatible CUDA 12 cuBLAS and cuDNN 9 runtime libraries. If CUDA initialization fails, configure `WHISPER_DEVICE=cpu`. The gateway reports its actual Whisper device after initialization.

The handover PC has an NVIDIA GeForce RTX 4050 Laptop GPU with approximately 6 GiB VRAM and 64 GiB system RAM. Qwen3 4B is the initial local model target; verify actual GPU offload and VRAM use with `nvidia-smi`. Whisper is deliberately configured for CPU by default so it does not compete with Ollama for the limited GPU memory.

### Browser access notes

For the first local test, open the web app at `http://127.0.0.1:8000` on the same PC as the gateway. The default CORS allowlist permits that local origin. A Render-hosted HTTPS page calling a local HTTP gateway may be blocked by browser local-network/private-network security controls even if CORS is configured. Hosted-to-local access is not guaranteed; the hosted application remains usable without it.

## Deploy the hosted demo on Render

Connect this repository to Render and apply the existing `render.yaml` Blueprint. The hosted service installs only `requirements.txt`; local speech dependencies are isolated in `requirements-local.txt` and are not installed on Render.

The free Render service uses SQLite on its filesystem, which may reset after restarts or redeploys. Treat this as a demonstration environment, not durable customer storage.

## Tests

```powershell
pip install -r requirements-dev.txt
python -m pytest -q
```

If `requirements-dev.txt` is not present in the current branch yet, install pytest separately with `pip install pytest`.

## Current limitations

- The agent conversation panel can use the local Ollama gateway for chat, faster-whisper for recorded browser microphone transcription, and Piper for speech output when each local component is installed. If the gateway or model is unavailable, the hosted backend stores an approved-FAQ answer or a clearly labeled safe fallback.
- Ollama, the Qwen model, faster-whisper, Piper and a licensed Piper voice must be installed and verified on the Windows PC.
- Local browser-to-gateway integration must be tested in the target browser; hosted HTTPS pages may be restricted from accessing loopback HTTP services.
- Calls and messages are simulations only. No carrier, SMS, WhatsApp, social platform, external calendar or production telephony provider is connected.
- SQLite is suitable for this demo, not multi-instance production.
