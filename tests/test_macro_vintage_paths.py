"""test_macro_vintage_paths — 라이브는 수집 시각, 재생은 빈티지 점조회 (R07).

결함: 스냅샷 응답(모든 행 `realtime_start` = 요청 당일)에 관측일 기준 보수 규칙을 적용해
2026-11-20 에 본 2025-02-27 관측치가 **2025-03-01 컷오프를 통과**했다(= 2025년 3월의 결정이
2026년 11월의 개정치를 본다). 라이브와 재생의 경로를 나눠 이 누수를 막는다.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from engine.data.collect import collect_macro
from engine.data.macro import MacroVintageError, parse_observations


def _snapshot(rows: list[tuple[str, str]], stamp: str = "2026-11-19") -> dict:
    return {"observations": [{"realtime_start": stamp, "realtime_end": stamp, "date": d, "value": v}
                             for d, v in rows]}


class FakeFred:
    """FRED 대신 정해진 응답을 돌려주는 클라이언트 — `collect_macro` 의 주입 지점."""

    def __init__(self, payloads: dict[str, dict]):
        self.payloads = payloads
        self.calls: list[dict] = []

    def fetch_many(self, ids, observation_start=None, realtime_start=None, realtime_end=None,
                   mode="auto", first_ingested_at=None):
        self.calls.append({"realtime_start": realtime_start, "realtime_end": realtime_end, "mode": mode})
        out = {}
        for sid in ids:
            payload = self.payloads.get(sid)
            if payload is None:
                continue
            try:
                out[sid] = parse_observations(payload, sid, mode=mode,
                                              first_ingested_at=first_ingested_at,
                                              vintage=realtime_start if mode == "replay" else None)
            except MacroVintageError:
                continue
        return out


def _dt(value: str) -> datetime:
    return pd.Timestamp(value).to_pydatetime()


def test_live_snapshot_uses_ingest_time():
    """라이브는 모든 행의 `available_at` 이 **최초 수집 시각**이고 출처가 표시된다."""
    ingest = _dt("2026-11-20T02:34:58Z")
    df = parse_observations(_snapshot([("2026-11-17", "5.02"), ("2026-11-18", "5.07")]),
                            "DGS10", mode="live", first_ingested_at=ingest)
    assert set(df["available_at"]) == {ingest}
    assert set(df["available_at_source"]) == {"first_ingested_at"}


def test_live_mode_requires_ingest_time():
    with pytest.raises(ValueError):
        parse_observations(_snapshot([("2026-11-18", "5.07")]), "DGS10", mode="live")


def test_collect_macro_live_records_ingest_lag():
    """마감(22:00Z)보다 4시간 34분 늦게 모은 값을 숨기지 않고 lag 로 드러낸다."""
    cutoff = _dt("2026-11-19T22:00:00Z")
    ingest = _dt("2026-11-20T02:34:58Z")
    client = FakeFred({"DGS10": _snapshot([("2026-11-17", "5.02"), ("2026-11-18", "5.07")])})
    diag = {"sources": {}, "degraded": []}
    values, previous = collect_macro(cutoff, diag, mode="live", client=client, now=ingest)
    assert values["dgs10"] == pytest.approx(5.07)
    ingest_info = diag["macro_ingest"]
    assert ingest_info["available_at_source"] == "first_ingested_at"
    assert ingest_info["after_cutoff"] is True
    assert ingest_info["lag_seconds"] == pytest.approx(4 * 3600 + 34 * 60 + 58, abs=1.0)
    assert ingest_info["inputs_ingested_at"] == "2026-11-20T02:34:58Z"
    assert previous == {} or isinstance(previous, dict)


def test_replay_refuses_snapshot_and_does_not_backfill():
    """재생에서 스냅샷 응답이 오면 그 시리즈를 버린다 — 관측일로 역산해 앞당기지 않는다."""
    with pytest.raises(MacroVintageError):
        parse_observations(_snapshot([("2025-02-20", "4.5"), ("2025-02-27", "4.6")]),
                           "DGS10", mode="replay", vintage="2025-02-28")
    client = FakeFred({"DGS10": _snapshot([("2025-02-20", "4.5"), ("2025-02-27", "4.6")])})
    diag = {"sources": {}, "degraded": []}
    values, _ = collect_macro(_dt("2025-03-01T22:00:00Z"), diag, mode="replay", client=client)
    assert values == {}                                   # 2025-03-01 결정이 2026-11 스냅샷을 보지 않는다
    assert "DGS10" in diag["macro_missing_series"]
    assert client.calls[0]["realtime_start"] == "2025-02-28"   # 전날 빈티지만 점조회
    assert client.calls[0]["mode"] == "replay"


def test_replay_vintage_point_query_uses_vintage_date():
    """빈티지 점조회 응답은 행별 `realtime_start` 가 같아도 스냅샷이 아니다 — D 종료 후 첫 결정."""
    payload = {"observations": [
        {"realtime_start": "2025-02-28", "realtime_end": "9999-12-31", "date": "2025-02-20", "value": "4.5"},
        {"realtime_start": "2025-02-28", "realtime_end": "9999-12-31", "date": "2025-02-27", "value": "4.6"},
    ]}
    df = parse_observations(payload, "DGS10", mode="replay", vintage="2025-02-28")
    assert set(df["available_at_source"]) == {"date_plus_one"}
    assert set(df["available_at"]) == {_dt("2025-03-01T12:50:00Z")}
    client = FakeFred({"DGS10": payload})
    diag = {"sources": {}, "degraded": []}
    values, _ = collect_macro(_dt("2025-03-03T12:50:00Z"), diag, mode="replay", client=client)
    assert values["dgs10"] == pytest.approx(4.6)


def test_previous_and_cpi_yoy_use_same_vintage():
    """결정마다 **그 빈티지의 값**으로 CPI YoY 를 계산한다 — 나중 개정치가 과거 결정에 새지 않는다."""
    def monthly(date_str: str, value: str, stamp: str) -> dict:
        return {"realtime_start": stamp, "realtime_end": "9999-12-31", "date": date_str, "value": value}

    def months(stamp: str, january: str) -> list[dict]:
        rows = []
        for i in range(13):                  # 2024-01 … 2025-01
            month = (i % 12) + 1
            year = 2024 + i // 12
            value = january if i == 12 else f"{290 + i}.0"
            rows.append(monthly(f"{year}-{month:02d}-01", value, stamp))
        return rows

    # 2025-03-01 결정의 빈티지(2025-02-28)에는 2025-01 = 302.0 이 실려 있다
    early, _ = collect_macro(_dt("2025-03-01T22:00:00Z"), {"sources": {}, "degraded": []},
                             mode="replay", client=FakeFred({"CPIAUCSL": {"observations": months("2025-02-12", "302.0")}}))
    assert early["cpi_yoy"] == pytest.approx((302.0 / 290.0 - 1.0) * 100.0, abs=1e-6)

    # 이후 빈티지(2025-06-10 개정)에서는 305.0 으로 다시 계산된다
    late, _ = collect_macro(_dt("2025-07-01T22:00:00Z"), {"sources": {}, "degraded": []},
                            mode="replay", client=FakeFred({"CPIAUCSL": {"observations": months("2025-06-10", "305.0")}}))
    assert late["cpi_yoy"] == pytest.approx((305.0 / 290.0 - 1.0) * 100.0, abs=1e-6)


def test_is_snapshot_response_still_used_by_auto_mode():
    """기본(auto) 모드는 v1.3.2 규칙 그대로 — 기존 경로는 바뀌지 않았다."""
    from engine.data.macro import as_of_macro

    df = parse_observations(_snapshot([("2026-09-01", "336.298"), ("2026-10-01", "337.476")]), "CPIAUCSL")
    assert set(df["available_at_source"]) == {"obs_month_lag_20d"}
    visible = as_of_macro(df, _dt("2026-11-19T22:00:00Z"))
    assert list(visible["date"]) == [pd.Timestamp("2026-09-01").date()]


def test_collect_accepts_mode_keyword_without_breaking_caller():
    """경계 계약(§4.2): `collect(..., mode=)` 는 기본값이 있어 `run_daily` 호출부가 그대로 돈다."""
    import inspect

    from engine.data.collect import collect

    signature = inspect.signature(collect)
    assert signature.parameters["mode"].default == "live"
    assert "mode" in signature.parameters


def test_utc_datetime_is_used_for_vintage_day():
    """빈티지 날짜는 **ET 날짜 − 1일** 기준이다(UTC 날짜가 아니다)."""
    client = FakeFred({})
    collect_macro(datetime(2026, 11, 20, 2, 34, tzinfo=timezone.utc),
                  {"sources": {}, "degraded": []}, mode="replay", client=client)
    assert client.calls[0]["realtime_start"] == "2026-11-18"   # 11-19(ET) − 1
