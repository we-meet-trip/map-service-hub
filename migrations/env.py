"""Alembic 환경 진입점.

Alembic CLI(`alembic upgrade`/`downgrade`) 가 임포트하는 모듈이며,
다음 책임을 가진다:

  1) alembic.ini 의 로깅 설정 적용
  2) 환경변수 HUB_DATABASE_URL 을 읽어 동기 드라이버 명세로 치환 후
     Alembic 의 sqlalchemy.url 옵션으로 주입
  3) offline / online 모드 분기 실행
  4) online 모드 시작 시 hub_data 스키마 보장 + alembic_version 테이블을
     hub_data 스키마 아래에 둠

호출 관계:
  - revision 파일 (migrations/revisions/*.py) 들은 본 모듈을 거쳐 실행된다.
  - revision 파일들은 app 코드를 임포트하지 않으므로,
    target_metadata 는 None 으로 둔다 (autogenerate 사용 안 함).
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool, text

# Alembic 이 alembic.ini 로부터 채워주는 설정 컨테이너.
config = context.config

# alembic.ini 의 [loggers]/[handlers]/[formatters] 섹션을 활성화.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 환경변수 HUB_DATABASE_URL 은 비동기 드라이버 명세일 수 있다.
# Alembic 은 동기 엔진으로 실행되므로 드라이버 토큰을 동기형으로 치환한다.
dsn = os.environ.get("HUB_DATABASE_URL")
if not dsn:
    raise RuntimeError("HUB_DATABASE_URL 환경변수가 설정되지 않았다.")
sync_dsn = (
    dsn.replace("+psycopg_async", "+psycopg")
       .replace("postgresql+asyncpg", "postgresql+psycopg")
)
# 치환한 DSN 을 alembic.ini 의 sqlalchemy.url 자리에 주입.
config.set_main_option("sqlalchemy.url", sync_dsn)

# autogenerate 를 쓰지 않으므로 metadata 는 비워 둔다.
# 본 프로젝트의 모든 revision 은 op.execute 로 raw SQL 을 실행한다.
target_metadata = None


def run_migrations_offline() -> None:
    """offline 모드 — DB 에 접속하지 않고 SQL 문만 생성/실행한다.

    `alembic upgrade --sql` 처럼 트랜잭션을 거치지 않는 시나리오에서 사용.
    version_table 은 hub_data 스키마 아래에 두어 다른 서비스(예: agent,
    user) 의 마이그레이션과 충돌하지 않게 한다.
    """
    context.configure(
        url=sync_dsn,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table="alembic_version",
        version_table_schema="hub_data",
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """online 모드 — 실제 DB 에 접속해 마이그레이션을 실행한다.

    동작:
      1) NullPool 로 임시 엔진을 만들어 단일 connection 사용
      2) 트랜잭션 시작 직후 hub_data 스키마 생성(없으면)
      3) alembic_version 테이블이 hub_data 스키마 안에 있도록 설정
      4) 등록된 revision 들을 순차 실행
    """
    engine = create_engine(sync_dsn, poolclass=pool.NullPool, future=True)
    with engine.begin() as connection:
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS hub_data"))
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table="alembic_version",
            version_table_schema="hub_data",
            include_schemas=True,
        )
        context.run_migrations()


# Alembic 호출자가 --offline 옵션을 줬는지에 따라 분기.
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
