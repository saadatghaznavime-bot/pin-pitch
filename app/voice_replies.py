import os
import uuid
from pathlib import Path
import httpx
from fastapi import HTTPException

AUDIO_DIR = Path(__file__).resolve().parent.parent / "static" / "generated-audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

async def elevenlabs_tts(text: str, voice_id: str | None = None) -> tuple[Path, str]:
    api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    voice = (voice_id or os.getenv("ELEVENLABS_VOICE_ID", "")).strip()
    if not api_key:
        raise HTTPException(503, "ElevenLabs is not configured. Add ELEVENLABS_API_KEY in Render environment variables.")
    if not voice:
        raise HTTPException(503, "Set ELEVENLABS_VOICE_ID in Render environment variables.")
    if not text or len(text) > 4000:
        raise HTTPException(400, "Voice reply text must contain 1–4000 characters.")
    model = os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5")
    output_format = os.getenv("ELEVENLABS_OUTPUT_FORMAT", "opus_48000_128")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                url,
                params={"output_format": output_format},
                headers={"xi-api-key": api_key, "Content-Type": "application/json", "Accept": "audio/ogg"},
                json={"text": text, "model_id": model}
            )
        if response.status_code >= 400:
            detail = response.text[:500]
            raise HTTPException(502, f"ElevenLabs error ({response.status_code}): {detail}")
        # WhatsApp voice notes require OGG/Opus. The selected ElevenLabs Opus output
        # is stored as .ogg; validate provider delivery in a live test before launch.
        filename = f"{uuid.uuid4().hex}.ogg"
        path = AUDIO_DIR / filename
        path.write_bytes(response.content)
        return path, filename
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"ElevenLabs request failed: {exc.__class__.__name__}")

async def deliver_text(channel: str, recipient: str, body: str) -> dict:
    if channel == "whatsapp":
        token = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
        phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
        version = os.getenv("META_GRAPH_API_VERSION", "v23.0")
        if not token or not phone_id:
            raise HTTPException(503, "WhatsApp Cloud API credentials are not configured.")
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(f"https://graph.facebook.com/{version}/{phone_id}/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={"messaging_product":"whatsapp","recipient_type":"individual","to":recipient,"type":"text","text":{"preview_url":False,"body":body}})
        if res.status_code >= 400: raise HTTPException(502, f"WhatsApp delivery failed: {res.text[:300]}")
        return {"provider":"whatsapp_cloud_api","provider_message_id":res.json().get("messages",[{}])[0].get("id","")}
    if channel == "sms":
        sid=os.getenv("TWILIO_ACCOUNT_SID","").strip(); token=os.getenv("TWILIO_AUTH_TOKEN","").strip(); sender=os.getenv("TWILIO_SMS_FROM","").strip()
        if not sid or not token or not sender: raise HTTPException(503,"Twilio SMS credentials are not configured.")
        async with httpx.AsyncClient(timeout=30) as client:
            res=await client.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",auth=(sid,token),data={"To":recipient,"From":sender,"Body":body})
        if res.status_code >= 400: raise HTTPException(502,f"Twilio SMS delivery failed: {res.text[:300]}")
        return {"provider":"twilio_sms","provider_message_id":res.json().get("sid","")}
    if channel == "webchat":
        return {"provider":"pinpitch_webchat","provider_message_id":""}
    raise HTTPException(501, f"Outbound text delivery for {channel} is not enabled yet. Provider adapter and platform permissions are required.")

async def deliver_whatsapp_voice(recipient: str, audio_path: Path) -> dict:
    token=os.getenv("WHATSAPP_ACCESS_TOKEN","").strip()
    phone_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID","").strip()
    version=os.getenv("META_GRAPH_API_VERSION","v23.0")
    if not token or not phone_id: raise HTTPException(503,"WhatsApp Cloud API credentials are not configured.")
    async with httpx.AsyncClient(timeout=60) as client:
        with audio_path.open("rb") as audio:
            upload=await client.post(f"https://graph.facebook.com/{version}/{phone_id}/media",
                headers={"Authorization":f"Bearer {token}"},
                data={"messaging_product":"whatsapp"},
                files={"file":(audio_path.name,audio,"audio/ogg")})
        if upload.status_code>=400: raise HTTPException(502,f"WhatsApp media upload failed: {upload.text[:300]}")
        media_id=upload.json().get("id")
        if not media_id: raise HTTPException(502,"WhatsApp did not return a media ID.")
        sent=await client.post(f"https://graph.facebook.com/{version}/{phone_id}/messages",
            headers={"Authorization":f"Bearer {token}"},
            json={"messaging_product":"whatsapp","recipient_type":"individual","to":recipient,"type":"audio","audio":{"id":media_id,"voice":True}})
    if sent.status_code>=400: raise HTTPException(502,f"WhatsApp voice-note delivery failed: {sent.text[:300]}")
    return {"provider":"whatsapp_cloud_api","provider_message_id":sent.json().get("messages",[{}])[0].get("id","")}
