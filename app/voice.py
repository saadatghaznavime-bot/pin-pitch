import os, json, hashlib
import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from jose import JWTError
from sqlalchemy.orm import Session
from .db import get_db
from .models import Agent, Call
from .security import decode_token

router = APIRouter(prefix="/voice", tags=["browser voice"])

def claims_from_header(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Sign in to PIN&PITCH first.")
    try:
        return decode_token(authorization.split(" ", 1)[1])
    except JWTError:
        raise HTTPException(401, "Your session has expired. Please sign in again.")

@router.post("/session")
async def create_browser_voice_session(
    request: Request,
    claims=Depends(claims_from_header),
    db: Session=Depends(get_db),
):
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(503, "OpenAI voice is not configured yet. Add OPENAI_API_KEY in Render.")
    data = await request.json()
    sdp = data.get("sdp")
    try:
        agent_id = int(data.get("agent_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Choose an AI agent.")
    if not isinstance(sdp, str) or not sdp.startswith("v="):
        raise HTTPException(400, "Invalid WebRTC offer.")
    agent = db.query(Agent).filter(
        Agent.id == agent_id,
        Agent.workspace_id == claims["workspace"]
    ).first()
    if not agent:
        raise HTTPException(404, "Agent not found in this workspace.")
    voice = agent.voice if agent.voice in {
        "alloy","ash","ballad","coral","echo","sage","shimmer","verse","marin","cedar"
    } else "marin"
    instructions = (
        f"You are {agent.name}, a voice agent inside PIN&PITCH. "
        + (agent.instructions or "You are a helpful, friendly business assistant.")
        + " Speak naturally and concisely. This is a live voice conversation with the business owner testing you. "
          "Be transparent that you are an AI assistant. Do not claim to perform actions you cannot perform."
    )
    session = {
        "type": "realtime",
        "model": os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1"),
        "instructions": instructions,
        "audio": {
            "input": {
                "turn_detection": {"type": "semantic_vad"},
                "transcription": {"model": "gpt-4o-mini-transcribe"}
            },
            "output": {"voice": voice}
        }
    }
    # Unified WebRTC endpoint: the standard API key stays on this server.
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                "https://api.openai.com/v1/realtime/calls",
                headers={
                    "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                    "OpenAI-Safety-Identifier": hashlib.sha256(
                        str(claims["sub"]).encode()
                    ).hexdigest()
                },
                data={
                    "sdp": sdp,
                    "session": json.dumps(session)
                }
            )
        if response.status_code >= 400:
            detail = "OpenAI could not create the voice session."
            try:
                payload = response.json()
                detail = payload.get("error", {}).get("message", detail)
            except Exception:
                pass
            raise HTTPException(response.status_code, detail[:500])
        call = Call(
            workspace_id=claims["workspace"],
            agent_id=agent.id,
            direction="web",
            phone_number="Browser microphone",
            status="connected",
            duration_seconds=0,
            summary="Browser voice session started."
        )
        db.add(call)
        db.commit()
        return PlainTextResponse(response.text, media_type="application/sdp")
    except httpx.TimeoutException:
        raise HTTPException(504, "OpenAI voice session timed out. Please try again.")
    except httpx.RequestError:
        raise HTTPException(502, "Could not reach OpenAI Realtime. Please try again.")
