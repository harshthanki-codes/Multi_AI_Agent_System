import logging
import uuid
import threading
from typing import Optional, List, Dict
from contextlib import contextmanager

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.state_machine import AgentRole, SessionStatus, StateMachine, InvalidStateTransitionError
from app.db.models import SessionModel, MessageModel, VerdictModel, HumanDecisionModel
from app.llm.client import BaseLLMClient, get_llm_client, LLMProviderError
from app.llm.prompts.moderator import MODERATOR_SYSTEM_PROMPT_V1, MODERATOR_OPENING_USER_PROMPT
from app.llm.prompts.supporting import (
    SUPPORTING_SYSTEM_PROMPT_V1,
    SUPPORTING_OPENING_PROMPT,
    SUPPORTING_RESPONSE_PROMPT,
)
from app.llm.prompts.opposing import (
    OPPOSING_SYSTEM_PROMPT_V1,
    OPPOSING_REBUTTAL_PROMPT,
    OPPOSING_RESPONSE_PROMPT,
)
from app.llm.prompts.judge import JUDGE_SYSTEM_PROMPT_V1, JUDGE_VERDICT_PROMPT
from app.schemas.session import VerdictStructuredOutput

logger = logging.getLogger(__name__)


class OrchestrationError(Exception):
    pass


