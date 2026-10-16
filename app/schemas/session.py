from datetime import datetime
from typing import List, Optional, Literal
from pydantic import BaseModel, Field, field_validator, ConfigDict
from app.core.state_machine import AgentRole, SessionStatus


# --- Message Schemas ---
class MessageResponse(BaseModel):
    id: str
    session_id: str
    speaker: AgentRole
    round: int
    sequence_number: int
    content: str
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Verdict Schemas ---
class VerdictStructuredOutput(BaseModel):
    strongest_supporting: str = Field(
        ...,
        description="The strongest reasonable argument presented in favor of the proposition."
    )
    supporting_weaknesses: str = Field(
        ...,
        description="Unaddressed gaps, unsupported assumptions, or risks in the supporting case."
    )
    strongest_opposing: str = Field(
        ...,
        description="The strongest counterargument or challenge presented against the proposition."
    )
    opposing_weaknesses: str = Field(
        ...,
        description="Unaddressed gaps, overstatements, or risks in the opposing case."
    )
    reasoning: str = Field(
        ...,
        description="Comprehensive evaluation of reasoning quality, relevance, evidence, and logical consistency."
    )
    final_assessment: str = Field(
        ...,
        description="Balanced final synthesized recommendation or verdict for the human decision maker."
    )


class VerdictResponse(BaseModel):
    id: str
    session_id: str
    strongest_supporting: str
    supporting_weaknesses: str
    strongest_opposing: str
    opposing_weaknesses: str
    reasoning: str
    final_assessment: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Human Decision Schemas ---
class HumanDecisionCreate(BaseModel):
    decision: Literal["ACCEPTED", "REJECTED"] = Field(
        ...,
        description="User's decision to accept or reject the AI Judge's verdict."
    )
    rationale: Optional[str] = Field(
        None,
        max_length=2000,
        description="Optional human rationale or instructions accompanying the decision."
    )


class HumanDecisionResponse(BaseModel):
    id: str
    session_id: str
    decision: str
    rationale: Optional[str]
    decided_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Session Schemas ---
class SessionCreate(BaseModel):
    topic: str = Field(
        ...,
        min_length=5,
        max_length=500,
        description="Topic or debate question for the boardroom."
    )
    max_rounds: int = Field(
        default=2,
        ge=1,
        le=5,
        description="Number of debate rounds (1 to 5). Default is 2."
    )

    @field_validator("topic")
    def clean_topic(cls, v: str) -> str:
        cleaned = v.strip()
        if len(cleaned) < 5:
            raise ValueError("Topic must be at least 5 non-whitespace characters long.")
        return cleaned


class SessionResponse(BaseModel):
    id: str
    topic: str
    status: SessionStatus
    current_round: int
    max_rounds: int
    created_at: datetime
    updated_at: datetime
    error_message: Optional[str] = None
    messages: List[MessageResponse] = []
    verdict: Optional[VerdictResponse] = None
    decision: Optional[HumanDecisionResponse] = None

    model_config = ConfigDict(from_attributes=True)


class AdvanceSessionRequest(BaseModel):
    idempotency_key: Optional[str] = Field(
        None,
        description="Optional unique key to safely retry the transition without duplicating turns."
    )
