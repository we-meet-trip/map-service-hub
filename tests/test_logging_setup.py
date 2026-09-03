"""루트 로거 구성 단위 테스트.

uvicorn 은 자기 로거만 구성하고 루트 로거에는 핸들러를 붙이지 않는다. 그래서 루트를
따로 세우지 않으면 `app.*` 로거로 남긴 기록이 출력 대상을 못 찾아 전량 사라진다 —
폴링 성공·실패, 외부 API 오류, 스텁 전환이 전부 보이지 않게 된다.

여기서 고정하는 것 네 가지:
  - 핸들러가 없을 때는 붙인다(로그가 살아난다).
  - 이미 있으면 손대지 않는다(같은 줄이 두 번 나오지 않는다).
  - 붙인 핸들러에도 좌표 가림이 걸린다(접근 로그에만 걸면 규칙이 반쪽이 된다).
  - 나가는 요청 로그에 실린 외부 인증키가 가려진다.

마지막 항목은 실제로 새고 있던 것을 막는다. 나가는 요청을 남기는 로거가 URL 을
통째로 찍는데, 키를 쿼리나 경로에 실어 보내는 발급처가 여럿이라 정상 응답 한 줄마다
키가 그대로 남았다.

pytest 의 로깅 플러그인이 테스트 실행 직전에 루트 핸들러를 붙이므로, 정리는 픽스처가
아니라 테스트 본문 안에서 해야 "핸들러가 없는 루트"를 실제로 만들 수 있다.
"""
from __future__ import annotations

import contextlib
import logging

import pytest

from app.main import _CoordinateRedactingFilter, _configure_logging


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


# 나가는 요청 로그에 실제로 찍히던 모양들. (원문, 남으면 안 되는 조각).
_OUTBOUND_LINES = [
    (
        "HTTP Request: GET https://api.odsay.com/v1/api/searchPubTransPathT"
        "?SX=127.0&apiKey=FAKEodsayKEYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        ' "HTTP/1.1 200 OK"',
        "FAKEodsayKEY",
    ),
    (
        "HTTP Request: GET http://openapi.seoul.go.kr:8088"
        "/7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a/json/bikeList/1/1000/"
        ' "HTTP/1.1 200 OK"',
        "7a7a7a7a7a7a7a7a",
    ),
    (
        "HTTP Request: GET https://apis.data.go.kr/1360000/x"
        '?serviceKey=7a7a7a7a7a7a7a7a&numOfRows=1 "200 OK"',
        "7a7a7a7a7a7a7a7a",
    ),
]


@pytest.mark.parametrize("line,secret", _OUTBOUND_LINES)
def test_outbound_request_log_hides_credentials(line, secret):
    """나가는 요청 로그에서 인증키가 사라진다.

    키가 퍼센트 인코딩된 채 찍히는 경우까지 본다. 인코딩된 문자를 빼고
    매칭하면 값의 앞부분만 가려지고 나머지가 로그에 남는다.
    """
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=line,
        args=(),
        exc_info=None,
    )
    _CoordinateRedactingFilter().filter(record)

    assert secret not in record.msg
    assert "***" in record.msg


# 좌표를 담아 나가던 접근 로그 모양들. 이름 앞에 말이 붙은 것까지 본다.
_COORD_LINES = [
    '"GET /v1/weather/now?lat=37.5665&lng=126.9780 HTTP/1.1" 200 OK',
    '"GET /v1/mobility/pm-vehicles?lat=-37.5&lng=126.978&radius_m=1000"',
    '"GET /v1/transit/subway?start_lat=37.5665&start_lng=126.978'
    '&end_lat=37.5000&end_lng=127.0300 HTTP/1.1" 200 OK',
    '"GET /x?latitude=37.5665&longitude=126.9780"',
]


@pytest.mark.parametrize("line", _COORD_LINES)
def test_access_log_hides_every_coordinate_name(line):
    """접근 로그의 좌표가 이름 모양과 상관없이 전부 가려진다.

    출발·도착을 함께 받는 조회는 start_lat 처럼 이름 앞에 말을 붙이는데,
    밑줄 뒤에는 단어 경계가 생기지 않아 짧은 이름만 찾는 규칙에는 걸리지
    않는다. 그 자리로 사용자의 이동 경로가 통째로 새던 것을 막는다.
    """
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=line,
        args=(),
        exc_info=None,
    )
    _CoordinateRedactingFilter().filter(record)

    for fragment in ("37.5665", "126.9780", "126.978", "37.5000", "127.0300"):
        assert fragment not in record.msg
    assert "***" in record.msg


class _UrlLikeArg:
    """문자열이 아닌 인자. 나가는 요청 로그가 주소를 이 모양으로 넘긴다."""

    def __init__(self, value: str) -> None:
        self._value = value

    def __str__(self) -> str:
        return self._value


@pytest.mark.parametrize("line,secret", _OUTBOUND_LINES)
def test_non_string_argument_is_also_hidden(line, secret):
    """인자가 문자열이 아니어도 가려진다.

    실제로 새고 있던 자리가 여기다. 문자열 인자만 훑으면 주소 객체가 그대로
    통과해, 완성된 문장에는 키가 남는다.
    """
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="HTTP Request: GET %s",
        args=(_UrlLikeArg(line),),
        exc_info=None,
    )
    _CoordinateRedactingFilter().filter(record)

    assert secret not in record.getMessage()
    assert "***" in record.getMessage()


def test_ordinary_record_keeps_lazy_formatting():
    """가릴 것이 없는 레코드는 인자를 그대로 둔다.

    모든 레코드를 미리 조립하면 지연 서식의 이점이 사라지고, 인자를 따로
    보는 처리기가 있으면 그 값도 함께 잃는다.
    """
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="places cache hit key=%s count=%d",
        args=("kakao:abc", 3),
        exc_info=None,
    )
    _CoordinateRedactingFilter().filter(record)

    assert record.args == ("kakao:abc", 3)
    assert record.msg == "places cache hit key=%s count=%d"


def test_outbound_logger_has_the_filter_attached():
    """가림이 걸리는 자리를 고정한다.

    필터는 레코드를 만든 로거에서 먼저 돈다. 나가는 요청을 남기는 로거에
    직접 걸어 두지 않으면, 그 로거가 자기 핸들러를 갖게 되는 순간 가림이
    빠진 채로 출력된다.
    """
    attached = logging.getLogger("httpx").filters
    assert any(
        isinstance(f, _CoordinateRedactingFilter) for f in attached
    ), "나가는 요청 로거에 가림이 없으면 정상 응답마다 키가 남는다"
