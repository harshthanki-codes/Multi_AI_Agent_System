import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import SessionModel, VerdictModel
from app.llm.client import BaseLLMClient, get_llm_client
from app.services.orchestrator import BoardroomOrchestrator, OrchestrationError
from app.schemas.session import (
    SessionCreate,
    SessionResponse,
    AdvanceSessionRequest,
    VerdictResponse,
    HumanDecisionCreate,
    HumanDecisionResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.post("", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
def create_session(payload: SessionCreate, db: Session = Depends(get_db)):
    """Create a new boardroom discussion session."""
    orchestrator = BoardroomOrchestrator(db)
    session = orchestrator.create_session(
        topic=payload.topic,
        max_rounds=payload.max_rounds,
    )
    return session


@router.get("", response_model=List[SessionResponse])
def list_sessions(limit: int = 20, db: Session = Depends(get_db)):
    """List recent boardroom sessions."""
    sessions = (
        db.query(SessionModel)
        .order_by(SessionModel.created_at.desc())
        .limit(limit)
        .all()
    )
    return sessions


@router.get("/{session_id}", response_model=SessionResponse)
def get_session(session_id: str, db: Session = Depends(get_db)):
    """Retrieve full details, messages, and state of a boardroom session."""
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Boardroom session '{session_id}' not found.",
        )
    return session


@router.post("/{session_id}/advance", response_model=SessionResponse)
def advance_session(
    session_id: str,
    payload: Optional[AdvanceSessionRequest] = None,
    db: Session = Depends(get_db),
    llm: BaseLLMClient = Depends(get_llm_client),
):
    """
    Advance the session by one discrete agent turn.
    Idempotent when idempotency_key is provided.
    """
    orchestrator = BoardroomOrchestrator(db, llm_client=llm)
    idempotency_key = payload.idempotency_key if payload else None
    try:
        session = orchestrator.advance_session(
            session_id=session_id,
            idempotency_key=idempotency_key,
        )
        return session
    except OrchestrationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.exception(f"Unhandled error advancing session {session_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/{session_id}/auto-run", response_model=SessionResponse)
def auto_run_session(
    session_id: str,
    db: Session = Depends(get_db),
    llm: BaseLLMClient = Depends(get_llm_client),
):
    """
    Auto-advance the session across all configured rounds until reaching
    AWAITING_HUMAN_DECISION or a terminal/failed state.
    """
    orchestrator = BoardroomOrchestrator(db, llm_client=llm)
    try:
        session = orchestrator.auto_run_until_human(session_id)
        return session
    except OrchestrationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.exception(f"Error during auto-run for session {session_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/{session_id}/verdict", response_model=VerdictResponse)
def get_verdict(session_id: str, db: Session = Depends(get_db)):
    """Retrieve the AI Judge's structured verdict for the session."""
    verdict = db.query(VerdictModel).filter(VerdictModel.session_id == session_id).first()
    if not verdict:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No verdict generated yet for session '{session_id}'.",
        )
    return verdict


@router.post("/{session_id}/decision", response_model=HumanDecisionResponse)
def submit_human_decision(
    session_id: str,
    payload: HumanDecisionCreate,
    db: Session = Depends(get_db),
):
    """
    Submit the final human decision (ACCEPTED or REJECTED) to conclude the session.
    """
    orchestrator = BoardroomOrchestrator(db)
    try:
        decision = orchestrator.record_human_decision(
            session_id=session_id,
            decision=payload.decision,
            rationale=payload.rationale,
        )
        return decision
    except OrchestrationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.exception(f"Error recording decision for session {session_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/{session_id}/retry", response_model=SessionResponse)
def retry_failed_session(
    session_id: str,
    db: Session = Depends(get_db),
    llm: BaseLLMClient = Depends(get_llm_client),
):
    """
    Recover and retry a session that previously stalled or failed due to transient errors.
    """
    orchestrator = BoardroomOrchestrator(db, llm_client=llm)
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Boardroom session '{session_id}' not found.",
        )
    if session.status != "FAILED":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Session status is '{session.status}', only 'FAILED' sessions can be retried via this endpoint.",
        )

    # Resume from previous state
    # If messages exist, we determine what the next message should be based on messages count/types
    try:
        session = orchestrator.advance_session(session_id)
        return session
    except OrchestrationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
