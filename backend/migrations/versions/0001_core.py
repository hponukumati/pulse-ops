"""Core schema: accounts, sessions, services, routes, ingest tokens, events, audit, deletion jobs.

Revision ID: 0001_core
Revises:
"""

from pulseops.migration_helpers import grant, run_sql

revision = "0001_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql(
        """
        CREATE TABLE users (
            id uuid PRIMARY KEY,
            email varchar(254) NOT NULL UNIQUE,
            password_hash text NOT NULL,
            role varchar(16) NOT NULL,
            disabled_at timestamptz,
            password_changed_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL,
            CONSTRAINT ck_users_role CHECK (role IN ('ADMIN','OPERATOR','VIEWER')),
            CONSTRAINT ck_users_email CHECK (email = lower(email) AND char_length(email) BETWEEN 3 AND 254)
        );

        CREATE TABLE sessions (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL,
            revoked_at timestamptz,
            revoke_reason varchar(32),
            reauth_at timestamptz,
            CONSTRAINT ck_sessions_expiry CHECK (expires_at > created_at)
        );
        CREATE INDEX ix_sessions_user_id ON sessions (user_id);
        CREATE INDEX ix_sessions_expires_at ON sessions (expires_at);

        CREATE TABLE refresh_tokens (
            id uuid PRIMARY KEY,
            session_id uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            digest bytea NOT NULL UNIQUE,
            key_version smallint NOT NULL,
            created_at timestamptz NOT NULL,
            spent_at timestamptz
        );
        CREATE INDEX ix_refresh_tokens_session_id ON refresh_tokens (session_id);

        CREATE TABLE services (
            id uuid PRIMARY KEY,
            name varchar(80) NOT NULL,
            environment varchar(32) NOT NULL,
            created_by uuid REFERENCES users(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL,
            deleted_at timestamptz,
            CONSTRAINT ck_services_name CHECK (char_length(name) BETWEEN 1 AND 80),
            CONSTRAINT ck_services_env CHECK (char_length(environment) BETWEEN 1 AND 32)
        );
        CREATE UNIQUE INDEX uq_services_name_env_active ON services (name, environment) WHERE deleted_at IS NULL;

        CREATE TABLE service_grants (
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            service_id uuid NOT NULL REFERENCES services(id) ON DELETE CASCADE,
            granted_by uuid,
            created_at timestamptz NOT NULL,
            PRIMARY KEY (user_id, service_id)
        );
        CREATE INDEX ix_service_grants_service_id ON service_grants (service_id);

        CREATE TABLE routes (
            id uuid PRIMARY KEY,
            service_id uuid NOT NULL REFERENCES services(id),
            template varchar(200) NOT NULL,
            created_at timestamptz NOT NULL,
            retired_at timestamptz,
            CONSTRAINT uq_routes_service_id UNIQUE (service_id, id),
            CONSTRAINT ck_routes_template CHECK (char_length(template) BETWEEN 1 AND 200)
        );
        CREATE UNIQUE INDEX uq_routes_template_active ON routes (service_id, template) WHERE retired_at IS NULL;

        CREATE TABLE ingest_tokens (
            id uuid PRIMARY KEY,
            public_id varchar(32) NOT NULL UNIQUE,
            service_id uuid NOT NULL REFERENCES services(id),
            digest bytea NOT NULL,
            key_version smallint NOT NULL,
            created_by uuid,
            created_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL,
            revoked_at timestamptz,
            CONSTRAINT uq_ingest_tokens_service_id UNIQUE (service_id, id)
        );
        CREATE INDEX ix_ingest_tokens_service_id ON ingest_tokens (service_id);

        CREATE TABLE request_events (
            service_id uuid NOT NULL REFERENCES services(id),
            event_id uuid NOT NULL,
            route_id uuid NOT NULL,
            occurred_at timestamptz NOT NULL,
            received_at timestamptz NOT NULL,
            method varchar(7) NOT NULL,
            status_code smallint NOT NULL,
            duration_ms double precision NOT NULL,
            digest bytea NOT NULL,
            PRIMARY KEY (service_id, event_id),
            CONSTRAINT fk_events_route_service FOREIGN KEY (service_id, route_id) REFERENCES routes (service_id, id),
            CONSTRAINT ck_events_method CHECK (method IN ('GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS')),
            CONSTRAINT ck_events_status CHECK (status_code BETWEEN 100 AND 599),
            CONSTRAINT ck_events_duration CHECK (duration_ms >= 0 AND duration_ms <= 120000),
            CONSTRAINT ck_events_digest CHECK (octet_length(digest) = 32)
        );
        CREATE INDEX ix_events_service_time ON request_events (service_id, occurred_at);
        CREATE INDEX ix_events_service_route_time ON request_events (service_id, route_id, occurred_at);
        CREATE INDEX ix_events_time ON request_events (occurred_at);

        CREATE TABLE audit_events (
            id uuid PRIMARY KEY,
            occurred_at timestamptz NOT NULL,
            actor_kind varchar(16) NOT NULL,
            actor_id uuid,
            action varchar(48) NOT NULL,
            target_type varchar(24),
            target_id uuid,
            result varchar(16) NOT NULL,
            request_id uuid,
            coalesce_key varchar(96),
            CONSTRAINT ck_audit_actor_kind CHECK (actor_kind IN ('USER','CLI','SYSTEM','ANONYMOUS')),
            CONSTRAINT ck_audit_result CHECK (result IN ('SUCCESS','FAILURE','DENIED'))
        );
        CREATE INDEX ix_audit_events_occurred_at ON audit_events (occurred_at);
        CREATE UNIQUE INDEX uq_audit_coalesce ON audit_events (coalesce_key) WHERE coalesce_key IS NOT NULL;

        CREATE TABLE deletion_jobs (
            id uuid PRIMARY KEY,
            service_id uuid NOT NULL UNIQUE,
            requested_at timestamptz NOT NULL,
            requested_by uuid,
            status varchar(16) NOT NULL,
            attempts integer NOT NULL DEFAULT 0,
            next_attempt_at timestamptz NOT NULL,
            last_error_code varchar(48),
            completed_at timestamptz,
            CONSTRAINT ck_deletion_status CHECK (status IN ('PENDING','RUNNING','FAILED','DONE'))
        );

        CREATE TABLE ingest_quota (
            id smallint PRIMARY KEY,
            used bigint NOT NULL,
            CONSTRAINT ck_quota_singleton CHECK (id = 1),
            CONSTRAINT ck_quota_used CHECK (used >= 0)
        );
        INSERT INTO ingest_quota (id, used) VALUES (1, 0);

        CREATE TABLE app_settings (
            id smallint PRIMARY KEY,
            event_retention_days smallint NOT NULL,
            updated_at timestamptz NOT NULL,
            CONSTRAINT ck_settings_singleton CHECK (id = 1),
            CONSTRAINT ck_settings_retention CHECK (event_retention_days BETWEEN 1 AND 7)
        );
        INSERT INTO app_settings (id, event_retention_days, updated_at) VALUES (1, 7, now());
        """
    )
    # Least-privilege runtime grants. Users are disabled, never deleted, by the runtime role.
    grant("runtime", "users", "SELECT, INSERT, UPDATE")
    grant("runtime", "sessions", "SELECT, INSERT, UPDATE, DELETE")
    grant("runtime", "refresh_tokens", "SELECT, INSERT, UPDATE, DELETE")
    grant("runtime", "services", "SELECT, INSERT, UPDATE, DELETE")
    grant("runtime", "service_grants", "SELECT, INSERT, DELETE")
    grant("runtime", "routes", "SELECT, INSERT, UPDATE, DELETE")
    grant("runtime", "ingest_tokens", "SELECT, INSERT, UPDATE, DELETE")
    grant("runtime", "request_events", "SELECT, INSERT, DELETE")
    # Audit records: runtime may insert and read but never modify or delete.
    grant("runtime", "audit_events", "SELECT, INSERT")
    grant("runtime", "deletion_jobs", "SELECT, INSERT, UPDATE, DELETE")
    grant("runtime", "ingest_quota", "SELECT, UPDATE")
    grant("runtime", "app_settings", "SELECT, UPDATE")
    # Maintenance role: audit retention only.
    grant("maint", "audit_events", "SELECT, DELETE")


def downgrade() -> None:
    # Destructive downgrades are never run automatically; see docs/OPERATIONS.md.
    raise RuntimeError("downgrade of the core schema is not supported; restore from backup instead")
