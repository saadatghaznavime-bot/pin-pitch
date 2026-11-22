# PIN&PITCH — zero-cost communications demo

PIN&PITCH is a workspace-scoped communications demo. It includes account/workspace authentication, AI agent setup, fictional browser call simulations, a unified demo inbox, approved business FAQs, and a mobile-first PWA.

## Zero-cost and traffic boundaries

- The hosted application works without a local AI server.
- Simulated calls, conversations and replies are PIN&PITCH database records. They do not place calls or deliver messages.
- Outbound SMS, WhatsApp, social messaging, OpenAI Realtime and paid voice providers are disabled in the demo backend.
- Inbox replies are saved with the status `saved_demo_only`; they are not sent externally.
- Optional local AI runs on the Windows PC using Ollama, faster-whisper and Kokoro. It does not require paid API keys.
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
ollama pull qwen2.5:3b
ollama run qwen2.5:3b
```

Exit the model prompt with `/bye`. Ollama should remain running in the background.

### 2. Install optional local speech packages

In the activated project environment:

```powershell
pip install -r requirements-local.txt
```

This installs faster-whisper, Kokoro ONNX TTS and multipart upload support. Kokoro uses one ONNX model file plus one voice-embedding bundle; the three selected voice IDs are included in that bundle. Download the two files in the Kokoro section below and place them in `$HOME\.local\share\kokoro\`.

### 3. Start the local gateway

```powershell
$env:OLLAMA_URL = "http://127.0.0.1:11434"
$env:OLLAMA_MODEL = "qwen2.5:3b"
$env:WHISPER_MODEL = "small"
$env:WHISPER_DEVICE = "cpu"
$env:WHISPER_COMPUTE_TYPE = "int8"
$env:PIPER_MODEL = "C:\full\path\to\your\voice.onnx"
$env:LOCAL_AI_ALLOWED_ORIGINS = "http://127.0.0.1:8000,http://localhost:8000"
# Generate a private token once and keep it secret:
$env:LOCAL_AI_API_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(48))"
$env:LOCAL_AI_API_TOKEN
python -m app.local_gateway
```

The gateway health endpoint is http://127.0.0.1:8765/health. The authenticated `GET /voices` endpoint lists supported local presets and whether their model files are installed. `POST /speak` accepts text and a voice ID; only known voice IDs are resolved inside the local voice directory. The selected voice is saved to the agent’s voice setting in your workspace. Paste the generated token into the Local gateway security token field in the PIN&PITCH agent panel and select Unlock local AI. The token is kept in that browser tab session storage and cleared when the tab session ends.

### Local gateway security controls

- Binds only to 127.0.0.1; never use 0.0.0.0, router port forwarding, public tunnels or LAN exposure.
- Requires a randomly generated bearer token of at least 32 characters for health, chat, transcription and speech endpoints.
- Checks the request Host and exact browser Origin against the local allowlist. CORS is an additional browser control, not a replacement for authentication.
- Applies in-memory rate limits: 60 requests/minute and 12 transcription requests/minute per client.
- Disables interactive API documentation and OpenAPI routes on the local gateway.
- Caps audio uploads at 20 MB and speech input at 4,000 characters.
- Keeps the token out of source control; do not paste it into GitHub, screenshots, chats or public logs.

This reduces casual access by other websites and network devices. It does not replace Windows account security, software updates, or protection against malware or XSS running inside the trusted PIN&PITCH page. Anyone with access to your Windows account or browser session may be able to use the local service. Rotate the token by generating a new value and restarting the gateway.

Available endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /health` | Ollama, model, speech and NVIDIA GPU diagnostics |
| `POST /chat` | Local Qwen chat via Ollama |
| `POST /transcribe` | Local faster-whisper transcription |
| `GET /voices` | List supported local Kokoro voices and installation status |
| `POST /speak` | Local Kokoro WAV generation using a selected voice ID |

The first Whisper transcription downloads the selected model. Start with CPU transcription to avoid CUDA setup and GPU memory contention. For GPU transcription on Windows, faster-whisper requires compatible CUDA 12 cuBLAS and cuDNN 9 runtime libraries. If CUDA initialization fails, configure `WHISPER_DEVICE=cpu`. The gateway reports its actual Whisper device after initialization.

The handover PC has an NVIDIA GeForce RTX 4050 Laptop GPU with approximately 6 GiB VRAM and 64 GiB system RAM. Qwen2.5 3B is the initial local model target; verify actual GPU offload and VRAM use with `nvidia-smi`. Whisper is deliberately configured for CPU by default so it does not compete with Ollama for the limited GPU memory.

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

- The agent conversation panel can use the local Ollama gateway for chat, faster-whisper for recorded browser microphone transcription, and Kokoro for speech output when each local component is installed. If the gateway or model is unavailable, the hosted backend stores an approved-FAQ answer or a clearly labeled safe fallback.
- Ollama, the Qwen model, faster-whisper, Kokoro and a licensed Kokoro voice must be installed and verified on the Windows PC.
- Local browser-to-gateway integration must be tested in the target browser; hosted HTTPS pages may be restricted from accessing loopback HTTP services.
- Calls and messages are simulations only. No carrier, SMS, WhatsApp, social platform, external calendar or production telephony provider is connected.
- SQLite is suitable for this demo, not multi-instance production.


## Kokoro local voices (Windows)

The local gateway uses Kokoro ONNX for speech generation. Install local dependencies with:

```powershell
python -m pip install -r requirements-local.txt
```

Download the official Kokoro ONNX model and voice bundle from the upstream model release:
- `kokoro-v1.0.onnx`
- `voices-v1.0.bin`

Place both files in `$HOME\.local\share\kokoro\` (create the folder if needed). The gateway's selected voice presets are `af_heart`, `af_jessica`, and `am_eric`. They are assigned per agent through the existing Agent voice field. Kokoro runs locally; no paid speech API is used. The gateway loads the ONNX model lazily on the first preview or speech request.
