"""hub-service 폴링 지표 모듈.

외부 예보를 받아 오는 백그라운드 루프의 성패를 수치로 남긴다.

폴링은 요청 경로 밖에서 돌기 때문에, HTTP 지표만 보고 있으면 예보가
며칠째 안 들어와도 아무 신호가 없다. 전체 행 수나 최신 발표 시각을
합계로 보는 것도 부족하다 — 일부 구역만 계속 실패하면 다른 구역 덕분에
합계는 정상으로 보인다. 그래서 종류별로 실패 횟수와 마지막 성공 시각을
따로 노출한다.

호출 관계:
  - app.scheduler.hub_scheduler 가 적재 성공/실패마다 갱신한다.
  - app.main 의 Instrumentator 가 /metrics 로 노출한다(기본 레지스트리를
    공유하므로 별도 등록이 필요 없다).
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge

# kind 라벨: "short" | "mid_land" | "mid_temp"
poll_failure_total = Counter(
    "hub_kma_poll_failure_total",
    "Failed KMA polling attempts by forecast kind",
    ["kind"],
)

poll_success_total = Counter(
    "hub_kma_poll_success_total",
    "Successful KMA polling attempts by forecast kind",
    ["kind"],
)

# 마지막으로 한 건이라도 적재에 성공한 시각(unix epoch, 초).
# 값이 오래 멈춰 있으면 그 종류의 예보가 갱신되지 않고 있다는 뜻이다.
last_success_timestamp = Gauge(
    "hub_kma_last_success_timestamp_seconds",
    "Unix timestamp of the last successful KMA poll by forecast kind",
    ["kind"],
)

# 발표분을 끝내 채우지 못한 채 시간 예산을 소진한 대상 수.
# 다음 발표까지 그 지역이 비어 있게 된다.
poll_unloaded = Gauge(
    "hub_kma_unloaded_targets",
    "Targets still missing when the polling deadline was reached",
    ["kind"],
)


def record_success(kind: str, at_epoch: float) -> None:
    """성공 1건을 기록한다.

    kind: 예보 종류 라벨.
    at_epoch: 성공 시각(unix epoch 초). 호출자가 KST 인지 여부와 무관하게
        epoch 로 넘긴다 — 지표는 타임존이 없는 절대 시각으로 다룬다.
    """
    poll_success_total.labels(kind=kind).inc()
    last_success_timestamp.labels(kind=kind).set(at_epoch)


def record_failure(kind: str) -> None:
    """실패 1건을 기록한다."""
    poll_failure_total.labels(kind=kind).inc()


def record_unloaded(kind: str, count: int) -> None:
    """시간 예산 소진 시점에 남은 미적재 대상 수를 기록한다."""
    poll_unloaded.labels(kind=kind).set(count)
