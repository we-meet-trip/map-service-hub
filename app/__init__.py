# app — hub-service 의 최상위 애플리케이션 패키지.
#
# 본 패키지는 다음 하위 모듈로 구성된다:
#   app.main           — FastAPI 진입점 + lifespan (스케줄러/DB 라이프사이클)
#   app.config         — 환경변수 → Settings 매핑
#   app.routers.*      — 외부/내부 HTTP 엔드포인트
#   app.db.*           — 비동기 RDB 세션 / forecast 테이블 read/upsert
#   app.clients.*      — KMA / Kakao / Naver 외부 API 클라이언트
#   app.scheduler.*    — APScheduler cron job + 폴링 루프
#   app.cache.*        — L1(Redis) / L2(RDB) 캐시 어댑터
#   app.utils.*        — KMA 격자 좌표 변환 등 순수 유틸
#   app.schemas.*      — 응답 Pydantic 모델
#   app.codes.*        — KMA 카테고리 코드 상수
#
# 파일 자체는 패키지 마커로만 동작하며 export 는 없다.
