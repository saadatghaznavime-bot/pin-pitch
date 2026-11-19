from abc import ABC, abstractmethod
from uuid import uuid4

class TelephonyProvider(ABC):
    @abstractmethod
    def create_call(self, direction: str, phone_number: str, agent_id: int) -> dict:
        raise NotImplementedError

class MockTelephonyProvider(TelephonyProvider):
    """Fictional provider: creates demo call records; never dials a real number."""
    def create_call(self, direction: str, phone_number: str, agent_id: int) -> dict:
        return {
            "provider": "mock",
            "provider_call_id": "demo_" + uuid4().hex[:12],
            "direction": direction,
            "phone_number": phone_number,
            "agent_id": agent_id,
            "status": "completed",
            "duration_seconds": 42 if direction == "inbound" else 36,
            "summary": f"Simulated {direction} conversation with a PIN&PITCH demo AI agent.",
            "transcript": (
                "AI: Hello, thanks for contacting the business. How can I help?\n"
                "Caller: I'd like some information, please.\n"
                "AI: Absolutely. I can help with that.\n"
                "Caller: Thank you.\n"
                "AI: You're welcome. Have a great day."
            )
        }
