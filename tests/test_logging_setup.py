"""루트 로거 구성 단위 테스트.

uvicorn 은 자기 로거만 구성하고 루트 로거에는 핸들러를 붙이지 않는다. 그래서 루트를
따로 세우지 않으면 `app.*` 로거로 남긴 기록이 출력 대상을 못 찾아 전량 사라진다 —
폴링 성공·실패, 외부 API 오류, 스텁 전환이 전부 보이지 않게 된다.

여기서 고정하는 것 세 가지:
  - 핸들러가 없을 때는 붙인다(로그가 살아난다).
  - 이미 있으면 손대지 않는다(같은 줄이 두 번 나오지 않는다).
  - 붙인 핸들러에도 좌표 가림이 걸린다(접근 로그에만 걸면 규칙이 반쪽이 된다).

pytest 의 로깅 플러그인이 테스트 실행 직전에 루트 핸들러를 붙이므로, 정리는 픽스처가
아니라 테스트 본문 안에서 해야 "핸들러가 없는 루트"를 실제로 만들 수 있다.
"""
from __future__ import annotations

import contextlib
import logging

from app.main import _configure_logging


@contextlib.contextmanager
def bare_root():
    """루트 로거를 비운 채로 넘기고, 빠져나올 때 원래 구성으로 되돌린다."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    root.handlers = []
    try:
        yield root
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def test_attaches_handler_when_root_is_bare():
    with bare_root() as root:
        _configure_logging("INFO")

        assert root.handlers, "핸들러가 없으면 app.* 로그가 전량 유실된다"
        assert root.level == logging.INFO


def test_leaves_existing_handlers_alone():
    with bare_root() as root:
        existing = logging.NullHandler()
        root.addHandler(existing)

        _configure_logging("INFO")

        assert root.handlers == [existing], "덧붙이면 같은 로그가 두 줄씩 나온다"


def test_level_follows_setting():
    with bare_root() as root:
        _configure_logging("warning")

        assert root.level == logging.WARNING


def test_attached_handler_redacts_coordinates():
    """붙인 핸들러가 좌표를 가리는지 실제 레코드를 통과시켜 확인한다."""
    with bare_root() as root:
        _configure_logging("INFO")
        handler = root.handlers[0]

        record = logging.LogRecord(
            name="app.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="weather lookup lat=37.5665 lng=126.9780",
            args=(),
            exc_info=None,
        )
        assert handler.filters, "붙인 핸들러에 좌표 가림이 없으면 위치가 로그로 샌다"
        for log_filter in handler.filters:
            log_filter.filter(record)

        assert "37.5665" not in record.msg
        assert "126.9780" not in record.msg
        assert "lat=***" in record.msg
        assert "lng=***" in record.msg
