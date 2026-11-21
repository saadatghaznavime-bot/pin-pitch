import os, uuid, asyncio
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, timezone
from fastapi import Request
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import inspect, text
from jose import JWTError
from .db import Base, engine, get_db
from .models import User, Agent, Call, ChannelConnection, Conversation, Message, BusinessFAQ, Contact, Appointment, AgentSession, AgentSessionTurn
from .schemas import AuthIn, TokenOut, AgentIn, AgentOut, DemoCallIn
from .security import hash_password, verify_password, create_token, decode_token
from .providers import MockTelephonyProvider

Base.metadata.create_all(bind=engine)
# Small additive migration for existing demo databases (create_all does not add columns).
with engine.begin() as connection:
    columns = {col['name'] for col in inspect(engine).get_columns('messages')}
    if 'audio_url' not in columns:
        connection.execute(text("ALTER TABLE messages ADD COLUMN audio_url VARCHAR(600) DEFAULT ''"))
app = FastAPI(title="PIN&PITCH", version="0.3.0")
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
    return {"status":"ok","mode":"zero-cost-demo","external_providers_enabled":False}

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
    msg=Message(workspace_id=claims["workspace"],conversation_id=conv.id,direction="outbound",body=body,delivery_status="saved_demo_only")
    conv.last_message=body; conv.updated_at=datetime.now(timezone.utc)
    db.add(msg); db.commit(); db.refresh(msg)
    await broadcast(claims["workspace"],{"type":"inbox.message","conversation_id":conv.id,"message_id":msg.id})
    return {"id":msg.id,"delivery_status":"saved_demo_only","external_delivery":False,"note":"Saved in the PIN&PITCH demo inbox only. No external message was sent."}

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
        "enabled": False,
        "provider": "local_piper_gateway",
        "default_format": "text_and_voice_in_local_browser",
        "permission_mode": "demo_only",
        "external_delivery": False,
        "note": "Replies are stored in the PIN&PITCH demo inbox. Local speech is available through the optional loopback AI gateway."
    }

@app.post("/voice-replies/generate")
async def generate_voice_reply(payload:dict, claims=Depends(current_claims)):
    raise HTTPException(410, "External voice generation is disabled. Use the optional local PIN&PITCH gateway /speak endpoint from the local browser.")

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
async def run_hybrid_ai_reply(conversation_id:int, claims=Depends(current_claims),db:Session=Depends(get_db)):
    conv=db.query(Conversation).filter_by(id=conversation_id,workspace_id=claims["workspace"]).first()
    if not conv: raise HTTPException(404,"Conversation not found")
    incoming=db.query(Message).filter_by(conversation_id=conv.id,workspace_id=claims["workspace"],direction="inbound").order_by(Message.id.desc()).first()
    if not incoming: raise HTTPException(400,"No inbound customer message to answer.")
    faq=find_faq_reply(claims["workspace"],incoming.body,db)
    if not faq:
        return {"status":"needs_human_approval","reason":"No approved FAQ matched. Add an approved FAQ or have a staff member write and approve the answer.","customer_message":incoming.body}
    answer=faq.answer
    msg=Message(workspace_id=claims["workspace"],conversation_id=conv.id,direction="outbound",body=answer,delivery_status="saved_demo_only",audio_url="")
    conv.last_message=answer
    conv.updated_at=datetime.now(timezone.utc)
    db.add(msg);db.commit();db.refresh(msg)
    await broadcast(claims["workspace"],{"type":"inbox.message","conversation_id":conv.id,"message_id":msg.id})
    return {"status":"saved_demo_only","mode":"approved_faq_match","text":answer,"external_delivery":False,"note":"Saved in the PIN&PITCH demo inbox only. No message or voice note was sent."}

