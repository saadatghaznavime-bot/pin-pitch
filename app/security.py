import os
from datetime import datetime, timedelta, timezone
from jose import jwt
from passlib.context import CryptContext

pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
SECRET = os.getenv("JWT_SECRET", "dev-only-change-this-secret")
ALGORITHM = "HS256"

def hash_password(password: str) -> str:
    return pwd.hash(password)

def verify_password(password: str, hashed: str) -> bool:
    return pwd.verify(password, hashed)

def create_token(user_id: int, workspace_id: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=int(os.getenv("JWT_EXPIRE_MINUTES", "60")))
    return jwt.encode({"sub": str(user_id), "workspace": workspace_id, "exp": exp}, SECRET, algorithm=ALGORITHM)

def decode_token(token: str):
    return jwt.decode(token, SECRET, algorithms=[ALGORITHM])
