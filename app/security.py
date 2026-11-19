
import os
import hashlib
import secrets
import hmac
from datetime import datetime, timedelta, timezone
from jose import jwt

SECRET = os.getenv("JWT_SECRET", "dev-only-change-this-secret")
ALGORITHM = "HS256"
ITERATIONS = 600_000

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    hashed = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, ITERATIONS
    )
    return "pbkdf2_sha256${}${}${}".format(
        ITERATIONS,
        salt.hex(),
        hashed.hex()
    )

def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt_hex, hash_hex = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        expected = bytes.fromhex(hash_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False

def create_token(user_id: int, workspace_id: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(
        minutes=int(os.getenv("JWT_EXPIRE_MINUTES", "60"))
    )
    return jwt.encode(
        {"sub": str(user_id), "workspace": workspace_id, "exp": exp},
        SECRET,
        algorithm=ALGORITHM
    )

def decode_token(token: str):
    return jwt.decode(token, SECRET, algorithms=[ALGORITHM])
