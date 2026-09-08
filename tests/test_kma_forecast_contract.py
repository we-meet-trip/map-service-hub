"""Stored forecast correctness: no external API, secrets or user data."""
import asyncio
from datetime import datetime, timedelta
import socket
import ssl

import httpx
import pytest

from app.clients.hub_clients import KMAApiError, KMAClient, _transport_code
from app.routers.hub_routers import _coerce_int
from app.rules.rule_engine import indoor_bonus
from app.scheduler import hub_scheduler
from tests.test_weather_route import _TODAY, KST, _stub, _get, _full_day, _region


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-Infinity', 900, -900, True])
def test_invalid_provider_values_remain_missing(value):
    assert _coerce_int(value) is None


@pytest.mark.parametrize('error,expected', [
    (httpx.ConnectTimeout('secret'), 'CONNECT_TIMEOUT'),
    (httpx.ReadTimeout('secret'), 'READ_TIMEOUT'),
    (httpx.WriteTimeout('secret'), 'WRITE_TIMEOUT'),
    (httpx.PoolTimeout('secret'), 'POOL_TIMEOUT'),
    (httpx.ConnectError('secret'), 'CONNECT_ERROR'),
    (httpx.RemoteProtocolError('secret'), 'PROTOCOL_ERROR'),
])
def test_transport_errors_have_safe_specific_codes(error, expected):
    assert _transport_code(error) == expected


@pytest.mark.parametrize('cause,expected', [(socket.gaierror('secret'), 'DNS_ERROR'),
                                           (ssl.SSLError('secret'), 'TLS_ERROR')])
def test_transport_cause_is_classified_without_printing_message(cause, expected):
    error = httpx.ConnectError('https://provider/?serviceKey=secret')
    error.__cause__ = cause
    assert _transport_code(error) == expected


def _envelope(items, total):
    return {'response': {'header': {'resultCode': '00'}, 'body': {
        'items': {'item': items}, 'totalCount': total}}}


@pytest.mark.asyncio
async def test_extended_forecast_paginates_before_return(monkeypatch):
    async with KMAClient('fake') as client:
        pages = []
        async def get(_url, params):
            page = params['pageNo']; pages.append(page)
            return _envelope([{'fcstDate': '20260911', 'fcstTime': '1200',
                               'category': 'TMP' if page == 1 else 'POP'}], 2)
        monkeypatch.setattr(client, '_get_json', get)
        result = await client.fetch_short_term(60, 127, '20260907', '1700')
        assert len(result) == 2 and pages == [1, 2]


@pytest.mark.asyncio
async def test_repeated_page_never_marks_edition_complete(monkeypatch):
    async with KMAClient('fake') as client:
        async def get(_url, _params):
            return _envelope([{'fcstDate': '20260911', 'fcstTime': '1200', 'category': 'TMP'}], 2)
        monkeypatch.setattr(client, '_get_json', get)
        with pytest.raises(KMAApiError, match='DUPLICATE_ITEMS'):
            await client.fetch_short_term(60, 127, '20260907', '1700')


@pytest.mark.asyncio
async def test_transport_exception_and_raw_provider_body_do_not_escape(monkeypatch):
    async with KMAClient('fake') as client:
        async def fail(*_a, **_k):
            raise httpx.ReadTimeout('https://x/?serviceKey=TOP_SECRET')
        monkeypatch.setattr(client._client, 'get', fail)
        with pytest.raises(KMAApiError) as result:
            await client._get_json('https://example.invalid', {})
        assert result.value.code == 'READ_TIMEOUT'
        assert 'TOP_SECRET' not in str(result.value)
        assert result.value.__suppress_context__


@pytest.mark.asyncio
async def test_non_json_failure_never_logs_provider_body_or_decode_context(monkeypatch):
    import traceback
    async with KMAClient('fake') as client:
        async def fail(*_a, **_k):
            return httpx.Response(200, text='<html>TOP_SECRET ServiceKey=TOP_SECRET</html>')
        monkeypatch.setattr(client._client, 'get', fail)
        with pytest.raises(KMAApiError) as result:
            await client._get_json('https://example.invalid', {})
        assert result.value.code == 'NON_JSON'
        assert result.value.msg == 'provider returned non-JSON'
        assert 'TOP_SECRET' not in ''.join(traceback.format_exception(result.value))
        assert result.value.__suppress_context__


