import os, uuid, asyncio
from datetime import datetime, timezone
from fastapi import Request
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import inspect, text
from jose import JWTError
from .db import Base, engine, get_db
from .models import User, Agent, Call, ChannelConnection, Conversation, Message, BusinessFAQ
from .voice_replies import elevenlabs_tts, deliver_text, deliver_whatsapp_voice
from .schemas import AuthIn, TokenOut, AgentIn, AgentOut, DemoCallIn
from .security import hash_password, verify_password, create_token, decode_token
from .providers import MockTelephonyProvider
from .voice import router as voice_router

Base.metadata.create_all(bind=engine)
# Small additive migration for existing demo databases (create_all does not add columns).
with engine.begin() as connection:
    columns = {col['name'] for col in inspect(engine).get_columns('messages')}
    if 'audio_url' not in columns:
        connection.execute(text("ALTER TABLE messages ADD COLUMN audio_url VARCHAR(600) DEFAULT ''"))
app = FastAPI(title="PIN&PITCH", version="0.3.0")
app.include_router(voice_router)
provider = MockTelephonyProvider()
sockets: dict[str, set[WebSocket]] = {}

def current_claims(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Bearer token required")
    try: return decode_token(authorization.split(" ", 1)[1])
    except JWTError: raise HTTPException(401, "Invalid or expired token")

async def broadcast(workspace: str, event: dict):
    for ws in list(sockets.get(workspace, set())):
        try: await ws.send_json(event)
        except Exception: sockets[workspace].discard(ws)

@app.get("/health")
def health():
    return {"status":"ok","mode":"browser-realtime-ready"}

@app.post("/auth/register", response_model=TokenOut)
def register(data: AuthIn, db: Session=Depends(get_db)):
    if db.query(User).filter(User.email==data.email).first(): raise HTTPException(409,"Email already registered")
    workspace=str(uuid.uuid4()); user=User(email=data.email,password_hash=hash_password(data.password),workspace_id=workspace)
    db.add(user); db.commit(); db.refresh(user)
    return {"access_token":create_token(user.id,workspace)}

@app.post("/auth/login", response_model=TokenOut)
def login(data:AuthIn,db:Session=Depends(get_db)):
    user=db.query(User).filter(User.email==data.email).first()
    if not user or not verify_password(data.password,user.password_hash): raise HTTPException(401,"Incorrect email or password")
    return {"access_token":create_token(user.id,user.workspace_id)}

@app.get("/me")
def me(claims=Depends(current_claims),db:Session=Depends(get_db)):
    user=db.get(User,int(claims["sub"]))
    if not user: raise HTTPException(404,"User not found")
    return {"id":user.id,"email":user.email,"workspace_id":user.workspace_id}

@app.get("/agents",response_model=list[AgentOut])
def list_agents(claims=Depends(current_claims),db:Session=Depends(get_db)):
    return db.query(Agent).filter(Agent.workspace_id==claims["workspace"]).all()

@app.post("/agents",response_model=AgentOut)
def create_agent(data:AgentIn,claims=Depends(current_claims),db:Session=Depends(get_db)):
    agent=Agent(workspace_id=claims["workspace"],**data.model_dump()); db.add(agent); db.commit(); db.refresh(agent); return agent

@app.patch("/agents/{agent_id}",response_model=AgentOut)
def update_agent(agent_id:int,data:AgentIn,claims=Depends(current_claims),db:Session=Depends(get_db)):
    agent=db.query(Agent).filter(Agent.id==agent_id,Agent.workspace_id==claims["workspace"]).first()
    if not agent: raise HTTPException(404,"Agent not found")
    for key,value in data.model_dump().items(): setattr(agent,key,value)
    db.commit(); db.refresh(agent); return agent

@app.delete("/agents/{agent_id}")
def delete_agent(agent_id:int,claims=Depends(current_claims),db:Session=Depends(get_db)):
    agent=db.query(Agent).filter(Agent.id==agent_id,Agent.workspace_id==claims["workspace"]).first()
    if not agent: raise HTTPException(404,"Agent not found")
    db.delete(agent); db.commit(); return {"deleted":True}

def run_demo_call(direction:str,data:DemoCallIn,claims,db:Session):
    agent=db.query(Agent).filter(Agent.id==data.agent_id,Agent.workspace_id==claims["workspace"]).first()
    if not agent: raise HTTPException(404,"Agent not found in this workspace")
    result=provider.create_call(direction,data.phone_number,agent.id)
    record=Call(workspace_id=claims["workspace"],agent_id=agent.id,direction=direction,phone_number=data.phone_number,status=result["status"],duration_seconds=result["duration_seconds"],summary=result["summary"],transcript=result["transcript"])
    db.add(record); db.commit(); db.refresh(record); return record,result

@app.post("/demo/calls/outbound")
async def demo_outbound(data:DemoCallIn,claims=Depends(current_claims),db:Session=Depends(get_db)):
    record,result=run_demo_call("outbound",data,claims,db)
    await broadcast(claims["workspace"],{"type":"call.completed","call_id":record.id,"direction":"outbound"})
    return {"demo_only":True,"call_id":record.id,**result}

@app.post("/demo/calls/inbound")
async def demo_inbound(data:DemoCallIn,claims=Depends(current_claims),db:Session=Depends(get_db)):
    record,result=run_demo_call("inbound",data,claims,db)
    await broadcast(claims["workspace"],{"type":"call.completed","call_id":record.id,"direction":"inbound"})
    return {"demo_only":True,"call_id":record.id,**result}

@app.get("/calls")
def list_calls(claims=Depends(current_claims),db:Session=Depends(get_db)):
    rows=db.query(Call).filter(Call.workspace_id==claims["workspace"]).order_by(Call.id.desc()).all()
    return [{"id":r.id,"agent_id":r.agent_id,"direction":r.direction,"phone_number":r.phone_number,"status":r.status,"duration_seconds":r.duration_seconds,"summary":r.summary,"transcript":r.transcript} for r in rows]

@app.websocket("/ws")
async def websocket_endpoint(ws:WebSocket,token:str):
    await ws.accept()
    try:
        claims=decode_token(token); workspace=claims["workspace"]; sockets.setdefault(workspace,set()).add(ws)
        await ws.send_json({"type":"connected","mode":"live"})
        while True: await ws.receive_text()
    except (WebSocketDisconnect,JWTError): pass
    finally:
        for clients in sockets.values(): clients.discard(ws)


CHANNELS = ["whatsapp", "instagram", "facebook_messenger", "phone", "sms", "webchat", "tiktok"]

def conversation_dict(c):
    return {"id":c.id,"channel":c.channel,"customer_name":c.customer_name,"customer_handle":c.customer_handle,"status":c.status,"last_message":c.last_message,"updated_at":c.updated_at.isoformat()}

@app.get("/inbox/channels")
def inbox_channels(claims=Depends(current_claims), db:Session=Depends(get_db)):
    rows=db.query(ChannelConnection).filter(ChannelConnection.workspace_id==claims["workspace"]).all()
    by_channel={r.channel:r for r in rows}
    return [{"channel":name,"status":by_channel[name].status if name in by_channel else "not_connected","display_name":by_channel[name].display_name if name in by_channel else ""} for name in CHANNELS]

@app.post("/inbox/channels/{channel}/setup")
def setup_channel(channel:str, claims=Depends(current_claims), db:Session=Depends(get_db)):
    if channel not in CHANNELS: raise HTTPException(404,"Unsupported channel")
    row=db.query(ChannelConnection).filter_by(workspace_id=claims["workspace"],channel=channel).first()
    if not row:
        row=ChannelConnection(workspace_id=claims["workspace"],channel=channel,status="pending_setup")
        db.add(row)
    else: row.status="pending_setup"
    db.commit()
    return {"channel":channel,"status":row.status,"message":"Provider credentials and webhook approval are required before live traffic can flow."}

@app.get("/inbox/conversations")
def inbox_conversations(channel:str|None=None, claims=Depends(current_claims), db:Session=Depends(get_db)):
    q=db.query(Conversation).filter(Conversation.workspace_id==claims["workspace"])
    if channel and channel!="all": q=q.filter(Conversation.channel==channel)
    return [conversation_dict(c) for c in q.order_by(Conversation.updated_at.desc()).limit(200).all()]

@app.get("/inbox/conversations/{conversation_id}/messages")
def inbox_messages(conversation_id:int, claims=Depends(current_claims), db:Session=Depends(get_db)):
    conv=db.query(Conversation).filter_by(id=conversation_id,workspace_id=claims["workspace"]).first()
    if not conv: raise HTTPException(404,"Conversation not found")
    rows=db.query(Message).filter_by(conversation_id=conv.id,workspace_id=claims["workspace"]).order_by(Message.created_at.asc()).all()
    return [{"id":m.id,"direction":m.direction,"body":m.body,"delivery_status":m.delivery_status,"audio_url":m.audio_url,"created_at":m.created_at.isoformat()} for m in rows]

@app.post("/inbox/conversations/{conversation_id}/reply")
async def inbox_reply(conversation_id:int, payload:dict, claims=Depends(current_claims), db:Session=Depends(get_db)):
    conv=db.query(Conversation).filter_by(id=conversation_id,workspace_id=claims["workspace"]).first()
    if not conv: raise HTTPException(404,"Conversation not found")
    body=str(payload.get("body","")).strip()
    if not body or len(body)>10000: raise HTTPException(400,"Message must be 1–10000 characters")
    msg=Message(workspace_id=claims["workspace"],conversation_id=conv.id,direction="outbound",body=body,delivery_status="pending_provider")
    conv.last_message=body; conv.updated_at=datetime.now(timezone.utc)
    db.add(msg); db.commit(); db.refresh(msg)
    await broadcast(claims["workspace"],{"type":"inbox.message","conversation_id":conv.id,"message_id":msg.id})
    return {"id":msg.id,"delivery_status":"pending_provider","note":"Saved in PIN&PITCH. A channel provider must be connected before this can be delivered externally."}

@app.post("/inbox/demo/inbound")
async def inbox_demo_inbound(payload:dict, claims=Depends(current_claims), db:Session=Depends(get_db)):
    channel=str(payload.get("channel","webchat"))
    if channel not in CHANNELS: raise HTTPException(400,"Unsupported channel")
    name=str(payload.get("customer_name","Demo customer"))[:160]
    handle=str(payload.get("customer_handle",""))[:255]
    body=str(payload.get("body","")).strip()
    if not body: raise HTTPException(400,"Message body required")
    thread=str(payload.get("external_thread_id","demo-"+uuid.uuid4().hex))
    conv=db.query(Conversation).filter_by(workspace_id=claims["workspace"],channel=channel,external_thread_id=thread).first()
    if not conv:
        conv=Conversation(workspace_id=claims["workspace"],channel=channel,external_thread_id=thread,customer_name=name,customer_handle=handle)
        db.add(conv); db.flush()
    conv.last_message=body; conv.updated_at=datetime.now(timezone.utc)
    msg=Message(workspace_id=claims["workspace"],conversation_id=conv.id,direction="inbound",body=body,delivery_status="received")
    db.add(msg); db.commit(); db.refresh(conv); db.refresh(msg)
    await broadcast(claims["workspace"],{"type":"inbox.message","conversation_id":conv.id,"message_id":msg.id})
    return {"conversation":conversation_dict(conv),"message_id":msg.id,"demo_only":True}

@app.post("/webhooks/inbox/{channel}")
async def inbox_provider_webhook(channel:str, request:Request, db:Session=Depends(get_db)):
    # Canonical adapter ingress. Provider-specific signature validation/normalization
    # must be implemented before enabling this endpoint for public production traffic.
    if channel not in CHANNELS: raise HTTPException(404,"Unsupported channel")
    secret=os.getenv("INBOX_WEBHOOK_SECRET","")
    supplied=request.headers.get("x-pinpitch-webhook-secret","")
    if not secret or not supplied or not __import__("hmac").compare_digest(secret,supplied):
        raise HTTPException(401,"Webhook authentication failed")
    payload=await request.json()
    workspace=str(payload.get("workspace_id",""))
    body=str(payload.get("body","")).strip()
    if not workspace or not body: raise HTTPException(400,"workspace_id and body are required")
    thread=str(payload.get("external_thread_id",""))
    if not thread: raise HTTPException(400,"external_thread_id is required")
    conv=db.query(Conversation).filter_by(workspace_id=workspace,channel=channel,external_thread_id=thread).first()
    if not conv:
        conv=Conversation(workspace_id=workspace,channel=channel,external_thread_id=thread,customer_name=str(payload.get("customer_name","Customer"))[:160],customer_handle=str(payload.get("customer_handle",""))[:255])
        db.add(conv); db.flush()
    conv.last_message=body; conv.updated_at=datetime.now(timezone.utc)
    msg=Message(workspace_id=workspace,conversation_id=conv.id,direction="inbound",body=body,external_message_id=str(payload.get("external_message_id",""))[:255],delivery_status="received")
    db.add(msg); db.commit()
    await broadcast(workspace,{"type":"inbox.message","conversation_id":conv.id})
    return {"received":True}



@app.get("/voice-replies/settings")
def voice_reply_settings(claims=Depends(current_claims)):
    return {
        "enabled": bool(os.getenv("ELEVENLABS_API_KEY") and os.getenv("ELEVENLABS_VOICE_ID")),
        "provider": "elevenlabs",
        "default_format": "text_and_voice",
        "permission_mode": os.getenv("AI_REPLY_PERMISSION_MODE", "hybrid"),
        "model_id": os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5"),
        "whatsapp_ready": bool(os.getenv("WHATSAPP_ACCESS_TOKEN") and os.getenv("WHATSAPP_PHONE_NUMBER_ID")),
        "sms_ready": bool(os.getenv("TWILIO_ACCOUNT_SID") and os.getenv("TWILIO_AUTH_TOKEN") and os.getenv("TWILIO_SMS_FROM"))
    }

@app.post("/voice-replies/generate")
async def generate_voice_reply(payload:dict, request:Request, claims=Depends(current_claims)):
    text_body=str(payload.get("text","")).strip()
    path,filename=await elevenlabs_tts(text_body, payload.get("voice_id"))
    base=os.getenv("PUBLIC_BASE_URL",str(request.base_url).rstrip("/"))
    return {"audio_url":base+"/generated-audio/"+filename,"format":"ogg_opus","provider":"elevenlabs"}

@app.get("/business/faqs")
def list_business_faqs(claims=Depends(current_claims),db:Session=Depends(get_db)):
    rows=db.query(BusinessFAQ).filter_by(workspace_id=claims["workspace"],enabled=True).order_by(BusinessFAQ.id.desc()).all()
    return [{"id":r.id,"question":r.question,"answer":r.answer,"keywords":r.keywords} for r in rows]

@app.post("/business/faqs")
def create_business_faq(payload:dict,claims=Depends(current_claims),db:Session=Depends(get_db)):
    question=str(payload.get("question","")).strip()
    answer=str(payload.get("answer","")).strip()
    keywords=str(payload.get("keywords","")).strip()
    if not question or not answer: raise HTTPException(400,"FAQ question and answer are required.")
    if len(question)>1000 or len(answer)>3000 or len(keywords)>1000: raise HTTPException(400,"FAQ fields exceed allowed length.")
    row=BusinessFAQ(workspace_id=claims["workspace"],question=question,answer=answer,keywords=keywords)
    db.add(row);db.commit();db.refresh(row)
    return {"id":row.id,"question":row.question,"answer":row.answer,"keywords":row.keywords}

def find_faq_reply(workspace:str, incoming:str, db:Session):
    rows=db.query(BusinessFAQ).filter_by(workspace_id=workspace,enabled=True).all()
    normalized=incoming.lower()
    for row in rows:
        terms=[x.strip().lower() for x in row.keywords.replace(",", " ").split() if len(x.strip())>=3]
        if terms and sum(1 for term in terms if term in normalized)>=max(1,min(2,len(terms))):
            return row
    return None

@app.post("/inbox/conversations/{conversation_id}/ai-reply")
async def run_hybrid_ai_reply(conversation_id:int, request:Request, claims=Depends(current_claims),db:Session=Depends(get_db)):
    conv=db.query(Conversation).filter_by(id=conversation_id,workspace_id=claims["workspace"]).first()
    if not conv: raise HTTPException(404,"Conversation not found")
    incoming=db.query(Message).filter_by(conversation_id=conv.id,workspace_id=claims["workspace"],direction="inbound").order_by(Message.id.desc()).first()
    if not incoming: raise HTTPException(400,"No inbound customer message to answer.")
    faq=find_faq_reply(claims["workspace"],incoming.body,db)
    if not faq:
        return {"status":"needs_human_approval","reason":"No approved FAQ matched. Add an approved FAQ or have a staff member write and approve the answer.","customer_message":incoming.body}
    answer=faq.answer
    # Hybrid mode: only pre-approved FAQ answers can be sent automatically.
    if conv.channel not in ("whatsapp","sms","webchat"):
        return {"status":"needs_human_approval","reason":"This channel's outbound adapter is not enabled yet.","draft":answer}
    path,filename=await elevenlabs_tts(answer)
    base=os.getenv("PUBLIC_BASE_URL",str(request.base_url).rstrip("/"))
    audio_url=base+"/generated-audio/"+filename
    delivery=[]
    if conv.channel in ("whatsapp","sms"):
        text_result=await deliver_text(conv.channel,conv.customer_handle,answer)
        delivery.append(text_result)
    if conv.channel=="whatsapp":
        voice_result=await deliver_whatsapp_voice(conv.customer_handle,path)
        delivery.append(voice_result)
    msg=Message(workspace_id=claims["workspace"],conversation_id=conv.id,direction="outbound",body=answer,delivery_status=("sent" if conv.channel=="whatsapp" else "text_sent_audio_in_app"),audio_url=audio_url)
    conv.last_message=answer;conv.updated_at=datetime.now(timezone.utc)
    db.add(msg);db.commit();db.refresh(msg)
    await broadcast(claims["workspace"],{"type":"inbox.message","conversation_id":conv.id,"message_id":msg.id})
    return {"status":"sent","mode":"hybrid_faq_auto","text":answer,"audio_url":audio_url,"voice_delivery":("whatsapp_voice_note" if conv.channel=="whatsapp" else "playable_inside_pinpitch_only"),"delivery":delivery}

@app.post("/inbox/conversations/{conversation_id}/send-voice-reply")
async def send_approved_voice_reply(conversation_id:int,payload:dict,request:Request,claims=Depends(current_claims),db:Session=Depends(get_db)):
    conv=db.query(Conversation).filter_by(id=conversation_id,workspace_id=claims["workspace"]).first()
    if not conv: raise HTTPException(404,"Conversation not found")
    answer=str(payload.get("text","")).strip()
    if not answer or len(answer)>4000: raise HTTPException(400,"Approved reply must contain 1–4000 characters.")
    path,filename=await elevenlabs_tts(answer)
    base=os.getenv("PUBLIC_BASE_URL",str(request.base_url).rstrip("/"))
    audio_url=base+"/generated-audio/"+filename
    delivery=[]
    if conv.channel in ("whatsapp","sms"):
        delivery.append(await deliver_text(conv.channel,conv.customer_handle,answer))
    if conv.channel=="whatsapp":
        delivery.append(await deliver_whatsapp_voice(conv.customer_handle,path))
    elif conv.channel not in ("sms","webchat"):
        raise HTTPException(501,"This channel's outbound delivery adapter is not enabled. Reply was not sent.")
    msg=Message(workspace_id=claims["workspace"],conversation_id=conv.id,direction="outbound",body=answer,delivery_status=("sent" if conv.channel=="whatsapp" else ("text_sent_audio_in_app" if conv.channel=="sms" else "stored_in_webchat")),audio_url=audio_url)
    conv.last_message=answer;conv.updated_at=datetime.now(timezone.utc)
    db.add(msg);db.commit();db.refresh(msg)
    await broadcast(claims["workspace"],{"type":"inbox.message","conversation_id":conv.id,"message_id":msg.id})
    return {"status":"sent","text":answer,"audio_url":audio_url,"voice_delivery":("whatsapp_voice_note" if conv.channel=="whatsapp" else "playable_inside_pinpitch_only"),"delivery":delivery}


STATIC_DIR=os.path.join(os.path.dirname(os.path.dirname(__file__)),"static")
@app.get("/",include_in_schema=False)
def app_home(): return FileResponse(os.path.join(STATIC_DIR,"index.html"))
app.mount("/",StaticFiles(directory=STATIC_DIR,html=True),name="pwa")