class BoardroomOrchestrator:
    # Process-wide lock registry to serialize mutations per session_id
    _session_locks: Dict[str, threading.Lock] = {}
    _registry_lock: threading.Lock = threading.Lock()

    @classmethod
    def _get_session_lock(cls, session_id: str) -> threading.Lock:
        with cls._registry_lock:
            if session_id not in cls._session_locks:
                cls._session_locks[session_id] = threading.Lock()
            return cls._session_locks[session_id]

    def __init__(self, db: Session, llm_client: Optional[BaseLLMClient] = None):
        self.db = db
        self.llm_client = llm_client or get_llm_client()

    def create_session(self, topic: str, max_rounds: int = 2) -> SessionModel:
        session_id = str(uuid.uuid4())
        session = SessionModel(
            id=session_id,
            topic=topic,
            status=SessionStatus.CREATED.value,
            current_round=1,
            max_rounds=max_rounds,
        )
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        logger.info(f"Created boardroom session {session_id} on topic: '{topic}' with max {max_rounds} rounds.")
        return session

    def advance_session(
        self,
        session_id: str,
        idempotency_key: Optional[str] = None,
    ) -> SessionModel:
        """
        Executes exactly one discrete turn of the boardroom debate state machine.
        Idempotent: if an idempotency_key is provided that matches the latest message,
        it avoids duplicating the work. Serialized per session_id.
        """
        lock = self._get_session_lock(session_id)
        with lock:
            return self._advance_session_locked(session_id, idempotency_key)

    def _advance_session_locked(
        self,
        session_id: str,
        idempotency_key: Optional[str] = None,
    ) -> SessionModel:
        # Refresh or fetch current session under lock
        session = self.db.query(SessionModel).filter(SessionModel.id == session_id).first()
        if not session:
            raise OrchestrationError(f"Session '{session_id}' not found.")

        current_status = SessionStatus(session.status)

        # Check terminal or human-blocked states
        if current_status in {SessionStatus.COMPLETED, SessionStatus.TERMINATED}:
            raise OrchestrationError(f"Session is already finished with status '{current_status.value}'.")
        if current_status == SessionStatus.AWAITING_HUMAN_DECISION:
            raise OrchestrationError("Session is awaiting human decision. Call submit_decision endpoint.")

        # Check idempotency
        if idempotency_key:
            existing_msg = (
                self.db.query(MessageModel)
                .filter(
                    MessageModel.session_id == session_id,
                    MessageModel.idempotency_key == idempotency_key,
                )
                .first()
            )
            if existing_msg:
                logger.info(f"Idempotent request detected for key {idempotency_key}. Returning current session state.")
                return session

        # If previous status was FAILED, we determine recovery status based on last message
        if current_status == SessionStatus.FAILED:
            last_msg = (
                self.db.query(MessageModel)
                .filter(MessageModel.session_id == session_id)
                .order_by(MessageModel.sequence_number.desc())
                .first()
            )
            last_role = AgentRole(last_msg.speaker) if last_msg else None
            target_status = StateMachine.determine_recovery_status(
                last_speaker=last_role,
                current_round=session.current_round,
                max_rounds=session.max_rounds,
            )
        else:
            target_status = StateMachine.determine_next_status(
                current_status=current_status,
                current_round=session.current_round,
                max_rounds=session.max_rounds,
            )

        StateMachine.validate_transition(current_status, target_status)
        logger.info(f"Transitioning session {session_id}: {current_status.value} -> {target_status.value}")

        try:
            if target_status == SessionStatus.MODERATOR_OPENING:
                self._execute_moderator_opening(session, idempotency_key)
            elif target_status == SessionStatus.SUPPORTING_ARGUMENT:
                self._execute_supporting_opening(session, idempotency_key)
            elif target_status == SessionStatus.OPPOSING_REBUTTAL:
                self._execute_opposing_rebuttal(session, idempotency_key)
            elif target_status == SessionStatus.SUPPORTING_RESPONSE:
                # Transition to round increment if we completed previous opposing response
                if current_status == SessionStatus.OPPOSING_REBUTTAL:
                    session.current_round = 2
                elif current_status == SessionStatus.OPPOSING_RESPONSE:
                    session.current_round += 1
                self._execute_supporting_response(session, idempotency_key)
            elif target_status == SessionStatus.OPPOSING_RESPONSE:
                self._execute_opposing_response(session, idempotency_key)
            elif target_status == SessionStatus.JUDGE_VERDICT:
                self._execute_judge_verdict(session, idempotency_key)
                session.status = SessionStatus.JUDGE_VERDICT.value
                # Formally transition from JUDGE_VERDICT to AWAITING_HUMAN_DECISION
                StateMachine.validate_transition(
                    SessionStatus.JUDGE_VERDICT, SessionStatus.AWAITING_HUMAN_DECISION
                )
                target_status = SessionStatus.AWAITING_HUMAN_DECISION

            session.status = target_status.value
            session.error_message = None
            self.db.commit()
            self.db.refresh(session)
            return session

        except LLMProviderError as e:
            logger.error(f"LLM failure during session {session_id} at step {target_status.value}: {e}")
            session.status = SessionStatus.FAILED.value
            session.error_message = str(e)
            self.db.commit()
            raise OrchestrationError(f"Session failed during agent execution: {e}") from e
        except Exception as e:
            logger.exception(f"Unexpected error advancing session {session_id}: {e}")
            session.status = SessionStatus.FAILED.value
            session.error_message = str(e)
            self.db.commit()
            raise

    def auto_run_until_human(self, session_id: str) -> SessionModel:
        """Runs consecutive turns until session reaches AWAITING_HUMAN_DECISION or fails."""
        while True:
            session = self.db.query(SessionModel).filter(SessionModel.id == session_id).first()
            if not session:
                raise OrchestrationError(f"Session {session_id} not found.")

            status = SessionStatus(session.status)
            if status in {
                SessionStatus.AWAITING_HUMAN_DECISION,
                SessionStatus.COMPLETED,
                SessionStatus.TERMINATED,
                SessionStatus.FAILED,
            }:
                return session

            session = self.advance_session(session_id)

    def record_human_decision(
        self,
        session_id: str,
        decision: str,
        rationale: Optional[str] = None,
    ) -> HumanDecisionModel:
        lock = self._get_session_lock(session_id)
        with lock:
            session = self.db.query(SessionModel).filter(SessionModel.id == session_id).first()
            if not session:
                raise OrchestrationError(f"Session '{session_id}' not found.")

            current_status = SessionStatus(session.status)
            if current_status != SessionStatus.AWAITING_HUMAN_DECISION:
                raise OrchestrationError(
                    f"Cannot record human decision when session status is '{current_status.value}'."
                )

            decision_upper = decision.upper()
            if decision_upper not in {"ACCEPTED", "REJECTED"}:
                raise OrchestrationError("Decision must be either 'ACCEPTED' or 'REJECTED'.")

            # Check if decision already exists
            existing_dec = (
                self.db.query(HumanDecisionModel)
                .filter(HumanDecisionModel.session_id == session_id)
                .first()
            )
            if existing_dec:
                raise OrchestrationError("Human decision has already been recorded for this session.")

            decision_record = HumanDecisionModel(
                id=str(uuid.uuid4()),
                session_id=session_id,
                decision=decision_upper,
                rationale=rationale,
            )
            self.db.add(decision_record)

            # Record human entry in messages timeline
            next_seq = self._get_next_sequence_number(session_id)
            msg = MessageModel(
                id=str(uuid.uuid4()),
                session_id=session_id,
                speaker=AgentRole.HUMAN.value,
                round=session.current_round,
                sequence_number=next_seq,
                content=f"Human Decision: {decision_upper}. Note: {rationale or 'None'}",
                status="COMPLETED",
            )
            self.db.add(msg)

            # Formally transition from AWAITING_HUMAN_DECISION to COMPLETED
            StateMachine.validate_transition(current_status, SessionStatus.COMPLETED)
            session.status = SessionStatus.COMPLETED.value
            self.db.commit()
            self.db.refresh(decision_record)
            return decision_record

    # --- Step Executors ---

    def _execute_moderator_opening(self, session: SessionModel, idempotency_key: Optional[str]):
        user_prompt = MODERATOR_OPENING_USER_PROMPT.format(
            topic=session.topic,
            max_rounds=session.max_rounds,
        )
        content = self.llm_client.generate(
            system_prompt=MODERATOR_SYSTEM_PROMPT_V1,
            user_prompt=user_prompt,
            role=AgentRole.MODERATOR,
            max_tokens=settings.MAX_AGENT_TOKENS,
        )
        self._save_message(
            session_id=session.id,
            speaker=AgentRole.MODERATOR,
            round_num=session.current_round,
            content=content,
            idempotency_key=idempotency_key,
        )

    def _execute_supporting_opening(self, session: SessionModel, idempotency_key: Optional[str]):
        user_prompt = SUPPORTING_OPENING_PROMPT.format(topic=session.topic)
        content = self.llm_client.generate(
            system_prompt=SUPPORTING_SYSTEM_PROMPT_V1,
            user_prompt=user_prompt,
            role=AgentRole.SUPPORTING,
            max_tokens=settings.MAX_AGENT_TOKENS,
        )
        self._save_message(
            session_id=session.id,
            speaker=AgentRole.SUPPORTING,
            round_num=session.current_round,
            content=content,
            idempotency_key=idempotency_key,
        )

    def _execute_opposing_rebuttal(self, session: SessionModel, idempotency_key: Optional[str]):
        last_supporting = self._get_last_message_by_speaker(session.id, AgentRole.SUPPORTING)
        content_to_rebut = last_supporting.content if last_supporting else session.topic

        user_prompt = OPPOSING_REBUTTAL_PROMPT.format(
            topic=session.topic,
            round_num=session.current_round,
            supporting_argument=content_to_rebut,
        )
        content = self.llm_client.generate(
            system_prompt=OPPOSING_SYSTEM_PROMPT_V1,
            user_prompt=user_prompt,
            role=AgentRole.OPPOSING,
            max_tokens=settings.MAX_AGENT_TOKENS,
        )
        self._save_message(
            session_id=session.id,
            speaker=AgentRole.OPPOSING,
            round_num=session.current_round,
            content=content,
            idempotency_key=idempotency_key,
        )

    def _execute_supporting_response(self, session: SessionModel, idempotency_key: Optional[str]):
        last_opposing = self._get_last_message_by_speaker(session.id, AgentRole.OPPOSING)
        last_opposing_text = last_opposing.content if last_opposing else "No previous challenge found."
        history_text = self._build_transcript_text(session.id)

        user_prompt = SUPPORTING_RESPONSE_PROMPT.format(
            topic=session.topic,
            round_num=session.current_round,
            history=history_text,
            last_opposing_argument=last_opposing_text,
        )
        content = self.llm_client.generate(
            system_prompt=SUPPORTING_SYSTEM_PROMPT_V1,
            user_prompt=user_prompt,
            role=AgentRole.SUPPORTING,
            max_tokens=settings.MAX_AGENT_TOKENS,
        )
        self._save_message(
            session_id=session.id,
            speaker=AgentRole.SUPPORTING,
            round_num=session.current_round,
            content=content,
            idempotency_key=idempotency_key,
        )

    def _execute_opposing_response(self, session: SessionModel, idempotency_key: Optional[str]):
        last_supporting = self._get_last_message_by_speaker(session.id, AgentRole.SUPPORTING)
        last_supporting_text = last_supporting.content if last_supporting else "No prior argument found."
        history_text = self._build_transcript_text(session.id)

        user_prompt = OPPOSING_RESPONSE_PROMPT.format(
            topic=session.topic,
            round_num=session.current_round,
            history=history_text,
            supporting_argument=last_supporting_text,
        )
        content = self.llm_client.generate(
            system_prompt=OPPOSING_SYSTEM_PROMPT_V1,
            user_prompt=user_prompt,
            role=AgentRole.OPPOSING,
            max_tokens=settings.MAX_AGENT_TOKENS,
        )
        self._save_message(
            session_id=session.id,
            speaker=AgentRole.OPPOSING,
            round_num=session.current_round,
            content=content,
            idempotency_key=idempotency_key,
        )

    def _execute_judge_verdict(self, session: SessionModel, idempotency_key: Optional[str]):
        transcript = self._build_transcript_text(session.id)
        user_prompt = JUDGE_VERDICT_PROMPT.format(
            topic=session.topic,
            rounds_completed=session.current_round,
            transcript=transcript,
        )
        verdict_data: VerdictStructuredOutput = self.llm_client.generate_structured_verdict(
            system_prompt=JUDGE_SYSTEM_PROMPT_V1,
            user_prompt=user_prompt,
            max_tokens=1000,
        )

        verdict_model = VerdictModel(
            id=str(uuid.uuid4()),
            session_id=session.id,
            strongest_supporting=verdict_data.strongest_supporting,
            supporting_weaknesses=verdict_data.supporting_weaknesses,
            strongest_opposing=verdict_data.strongest_opposing,
            opposing_weaknesses=verdict_data.opposing_weaknesses,
            reasoning=verdict_data.reasoning,
            final_assessment=verdict_data.final_assessment,
            raw_payload=verdict_data.model_dump(),
        )
        self.db.add(verdict_model)

        summary_msg = (
            f"JUDGE VERDICT:\n"
            f"• Strongest Supporting: {verdict_data.strongest_supporting}\n"
            f"• Supporting Weaknesses: {verdict_data.supporting_weaknesses}\n"
            f"• Strongest Opposing: {verdict_data.strongest_opposing}\n"
            f"• Opposing Weaknesses: {verdict_data.opposing_weaknesses}\n"
            f"• Reasoning: {verdict_data.reasoning}\n"
            f"• Final Assessment: {verdict_data.final_assessment}"
        )
        self._save_message(
            session_id=session.id,
            speaker=AgentRole.JUDGE,
            round_num=session.current_round,
            content=summary_msg,
            idempotency_key=idempotency_key,
        )

    # --- Helpers ---

    def _save_message(
        self,
        session_id: str,
        speaker: AgentRole,
        round_num: int,
        content: str,
        idempotency_key: Optional[str] = None,
    ) -> MessageModel:
        seq = self._get_next_sequence_number(session_id)
        msg = MessageModel(
            id=str(uuid.uuid4()),
            session_id=session_id,
            speaker=speaker.value,
            round=round_num,
            sequence_number=seq,
            content=content,
            status="COMPLETED",
            idempotency_key=idempotency_key,
        )
        self.db.add(msg)
        self.db.flush()
        return msg

    def _get_next_sequence_number(self, session_id: str) -> int:
        last = (
            self.db.query(MessageModel)
            .filter(MessageModel.session_id == session_id)
            .order_by(MessageModel.sequence_number.desc())
            .first()
        )
        return (last.sequence_number + 1) if last else 1

    def _get_last_message_by_speaker(self, session_id: str, speaker: AgentRole) -> Optional[MessageModel]:
        return (
            self.db.query(MessageModel)
            .filter(
                MessageModel.session_id == session_id,
                MessageModel.speaker == speaker.value,
            )
            .order_by(MessageModel.sequence_number.desc())
            .first()
        )

    def _build_transcript_text(self, session_id: str) -> str:
        messages = (
            self.db.query(MessageModel)
            .filter(MessageModel.session_id == session_id)
            .order_by(MessageModel.sequence_number.asc())
            .all()
        )
        lines = []
        for m in messages:
            lines.append(f"[{m.speaker} | Round {m.round}]: {m.content}")
        return "\n\n".join(lines)
