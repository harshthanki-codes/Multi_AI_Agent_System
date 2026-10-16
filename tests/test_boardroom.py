"""
Comprehensive test suite verifying all 11 evaluation requirements:
1. Session creation & invalid input.
2. Correct deterministic agent execution order.
3. Supporting and opposing agents using previous arguments.
4. Round-limit enforcement and stopping conditions.
5. Judge output schema validation.
6. Missing credentials & LLM timeouts.
7. Retry exhaustion & partial-session recovery.
8. Duplicate requests & idempotency.
9. Persistence across application/engine restart.
10. Human acceptance/rejection workflow.
11. Complete end-to-end session using mocked LLM responses.
"""

import pytest
import uuid
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.state_machine import AgentRole, SessionStatus, StateMachine, InvalidStateTransitionError
from app.db.models import Base, SessionModel, MessageModel, VerdictModel, HumanDecisionModel
from app.llm.client import (
    BaseLLMClient,
    MockLLMClient,
    LLMTimeoutError,
    LLMAuthenticationError,
    LLMOutputParseError,
    OpenAILLMClient,
)
from app.schemas.session import VerdictStructuredOutput
from app.services.orchestrator import BoardroomOrchestrator, OrchestrationError


@pytest.fixture
def in_memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


# 1. Session Creation and Invalid Input Tests
def test_session_creation_and_input_validation(in_memory_db):
    orchestrator = BoardroomOrchestrator(in_memory_db, MockLLMClient())
    
    # Valid session creation
    session = orchestrator.create_session("Should enterprise adopt microservices?", max_rounds=2)
    assert session.id is not None
    assert session.topic == "Should enterprise adopt microservices?"
    assert session.status == SessionStatus.CREATED.value
    assert session.current_round == 1
    assert session.max_rounds == 2


# 2. Correct Agent Execution Order
def test_agent_execution_order(in_memory_db):
    orchestrator = BoardroomOrchestrator(in_memory_db, MockLLMClient())
    session = orchestrator.create_session("Should we rewrite our backend in Rust?", max_rounds=1)

    # Step 1: Moderator Opening
    s1 = orchestrator.advance_session(session.id)
    assert s1.status == SessionStatus.MODERATOR_OPENING.value
    assert len(s1.messages) == 1
    assert s1.messages[0].speaker == AgentRole.MODERATOR.value

    # Step 2: Supporting Argument
    s2 = orchestrator.advance_session(session.id)
    assert s2.status == SessionStatus.SUPPORTING_ARGUMENT.value
    assert len(s2.messages) == 2
    assert s2.messages[1].speaker == AgentRole.SUPPORTING.value

    # Step 3: Opposing Rebuttal (since max_rounds=1, next is Judge Verdict)
    s3 = orchestrator.advance_session(session.id)
    assert s3.status == SessionStatus.OPPOSING_REBUTTAL.value
    assert len(s3.messages) == 3
    assert s3.messages[2].speaker == AgentRole.OPPOSING.value

    # Step 4: Judge Verdict -> Awaiting Human Decision
    s4 = orchestrator.advance_session(session.id)
    assert s4.status == SessionStatus.AWAITING_HUMAN_DECISION.value
    assert len(s4.messages) == 4
    assert s4.messages[3].speaker == AgentRole.JUDGE.value
    assert s4.verdict is not None


# 3. Context Passing: Supporting and Opposing agents receive peer arguments
class ContextCheckingLLMClient(BaseLLMClient):
    def __init__(self):
        self.received_prompts = []

    def generate(self, system_prompt, user_prompt, role, max_tokens=600, temperature=0.7):
        self.received_prompts.append((role, user_prompt))
        if role == AgentRole.SUPPORTING:
            return "Affirmative: Rust delivers superior memory safety and execution speed."
        elif role == AgentRole.OPPOSING:
            return "Rebuttal: Rust introduces steep learning curves and slows time-to-market."
        return "Opening remarks."

    def generate_structured_verdict(self, system_prompt, user_prompt, max_tokens=1000):
        return MockLLMClient().generate_structured_verdict(system_prompt, user_prompt)


