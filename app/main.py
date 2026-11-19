import os, uuid, asyncio
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from jose import JWTError
from .db import Base, engine, get_db
from .models import User, Agent, Call
from .schemas import AuthIn, TokenOut, AgentIn, AgentOut, DemoCallIn
from .security import hash_password, verify_password, create_token, decode_token
from .providers import MockTelephonyProvider
from .voice import router as voice_router

Base.metadata.create_all(bind=engine)
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

STATIC_DIR=os.path.join(os.path.dirname(os.path.dirname(__file__)),"static")
@app.get("/",include_in_schema=False)
def app_home(): return FileResponse(os.path.join(STATIC_DIR,"index.html"))
app.mount("/",StaticFiles(directory=STATIC_DIR,html=True),name="pwa")
