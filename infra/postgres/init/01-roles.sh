#!/bin/sh
# First-boot role setup (runs once on an empty data volume, as the bootstrap superuser).
# Roles: migrator (schema owner, used only by Alembic), app (runtime, least privilege,
# not superuser/owner), maint (audit retention only). Passwords are read from mounted
# secret files via psql backticks so they never appear in process arguments.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<'SQL'
\set migrator_pw `cat /run/secrets/db_migrator_password`
\set app_pw `cat /run/secrets/db_app_password`
\set maint_pw `cat /run/secrets/db_maint_password`
CREATE ROLE pulseops_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'migrator_pw';
CREATE ROLE pulseops_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD :'app_pw';
CREATE ROLE pulseops_maint LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD :'maint_pw';
CREATE DATABASE pulseops OWNER pulseops_migrator;
REVOKE ALL ON DATABASE pulseops FROM PUBLIC;
GRANT CONNECT ON DATABASE pulseops TO pulseops_app, pulseops_maint;
\connect pulseops
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO pulseops_migrator;
GRANT USAGE ON SCHEMA public TO pulseops_app, pulseops_maint;
SQL