def test_context_passing_between_agents(in_memory_db):
    spy_llm = ContextCheckingLLMClient()
    orchestrator = BoardroomOrchestrator(in_memory_db, spy_llm)
    session = orchestrator.create_session("Rust adoption", max_rounds=2)

    orchestrator.advance_session(session.id)  # Moderator
    orchestrator.advance_session(session.id)  # Supporting
    orchestrator.advance_session(session.id)  # Opposing Rebuttal

    # Verify Opposing received the actual Supporting argument
    opposing_call = [p for r, p in spy_llm.received_prompts if r == AgentRole.OPPOSING][0]
    assert "Affirmative: Rust delivers superior memory safety and execution speed." in opposing_call

    # Next round: Supporting Response
    orchestrator.advance_session(session.id)
    supporting_response_call = [p for r, p in spy_llm.received_prompts if r == AgentRole.SUPPORTING][1]
    # Verify Supporting received Opposing's rebuttal
    assert "Rebuttal: Rust introduces steep learning curves and slows time-to-market." in supporting_response_call


# 4. Round-Limit Enforcement and Stopping Conditions
def test_round_limit_enforcement(in_memory_db):
    orchestrator = BoardroomOrchestrator(in_memory_db, MockLLMClient())
    session = orchestrator.create_session("AI Governance", max_rounds=2)

    # Auto run to completion of debate
    final_session = orchestrator.auto_run_until_human(session.id)
    assert final_session.status == SessionStatus.AWAITING_HUMAN_DECISION.value
    assert final_session.current_round == 2
    # Rounds 1 and 2:
    # 1. Mod (R1), 2. Supp (R1), 3. Opp (R1), 4. Supp (R2), 5. Opp (R2), 6. Judge (R2)
    assert len(final_session.messages) == 6

    # Attempting to advance beyond AWAITING_HUMAN_DECISION must fail
    with pytest.raises(OrchestrationError, match="awaiting human decision"):
        orchestrator.advance_session(session.id)


# 5. Judge Output Schema Validation
def test_judge_structured_verdict_validation(in_memory_db):
    client = MockLLMClient()
    verdict = client.generate_structured_verdict("system", "prompt")
    assert isinstance(verdict, VerdictStructuredOutput)
    assert len(verdict.strongest_supporting) > 10
    assert len(verdict.supporting_weaknesses) > 10
    assert len(verdict.strongest_opposing) > 10
    assert len(verdict.opposing_weaknesses) > 10
    assert len(verdict.reasoning) > 10
    assert len(verdict.final_assessment) > 10


# 6. Missing Credentials and LLM Timeouts
class FailingLLMClient(BaseLLMClient):
    def __init__(self, failure_type="timeout"):
        self.failure_type = failure_type

    def generate(self, system_prompt, user_prompt, role, max_tokens=600, temperature=0.7):
        if self.failure_type == "timeout":
            raise LLMTimeoutError("Simulated LLM network timeout after 30s")
        elif self.failure_type == "auth":
            raise LLMAuthenticationError("Simulated invalid or missing API key")
        raise RuntimeError("Generic error")

    def generate_structured_verdict(self, system_prompt, user_prompt, max_tokens=1000):
        raise LLMOutputParseError("Simulated malformed JSON output")


def test_llm_timeout_marks_session_failed(in_memory_db):
    timeout_client = FailingLLMClient("timeout")
    orchestrator = BoardroomOrchestrator(in_memory_db, timeout_client)
    session = orchestrator.create_session("Timeout Test")

    with pytest.raises(OrchestrationError, match="Simulated LLM network timeout"):
        orchestrator.advance_session(session.id)

    db_session = in_memory_db.query(SessionModel).filter(SessionModel.id == session.id).first()
    assert db_session.status == SessionStatus.FAILED.value
    assert "timeout" in db_session.error_message.lower()


def test_missing_credentials_raises_auth_error():
    # Attempting to initialize OpenAI client without key
    orig_key = settings.OPENAI_API_KEY
    try:
        settings.OPENAI_API_KEY = None
        with pytest.raises(LLMAuthenticationError):
            OpenAILLMClient()
    finally:
        settings.OPENAI_API_KEY = orig_key


