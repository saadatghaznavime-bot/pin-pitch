import os
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("JWT_SECRET", "pinpitch-test-secret-not-for-production")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.db import Base, get_db

test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)
Base.metadata.create_all(bind=test_engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)


def register(email):
    response = client.post("/auth/register", json={"email": email, "password": "pinpitch-test-123"})
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def test_workspace_crm_appointments_sessions_and_analytics_are_scoped():
    owner_headers = register("owner@example.test")
    other_headers = register("other@example.test")

    agent = client.post(
        "/agents",
        headers=owner_headers,
        json={"name": "Demo Receptionist", "instructions": "Be helpful.", "voice": "alloy", "enabled": True},
    )
    assert agent.status_code == 200
    agent_id = agent.json()["id"]

    contact = client.post(
        "/contacts",
        headers=owner_headers,
        json={"name": "Jamie Customer", "email": "jamie@example.test", "lead_status": "qualified"},
    )
    assert contact.status_code == 200
    contact_id = contact.json()["id"]

    appointment = client.post(
        "/appointments",
        headers=owner_headers,
        json={"contact_id": contact_id, "service": "Consultation", "scheduled_at": "2030-01-02T10:00:00Z"},
    )
    assert appointment.status_code == 200
    assert appointment.json()["customer_name"] == "Jamie Customer"

    session = client.post(
        "/agent-sessions",
        headers=owner_headers,
        json={"agent_id": agent_id},
    )
    assert session.status_code == 200
    session_id = session.json()["id"]

    turn = client.post(
        f"/agent-sessions/{session_id}/turn",
        headers=owner_headers,
        json={"message": "Can you tell me a price you do not know?"},
    )
    assert turn.status_code == 200
    assert turn.json()["mode"] == "safe_fallback"

    turns = client.get(f"/agent-sessions/{session_id}/turns", headers=owner_headers)
    assert turns.status_code == 200
    assert len(turns.json()) == 2

    analytics = client.get("/analytics", headers=owner_headers)
    assert analytics.status_code == 200
    assert analytics.json()["contacts"] == 1
    assert analytics.json()["appointments"] == 1
    assert analytics.json()["agent_sessions"] == 1

    other_contacts = client.get("/contacts", headers=other_headers)
    assert other_contacts.status_code == 200
    assert other_contacts.json() == []


def test_external_delivery_is_disabled():
    headers = register("demo@example.test")
    settings = client.get("/voice-replies/settings", headers=headers)
    assert settings.status_code == 200
    assert settings.json()["external_delivery"] is False
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["external_providers_enabled"] is False
