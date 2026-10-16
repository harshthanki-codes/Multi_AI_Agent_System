from enum import Enum
from typing import List, Optional, Set


class AgentRole(str, Enum):
    MODERATOR = "MODERATOR"
    SUPPORTING = "SUPPORTING"
    OPPOSING = "OPPOSING"
    JUDGE = "JUDGE"
    HUMAN = "HUMAN"


class SessionStatus(str, Enum):
    CREATED = "CREATED"
    MODERATOR_OPENING = "MODERATOR_OPENING"
    SUPPORTING_ARGUMENT = "SUPPORTING_ARGUMENT"
    OPPOSING_REBUTTAL = "OPPOSING_REBUTTAL"
    SUPPORTING_RESPONSE = "SUPPORTING_RESPONSE"
    OPPOSING_RESPONSE = "OPPOSING_RESPONSE"
    JUDGE_VERDICT = "JUDGE_VERDICT"
    AWAITING_HUMAN_DECISION = "AWAITING_HUMAN_DECISION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TERMINATED = "TERMINATED"


# Mapping from current status to expected next status and next agent speaker
VALID_TRANSITIONS = {
    SessionStatus.CREATED: {SessionStatus.MODERATOR_OPENING, SessionStatus.FAILED},
    SessionStatus.MODERATOR_OPENING: {SessionStatus.SUPPORTING_ARGUMENT, SessionStatus.FAILED},
    SessionStatus.SUPPORTING_ARGUMENT: {SessionStatus.OPPOSING_REBUTTAL, SessionStatus.FAILED},
    SessionStatus.OPPOSING_REBUTTAL: {
        SessionStatus.SUPPORTING_RESPONSE,
        SessionStatus.JUDGE_VERDICT,
        SessionStatus.FAILED,
    },
    SessionStatus.SUPPORTING_RESPONSE: {SessionStatus.OPPOSING_RESPONSE, SessionStatus.FAILED},
    SessionStatus.OPPOSING_RESPONSE: {
        SessionStatus.SUPPORTING_RESPONSE,
        SessionStatus.JUDGE_VERDICT,
        SessionStatus.FAILED,
    },
    SessionStatus.JUDGE_VERDICT: {SessionStatus.AWAITING_HUMAN_DECISION, SessionStatus.FAILED},
    SessionStatus.AWAITING_HUMAN_DECISION: {
        SessionStatus.COMPLETED,
        SessionStatus.TERMINATED,
        SessionStatus.FAILED,
    },
    SessionStatus.COMPLETED: set(),
    SessionStatus.TERMINATED: set(),
    SessionStatus.FAILED: {
        # Allow retry/resume from FAILED back to whichever state was interrupted
        SessionStatus.MODERATOR_OPENING,
        SessionStatus.SUPPORTING_ARGUMENT,
        SessionStatus.OPPOSING_REBUTTAL,
        SessionStatus.SUPPORTING_RESPONSE,
        SessionStatus.OPPOSING_RESPONSE,
        SessionStatus.JUDGE_VERDICT,
    },
}


class InvalidStateTransitionError(Exception):
    """Raised when an illegal state machine transition is attempted."""
    pass


class StateMachine:
    @staticmethod
    def get_role_for_status(status: SessionStatus) -> Optional[AgentRole]:
        mapping = {
            SessionStatus.MODERATOR_OPENING: AgentRole.MODERATOR,
            SessionStatus.SUPPORTING_ARGUMENT: AgentRole.SUPPORTING,
            SessionStatus.OPPOSING_REBUTTAL: AgentRole.OPPOSING,
            SessionStatus.SUPPORTING_RESPONSE: AgentRole.SUPPORTING,
            SessionStatus.OPPOSING_RESPONSE: AgentRole.OPPOSING,
            SessionStatus.JUDGE_VERDICT: AgentRole.JUDGE,
            SessionStatus.AWAITING_HUMAN_DECISION: AgentRole.HUMAN,
        }
        return mapping.get(status)

    @staticmethod
    def determine_next_status(
        current_status: SessionStatus,
        current_round: int,
        max_rounds: int,
    ) -> SessionStatus:
        """
        Determines the deterministic next step in the boardroom debate.
        Bounded flow:
        - Round 1:
          CREATED -> MODERATOR_OPENING -> SUPPORTING_ARGUMENT -> OPPOSING_REBUTTAL
          If max_rounds == 1:
            OPPOSING_REBUTTAL -> JUDGE_VERDICT
          If max_rounds > 1:
            OPPOSING_REBUTTAL -> SUPPORTING_RESPONSE (starts round 2)
        - Round 2..N:
          SUPPORTING_RESPONSE -> OPPOSING_RESPONSE
          If current_round < max_rounds:
            OPPOSING_RESPONSE -> SUPPORTING_RESPONSE (starts next round)
          If current_round >= max_rounds:
            OPPOSING_RESPONSE -> JUDGE_VERDICT
        - Then:
          JUDGE_VERDICT -> AWAITING_HUMAN_DECISION
        """
        if current_status == SessionStatus.CREATED:
            return SessionStatus.MODERATOR_OPENING

        elif current_status == SessionStatus.MODERATOR_OPENING:
            return SessionStatus.SUPPORTING_ARGUMENT

        elif current_status == SessionStatus.SUPPORTING_ARGUMENT:
            return SessionStatus.OPPOSING_REBUTTAL

        elif current_status == SessionStatus.OPPOSING_REBUTTAL:
            if current_round >= max_rounds:
                return SessionStatus.JUDGE_VERDICT
            else:
                return SessionStatus.SUPPORTING_RESPONSE

        elif current_status == SessionStatus.SUPPORTING_RESPONSE:
            return SessionStatus.OPPOSING_RESPONSE

        elif current_status == SessionStatus.OPPOSING_RESPONSE:
            if current_round >= max_rounds:
                return SessionStatus.JUDGE_VERDICT
            else:
                return SessionStatus.SUPPORTING_RESPONSE

        elif current_status == SessionStatus.JUDGE_VERDICT:
            return SessionStatus.AWAITING_HUMAN_DECISION

        raise InvalidStateTransitionError(
            f"No automated successor for terminal or human-gated status: {current_status}"
        )

    @staticmethod
    def determine_recovery_status(
        last_speaker: Optional[AgentRole],
        current_round: int,
        max_rounds: int,
    ) -> SessionStatus:
        """
        Determines the state to resume when recovering a FAILED session,
        based on the last completed speaker message.
        """
        if not last_speaker:
            return SessionStatus.MODERATOR_OPENING
        if last_speaker == AgentRole.MODERATOR:
            return SessionStatus.SUPPORTING_ARGUMENT
        if last_speaker == AgentRole.SUPPORTING:
            return SessionStatus.OPPOSING_REBUTTAL if current_round == 1 else SessionStatus.OPPOSING_RESPONSE
        if last_speaker == AgentRole.OPPOSING:
            if current_round >= max_rounds:
                return SessionStatus.JUDGE_VERDICT
            else:
                return SessionStatus.SUPPORTING_RESPONSE
        if last_speaker == AgentRole.JUDGE:
            return SessionStatus.AWAITING_HUMAN_DECISION

        return SessionStatus.MODERATOR_OPENING

    @staticmethod
    def validate_transition(from_status: SessionStatus, to_status: SessionStatus) -> None:
        allowed = VALID_TRANSITIONS.get(from_status, set())
        if to_status not in allowed:
            raise InvalidStateTransitionError(
                f"Invalid state transition from '{from_status.value}' to '{to_status.value}'. "
                f"Allowed transitions: {[s.value for s in allowed]}"
            )