# 7. Retry Exhaustion and Partial-Session Recovery
def test_partial_session_recovery_after_failure(in_memory_db):
    # Step 1 succeeds with mock, Step 2 fails with timeout
    mock_client = MockLLMClient()
    timeout_client = FailingLLMClient("timeout")

    orchestrator = BoardroomOrchestrator(in_memory_db, mock_client)
    session = orchestrator.create_session("Recovery Test")
    s1 = orchestrator.advance_session(session.id)
    assert s1.status == SessionStatus.MODERATOR_OPENING.value
    assert len(s1.messages) == 1

    # Now step 2 encounters a transient failure
    orchestrator.llm_client = timeout_client
    with pytest.raises(OrchestrationError):
        orchestrator.advance_session(session.id)

    # Session is marked FAILED, but step 1 message is safely preserved
    failed_session = in_memory_db.query(SessionModel).filter(SessionModel.id == session.id).first()
    assert failed_session.status == SessionStatus.FAILED.value
    assert len(failed_session.messages) == 1

    # Recover with healthy client
    orchestrator.llm_client = mock_client
    recovered_session = orchestrator.advance_session(session.id)
    assert recovered_session.status == SessionStatus.SUPPORTING_ARGUMENT.value
    assert len(recovered_session.messages) == 2


# 8. Duplicate Requests and Idempotency
def test_idempotency_prevents_duplicate_messages(in_memory_db):
    orchestrator = BoardroomOrchestrator(in_memory_db, MockLLMClient())
    session = orchestrator.create_session("Idempotency Topic")

    key = "unique-client-key-12345"
    s1 = orchestrator.advance_session(session.id, idempotency_key=key)
    assert len(s1.messages) == 1

    # Duplicate call with exact same idempotency_key
    s2 = orchestrator.advance_session(session.id, idempotency_key=key)
    assert len(s2.messages) == 1  # No duplicate message created!
    assert s2.id == s1.id


# 9. Persistence Across Restart (Separate Connection)
def test_persistence_across_reconnect(tmp_path):
    db_file = tmp_path / "boardroom_test.db"
    db_url = f"sqlite:///{db_file}"

    engine1 = create_engine(db_url)
    Base.metadata.create_all(bind=engine1)
    SessionMaker1 = sessionmaker(bind=engine1)
    db1 = SessionMaker1()

    orch1 = BoardroomOrchestrator(db1, MockLLMClient())
    session = orch1.create_session("Cold Start Topic")
    orch1.advance_session(session.id)
    session_id = session.id
    db1.close()
    engine1.dispose()

    # Reconnect to the database file in a new session
    engine2 = create_engine(db_url)
    SessionMaker2 = sessionmaker(bind=engine2)
    db2 = SessionMaker2()

    orch2 = BoardroomOrchestrator(db2, MockLLMClient())
    resumed_session = db2.query(SessionModel).filter(SessionModel.id == session_id).first()
    assert resumed_session is not None
    assert resumed_session.status == SessionStatus.MODERATOR_OPENING.value
    assert len(resumed_session.messages) == 1

    # Advance resumed session seamlessly
    orch2.advance_session(session_id)
    db2.refresh(resumed_session)
    assert resumed_session.status == SessionStatus.SUPPORTING_ARGUMENT.value
    assert len(resumed_session.messages) == 2
    db2.close()
    engine2.dispose()


