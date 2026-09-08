#!/usr/bin/env python3
"""Fresh isolated PG only. No Docker lifecycle or external provider requests.

Use an ephemeral local PG17/PostGIS instance and an EMPTY DB named r2_*.
Needs psql plus existing service Python environments. Credentials are env-only.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import re
import hashlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo


PHASE = 'initialization'
REPORT = None


def execute(args, env, cwd=None):
    global PHASE
    PHASE = ((Path(cwd).name + ':') if cwd else '') + (args[2] if len(args)>2 and args[1]=='-m' else Path(args[0]).name)
    completed = subprocess.run(args, env=env, cwd=cwd, capture_output=True, text=True, timeout=900)
    if completed.returncode:
        classes = sorted(set(re.findall(r"(?:psycopg(?:\.errors)?|sqlalchemy\.exc)\.([A-Za-z][A-Za-z0-9_]{0,80})", completed.stderr)))
        safe_lines = [line[:200] for line in completed.stderr.splitlines() if re.fullmatch(r"(?:Hub|Agent|R2) [A-Za-z0-9 _;:=(),.-]{1,190}", line)]
        details = dict(phase=PHASE, exit=completed.returncode, error_classes=classes, safe_messages=safe_lines[-3:], stderr_sha256=hashlib.sha256(completed.stderr.encode()).hexdigest())
        raise RuntimeError(json.dumps(details))


def base_env():
    return {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'LANG', 'TMPDIR', 'SSL_CERT_FILE')}


def denied(conn, statement):
    try:
        conn.execute(statement)
    except psycopg.Error as error:
        if error.sqlstate != '42501':
            raise RuntimeError('unexpected SQLSTATE ' + str(error.sqlstate)) from None
    else:
        raise RuntimeError('forbidden operation accepted')


async def worker(service, source, dsn):
    sys.path.insert(0, str(source))
    if service == 'hub':
        os.environ['KMA_SERVICE_KEY'] = 'fixture-unused'
        os.environ['HUB_DATABASE_URL'] = dsn.replace('postgresql://', 'postgresql+psycopg://')
        from app.db.hub_db import get_hub_db, dispose_hub_db
        from app.db.schema_contract import validate_runtime_schema
        from app.db import forecast_repo, admin_ops_repo
        try:
            await validate_runtime_schema(get_hub_db())
            kst = timezone(timedelta(hours=9))
            base = datetime.now(kst).replace(minute=0, second=0, microsecond=0)
            fcst = base + timedelta(hours=1)
            item = dict(fcstDate=fcst.strftime('%Y%m%d'), fcstTime=fcst.strftime('%H%M'), category='TMP', fcstValue='22')
            assert await forecast_repo.upsert_short_term_items(60, 127, base, [item]) == 1
            item['fcstValue'] = '23'
            assert await forecast_repo.upsert_short_term_items(60, 127, base + timedelta(minutes=1), [item]) == 1
            rows, _ = await forecast_repo.fetch_short_term_range(60, 127, fcst.date(), fcst.date())
            assert len(rows) == 1 and rows[0]['fcst_value'] == '23'
            assert not (await forecast_repo.fetch_short_term_range(61, 127, fcst.date(), fcst.date()))[0]
            zone = dict(type='Polygon', coordinates=[[[127,37],[127.001,37],[127.001,37.001],[127,37]]])
            created = await admin_ops_repo.create_forbidden_zone('r2-fixture', 'synthetic', zone)
            zid = created['zone_id']
            assert (await admin_ops_repo.get_forbidden_zone(zid))['name'] == 'r2-fixture'
            assert (await admin_ops_repo.update_forbidden_zone(zid, 'r2-updated', 'synthetic', zone))['name'] == 'r2-updated'
            assert await admin_ops_repo.delete_forbidden_zone(zid)
            assert await admin_ops_repo.get_forbidden_zone(zid) is None
            await forecast_repo.housekeeping_expire()
        finally:
            await dispose_hub_db()
    elif service == 'agent':
        from langgraph.checkpoint.base import empty_checkpoint
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
        from psycopg.rows import dict_row
        from app.checkpoint_db import validate_runtime_schema
        from app.crypto.checkpoint_seal import CheckpointCipher
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True, row_factory=dict_row, options='-c search_path=langgraph') as conn:
            await validate_runtime_schema(conn, 'langgraph')
            saver = AsyncPostgresSaver(conn, serde=EncryptedSerializer(cipher=CheckpointCipher({'fixture': secrets.token_bytes(32)}, 'fixture')))
            checkpoint = empty_checkpoint()
            checkpoint['channel_values'] = {'location': 'r2-synthetic-location'}
            checkpoint['channel_versions'] = {'location': '1'}
            config = {'configurable': {'thread_id': 'r2-fixture', 'checkpoint_ns': ''}}
            saved = await saver.aput(config, checkpoint, {'source':'input','step':0,'parents':{}}, {'location':'1'})
            await saver.aput_writes(saved, [('location','r2-synthetic-write')], 'r2-task')
            loaded = await saver.aget_tuple(saved)
            assert loaded.checkpoint['channel_values']['location'] == 'r2-synthetic-location'
            assert len([item async for item in saver.alist(config)]) == 1
            blobs = await (await conn.execute('SELECT type,blob FROM checkpoint_blobs UNION ALL SELECT type,blob FROM checkpoint_writes')).fetchall()
            assert blobs and all('aesgcm:fixture' in row['type'] and b'r2-synthetic' not in bytes(row['blob']) for row in blobs)
            await saver.adelete_thread('r2-fixture')
            assert await saver.aget_tuple(saved) is None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('hub', 'agent', 'admin'):
        parser.add_argument('--'+name+'-source', type=Path)
        parser.add_argument('--'+name+'-python', default=sys.executable)
    parser.add_argument('--operator-dsn-env', default='R2_FIXTURE_DSN')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', choices=('hub','agent'))
    args = parser.parse_args()
    global REPORT
    REPORT = args.output
    if args.worker:
        asyncio.run(worker(args.worker, getattr(args,args.worker+'_source'), os.environ['R2_RUNTIME_DSN']))
        return
    if not args.output or not all((args.hub_source, args.agent_source, args.admin_source)):
        parser.error('source paths and output required')
    dsn = os.environ[args.operator_dsn_env]
    cfg = conninfo_to_dict(dsn)
    if cfg.get('host') not in ('127.0.0.1','localhost','::1') or not cfg.get('dbname','').startswith('r2_'):
        raise RuntimeError('only a local explicitly named r2_* empty fixture database is allowed')
    results = {'environment':'isolated PostgreSQL fixture','started_utc':datetime.now(timezone.utc).isoformat(),'checks':{},'sources':{}}
    for service in ('hub','agent','admin'):
        root = getattr(args,service+'_source').resolve()
        results['sources'][service] = subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
    env = base_env()
    for key, value in cfg.items():
        if key in ('host','port','dbname','user','password'):
            env[{'dbname':'PGDATABASE'}.get(key, 'PG'+key.upper())] = value
    def psql(path):
        execute(['psql','-X','-v','ON_ERROR_STOP=1','-f',str(path)], env)
    with psycopg.connect(dsn,autocommit=True) as operator:
        assert operator.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p')").fetchone()[0] == 0
        results['postgres_version']=operator.execute('SHOW server_version').fetchone()[0]
        assert results['postgres_version'].startswith('17.')
        operator.execute('CREATE EXTENSION postgis')
        results['postgis_version']=operator.execute('SELECT postgis_full_version()').fetchone()[0]
        operator.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
        operator.execute('CREATE TABLE public.r2_user_sentinel (id integer primary key, value text)')
        operator.execute("INSERT INTO public.r2_user_sentinel VALUES (1,'synthetic-preserved')")
        operator.execute('CREATE ROLE map_admin NOLOGIN NOINHERIT')
        operator.execute('CREATE ROLE map_admin_migrator LOGIN NOINHERIT')
        operator.execute('GRANT map_admin TO map_admin_migrator')
        operator.execute('CREATE SCHEMA admin_data AUTHORIZATION map_admin')
        psql(args.hub_source/'docs/database-roles.sql')
        psql(args.agent_source/'docs/database-roles.sql')
        passwords={role:secrets.token_urlsafe(24) for role in ('map_hub_migrator','map_hub_runtime','map_agent_migrator','map_agent_runtime','map_admin_migrator')}
        for role,password in passwords.items():
            operator.execute(sql.SQL('ALTER ROLE {} PASSWORD {}').format(sql.Identifier(role),sql.Literal(password)))
        def role_dsn(role, sqlalchemy=False, owner=None):
            from sqlalchemy.engine import URL
            uri = URL.create('postgresql+psycopg' if sqlalchemy else 'postgresql',username=role,password=passwords[role],host=cfg['host'],port=int(cfg.get('port',5432)),database=cfg['dbname'],query={'options':'-c role='+owner} if owner else {})
            return uri.render_as_string(hide_password=False)
        for service, module, key, role in [('hub','app.db.migrate','HUB_MIGRATION_DATABASE_URL','map_hub_migrator'),('agent','app.checkpoint_migrate','AGENT_CHECKPOINT_MIGRATION_DSN','map_agent_migrator'),('admin','alembic','ADMIN_DATABASE_URL','map_admin_migrator')]:
            child=base_env();child[key]=role_dsn(role,service!='agent','map_admin' if service=='admin' else None)
            command=[getattr(args,service+'_python'),'-m',module]+(['upgrade','head'] if service=='admin' else [])
            if service == 'hub':
                operator.execute(sql.SQL('GRANT CREATE ON DATABASE {} TO map_hub_owner').format(sql.Identifier(cfg['dbname'])))
            try:
                execute(command,child,getattr(args,service+'_source'))
            finally:
                if service == 'hub':
                    operator.execute(sql.SQL('REVOKE CREATE ON DATABASE {} FROM map_hub_owner').format(sql.Identifier(cfg['dbname'])))
            execute(command,child,getattr(args,service+'_source'))
            results['checks'][service+'_real_migrations_idempotent']='PASS'
        psql(args.hub_source/'docs/database-roles.sql');psql(args.agent_source/'docs/database-roles.sql')
        psql(Path(__file__).with_name('admin-exporter-roles.sql'))
        for role in ('map_admin_runtime','map_pg_exporter'):
            passwords[role]=secrets.token_urlsafe(24)
            operator.execute(sql.SQL('ALTER ROLE {} PASSWORD {}').format(sql.Identifier(role),sql.Literal(passwords[role])))
        for service in ('hub','agent'):
            child=base_env();child['R2_RUNTIME_DSN']=role_dsn('map_'+service+'_runtime')
            execute([getattr(args,service+'_python'),str(Path(__file__).resolve()),'--worker',service,'--'+service+'-source',str(getattr(args,service+'_source').resolve())],child)
            results['checks'][service+'_runtime_schema_and_repository_crud']='PASS'
        forbidden={'map_hub_runtime':['CREATE TABLE hub_data.r2_forbidden(id int)','ALTER TABLE hub_data.short_term_forecast ADD COLUMN forbidden int','DROP TABLE hub_data.short_term_forecast','SELECT * FROM langgraph.checkpoints','UPDATE hub_data.region_grid SET lv1=lv1','SET ROLE map_hub_owner'], 'map_agent_runtime':['CREATE TABLE langgraph.r2_forbidden(id int)','ALTER TABLE langgraph.checkpoints ADD COLUMN forbidden int','DROP TABLE langgraph.checkpoints','SELECT * FROM hub_data.short_term_forecast','INSERT INTO langgraph.checkpoint_migrations VALUES(1000)','SET ROLE map_agent_owner'], 'map_pg_exporter':['SELECT * FROM hub_data.short_term_forecast','SELECT * FROM langgraph.checkpoints'], 'map_admin_runtime':['UPDATE hub_data.subscribed_grids SET is_active=is_active','SELECT * FROM langgraph.checkpoints','CREATE TABLE admin_data.r2_forbidden(id int)']}
        for role,statements in forbidden.items():
            with psycopg.connect(role_dsn(role),autocommit=True) as conn:
                for statement in statements+['CREATE TABLE public.r2_forbidden(id int)','SELECT * FROM public.r2_user_sentinel']:
                    denied(conn,statement)
                if role=='map_pg_exporter':
                    assert conn.execute('SELECT count(*) FROM pg_stat_database').fetchone()[0]>0
                if role=='map_admin_runtime':
                    conn.execute('SELECT count(*) FROM hub_data.short_term_forecast')
                    row=conn.execute("INSERT INTO admin_data.admin_accounts(username,password_hash) VALUES('r2-fixture','synthetic-not-a-real-password') RETURNING id").fetchone()
                    conn.execute("INSERT INTO admin_data.admin_sessions(session_id,account_id,expires_at) VALUES('00000000-0000-0000-0000-000000000001',%s,now()+interval '1 hour')",(row[0],))
                    conn.execute("DELETE FROM admin_data.admin_sessions WHERE account_id=%s",(row[0],))
                    conn.execute("INSERT INTO admin_data.audit_logs(actor,action,status) VALUES('r2-fixture','permission-test','ok')")
            results['checks'][role+'_42501_count']=len(statements)+2
            results['checks'][role+'_temporary_privilege']=operator.execute("SELECT has_database_privilege(%s,current_database(),'TEMP')",(role,)).fetchone()[0]
        for service,owner,schema in [('hub','map_hub_owner','hub_data'),('agent','map_agent_owner','langgraph'),('admin','map_admin','admin_data')]:
            with psycopg.connect(role_dsn('map_'+service+'_migrator'),autocommit=True) as conn:
                conn.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(owner)))
                conn.execute(sql.SQL('CREATE TABLE {}.r2_migrator_probe(id int)').format(sql.Identifier(schema)))
                conn.execute(sql.SQL('DROP TABLE {}.r2_migrator_probe').format(sql.Identifier(schema)))
                for statement in ['CREATE TABLE public.r2_forbidden(id int)','CREATE SCHEMA r2_forbidden','SELECT * FROM public.r2_user_sentinel']:
                    denied(conn,statement)
            results['checks'][service+'_migrator_scoped_ddl_and_42501']='PASS'
        assert operator.execute('SELECT value FROM public.r2_user_sentinel WHERE id=1').fetchone()[0]=='synthetic-preserved' 
        results['checks']['sentinel_preserved']='PASS'
    results['status']='PASS';results['completed_utc']=datetime.now(timezone.utc).isoformat()
    args.output.write_text(json.dumps(results,indent=2)+'\n')
    print('R2 isolated database contract PASS')


if __name__=='__main__':
    try:
        main()
    except Exception as error:
        failure={'status':'FAIL','phase':PHASE,'error_class':type(error).__name__,'utc':datetime.now(timezone.utc).isoformat()}
        if isinstance(error,RuntimeError):
            try:
                failure['diagnostic']=json.loads(str(error))
            except (ValueError,TypeError):
                pass
        if REPORT:
            REPORT.write_text(json.dumps(failure,indent=2)+'\n')
        print('R2 isolated database contract FAIL: '+type(error).__name__+' phase='+PHASE,file=sys.stderr)
        raise SystemExit(1) from None
