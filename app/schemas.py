from pydantic import BaseModel, EmailStr, Field

class AuthIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)

class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"

class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    instructions: str = ""
    voice: str = "alloy"
    enabled: bool = False

class AgentOut(AgentIn):
    id: int
    class Config:
        from_attributes = True

class DemoCallIn(BaseModel):
    agent_id: int
    phone_number: str = Field(min_length=3, max_length=40)