@pytest.mark.asyncio
@pytest.mark.parametrize('total', [None, True, 1.5, '1.5', '-1', ''])
async def test_missing_or_invalid_count_cannot_mark_forecast_page_complete(monkeypatch, total):
    async with KMAClient('fake') as client:
        async def get(_url, _params):
            data = _envelope([{'fcstDate': '20260911', 'fcstTime': '1200', 'category': 'TMP'}], total)
            if total is None:
                del data['response']['body']['totalCount']
            return data
        monkeypatch.setattr(client, '_get_json', get)
        with pytest.raises(KMAApiError, match='INVALID_TOTAL'):
            await client.fetch_short_term(60, 127, '20260907', '1700')


@pytest.mark.asyncio
async def test_changing_total_cannot_mark_partial_edition_complete(monkeypatch):
    async with KMAClient('fake') as client:
        async def get(_url, params):
            page = params['pageNo']
            return _envelope([{'fcstDate': '20260911', 'fcstTime': '1200',
                               'category': 'TMP' if page == 1 else 'POP'}], 3 if page == 1 else 2)
        monkeypatch.setattr(client, '_get_json', get)
        with pytest.raises(KMAApiError, match='INVALID_TOTAL'):
            await client.fetch_short_term(60, 127, '20260907', '1700')


def test_d4_extended_short_forecast_is_used_over_mid(monkeypatch):
    day = _TODAY + timedelta(days=4)
    _stub(monkeypatch, short_rows=_full_day(day), land=(
        [{'offset': 4, 'am_pm': 'AM', 'weather': '비', 'rain_prob_pct': 90}],
        datetime.combine(_TODAY, datetime.min.time(), tzinfo=KST).replace(hour=6)))
    body = _get(day, day).json()
    assert body['daily'][0]['source'] == 'short_term'
    assert body['daily'][0]['precipitation_prob'] == 30


def test_real_target_dates_override_latest_publication_offset(monkeypatch):
    day = _TODAY + timedelta(days=4)
    # Rows from two valid editions: matching by today's/maximum issue offset
    # would combine different dates. The persisted actual date is authoritative.
    tm = datetime.combine(_TODAY, datetime.min.time(), tzinfo=KST).replace(hour=18)
    _stub(monkeypatch, land=([
        {'date': day, 'offset': 5, 'am_pm': 'AM', 'weather': '비', 'rain_prob_pct': 80},
        {'date': day + timedelta(days=1), 'offset': 5, 'am_pm': 'AM', 'weather': '맑음', 'rain_prob_pct': 0},
    ], tm))
    body = _get(day, day).json()
    assert body['daily'][0]['precipitation_prob'] == 80
    assert body['daily'][0]['temp_min'] is None
    assert 'temp_min' in body['daily'][0]['missing_fields']


def test_missing_temp_region_does_not_discard_land_forecast(monkeypatch):
    region = _region()
    from dataclasses import replace
    region = replace(region, mid_temp_reg_id='')
    day = _TODAY + timedelta(days=5)
    _stub(monkeypatch, region=region, land=([
        {'offset': 5, 'am_pm': 'AM', 'weather': '비', 'rain_prob_pct': 80}],
        datetime.combine(_TODAY, datetime.min.time(), tzinfo=KST)))
    assert _get(day, day).json()['daily'][0]['source'] == 'mid_land'


def test_outside_horizon_is_explained_without_normal_values(monkeypatch):
    _stub(monkeypatch)
    day = _TODAY + timedelta(days=11)
    body = _get(day, day).json()
    assert body['daily'] == []
    assert body['missing_reasons'][str(day)] == 'out_of_range'


@pytest.mark.asyncio
async def test_poll_guard_deduplicates_and_recovers_after_cancel():
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []
    @hub_scheduler._single_poll('fixture')
    async def poll():
        calls.append(1); entered.set(); await release.wait()
    first = asyncio.create_task(poll())
    await entered.wait()
    await poll()
    assert len(calls) == 1
    first.cancel()
    with pytest.raises(asyncio.CancelledError): await first
    release.set()
    await poll()
    assert len(calls) == 2 and 'fixture' not in hub_scheduler._polling_active


def test_missing_precipitation_preserves_base_score():
    assert indoor_bonus([{'content_id': 'a', 'indoor_flag': True, 'base_score': 0.8}], None)[0]['score'] == 0.8

