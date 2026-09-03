"""pytest 공용 fixture / 환경 부트스트랩.

pytest 는 테스트 컬렉션 단계에서 본 모듈을 자동 임포트한다.
본 모듈은 모든 테스트가 app.config.Settings 를 안전하게 로드할 수 있도록
필수 환경변수의 기본값을 setdefault 로 주입한다(이미 정의되어 있으면 보존).

호출 관계:
  - 각 테스트가 `from app...` 임포트를 만나기 전에 본 파일이 먼저 평가되어
    Settings(BaseSettings) 가 필수 필드 검증을 통과한다.
"""
from __future__ import annotations

import os

import pytest

# HUB_DATABASE_URL — Settings 의 필수 필드. 실제 DB 접속은 일어나지 않으나
# Settings 가 검증 단계에서 문자열을 요구하므로 더미 DSN 을 채워 둔다.
os.environ.setdefault(
    "HUB_DATABASE_URL",
    "postgresql+psycopg://test:test@localhost/test",
)
# KMA_SERVICE_KEY — Settings 의 필수 필드. KMAClient 인스턴스화를 위한 더미 값.
os.environ.setdefault("KMA_SERVICE_KEY", "test-service-key")
# INTERNAL_SERVICE_TOKEN — internal_guard 테스트에서 헤더 일치 시나리오용.
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "test-internal-token")

# app.config 는 위 setdefault 가 모두 끝난 뒤에 임포트해야 한다. 먼저 부르면
# Settings 가 필수 필드를 못 찾고 검증 단계에서 떨어진다.
from app.config import settings  # noqa: E402


@pytest.fixture
def stub_mode(monkeypatch):
    """외부 호출 없이 스텁 경로만 타도록 고정한다.

    "인증키가 비어 있으면 스텁"이라는 성질에 기대면 안 된다. Settings 가
    env_file=".env" 를 읽으므로, 개발자 로컬에 실 키가 놓여 있으면 같은
    테스트가 실호출 분기로 흘러 결과가 환경에 따라 달라진다.

    실호출 분기를 타야 하는 테스트는 반대로 각 모듈의 real_key 픽스처를 쓴다.
    """
    monkeypatch.setattr(settings, "PLACES_STUB_MODE", True)
