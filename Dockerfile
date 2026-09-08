# syntax=docker/dockerfile:1.7
# map-service-hub — FastAPI + PostGIS + APScheduler 외부 데이터 게이트웨이
#
# 빌드 전략: 멀티스테이지 (builder → runtime).
#   builder 단계에서 wheel을 준비한다. Shapely는 GEOS를 포함한 공식 binary wheel만 허용한다.
#   runtime 단계는 컴파일 결과 휠만 가져가 설치하므로,
#   최종 이미지에서는 컴파일러를 포함하지 않아 크기와 공격 표면이 작다.
#
# 실행 사용자: 비루트 사용자 `app` (uid 10001) 로 격리한다.
# 엔트리포인트: ASGI 서버를 통해 app.main:app 을 8000 포트에 바인딩.

# 베이스 이미지의 Python 버전을 빌드 인자로 노출.
ARG PYTHON_VERSION=3.12

# === builder 스테이지 ===
# 의존성을 .whl 로 미리 빌드해 두는 단계.
FROM python:${PYTHON_VERSION}-slim AS builder
# pip 캐시를 끄고(이미지 크기 절약), .pyc 생성을 막는다.
ENV PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
# Other dependencies may compile, but Shapely must use its bundled GEOS wheel.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
# requirements.txt 의 모든 패키지를 wheel 로 빌드해 /wheels 에 보관.
RUN pip wheel --only-binary=shapely --wheel-dir=/wheels -r requirements.txt

# === runtime 스테이지 ===
# 실제 실행될 최소 이미지. builder 와 분리되어 컴파일러가 포함되지 않는다.
FROM python:${PYTHON_VERSION}-slim AS runtime
# 로그 즉시 flush(PYTHONUNBUFFERED) — 컨테이너 stdout 으로 흘러가도록.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# No app/requirement uses PROJ; Shapely's official wheel bundles GEOS.
# Avoid the unused PROJ -> TIFF/curl/SSH system dependency chain.
RUN useradd -m -u 10001 app
WORKDIR /app
# builder 에서 만든 wheel 만 복사해 와서 오프라인 설치(--no-index).
# 설치 후 wheel 디렉터리는 삭제해 최종 이미지 크기를 줄인다.
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
 && pip check \
 && rm -rf /wheels
# Immutable serving images do not invoke Perl or account-management tools.
# Remove the installed package (including its dpkg record), never just scanner metadata.
# This runs only after all apt/user creation steps; do not run it on a serving host.
RUN apt-get purge -y --allow-remove-essential perl-base \
 && find /usr/bin /usr/sbin -xdev -type f -perm /6000 -exec chmod a-s {} + \
 && test ! -e /usr/bin/perl \
 && test -z "$(find /usr/bin /usr/sbin -xdev -type f -perm /6000 -print -quit)"
# 애플리케이션 소스. 소유자를 app:app 로 지정해 비루트 실행 환경과 정합.
COPY --chown=app:app app ./app
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app alembic.ini ./alembic.ini
USER app
RUN --network=none HUB_DATABASE_URL=postgresql+psycopg://build:build@127.0.0.1/build KMA_SERVICE_KEY=build python - <<'PYCODE'
import ctypes.util
import app.main
from shapely.geometry import Point
from geoalchemy2.shape import from_shape, to_shape
assert to_shape(from_shape(Point(0, 0), srid=4326)).equals(Point(0, 0))
for name in ['proj', 'tiff', 'curl', 'ssh2']:
    assert ctypes.util.find_library(name) is None, name
print('Hub import and synthetic GEOS/WKB roundtrip passed without PROJ/TIFF/curl/SSH')
PYCODE
EXPOSE 8000
# /health 엔드포인트(app.main:health) 가 200 을 반환해야 healthy.
# 표준 라이브러리만 사용해 별도 curl 등을 설치하지 않는다.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=5).status==200 else 1)"
# ASGI 서버로 FastAPI 앱 기동. 모든 인터페이스에 바인딩.
ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