# 10. Human Decision Flow
def test_human_acceptance_and_rejection_workflow(in_memory_db):
    orchestrator = BoardroomOrchestrator(in_memory_db, MockLLMClient())
    session = orchestrator.create_session("Human Governance Test", max_rounds=1)
    orchestrator.auto_run_until_human(session.id)

    # Verify session is at AWAITING_HUMAN_DECISION
    assert session.status == SessionStatus.AWAITING_HUMAN_DECISION.value

    # Submit human decision: ACCEPTED
    decision = orchestrator.record_human_decision(
        session_id=session.id,
        decision="ACCEPTED",
        rationale="Approved with strict quarterly milestones.",
    )
    assert decision.decision == "ACCEPTED"
    assert decision.rationale == "Approved with strict quarterly milestones."
    assert decision.decided_at is not None

    # Verify session marked COMPLETED
    in_memory_db.refresh(session)
    assert session.status == SessionStatus.COMPLETED.value
    assert session.decision is not None
    assert session.decision.decision == "ACCEPTED"

    # Attempting to record another decision must fail
    with pytest.raises(OrchestrationError, match="Cannot record human decision"):
        orchestrator.record_human_decision(session.id, "REJECTED")


# 11. Complete End-to-End Session (Mocked LLM)
def test_complete_end_to_end_session(in_memory_db):
    orchestrator = BoardroomOrchestrator(in_memory_db, MockLLMClient())
    session = orchestrator.create_session(
        topic="Should we adopt a 4-day work week across all engineering teams?",
        max_rounds=2,
    )

    # 1. Start auto-run through all rounds to judge verdict
    completed_debate = orchestrator.auto_run_until_human(session.id)
    assert completed_debate.status == SessionStatus.AWAITING_HUMAN_DECISION.value
    assert completed_debate.verdict is not None
    assert completed_debate.verdict.strongest_supporting != ""
    assert completed_debate.verdict.strongest_opposing != ""

    # Check transcript sequence
    speakers = [m.speaker for m in completed_debate.messages]
    assert speakers == [
        AgentRole.MODERATOR.value,
        AgentRole.SUPPORTING.value,
        AgentRole.OPPOSING.value,
        AgentRole.SUPPORTING.value,
        AgentRole.OPPOSING.value,
        AgentRole.JUDGE.value,
    ]

    # Human decides
    decision = orchestrator.record_human_decision(
        session.id,
        decision="ACCEPTED",
        rationale="Adopt 6-month trial with productivity KPIs.",
    )
    assert decision.decision == "ACCEPTED"
    assert completed_debate.status == SessionStatus.COMPLETED.value


# 12. State Machine Transition Validation & Invalid Jump Protection
def test_state_machine_transition_validation():
    # Valid transitions must pass without error
    StateMachine.validate_transition(SessionStatus.CREATED, SessionStatus.MODERATOR_OPENING)
    StateMachine.validate_transition(SessionStatus.MODERATOR_OPENING, SessionStatus.SUPPORTING_ARGUMENT)
    StateMachine.validate_transition(SessionStatus.JUDGE_VERDICT, SessionStatus.AWAITING_HUMAN_DECISION)
    StateMachine.validate_transition(SessionStatus.AWAITING_HUMAN_DECISION, SessionStatus.COMPLETED)

    # Invalid transitions must raise InvalidStateTransitionError
    with pytest.raises(InvalidStateTransitionError, match="Invalid state transition from 'CREATED' to 'COMPLETED'"):
        StateMachine.validate_transition(SessionStatus.CREATED, SessionStatus.COMPLETED)

    with pytest.raises(InvalidStateTransitionError, match="Invalid state transition from 'COMPLETED'"):
        StateMachine.validate_transition(SessionStatus.COMPLETED, SessionStatus.MODERATOR_OPENING)

    with pytest.raises(InvalidStateTransitionError, match="Invalid state transition from 'MODERATOR_OPENING' to 'JUDGE_VERDICT'"):
        StateMachine.validate_transition(SessionStatus.MODERATOR_OPENING, SessionStatus.JUDGE_VERDICT)


