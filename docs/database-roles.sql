\set ON_ERROR_STOP on
-- Run as a dedicated provisioning operator against the explicitly selected DB.
-- No passwords; R7 sets LOGIN credentials through its secret channel separately.
-- Reapply after every migration. Existing rows/history and other roles' ACLs survive.
BEGIN;
DO $roles$
DECLARE role_name text;
BEGIN
  FOREACH role_name IN ARRAY ARRAY['map_hub_owner','map_hub_migrator','map_hub_runtime'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT', role_name);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)) THEN
      RAISE EXCEPTION 'Refusing elevated existing service role';
    END IF;
  END LOOP;
END
$roles$;
ALTER ROLE map_hub_owner NOLOGIN NOINHERIT;
ALTER ROLE map_hub_migrator LOGIN NOINHERIT;
ALTER ROLE map_hub_runtime LOGIN NOINHERIT;
GRANT map_hub_owner TO map_hub_migrator;
REVOKE map_hub_owner FROM map_hub_runtime;
SELECT format('GRANT CONNECT ON DATABASE %I TO map_hub_runtime, map_hub_migrator', current_database()) \gexec
CREATE SCHEMA IF NOT EXISTS hub_data AUTHORIZATION map_hub_owner;
ALTER SCHEMA hub_data OWNER TO map_hub_owner;
REVOKE CREATE ON SCHEMA hub_data FROM PUBLIC;
REVOKE ALL ON SCHEMA hub_data FROM map_hub_runtime;
GRANT USAGE ON SCHEMA hub_data TO map_hub_runtime;
DO $ownership$
DECLARE obj record;
BEGIN
  FOR obj IN SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE NOT (c.relkind='S' AND EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid=c.oid AND d.deptype IN ('a','i')))
      AND n.nspname='hub_data' AND c.relkind IN ('r','p','S','v','m')
  LOOP
    EXECUTE format('ALTER %s hub_data.%I OWNER TO map_hub_owner',
      CASE obj.relkind WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE 'TABLE' END, obj.relname);
  END LOOP;
END
$ownership$;
REVOKE ALL ON ALL TABLES IN SCHEMA hub_data FROM map_hub_runtime;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA hub_data FROM map_hub_runtime;
-- New objects stay fail-closed until this explicitly reviewed ACL list is updated.
ALTER DEFAULT PRIVILEGES FOR ROLE map_hub_owner IN SCHEMA hub_data REVOKE ALL ON TABLES FROM map_hub_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE map_hub_owner IN SCHEMA hub_data REVOKE ALL ON SEQUENCES FROM map_hub_runtime;
DO $acl$
BEGIN
  IF to_regclass('hub_data.alembic_version') IS NOT NULL THEN
    GRANT SELECT ON hub_data.alembic_version TO map_hub_runtime;
  END IF;
  IF to_regclass('hub_data.region_grid') IS NOT NULL THEN
    GRANT SELECT ON hub_data.region_grid TO map_hub_runtime;
  END IF;
  IF to_regclass('hub_data.subscribed_grids') IS NOT NULL THEN
    GRANT SELECT, UPDATE ON hub_data.subscribed_grids TO map_hub_runtime;
  END IF;
  IF to_regclass('hub_data.short_term_forecast') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON hub_data.short_term_forecast TO map_hub_runtime;
  END IF;
  IF to_regclass('hub_data.mid_land_forecast') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON hub_data.mid_land_forecast TO map_hub_runtime;
  END IF;
  IF to_regclass('hub_data.mid_temp_forecast') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON hub_data.mid_temp_forecast TO map_hub_runtime;
  END IF;
  IF to_regclass('hub_data.places') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON hub_data.places TO map_hub_runtime;
  END IF;
  IF to_regclass('hub_data.forbidden_zones') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON hub_data.forbidden_zones TO map_hub_runtime;
  END IF;
  IF to_regclass('hub_data.weather_nowcast_snapshots') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON hub_data.weather_nowcast_snapshots TO map_hub_runtime;
  END IF;
  IF to_regclass('hub_data.air_quality_snapshots') IS NOT NULL THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON hub_data.air_quality_snapshots TO map_hub_runtime;
  END IF;
  IF to_regclass('hub_data.forbidden_zones_zone_id_seq') IS NOT NULL THEN
    GRANT USAGE ON SEQUENCE hub_data.forbidden_zones_zone_id_seq TO map_hub_runtime;
  END IF;
END
$acl$;
GRANT USAGE ON SCHEMA public TO map_hub_runtime;
COMMIT;
