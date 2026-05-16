# 외부 데이터 조회 라우터를 본 모듈에 정의한다.
# 장소·날씨·경로·룰 4개 도메인에 대한 REST 엔드포인트를 묶어 단일
# APIRouter 인스턴스로 노출한다.

from fastapi import APIRouter

router = APIRouter()
