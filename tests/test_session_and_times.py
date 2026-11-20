"""test_session_and_times — 실제 발행 시각 · 대상 거래일 · 정산 세션 (R08).

감사 재현: `signal_published_at` 은 계획값(07:02 KST)이었고 실제 생성은 11:34 KST 였다. 그리고
`settle()` 는 `next_trading_day` 로 한 세션 밀린 시가에 체결시켰다(금요일 밴드 → 월요일 시가).
이 파일은 ① 대상 세션 계산 ② 실제 시각 기반 불변식 ③ 세션 봉이 없을 때의 보류를 고정한다.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from engine.backtest.execution import build_entry_band
from engine.data.prices import build_price_frame
from engine.decide.signals import target_band
from engine.ledger import live
from engine.ledger.record import RecordError, check_target_session, publish_record
from engine.utils.dates import order_cutoff_at, target_session_for
from scripts import run_daily

FRIDAY = date(2026, 11, 20)
MONDAY = date(2026, 11, 23)


def ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# ── 대상 세션 ────────────────────────────────────────────────────────────────
def test_target_session_for_late_run():
    assert target_session_for(ts("2026-11-20T02:34:58Z")) == FRIDAY        # 11:34 KST — 같은 날
    assert target_session_for(ts("2026-11-20T12:50:00Z")) == FRIDAY        # 장전(premarket) — 같은 날
    assert target_session_for(ts("2026-11-20T15:00:00Z")) == MONDAY        # 주문 마감(14:25Z) 이후 → 다음 세션
    assert target_session_for(ts("2026-11-21T01:00:00Z")) == MONDAY        # 금요일 장 마감 후
    assert target_session_for(ts("2026-11-27T20:00:00Z")) == date(2026, 11, 30)   # 추수감사절 다음날 → 월요일


def test_three_times_uses_actual_publish_time():
    """실제 발행 시각이 기준이다 — 계획 시각으로 검사하면 늦은 실행이 통과한다."""
    early = run_daily.three_times(FRIDAY, "postclose", now=ts("2026-11-20T02:34:58Z"))
    assert early["signal_published_at"] == "2026-11-20T02:34:58Z"
    assert early["target_session"] == "2026-11-20"
    assert early["signal_published_planned_at"].isoformat().startswith("2026-11-19T22:02:11")
    assert ts(early["data_cutoff_at"]) <= ts(early["signal_published_at"]) < ts(early["order_cutoff_at"])

    # 주문 마감을 넘긴 실행 → 다음 세션으로 롤오버(그날 신호를 잃지 않는다)
    late = run_daily.three_times(FRIDAY, "postclose", now=ts("2026-11-20T15:00:00Z"))
    assert late["target_session"] == "2026-11-23"
    assert late["order_cutoff_at"] == order_cutoff_at(MONDAY).isoformat().replace("+00:00", "Z")

    # 과거 as_of 백필은 계획 시각을 쓴다(마감 전 실행 가정)
    backfill = run_daily.three_times(date(2026, 11, 24), "postclose", now=ts("2026-11-20T03:00:00Z"))
    assert backfill["signal_published_at"] == "2026-11-23T22:02:11Z"
    assert backfill["target_session"] == "2026-11-24"


# ── 레코드 불변식 ─────────────────────────────────────────────────────────────
def make_record(**kw) -> dict:
    base = dict(
        signal_id="2026-11-20-MSFT-BUY-001", ticker="MSFT", type="BUY", grade="B",
        entry_score=70.0, as_of="2026-11-20", data_cutoff_at="2026-11-19T22:00:00Z",
        signal_published_at="2026-11-20T02:34:58Z", order_cutoff_at="2026-11-20T14:25:00Z",
        generated_price=100.0, target_band=target_band(140.0, 100.0), expected_mdd=-0.09,
        plan_weight=0.02, stop_distance=0.09,
    )
    base.update(kw)
    return publish_record(**base)


def test_record_rejects_session_mismatch():
    with pytest.raises(RecordError):                      # 토요일은 거래일이 아니다
        make_record(target_session="2026-11-21")
    with pytest.raises(RecordError):                      # 주문 마감이 이미 지난 세션
        make_record(target_session="2026-11-19")
    with pytest.raises(RecordError):                      # 마감(14:25Z)을 넘긴 발행 시각
        make_record(target_session="2026-11-20", signal_published_at="2026-11-20T15:00:00Z",
                    order_cutoff_at="2026-11-23T14:25:00Z")
    ok = make_record(target_session="2026-11-20")
    assert ok["target_session"] == "2026-11-20"
    assert ok["signal_published_planned_at"] is None
    check_target_session(ok)


def test_attach_target_session_checks_and_keeps_record_immutable():
    from engine.ledger.record import attach_target_session

    rec = make_record()
    attach_target_session(rec, "2026-11-20", ts("2026-11-19T22:02:11Z"))
    assert rec["target_session"] == "2026-11-20"
    assert rec["signal_published_planned_at"] == "2026-11-19T22:02:11Z"
    assert rec["execution_event"] is None                  # 체결 정보는 여전히 비어 있다
    with pytest.raises(RecordError):
        attach_target_session(dict(rec), "2026-11-21")


# ── 정산 세션 ────────────────────────────────────────────────────────────────
def bars(days: list[date], open_price: float = 100.0, ticker: str = "MSFT"):
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in days])
    close = pd.Series([open_price] * len(days), index=idx)
    hist = pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Adj Close": close, "Volume": 1_000_000.0}, index=idx)
    return build_price_frame(ticker, hist, None)


def band_record(target_session: str = "2026-11-20") -> dict:
    rec = make_record(target_session=target_session)
    rec["entry_band"] = build_entry_band(
        iv_median_sadj=140.0, required_margin=0.20, dma200_sadj=90.0,
        signal_price_sadj=100.0, stop_distance=0.09).as_dict()
    return rec


def test_settle_uses_target_session_only():
    friday_panel = bars([date(2026, 11, 18), FRIDAY])
    rec = band_record()
    result = live.settle([rec], {}, {"MSFT": friday_panel}, FRIDAY)
    assert [e["quant_track"]["execution_event"] for e in result["new_events"]] == ["buy_filled"]
    event = result["new_events"][0]
    assert event["at"].startswith("2026-11-20T")          # 금요일 시가에 체결(월요일 아님)
    assert result["settled"][0]["target_session"] == "2026-11-20"

    # 대상 세션 봉이 없으면 대체 세션을 쓰지 않고 보류한다
    monday_only = bars([MONDAY])
    pending = live.settle([rec], {}, {"MSFT": monday_only}, MONDAY)
    assert pending["new_events"] == []
    assert pending["pending"][0]["reason"] == "SESSION_BAR_MISSING"
    assert pending["pending"][0]["expected_fill_day"] == "2026-11-20"


def test_settle_waits_for_target_session():
    rec = band_record(target_session="2026-11-23")
    result = live.settle([rec], {}, {"MSFT": bars([FRIDAY])}, FRIDAY)
    assert result["new_events"] == []
    assert result["pending"][0]["expected_fill_day"] == "2026-11-23"


def test_legacy_record_without_session_still_settles():
    """구 레코드(target_session 없음)는 신호일 다음 거래일로 폴백하고 그 사실을 남긴다."""
    rec = band_record()
    rec.pop("target_session")
    panel = bars([FRIDAY, MONDAY])
    result = live.settle([rec], {}, {"MSFT": panel}, MONDAY)
    assert result["settled"][0]["legacy_session"] is True
    assert result["settled"][0]["target_session"] == "2026-11-23"
    assert result["new_events"][0]["at"].startswith("2026-11-23T")