@app.post("/inbox/conversations/{conversation_id}/send-voice-reply")
async def send_approved_voice_reply(conversation_id:int,payload:dict,claims=Depends(current_claims),db:Session=Depends(get_db)):
    conv=db.query(Conversation).filter_by(id=conversation_id,workspace_id=claims["workspace"]).first()
    if not conv: raise HTTPException(404,"Conversation not found")
    answer=str(payload.get("text","")).strip()
    if not answer or len(answer)>4000: raise HTTPException(400,"Approved reply must contain 1–4000 characters.")
    msg=Message(workspace_id=claims["workspace"],conversation_id=conv.id,direction="outbound",body=answer,delivery_status="saved_demo_only",audio_url="")
    conv.last_message=answer
    conv.updated_at=datetime.now(timezone.utc)
    db.add(msg);db.commit();db.refresh(msg)
    await broadcast(claims["workspace"],{"type":"inbox.message","conversation_id":conv.id,"message_id":msg.id})
    return {"status":"saved_demo_only","text":answer,"audio_url":"","external_delivery":False,"note":"Saved in the PIN&PITCH demo inbox only. No text message or voice note was sent."}



# Workspace-scoped CRM, appointment and saved agent-conversation APIs.
LEAD_STATUSES = {"new", "qualified", "customer", "lost"}
APPOINTMENT_STATUSES = {"scheduled", "completed", "cancelled"}

def _parse_datetime(value):
    if not value:
        raise HTTPException(400, "scheduled_at is required as an ISO-8601 date/time.")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(400, "scheduled_at must be a valid ISO-8601 date/time.")

def _contact_dict(row):
    return {"id":row.id,"name":row.name,"email":row.email,"phone":row.phone,"company":row.company,"lead_status":row.lead_status,"source":row.source,"notes":row.notes,"created_at":row.created_at.isoformat()}

@app.get("/contacts")
def list_contacts(q:str|None=None, lead_status:str|None=None, claims=Depends(current_claims), db:Session=Depends(get_db)):
    query=db.query(Contact).filter(Contact.workspace_id==claims["workspace"])
    if lead_status:
        if lead_status not in LEAD_STATUSES: raise HTTPException(400,"Invalid lead_status.")
        query=query.filter(Contact.lead_status==lead_status)
    if q:
        term="%"+q.strip()[:100]+"%"
        query=query.filter((Contact.name.ilike(term))|(Contact.email.ilike(term))|(Contact.phone.ilike(term))|(Contact.company.ilike(term)))
    return [_contact_dict(row) for row in query.order_by(Contact.updated_at.desc()).limit(500).all()]

@app.post("/contacts")
def create_contact(payload:dict, claims=Depends(current_claims), db:Session=Depends(get_db)):
    name=str(payload.get("name","")).strip()
    if not name or len(name)>160: raise HTTPException(400,"Contact name is required (maximum 160 characters).")
    status=str(payload.get("lead_status","new"))
    if status not in LEAD_STATUSES: raise HTTPException(400,"Invalid lead_status.")
    row=Contact(workspace_id=claims["workspace"],name=name,email=str(payload.get("email",""))[:255],phone=str(payload.get("phone",""))[:40],company=str(payload.get("company",""))[:160],lead_status=status,source=str(payload.get("source","manual"))[:80],notes=str(payload.get("notes",""))[:5000])
    db.add(row);db.commit();db.refresh(row)
    return _contact_dict(row)

@app.patch("/contacts/{contact_id}")
def update_contact(contact_id:int,payload:dict,claims=Depends(current_claims),db:Session=Depends(get_db)):
    row=db.query(Contact).filter_by(id=contact_id,workspace_id=claims["workspace"]).first()
    if not row: raise HTTPException(404,"Contact not found.")
    for key,limit in (("name",160),("email",255),("phone",40),("company",160),("source",80),("notes",5000)):
        if key in payload:
            value=str(payload[key]).strip()
            if key=="name" and not value: raise HTTPException(400,"Contact name cannot be empty.")
            setattr(row,key,value[:limit])
    if "lead_status" in payload:
        if payload["lead_status"] not in LEAD_STATUSES: raise HTTPException(400,"Invalid lead_status.")
        row.lead_status=payload["lead_status"]
    row.updated_at=datetime.now(timezone.utc)
    db.commit();db.refresh(row)
    return _contact_dict(row)

def _appointment_dict(row):
    return {"id":row.id,"contact_id":row.contact_id,"customer_name":row.customer_name,"customer_email":row.customer_email,"customer_phone":row.customer_phone,"service":row.service,"scheduled_at":row.scheduled_at.isoformat(),"status":row.status,"notes":row.notes}

