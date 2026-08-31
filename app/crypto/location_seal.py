"""서비스 사이를 오가는 좌표를 감싼 봉투를 연다.

왜 필요한가. 좌표는 그동안 요청 줄에 그대로 실려 다녔다. 컨테이너 사이라
바깥에서 들여다볼 수는 없지만, 같은 망에 붙은 다른 컨테이너와 중간에 남는
기록에는 그대로 드러난다. 봉투로 감싸면 그 두 자리에서 사라진다.

봉투를 만들 수 있다는 것 자체가 자격이 된다. 열쇠를 가진 서비스만 만들 수
있고, 열쇠를 가진 서비스만 열 수 있다. 그래서 좌표를 쓰려면 서비스 인증을
먼저 거쳐야 한다는 조건이 형식 자체로 강제된다.

형식:
    v1.<iv>.<암호문+검증표>          두 조각 모두 URL 안전 base64(채움문자 없음)

안에 담기는 것은 좌표와 만든 시각이다. 만든 시각을 함께 담는 이유는, 한 번
지나간 봉투를 나중에 그대로 다시 보내는 것을 막기 위해서다. 좌표만 담으면
같은 봉투가 영원히 유효하다.

자바 쪽(BFF)의 LocationSeal 과 같은 형식을 쓴다. 한쪽만 바꾸면 그 순간부터
모든 좌표 요청이 거절되므로, 형식을 바꿀 때는 반드시 함께 바꾼다.
"""
from __future__ import annotations

import base64
import json
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings

VERSION = "v1"
AAD = b"map|loc|v1"
KEY_BYTES = 32
# 봉투가 유효한 시간. 요청 하나가 오가는 데 드는 시간보다 넉넉하되, 지나간
# 봉투를 주워 다시 쓰는 창은 좁게 둔다.
MAX_AGE_SECONDS = 300


class SealError(Exception):
    """봉투를 열 수 없다. 사유는 밖으로 내보내지 않는다 — 어느 단계에서
    막혔는지 알려 주면 그것만으로 탐색의 실마리가 된다."""


def _key() -> bytes:
    raw = settings.LOCATION_WIRE_KEY.get_secret_value()
    if not raw:
        raise SealError("wire key not configured")
    try:
        key = base64.b64decode(raw)
    except Exception as exc:  # noqa: BLE001 - 어떤 형태든 설정 오류로 접는다
        raise SealError("wire key is not base64") from exc
    if len(key) != KEY_BYTES:
        raise SealError("wire key must decode to 32 bytes")
    return key


def _b64url_decode(text: str) -> bytes:
    # 채움문자를 떼고 보내므로 길이를 4의 배수로 되돌린 뒤 푼다.
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def is_sealed(value: str | None) -> bool:
    """봉투 모양인지만 본다. 열어 보지는 않는다."""
    return bool(value) and value.startswith(VERSION + ".") and value.count(".") == 2


def open_seal(token: str) -> dict:
    """봉투를 열어 안에 담긴 값을 돌려준다.

    열쇠가 다르거나, 내용이 손대어졌거나, 너무 오래된 봉투면 SealError 를
    올린다. 세 경우를 구분하지 않는 것은 밖에서 그 차이를 보고 무엇을 바꿔
    가며 시도할 수 있기 때문이다.
    """
    if not is_sealed(token):
        raise SealError("not a sealed value")
    _, iv_part, ct_part = token.split(".", 2)
    try:
        plain = AESGCM(_key()).decrypt(
            _b64url_decode(iv_part), _b64url_decode(ct_part), AAD
        )
    except (InvalidTag, ValueError) as exc:
        raise SealError("cannot open sealed value") from exc

    try:
        payload = json.loads(plain)
    except json.JSONDecodeError as exc:
        raise SealError("sealed value is not readable") from exc
    if not isinstance(payload, dict):
        raise SealError("sealed value is not readable")

    issued = payload.get("iat")
    if not isinstance(issued, (int, float)):
        raise SealError("sealed value has no issue time")
    age = time.time() - issued
    # 앞뒤로 흔들리는 시계를 감안해 미래 쪽도 조금 허용한다. 서버끼리 시계가
    # 몇 초 어긋나는 것만으로 좌표 요청이 통째로 막히면 안 된다.
    if age > MAX_AGE_SECONDS or age < -60:
        raise SealError("sealed value expired")
    return payload
