"""폴리라인 점 수 축소 유틸.

라우팅 엔진(OSRM)이 돌려주는 경로 지오메트리는 회전점 해상도로 촘촘해
수 km 경로가 수백~수천 점에 이를 수 있다. B1(client) 페이로드와 룰
엔진의 점 수 상한을 지키기 위해, 시각적 형태를 보존하면서 점 수를 줄인다.

두 단계로 축소한다:
  1) Douglas-Peucker — 직선에서 epsilon(도 단위) 이하로 벗어난 중간 점 제거.
  2) 여전히 max_points 초과면 양 끝을 보존한 균등 스트라이드 다운샘플.

순수 함수만 두어(상태·외부 의존 없음) 단위 테스트가 쉽다. 좌표는
[lat, lng] 2원소 시퀀스로 다룬다(스왑은 호출부 책임).
"""
from __future__ import annotations

from typing import Sequence

Point = Sequence[float]  # [lat, lng]


def _perpendicular_distance(pt: Point, line_start: Point, line_end: Point) -> float:
    """pt 에서 (line_start, line_end) 직선까지의 수직 거리(도 단위 근사).

    좌표 단위(도)를 그대로 유클리드 평면으로 취급한다. 한국 위도대의
    짧은 leg 세그먼트에서는 이 근사로 충분하며(경도 왜곡 무시), 단순화
    임계값도 같은 단위로 준다. 두 끝점이 같으면 점-점 거리를 반환.
    """
    x0, y0 = pt[0], pt[1]
    x1, y1 = line_start[0], line_start[1]
    x2, y2 = line_end[0], line_end[1]
    dx, dy = x2 - x1, y2 - y1
    if dx == 0.0 and dy == 0.0:
        return ((x0 - x1) ** 2 + (y0 - y1) ** 2) ** 0.5
    # |cross| / |line| = 점에서 직선까지의 수직 거리.
    num = abs(dy * x0 - dx * y0 + x2 * y1 - y2 * x1)
    den = (dx * dx + dy * dy) ** 0.5
    return num / den


def _douglas_peucker(points: list[Point], epsilon: float) -> list[Point]:
    """Douglas-Peucker 재귀 단순화. 양 끝점은 항상 보존."""
    if len(points) < 3:
        return list(points)
    # 양 끝을 잇는 직선에서 가장 멀리 벗어난 중간 점을 찾는다.
    dmax = 0.0
    index = 0
    for i in range(1, len(points) - 1):
        d = _perpendicular_distance(points[i], points[0], points[-1])
        if d > dmax:
            dmax = d
            index = i
    # 임계값을 넘으면 그 점 기준으로 분할해 각 구간을 재귀 단순화.
    if dmax > epsilon:
        left = _douglas_peucker(points[: index + 1], epsilon)
        right = _douglas_peucker(points[index:], epsilon)
        return left[:-1] + right
    # 넘지 않으면 이 구간은 양 끝 두 점으로 충분.
    return [points[0], points[-1]]


def _stride_downsample(points: list[Point], max_points: int) -> list[Point]:
    """양 끝을 보존한 채 균등 간격으로 max_points 이하로 다운샘플."""
    n = len(points)
    if n <= max_points:
        return list(points)
    if max_points <= 2:
        return [points[0], points[-1]]
    # 마지막 점은 별도로 붙이고 앞쪽에서 max_points-1 개를 균등 추출.
    step = (n - 1) / (max_points - 1)
    out: list[Point] = [points[int(round(i * step))] for i in range(max_points - 1)]
    out.append(points[-1])
    # 반올림으로 인접 인덱스가 겹칠 수 있어 연속 중복 좌표를 제거.
    deduped: list[Point] = []
    for p in out:
        if not deduped or deduped[-1][0] != p[0] or deduped[-1][1] != p[1]:
            deduped.append(p)
    return deduped


def simplify(
    points: Sequence[Point],
    epsilon: float = 0.00005,
    max_points: int = 200,
) -> list[Point]:
    """경로 점 목록을 형태 보존하며 축소한다.

    points: [lat, lng] 점들의 시퀀스.
    epsilon: Douglas-Peucker 임계값(도 단위). 0.00005°≈5.5m.
    max_points: 최종 상한. DP 후에도 초과하면 균등 다운샘플.
    반환: 2점 이상을 보장(원본이 2점 이상이면). 원본이 1점 이하면 그대로.
    """
    pts = [list(p) for p in points]
    if len(pts) <= 2:
        return pts
    reduced = _douglas_peucker(pts, epsilon)
    if len(reduced) > max_points:
        reduced = _stride_downsample(reduced, max_points)
    return reduced