@app.get("/appointments")
def list_appointments(status:str|None=None,claims=Depends(current_claims),db:Session=Depends(get_db)):
    query=db.query(Appointment).filter(Appointment.workspace_id==claims["workspace"])
    if status:
        if status not in APPOINTMENT_STATUSES: raise HTTPException(400,"Invalid appointment status.")
        query=query.filter(Appointment.status==status)
    return [_appointment_dict(row) for row in query.order_by(Appointment.scheduled_at.asc()).limit(500).all()]

@app.post("/appointments")
def create_appointment(payload:dict,claims=Depends(current_claims),db:Session=Depends(get_db)):
    contact_id=payload.get("contact_id")
    contact=None
    if contact_id is not None:
        contact=db.query(Contact).filter_by(id=int(contact_id),workspace_id=claims["workspace"]).first()
        if not contact: raise HTTPException(404,"Contact not found in this workspace.")
    name=str(payload.get("customer_name") or (contact.name if contact else "")).strip()
    if not name or len(name)>160: raise HTTPException(400,"Customer name is required.")
    row=Appointment(workspace_id=claims["workspace"],contact_id=contact.id if contact else None,customer_name=name,customer_email=str(payload.get("customer_email") or (contact.email if contact else ""))[:255],customer_phone=str(payload.get("customer_phone") or (contact.phone if contact else ""))[:40],service=str(payload.get("service",""))[:160],scheduled_at=_parse_datetime(payload.get("scheduled_at")),status="scheduled",notes=str(payload.get("notes",""))[:5000])
    db.add(row);db.commit();db.refresh(row)
    return _appointment_dict(row)

@app.patch("/appointments/{appointment_id}")
def update_appointment(appointment_id:int,payload:dict,claims=Depends(current_claims),db:Session=Depends(get_db)):
    row=db.query(Appointment).filter_by(id=appointment_id,workspace_id=claims["workspace"]).first()
    if not row: raise HTTPException(404,"Appointment not found.")
    if "status" in payload:
        if payload["status"] not in APPOINTMENT_STATUSES: raise HTTPException(400,"Status must be scheduled, completed or cancelled.")
        row.status=payload["status"]
    if "scheduled_at" in payload: row.scheduled_at=_parse_datetime(payload["scheduled_at"])
    if "service" in payload: row.service=str(payload["service"])[:160]
    if "notes" in payload: row.notes=str(payload["notes"])[:5000]
    db.commit();db.refresh(row)
    return _appointment_dict(row)

@app.get("/agent-sessions")
def list_agent_sessions(claims=Depends(current_claims),db:Session=Depends(get_db)):
    rows=db.query(AgentSession).filter_by(workspace_id=claims["workspace"]).order_by(AgentSession.updated_at.desc()).limit(200).all()
    return [{"id":r.id,"agent_id":r.agent_id,"title":r.title,"status":r.status,"created_at":r.created_at.isoformat(),"updated_at":r.updated_at.isoformat()} for r in rows]

@app.post("/agent-sessions")
def create_agent_session(payload:dict,claims=Depends(current_claims),db:Session=Depends(get_db)):
    try: agent_id=int(payload.get("agent_id"))
    except (TypeError,ValueError): raise HTTPException(400,"agent_id is required.")
    agent=db.query(Agent).filter_by(id=agent_id,workspace_id=claims["workspace"]).first()
    if not agent: raise HTTPException(404,"Agent not found in this workspace.")
    row=AgentSession(workspace_id=claims["workspace"],agent_id=agent.id,title=str(payload.get("title") or (agent.name+" conversation"))[:180])
    db.add(row);db.commit();db.refresh(row)
    return {"id":row.id,"agent_id":row.agent_id,"title":row.title,"status":row.status}

