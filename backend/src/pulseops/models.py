"""SQLAlchemy models. Schema changes are made only through Alembic migrations.

Service-scoped child objects carry ``service_id`` and reference their parent through
composite ``(service_id, id)`` foreign keys so an object can never point at a parent
belonging to a different service.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

UTCDateTime = DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    password_changed_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    __table_args__ = (
        CheckConstraint("role IN ('ADMIN','OPERATOR','VIEWER')", name="ck_users_role"),
        CheckConstraint("email = lower(email) AND char_length(email) BETWEEN 3 AND 254", name="ck_users_email"),
    )


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoke_reason: Mapped[str | None] = mapped_column(String(32))
    reauth_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    __table_args__ = (CheckConstraint("expires_at > created_at", name="ck_sessions_expiry"),)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    digest: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    spent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Service(Base):
    __tablename__ = "services"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    __table_args__ = (
        CheckConstraint("char_length(name) BETWEEN 1 AND 80", name="ck_services_name"),
        CheckConstraint("char_length(environment) BETWEEN 1 AND 32", name="ck_services_env"),
        Index("uq_services_name_env_active", "name", "environment", unique=True, postgresql_where=text("deleted_at IS NULL")),
    )


class ServiceGrant(Base):
    __tablename__ = "service_grants"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    service_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"), primary_key=True, index=True)
    granted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class Route(Base):
    __tablename__ = "routes"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    service_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("services.id"), nullable=False)
    template: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    __table_args__ = (
        UniqueConstraint("service_id", "id", name="uq_routes_service_id"),
        Index("uq_routes_template_active", "service_id", "template", unique=True, postgresql_where=text("retired_at IS NULL")),
        CheckConstraint("char_length(template) BETWEEN 1 AND 200", name="ck_routes_template"),
    )


class IngestToken(Base):
    __tablename__ = "ingest_tokens"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    service_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("services.id"), nullable=False, index=True)
    digest: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    __table_args__ = (UniqueConstraint("service_id", "id", name="uq_ingest_tokens_service_id"),)


class RequestEvent(Base):
    __tablename__ = "request_events"
    service_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("services.id"), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    route_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    method: Mapped[str] = mapped_column(String(7), nullable=False)
    status_code: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    duration_ms: Mapped[float] = mapped_column(Double, nullable=False)
    digest: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["service_id", "route_id"], ["routes.service_id", "routes.id"], name="fk_events_route_service"),
        CheckConstraint("method IN ('GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS')", name="ck_events_method"),
        CheckConstraint("status_code BETWEEN 100 AND 599", name="ck_events_status"),
        CheckConstraint("duration_ms >= 0 AND duration_ms <= 120000", name="ck_events_duration"),
        CheckConstraint("octet_length(digest) = 32", name="ck_events_digest"),
        Index("ix_events_service_time", "service_id", "occurred_at"),
        Index("ix_events_service_route_time", "service_id", "route_id", "occurred_at"),
        Index("ix_events_time", "occurred_at"),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(24))
    target_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    coalesce_key: Mapped[str | None] = mapped_column(String(96))
    __table_args__ = (
        CheckConstraint("actor_kind IN ('USER','CLI','SYSTEM','ANONYMOUS')", name="ck_audit_actor_kind"),
        CheckConstraint("result IN ('SUCCESS','FAILURE','DENIED')", name="ck_audit_result"),
        Index("uq_audit_coalesce", "coalesce_key", unique=True, postgresql_where=text("coalesce_key IS NOT NULL")),
    )


class DeletionJob(Base):
    __tablename__ = "deletion_jobs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    service_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(48))
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    __table_args__ = (CheckConstraint("status IN ('PENDING','RUNNING','FAILED','DONE')", name="ck_deletion_status"),)


class IngestQuota(Base):
    __tablename__ = "ingest_quota"
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    used: Mapped[int] = mapped_column(BigInteger, nullable=False)
    __table_args__ = (CheckConstraint("id = 1", name="ck_quota_singleton"), CheckConstraint("used >= 0", name="ck_quota_used"))


class AppSettings(Base):
    __tablename__ = "app_settings"
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    event_retention_days: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_settings_singleton"),
        CheckConstraint("event_retention_days BETWEEN 1 AND 7", name="ck_settings_retention"),
    )


# --- Phase 3: alerting (migration 0002) ---


class AlertRule(Base):
    __tablename__ = "alert_rules"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    service_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("services.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    metric: Mapped[str] = mapped_column(String(24), nullable=False)
    comparator: Mapped[str] = mapped_column(String(4), nullable=False)
    threshold: Mapped[float] = mapped_column(Double, nullable=False)
    window_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    min_samples: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    breach_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ok_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_slot: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_status: Mapped[str | None] = mapped_column(String(24))
    last_value: Mapped[float | None] = mapped_column(Double)
    last_samples: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    __table_args__ = (
        UniqueConstraint("service_id", "id", name="uq_alert_rules_service_id"),
        CheckConstraint("metric IN ('ERROR_RATE_5XX','P95_LATENCY_MS','REQUEST_RATE')", name="ck_rules_metric"),
        CheckConstraint("comparator IN ('GT','GTE')", name="ck_rules_comparator"),
        CheckConstraint("window_minutes IN (5,15,60)", name="ck_rules_window"),
        CheckConstraint("min_samples BETWEEN 1 AND 100000", name="ck_rules_min_samples"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 80", name="ck_rules_name"),
        CheckConstraint(
            "(metric = 'ERROR_RATE_5XX' AND threshold >= 0 AND threshold <= 1)"
            " OR (metric = 'P95_LATENCY_MS' AND threshold >= 0 AND threshold <= 120000)"
            " OR (metric = 'REQUEST_RATE' AND threshold >= 0 AND threshold <= 1000000)",
            name="ck_rules_threshold",
        ),
    )


class RuleEvaluation(Base):
    __tablename__ = "rule_evaluations"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    service_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    rule_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    window_start: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    window_end: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    value: Mapped[float | None] = mapped_column(Double)
    samples: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        UniqueConstraint("rule_id", "rule_version", "scheduled_at", name="uq_rule_eval_slot"),
        ForeignKeyConstraint(["service_id", "rule_id"], ["alert_rules.service_id", "alert_rules.id"], name="fk_eval_rule_service"),
        CheckConstraint("status IN ('BREACH','OK','INSUFFICIENT_DATA','UNKNOWN')", name="ck_eval_status"),
    )


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    service_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    rule_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    metric: Mapped[str] = mapped_column(String(24), nullable=False)
    threshold: Mapped[float] = mapped_column(Double, nullable=False)
    opened_value: Mapped[float | None] = mapped_column(Double)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    resolve_reason: Mapped[str | None] = mapped_column(String(24))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    __table_args__ = (
        UniqueConstraint("service_id", "id", name="uq_incidents_service_id"),
        ForeignKeyConstraint(["service_id", "rule_id"], ["alert_rules.service_id", "alert_rules.id"], name="fk_incident_rule_service"),
        CheckConstraint("status IN ('OPEN','ACKNOWLEDGED','RESOLVED')", name="ck_incident_status"),
        CheckConstraint("(status = 'RESOLVED') = (resolved_at IS NOT NULL)", name="ck_incident_resolved"),
        Index("uq_incident_active_per_rule", "rule_id", unique=True, postgresql_where=text("status IN ('OPEN','ACKNOWLEDGED')")),
        Index("ix_incidents_service_opened", "service_id", "opened_at"),
    )


class IncidentEvent(Base):
    __tablename__ = "incident_events"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    service_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    incident_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(24))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    scheduled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    __table_args__ = (
        UniqueConstraint("incident_id", "version", name="uq_incident_event_version"),
        ForeignKeyConstraint(["service_id", "incident_id"], ["incidents.service_id", "incidents.id"], name="fk_incident_event_service"),
        CheckConstraint("kind IN ('OPENED','ACKNOWLEDGED','RESOLVED')", name="ck_incident_event_kind"),
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    service_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    incident_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(48))
    __table_args__ = (
        Index("ix_outbox_pending", "next_attempt_at", postgresql_where=text("sent_at IS NULL")),
        Index("ix_outbox_sent", "sent_at", postgresql_where=text("sent_at IS NOT NULL")),
    )


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    last_dispatch_ok_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_dispatch_error_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    evaluations_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    paused_reason: Mapped[str | None] = mapped_column(String(32))
    last_slot: Mapped[datetime | None] = mapped_column(UTCDateTime)