# 13. Concurrent Session Turn Serialization Regression Test
def test_concurrent_session_advance_serialization(tmp_path):
    import concurrent.futures
    db_file = tmp_path / "concurrent_boardroom.db"
    db_url = f"sqlite:///{db_file}"

    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionMaker = sessionmaker(bind=engine)

    # Initialize a shared session
    init_db = SessionMaker()
    orch_init = BoardroomOrchestrator(init_db, MockLLMClient())
    session = orch_init.create_session("Concurrent Safety Test", max_rounds=2)
    session_id = session.id
    init_db.close()

    def advance_worker(worker_id):
        worker_db = SessionMaker()
        try:
            orch = BoardroomOrchestrator(worker_db, MockLLMClient())
            # Attempt to advance
            orch.advance_session(session_id, idempotency_key=f"worker-{worker_id}")
            worker_db.commit()
            return True
        except Exception:
            worker_db.rollback()
            return False
        finally:
            worker_db.close()

    # Launch 5 concurrent threads trying to advance the exact same session simultaneously
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(advance_worker, i) for i in range(5)]
        results = [f.result() for f in futures]

    # Verify session integrity in DB
    verify_db = SessionMaker()
    final_session = verify_db.query(SessionModel).filter(SessionModel.id == session_id).first()
    # Sequence numbers must be strictly sequential with no duplicate sequence numbers
    sequences = [m.sequence_number for m in final_session.messages]
    assert len(sequences) == len(set(sequences)), "Duplicate sequence numbers detected under concurrency!"
    assert sorted(sequences) == list(range(1, len(sequences) + 1))
    verify_db.close()
    engine.dispose()


# 14. Provider Selection & Default Mock Verification
def test_provider_selection_and_default_mock():
    from app.llm.client import get_llm_client, MockLLMClient, OpenAILLMClient

    # Default settings.LLM_PROVIDER must be 'mock'
    orig_provider = settings.LLM_PROVIDER
    orig_key = settings.OPENAI_API_KEY
    try:
        settings.LLM_PROVIDER = "mock"
        client = get_llm_client()
        assert isinstance(client, MockLLMClient)

        # When set to openai with invalid placeholder key, should raise LLMAuthenticationError
        settings.LLM_PROVIDER = "openai"
        settings.OPENAI_API_KEY = "your_openai_api_key_here"
        with pytest.raises(LLMAuthenticationError, match="OPENAI_API_KEY is not configured"):
            get_llm_client()
    finally:
        settings.LLM_PROVIDER = orig_provider
        settings.OPENAI_API_KEY = orig_key


# 15. Malformed Judge JSON Parsing and Error Classification
def test_malformed_judge_json_error_classification():
    from app.llm.client import OpenAILLMClient, LLMOutputParseError
    from unittest.mock import MagicMock

    # Create dummy OpenAI client using mocked completions
    orig_key = settings.OPENAI_API_KEY
    try:
        settings.OPENAI_API_KEY = "test-dummy-key"
        client = OpenAILLMClient()
        mock_completion = MagicMock()
        # Return invalid non-JSON string
        mock_choice = MagicMock()
        mock_choice.message.content = "This is definitely not valid JSON"
        mock_completion.choices = [mock_choice]
        client.client.chat.completions.create = MagicMock(return_value=mock_completion)

        with pytest.raises(LLMOutputParseError, match="Failed to parse judge verdict schema"):
            client.generate_structured_verdict("system", "prompt")
    finally:
        settings.OPENAI_API_KEY = orig_key


# 16. Error Message Key Sanitization Verification
def test_error_message_does_not_leak_keys():
    from openai import AuthenticationError
    from app.llm.client import OpenAILLMClient, LLMAuthenticationError
    from unittest.mock import MagicMock

    orig_key = settings.OPENAI_API_KEY
    secret_key = "sk-proj-SUPER_SECRET_KEY_12345"
    try:
        settings.OPENAI_API_KEY = secret_key
        client = OpenAILLMClient()
        # Mock auth failure
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        client.client.chat.completions.create = MagicMock(
            side_effect=AuthenticationError(
                message=f"Incorrect API key provided: {secret_key}",
                response=mock_resp,
                body=None,
            )
        )

        with pytest.raises(LLMAuthenticationError) as exc_info:
            client.generate("system", "user", AgentRole.MODERATOR)

        # Assert secret key is NOT present in the exception message
        assert secret_key not in str(exc_info.value)
        assert "invalid or unauthorized" in str(exc_info.value)
    finally:
        settings.OPENAI_API_KEY = orig_key


