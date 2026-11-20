"""test_macro_vintage_paths — 라이브도 **마감 빈티지 점조회**(v1.3.3, 감사 01).

결함: 라이브 경로가 `available_at = 최초 수집 시각` 으로 두고 `ref = ingest_at` 으로 필터해,
마감(22:00Z)보다 6시간 39분 뒤 수집한 값이 레짐 5신호와 WACC 무위험금리에 그대로 들어갔다
(`status: normal`·`regime.verified: true`). 이 파일은 **라이브·재생이 같은 빈티지 점조회**를 쓰고,
수집 지연은 `ingest` 메타로만 남으며 판단 입력에서는 빠지는 것을 고정한다.

재생 경로(R07)의 규칙도 그대로 지킨다: 응답이 스냅샷(요청 빈티지보다 나중)이면 그 시리즈를 버린다.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from engine.data.collect import collect_macro
from engine.data.macro import MacroVintageError, cutoff_vintage, parse_observations
from engine.decide.macro import classify_regime


def _snapshot(rows: list[tuple[str, str]], stamp: str = "2026-11-19") -> dict:
    return {"observations": [{"realtime_start": stamp, "realtime_end": stamp, "date": d, "value": v}
                             for d, v in rows]}


class FakeFred:
    """FRED 대신 정해진 응답을 돌려주는 클라이언트 — `collect_macro` 의 주입 지점.

    `rewrite_vintage=True`(기본)면 요청한 `realtime_start` 로 모든 행의 빈티지를 바꾼다 —
    실제 빈티지 점조회 응답처럼(2025-02-28 조회면 `realtime_start: 2025-02-28`).
    `rewrite_vintage=False` 는 **스냅샷 응답**(오늘 빈티지)을 흉내낼 때 쓴다.
    """

    def __init__(self, payloads: dict[str, dict], rewrite_vintage: bool = True):
        self.payloads = payloads
        self.rewrite_vintage = rewrite_vintage
        self.calls: list[dict] = []

    def fetch_many(self, ids, observation_start=None, realtime_start=None, realtime_end=None,
                   mode="auto", first_ingested_at=None):
        self.calls.append({"realtime_start": realtime_start, "realtime_end": realtime_end, "mode": mode})
        out = {}
        for sid in ids:
            payload = self.payloads.get(sid)
            if payload is None:
                continue
            rows = [dict(r) for r in payload.get("observations", [])]
            if realtime_start and self.rewrite_vintage:
                for row in rows:
                    row["realtime_start"] = realtime_start
            try:
                out[sid] = parse_observations({"observations": rows}, sid, mode=mode,
                                              first_ingested_at=first_ingested_at,
                                              vintage=realtime_start if mode == "replay" else None)
            except MacroVintageError:
                continue
        return out


def _dt(value: str) -> datetime:
    return pd.Timestamp(value).to_pydatetime()


def _diag() -> dict:
    return {"sources": {}, "degraded": []}


# ── 라이브도 마감 빈티지 점조회 ───────────────────────────────────────────────
def test_collect_macro_live_uses_cutoff_vintage_not_ingest_time():
    """마감 6시간 39분 뒤 수집해도 **마감 빈티지**로 점조회한다 — 수집 시각은 lag 로만 남는다."""
    cutoff = _dt("2026-11-19T22:00:00Z")
    ingest = _dt("2026-11-20T04:39:00Z")
    client = FakeFred({"DGS10": _snapshot([("2026-11-17", "5.02"), ("2026-11-18", "5.07")])})
    diag = _diag()
    values, previous = collect_macro(cutoff, diag, mode="live", client=client, now=ingest)

    assert values["dgs10"] == pytest.approx(5.07)
    assert client.calls[0]["realtime_start"] == "2026-11-18"      # 마감일(ET 11-19) − 1일
    assert client.calls[0]["realtime_end"] == "2026-11-18"        # 점조회(범위 조회 아님)
    assert client.calls[0]["mode"] == "replay"                    # 스냅샷 응답은 거부하는 파서
    info = diag["macro_ingest"]
    assert info["policy"] == "cutoff_vintage_point_query"
    assert info["available_at_source"] == "vintage_point_query"
    assert info["vintage"] == "2026-11-18"
    assert info["cutoff_filter"] == "available_at <= data_cutoff_at"
    assert info["inputs_ingested_at"] == "2026-11-20T04:39:00Z"   # 지연은 숨기지 않고 기록
    assert info["after_cutoff"] is True
    assert info["lag_seconds"] == pytest.approx(6 * 3600 + 39 * 60, abs=1.0)
    assert previous == {} or isinstance(previous, dict)


def test_live_and_replay_use_identical_vintage():
    """라이브·재생이 **같은 빈티지**를 조회한다 — 두 경로의 입력이 갈라지지 않는다."""
    cutoff = _dt("2026-11-19T22:00:00Z")
    payload = {"DGS10": _snapshot([("2026-11-18", "5.07")])}
    live_client, replay_client = FakeFred(payload), FakeFred(payload)
    live, _ = collect_macro(cutoff, _diag(), mode="live", client=live_client,
                            now=_dt("2026-11-20T04:39:00Z"))
    replay, _ = collect_macro(cutoff, _diag(), mode="replay", client=replay_client)
    assert live == replay
    assert live_client.calls[0]["realtime_start"] == replay_client.calls[0]["realtime_start"]


def test_post_cutoff_series_are_dropped_not_slipped_in():
    """마감 후 수집에서 스냅샷 응답이 오면 그 시리즈는 **버린다** — 레짐이 검증된 것처럼 보이지 않는다."""
    cutoff = _dt("2026-11-19T22:00:00Z")
    ingest = _dt("2026-11-20T04:39:00Z")
    client = FakeFred({"DGS10": _snapshot([("2026-11-19", "5.17")])}, rewrite_vintage=False)
    diag = _diag()
    values, previous = collect_macro(cutoff, diag, mode="live", client=client, now=ingest)
    assert "dgs10" not in values                                  # 마감 후 값을 끌어오지 않는다
    assert "DGS10" in diag["macro_missing_series"]
    assert diag["macro_ingest"]["after_cutoff"] is True
    regime = classify_regime(values, previous)
    assert regime.verified is False and "real_rate" in regime.inputs_missing


def test_cutoff_vintage_is_et_day_minus_one():
    assert cutoff_vintage(_dt("2026-11-19T22:00:00Z")) == "2026-11-18"
    assert cutoff_vintage(_dt("2026-11-20T02:34:58Z")) == "2026-11-18"   # ET 11-19
    assert cutoff_vintage(datetime(2026, 11, 20, 2, 34, tzinfo=timezone.utc)) == "2026-11-18"


def test_parse_live_mode_still_marks_ingest_time():
    """저수준 파싱 모드(호환용) — `collect_macro` 는 이 모드를 쓰지 않는다(마감 빈티지 점조회)."""
    ingest = _dt("2026-11-20T02:34:58Z")
    df = parse_observations(_snapshot([("2026-11-17", "5.02"), ("2026-11-18", "5.07")]),
                            "DGS10", mode="live", first_ingested_at=ingest)
    assert set(df["available_at"]) == {ingest}
    assert set(df["available_at_source"]) == {"first_ingested_at"}


# ── 재생: 스냅샷 응답 거부 ────────────────────────────────────────────────────
def test_replay_refuses_snapshot_and_does_not_backfill():
    """재생에서 스냅샷 응답이 오면 그 시리즈를 버린다 — 관측일로 역산해 앞당기지 않는다."""
    with pytest.raises(MacroVintageError):
        parse_observations(_snapshot([("2025-02-20", "4.5"), ("2025-02-27", "4.6")]),
                           "DGS10", mode="replay", vintage="2025-02-28")
    client = FakeFred({"DGS10": _snapshot([("2025-02-20", "4.5"), ("2025-02-27", "4.6")])},
                      rewrite_vintage=False)
    diag = _diag()
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
    diag = _diag()
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
    early, _ = collect_macro(_dt("2025-03-01T22:00:00Z"), _diag(),
                             mode="replay", client=FakeFred({"CPIAUCSL": {"observations": months("2025-02-12", "302.0")}}))
    assert early["cpi_yoy"] == pytest.approx((302.0 / 290.0 - 1.0) * 100.0, abs=1e-6)

    # 이후 빈티지(2025-06-10 개정)에서는 305.0 으로 다시 계산된다
    late, _ = collect_macro(_dt("2025-07-01T22:00:00Z"), _diag(),
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
    collect_macro(datetime(2026, 11, 20, 2, 34, tzinfo=timezone.utc), _diag(), mode="replay", client=client)
    assert client.calls[0]["realtime_start"] == "2026-11-18"   # 11-19(ET) − 1
