from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    String,
    Integer,
    DateTime,
    ForeignKey,
    Text,
    JSON,
    Boolean,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc)


class SessionModel(Base):
    __tablename__ = "sessions"

    id = Column(String(36), primary_key=True, index=True)
    topic = Column(String(500), nullable=False)
    status = Column(String(50), nullable=False, default="CREATED", index=True)
    current_round = Column(Integer, nullable=False, default=1)
    max_rounds = Column(Integer, nullable=False, default=2)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    error_message = Column(Text, nullable=True)

    # Relationships
    messages = relationship(
        "MessageModel",
        back_populates="session",
        order_by="MessageModel.sequence_number",
        cascade="all, delete-orphan",
    )
    verdict = relationship(
        "VerdictModel",
        back_populates="session",
        uselist=False,
        cascade="all, delete-orphan",
    )
    decision = relationship(
        "HumanDecisionModel",
        back_populates="session",
        uselist=False,
        cascade="all, delete-orphan",
    )


class MessageModel(Base):
    __tablename__ = "messages"

    id = Column(String(36), primary_key=True, index=True)
    session_id = Column(String(36), ForeignKey("sessions.id"), nullable=False, index=True)
    speaker = Column(String(50), nullable=False)  # MODERATOR, SUPPORTING, OPPOSING, JUDGE, HUMAN
    round = Column(Integer, nullable=False)
    sequence_number = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="COMPLETED")  # COMPLETED, FAILED
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    idempotency_key = Column(String(100), nullable=True, index=True)

    session = relationship("SessionModel", back_populates="messages")

    __table_args__ = (
        UniqueConstraint("session_id", "sequence_number", name="uq_session_sequence"),
        UniqueConstraint("session_id", "idempotency_key", name="uq_session_idempotency"),
    )


class VerdictModel(Base):
    __tablename__ = "verdicts"

    id = Column(String(36), primary_key=True, index=True)
    session_id = Column(String(36), ForeignKey("sessions.id"), unique=True, nullable=False, index=True)
    strongest_supporting = Column(Text, nullable=False)
    supporting_weaknesses = Column(Text, nullable=False)
    strongest_opposing = Column(Text, nullable=False)
    opposing_weaknesses = Column(Text, nullable=False)
    reasoning = Column(Text, nullable=False)
    final_assessment = Column(Text, nullable=False)
    raw_payload = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    session = relationship("SessionModel", back_populates="verdict")


class HumanDecisionModel(Base):
    __tablename__ = "human_decisions"

    id = Column(String(36), primary_key=True, index=True)
    session_id = Column(String(36), ForeignKey("sessions.id"), unique=True, nullable=False, index=True)
    decision = Column(String(20), nullable=False)  # ACCEPTED, REJECTED
    rationale = Column(Text, nullable=True)
    decided_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    session = relationship("SessionModel", back_populates="decision")
