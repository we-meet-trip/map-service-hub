# app.utils — hub-service 의 순수 유틸 패키지.
#
# 본 패키지는 외부 의존성이 없는 계산/파싱 헬퍼만 모은다.
# 현재 멤버:
#   app.utils.kma_grid — KMA 격자(nx, ny) 좌표 변환 + 발표시각 파서
#                        (gps_to_grid / parse_kma_base_at /
#                         parse_kma_fcst_at / parse_kma_tm_fc / KST)
#
# 파일 자체는 패키지 마커로만 동작하며 export 는 없다.
