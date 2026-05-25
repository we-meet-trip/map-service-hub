# 데이터베이스 접근 레이어를 본 모듈에 정의한다.
# SQLAlchemy async engine과 PostGIS 공간 모델을 구성하여 외부 데이터의
# 영속 저장과 공간 질의를 제공한다.


class HubDB:
    """SQLAlchemy async engine과 session factory를 보유하는 어댑터.

    엔진 라이프사이클·세션 컨텍스트·트랜잭션 경계를 캡슐화한다.
    """
    pass
