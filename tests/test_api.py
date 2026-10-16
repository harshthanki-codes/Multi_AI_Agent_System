import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.db.database import get_db
from app.db.models import Base
from app.llm.client import MockLLMClient

# Setup test DB with StaticPool so all connections share the same in-memory database
engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


def override_get_llm():
    return MockLLMClient()


from app.llm.client import get_llm_client

app.dependency_overrides[get_db] = override_get_db
app.dependency_overrides[get_llm_client] = override_get_llm
client = TestClient(app)


def test_api_session_lifecycle():
    # 1. Create Session
    create_res = client.post("/api/sessions", json={
        "topic": "Should all developers be required to write unit tests?",
        "max_rounds": 1
    })
    assert create_res.status_code == 201
    session_data = create_res.json()
    session_id = session_data["id"]
    assert session_data["status"] == "CREATED"

    # 2. Advance to MODERATOR_OPENING
    adv_res = client.post(f"/api/sessions/{session_id}/advance")
    assert adv_res.status_code == 200
    assert adv_res.json()["status"] == "MODERATOR_OPENING"
    assert len(adv_res.json()["messages"]) == 1

    # 3. Auto-run until human decision
    autorun_res = client.post(f"/api/sessions/{session_id}/auto-run")
    assert autorun_res.status_code == 200
    auto_data = autorun_res.json()
    assert auto_data["status"] == "AWAITING_HUMAN_DECISION"

    # 4. Fetch Verdict
    verdict_res = client.get(f"/api/sessions/{session_id}/verdict")
    assert verdict_res.status_code == 200
    v_data = verdict_res.json()
    assert "strongest_supporting" in v_data
    assert "final_assessment" in v_data

    # 5. Submit Human Decision
    dec_res = client.post(f"/api/sessions/{session_id}/decision", json={
        "decision": "ACCEPTED",
        "rationale": "Mandate unit tests for all core payment and auth services."
    })
    assert dec_res.status_code == 200
    assert dec_res.json()["decision"] == "ACCEPTED"

    # 6. Verify Completed Session
    final_get = client.get(f"/api/sessions/{session_id}")
    assert final_get.status_code == 200
    assert final_get.json()["status"] == "COMPLETED"
    assert final_get.json()["decision"]["decision"] == "ACCEPTED"


def test_api_invalid_topic():
    res = client.post("/api/sessions", json={"topic": "abc", "max_rounds": 1})
    assert res.status_code == 422  # Validation error from pydantic (too short)
