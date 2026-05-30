# tests — hub-service 의 단위 테스트 패키지.
#
# 본 패키지의 conftest.py 가 임포트 시점에 테스트용 환경변수를 주입하므로,
# 개별 테스트는 별도 fixture 없이도 app.config.settings 를 로드할 수 있다.
#
# 멤버:
#   tests.conftest                       — 환경변수 기본값 주입 (Settings 로드용)
#   tests.test_forecast_repo_payload     — _safe_int / 중기예보 payload 분해 검증
#   tests.test_internal_guard            — 내부 endpoint CIDR 트러스트 판정
#   tests.test_kma_grid                  — KMA 격자 변환 + 발표시각 파서
#   tests.test_resolve_base              — 단기/중기 base 시각 해상 로직
#
# 파일 자체는 패키지 마커로만 동작하며 export 는 없다.