@pytest.mark.parametrize('distance,duration,coords', [
    (0, 100, [[127, 37], [127.1, 37.1]]),
    (100, float('nan'), [[127, 37], [127.1, 37.1]]),
    (100, 10, [[127, 37]]),
    (100, 10, [[127, 37], [127, 37]]),
    (100, 10, [[127, 37], [float('inf'), 37.1]]),
    (100, 10, [[127, 37], [127, 91]]),
])
def test_osrm_rejects_invalid_route_without_raw_payload(distance, duration, coords):
    from app.clients.hub_clients import OsrmClient, OsrmApiError
    with pytest.raises(OsrmApiError) as result:
        OsrmClient._normalize_route({'code': 'Ok', 'routes': [{
            'distance': distance, 'duration': duration,
            'geometry': {'coordinates': coords},
        }]})
    assert result.value.code == 'INVALID_ROUTE'
    assert '127' not in str(result.value)


@pytest.mark.asyncio
async def test_osrm_failure_never_echoes_location_or_upstream_message(monkeypatch):
    from app.clients.hub_clients import OsrmClient, OsrmApiError
    async with OsrmClient('http://osrm-foot:5000') as client:
        async def fail(*_a, **_k):
            raise httpx.ReadTimeout('raw-private-coordinates')
        monkeypatch.setattr(client._client, 'get', fail)
        with pytest.raises(OsrmApiError) as result:
            await client.route(37, 127, 37.1, 127.1)
        assert result.value.code == 'READ_TIMEOUT'
        assert 'raw-private' not in str(result.value)
    with pytest.raises(OsrmApiError) as result:
        OsrmClient._normalize_route({'code': 'evil-raw-location', 'message': 'raw-private-coordinates'})
    assert result.value.code == 'PROVIDER_ERROR'
    assert 'raw-' not in str(result.value)


def test_partial_overlap_keeps_real_pop_without_invented_daily_temperatures(monkeypatch):
    from tests.test_weather_route import _short_row
    day = _TODAY + timedelta(days=4)
    _stub(monkeypatch, short_rows=[_short_row(day, 'POP', '80'), _short_row(day, 'TMP', '20')])
    item = _get(day, day).json()['daily'][0]
    assert item['precipitation_prob'] == 80
    assert item['temp_min'] is None and item['temp_max'] is None


@pytest.mark.parametrize('value', [True, float('inf'), '-999', '999'])
def test_mid_missing_sentinels_are_not_persisted_as_values(value):
    from app.db.forecast_repo import _safe_int
    assert _safe_int(value) is None


@pytest.mark.asyncio
async def test_air_http_failure_has_no_raw_provider_content(monkeypatch):
    from app.clients.hub_clients import AirKoreaClient, AirKoreaApiError
    async with AirKoreaClient('fake') as client:
        async def fail(*_a, **_k):
            return httpx.Response(403, text='TOP_SECRET raw provider content')
        monkeypatch.setattr(client._client, 'get', fail)
        with pytest.raises(AirKoreaApiError) as result:
            await client.fetch_sido_realtime('서울')
        assert result.value.code == 'HTTP_403'
        assert 'TOP_SECRET' not in str(result.value)


@pytest.mark.asyncio
async def test_mid_watchdog_refills_partially_missing_regions(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    expected = hub_scheduler.parse_kma_tm_fc(hub_scheduler.resolve_mid_tm_fc(datetime.now(KST)))
    monkeypatch.setattr(hub_scheduler, 'latest_mid_tm_fc', AsyncMock(return_value=expected))
    monkeypatch.setattr(hub_scheduler, 'load_active_grids', AsyncMock(return_value=[SimpleNamespace(mid_land_reg_id='land', mid_temp_reg_id='temp')]))
    monkeypatch.setattr(hub_scheduler, 'loaded_mid_land_regs', AsyncMock(return_value={'land'}))
    monkeypatch.setattr(hub_scheduler, 'loaded_mid_temp_regs', AsyncMock(return_value=set()))
    loop = AsyncMock()
    monkeypatch.setattr(hub_scheduler, 'mid_term_polling_loop', loop)
    await hub_scheduler.mid_freshness_watchdog()
    loop.assert_awaited_once()


def test_publication_selection_uses_kst_across_utc_midnight():
    from datetime import timezone
    utc = datetime(2026, 9, 6, 15, 5, tzinfo=timezone.utc)
    assert hub_scheduler.resolve_short_term_base(utc) == ('20260906', '2300')
    assert hub_scheduler.resolve_mid_tm_fc(utc) == '202609061800'