@app.get("/agent-sessions/{session_id}/turns")
def list_agent_session_turns(session_id:int,claims=Depends(current_claims),db:Session=Depends(get_db)):
    session=db.query(AgentSession).filter_by(id=session_id,workspace_id=claims["workspace"]).first()
    if not session: raise HTTPException(404,"Conversation not found.")
    rows=db.query(AgentSessionTurn).filter_by(session_id=session.id,workspace_id=claims["workspace"]).order_by(AgentSessionTurn.id.asc()).all()
    return [{"id":r.id,"role":r.role,"content":r.content,"created_at":r.created_at.isoformat()} for r in rows]

@app.post("/agent-sessions/{session_id}/turn")
async def add_agent_session_turn(session_id:int,payload:dict,claims=Depends(current_claims),db:Session=Depends(get_db)):
    session=db.query(AgentSession).filter_by(id=session_id,workspace_id=claims["workspace"]).first()
    if not session: raise HTTPException(404,"Conversation not found.")
    message=str(payload.get("message","")).strip()
    if not message or len(message)>8000: raise HTTPException(400,"Message must contain 1–8000 characters.")
    agent=db.query(Agent).filter_by(id=session.agent_id,workspace_id=claims["workspace"]).first()
    user_turn=AgentSessionTurn(workspace_id=claims["workspace"],session_id=session.id,role="user",content=message)
    supplied_answer=payload.get("assistant_reply")
    if isinstance(supplied_answer,str) and supplied_answer.strip():
        answer=supplied_answer.strip()[:8000]
        mode="local_ollama"
    else:
        faq=find_faq_reply(claims["workspace"],message,db)
        if faq:
            answer=faq.answer
            mode="approved_faq"
        else:
            answer="I don't have a verified answer for that yet. I can take your details and have a team member follow up. What is the best name and contact number to reach you?"
            mode="safe_fallback"
    assistant_turn=AgentSessionTurn(workspace_id=claims["workspace"],session_id=session.id,role="assistant",content=answer)
    if session.title=="New conversation":
        session.title=message[:160]
    session.updated_at=datetime.now(timezone.utc)
    db.add_all([user_turn,assistant_turn]);db.commit()
    return {"user_turn":{"role":"user","content":message},"assistant_turn":{"role":"assistant","content":answer},"mode":mode,"agent_name":agent.name if agent else "PIN&PITCH agent","external_action":False,"note":"This hosted fallback uses approved FAQ data only. Connect the optional local gateway for local model inference."}

@app.get("/local-ai/settings")
def local_ai_settings():
    return {"enabled":True,"gateway_url":"http://127.0.0.1:8765","provider":"ollama_local","model":"qwen3:4b","external_api_cost":False,"note":"Gateway is optional and must be running on the same PC as the browser. Hosted HTTPS access may be blocked by browser local-network security."}

@app.get("/analytics")
def analytics(claims=Depends(current_claims),db:Session=Depends(get_db)):
    workspace=claims["workspace"]
    calls=db.query(Call).filter_by(workspace_id=workspace).count()
    contacts=db.query(Contact).filter_by(workspace_id=workspace).count()
    appointments=db.query(Appointment).filter_by(workspace_id=workspace).count()
    conversations=db.query(Conversation).filter_by(workspace_id=workspace).count()
    messages=db.query(Message).filter_by(workspace_id=workspace).count()
    sessions=db.query(AgentSession).filter_by(workspace_id=workspace).count()
    upcoming=db.query(Appointment).filter(Appointment.workspace_id==workspace,Appointment.status=="scheduled",Appointment.scheduled_at>=datetime.now(timezone.utc).replace(tzinfo=None)).count()
    return {"period":"all_time","calls":calls,"contacts":contacts,"appointments":appointments,"upcoming_appointments":upcoming,"conversations":conversations,"messages":messages,"agent_sessions":sessions,"lead_statuses":{status:db.query(Contact).filter_by(workspace_id=workspace,lead_status=status).count() for status in sorted(LEAD_STATUSES)},"demo_only":True}

STATIC_DIR=os.path.join(os.path.dirname(os.path.dirname(__file__)),"static")
@app.get("/",include_in_schema=False)
def app_home(): return FileResponse(os.path.join(STATIC_DIR,"index.html"))
app.mount("/",StaticFiles(directory=STATIC_DIR,html=True),name="pwa")
