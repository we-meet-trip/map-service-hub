\set ON_ERROR_STOP on
-- Cohost contract only: existing S8 Admin application, no host/control split.
-- This phase follows existing Admin Alembic revisions and Hub migrations.
BEGIN;
DO $roles$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='map_admin_runtime') THEN
    CREATE ROLE map_admin_runtime LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='map_pg_exporter') THEN
    CREATE ROLE map_pg_exporter LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname IN ('map_admin_runtime','map_pg_exporter') AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)) THEN
    RAISE EXCEPTION 'Refusing elevated existing runtime or exporter role';
  END IF;
END
$roles$;
SELECT format('GRANT CONNECT ON DATABASE %I TO map_admin_runtime,map_pg_exporter',current_database()) \gexec
GRANT USAGE ON SCHEMA admin_data,hub_data,public TO map_admin_runtime;
GRANT SELECT,INSERT,UPDATE ON admin_data.admin_accounts TO map_admin_runtime;
GRANT SELECT,INSERT,DELETE ON admin_data.admin_sessions TO map_admin_runtime;
GRANT SELECT,INSERT,UPDATE,DELETE ON admin_data.login_attempts TO map_admin_runtime;
GRANT SELECT,INSERT,UPDATE ON admin_data.audit_logs TO map_admin_runtime;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA admin_data TO map_admin_runtime;
GRANT SELECT ON admin_data.alembic_version TO map_admin_runtime;
GRANT SELECT ON ALL TABLES IN SCHEMA hub_data TO map_admin_runtime;
GRANT pg_monitor TO map_pg_exporter;
-- No table/schema DML or ownership is granted to the exporter.
COMMIT;
