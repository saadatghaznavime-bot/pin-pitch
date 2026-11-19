# PIN&PITCH connected demo (PWA + API)

This package combines the mobile-first PIN&PITCH PWA with its FastAPI demo backend. The PWA now uses the backend for registration/login, workspace-scoped agent creation/listing, simulated inbound/outbound call records, call history, and WebSocket call events.

## Run locally
Python 3.11+ required.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```
Set JWT_SECRET in .env to a long random secret, then run:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Open http://127.0.0.1:8000 (API docs at /docs). The PWA and API share one origin, so no API URL configuration is needed.

## iPhone access
Deploy this whole backend to a Python-capable host with HTTPS, then open its HTTPS URL in Safari and choose Share → Add to Home Screen. A static-only host such as a basic file drop cannot run this backend.

## Demo limitations
All calls are MOCK only. No actual phone calls, live voice audio, OpenAI Live session, carrier connection, SMS, or company APIs are connected. Call numbers and transcript content are demo data. Do not use real customer data. Use a test-only email/password.

Before public deployment: use PostgreSQL, HTTPS/WSS, rate limiting, email verification, password reset, secure secret management, audit logs, CORS policy, tenant-isolation tests, provider webhook signature verification, and outbound consent/calling-hours safeguards.


## Deploy on Render (phone-friendly, requires a Render account)
1. Upload this project to a GitHub repository (the repository root must contain `render.yaml`, `requirements.txt`, and `app/`).
2. In Render, choose **New > Blueprint**, connect that repository, and apply the `render.yaml` blueprint.
3. Render generates `JWT_SECRET` automatically. The start command uses Render's `$PORT`; do not hardcode port 8000 in hosted environments.
4. Wait for the deploy to finish, then open the `https://...onrender.com` service URL in Safari.
5. In Safari, tap Share > Add to Home Screen.

This free demo uses SQLite on the service filesystem; free hosting may not retain data across redeploys/restarts. Use a managed PostgreSQL database and persistent production configuration before real users.
