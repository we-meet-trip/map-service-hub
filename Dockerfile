# syntax=docker/dockerfile:1.7
# map-service-hub — FastAPI + PostGIS + APScheduler 외부 데이터 게이트웨이
#
# 빌드 전략: 멀티스테이지 (builder → runtime).
#   builder 단계는 C 컴파일러와 라이브러리 헤더(libgeos-dev/libproj-dev)를 설치해
#   shapely/geoalchemy2 등의 휠을 미리 컴파일한다.
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
# 네이티브 의존성: build-essential(gcc), libgeos-dev(공간 연산), libproj-dev(좌표 변환).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential libgeos-dev libproj-dev \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
# requirements.txt 의 모든 패키지를 wheel 로 빌드해 /wheels 에 보관.
RUN pip wheel --wheel-dir=/wheels -r requirements.txt

# === runtime 스테이지 ===
# 실제 실행될 최소 이미지. builder 와 분리되어 컴파일러가 포함되지 않는다.
FROM python:${PYTHON_VERSION}-slim AS runtime
# 로그 즉시 flush(PYTHONUNBUFFERED) — 컨테이너 stdout 으로 흘러가도록.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# 런타임에 필요한 공유 라이브러리만 설치(헤더 제외).
# uid 10001 의 비루트 사용자 `app` 생성으로 권한 격리.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libgeos-c1v5 libproj25 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 10001 app
WORKDIR /app
# builder 에서 만든 wheel 만 복사해 와서 오프라인 설치(--no-index).
# 설치 후 wheel 디렉터리는 삭제해 최종 이미지 크기를 줄인다.
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
 && rm -rf /wheels
# 애플리케이션 소스. 소유자를 app:app 로 지정해 비루트 실행 환경과 정합.
COPY --chown=app:app app ./app
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app alembic.ini ./alembic.ini
USER app
EXPOSE 8000
# /health 엔드포인트(app.main:health) 가 200 을 반환해야 healthy.
# 표준 라이브러리만 사용해 별도 curl 등을 설치하지 않는다.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=5).status==200 else 1)"
# ASGI 서버로 FastAPI 앱 기동. 모든 인터페이스에 바인딩.
ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
