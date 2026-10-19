"""Alerting: rules, evaluations, incidents, transitions, outbox, worker heartbeats.

Revision ID: 0002_alerting
Revises: 0001_core
"""

from pulseops.migration_helpers import grant, run_sql

revision = "0002_alerting"
down_revision = "0001_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql(
        """
        CREATE TABLE alert_rules (
            id uuid PRIMARY KEY,
            service_id uuid NOT NULL REFERENCES services(id),
            name varchar(80) NOT NULL,
            metric varchar(24) NOT NULL,
            comparator varchar(4) NOT NULL,
            threshold double precision NOT NULL,
            window_minutes smallint NOT NULL,
            min_samples integer NOT NULL,
            enabled boolean NOT NULL,
            version integer NOT NULL,
            breach_streak integer NOT NULL DEFAULT 0,
            ok_streak integer NOT NULL DEFAULT 0,
            last_slot timestamptz,
            last_status varchar(24),
            last_value double precision,
            last_samples integer,
            created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL,
            deleted_at timestamptz,
            CONSTRAINT uq_alert_rules_service_id UNIQUE (service_id, id),
            CONSTRAINT ck_rules_metric CHECK (metric IN ('ERROR_RATE_5XX','P95_LATENCY_MS','REQUEST_RATE')),
            CONSTRAINT ck_rules_comparator CHECK (comparator IN ('GT','GTE')),
            CONSTRAINT ck_rules_window CHECK (window_minutes IN (5,15,60)),
            CONSTRAINT ck_rules_min_samples CHECK (min_samples BETWEEN 1 AND 100000),
            CONSTRAINT ck_rules_name CHECK (char_length(name) BETWEEN 1 AND 80),
            CONSTRAINT ck_rules_threshold CHECK (
                (metric = 'ERROR_RATE_5XX' AND threshold >= 0 AND threshold <= 1)
                OR (metric = 'P95_LATENCY_MS' AND threshold >= 0 AND threshold <= 120000)
                OR (metric = 'REQUEST_RATE' AND threshold >= 0 AND threshold <= 1000000))
        );
        CREATE INDEX ix_alert_rules_service_id ON alert_rules (service_id);

        CREATE TABLE rule_evaluations (
            id uuid PRIMARY KEY,
            service_id uuid NOT NULL,
            rule_id uuid NOT NULL,
            rule_version integer NOT NULL,
            scheduled_at timestamptz NOT NULL,
            window_start timestamptz NOT NULL,
            window_end timestamptz NOT NULL,
            status varchar(24) NOT NULL,
            value double precision,
            samples integer NOT NULL,
            evaluated_at timestamptz NOT NULL,
            worker_id varchar(64) NOT NULL,
            CONSTRAINT uq_rule_eval_slot UNIQUE (rule_id, rule_version, scheduled_at),
            CONSTRAINT fk_eval_rule_service FOREIGN KEY (service_id, rule_id) REFERENCES alert_rules (service_id, id),
            CONSTRAINT ck_eval_status CHECK (status IN ('BREACH','OK','INSUFFICIENT_DATA','UNKNOWN'))
        );
        CREATE INDEX ix_rule_evaluations_scheduled_at ON rule_evaluations (scheduled_at);

        CREATE TABLE incidents (
            id uuid PRIMARY KEY,
            service_id uuid NOT NULL,
            rule_id uuid NOT NULL,
            status varchar(16) NOT NULL,
            version integer NOT NULL,
            metric varchar(24) NOT NULL,
            threshold double precision NOT NULL,
            opened_value double precision,
            opened_at timestamptz NOT NULL,
            acknowledged_at timestamptz,
            acknowledged_by uuid,
            resolved_at timestamptz,
            resolved_by uuid,
            resolve_reason varchar(24),
            updated_at timestamptz NOT NULL,
            CONSTRAINT uq_incidents_service_id UNIQUE (service_id, id),
            CONSTRAINT fk_incident_rule_service FOREIGN KEY (service_id, rule_id) REFERENCES alert_rules (service_id, id),
            CONSTRAINT ck_incident_status CHECK (status IN ('OPEN','ACKNOWLEDGED','RESOLVED')),
            CONSTRAINT ck_incident_resolved CHECK ((status = 'RESOLVED') = (resolved_at IS NOT NULL))
        );
        -- At most one active (OPEN/ACKNOWLEDGED) incident per rule, enforced by PostgreSQL.
        CREATE UNIQUE INDEX uq_incident_active_per_rule ON incidents (rule_id) WHERE status IN ('OPEN','ACKNOWLEDGED');
        CREATE INDEX ix_incidents_service_opened ON incidents (service_id, opened_at);

        CREATE TABLE incident_events (
            id uuid PRIMARY KEY,
            service_id uuid NOT NULL,
            incident_id uuid NOT NULL,
            version integer NOT NULL,
            kind varchar(16) NOT NULL,
            reason varchar(24),
            actor_id uuid,
            scheduled_at timestamptz,
            occurred_at timestamptz NOT NULL,
            CONSTRAINT uq_incident_event_version UNIQUE (incident_id, version),
            CONSTRAINT fk_incident_event_service FOREIGN KEY (service_id, incident_id) REFERENCES incidents (service_id, id),
            CONSTRAINT ck_incident_event_kind CHECK (kind IN ('OPENED','ACKNOWLEDGED','RESOLVED'))
        );

        CREATE TABLE outbox_events (
            id bigserial PRIMARY KEY,
            event_id uuid NOT NULL UNIQUE,
            service_id uuid NOT NULL,
            incident_id uuid NOT NULL,
            version integer NOT NULL,
            created_at timestamptz NOT NULL,
            sent_at timestamptz,
            attempts integer NOT NULL DEFAULT 0,
            next_attempt_at timestamptz NOT NULL,
            last_error_code varchar(48)
        );
        CREATE INDEX ix_outbox_pending ON outbox_events (next_attempt_at) WHERE sent_at IS NULL;
        CREATE INDEX ix_outbox_sent ON outbox_events (sent_at) WHERE sent_at IS NOT NULL;

        CREATE TABLE worker_heartbeats (
            worker_id varchar(64) PRIMARY KEY,
            started_at timestamptz NOT NULL,
            heartbeat_at timestamptz NOT NULL,
            last_dispatch_ok_at timestamptz,
            last_dispatch_error_at timestamptz,
            evaluations_paused boolean NOT NULL DEFAULT false,
            paused_reason varchar(32),
            last_slot timestamptz
        );
        """
    )
    for table in ("alert_rules", "rule_evaluations", "incidents", "incident_events", "outbox_events", "worker_heartbeats"):
        grant("runtime", table, "SELECT, INSERT, UPDATE, DELETE")
    grant("runtime", "outbox_events_id_seq", "USAGE", sequence=True)


def downgrade() -> None:
    raise RuntimeError("downgrade is not supported; restore from backup instead")
