"""test_dates_folds — NYSE 거래일 캘린더, 세 시각(마감·발행·주문), 폴드 재현 (4.2.1)."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from engine.utils.dates import (
    add_trading_days,
    data_cutoff_at,
    et_day_end,
    first_decision_after,
    fold_summary,
    is_trading_day,
    market_close_at,
    market_open_at,
    next_trading_day,
    order_cutoff_at,
    prev_trading_day,
    signal_published_at,
    trading_day_count,
    trading_days,
)


def test_holidays_2026():
    assert not is_trading_day(date(2026, 1, 1))     # 신정
    assert not is_trading_day(date(2026, 1, 19))    # MLK
    assert not is_trading_day(date(2026, 2, 16))    # Presidents
    assert not is_trading_day(date(2026, 4, 3))     # Good Friday 2026
    assert not is_trading_day(date(2026, 5, 25))    # Memorial
    assert not is_trading_day(date(2026, 6, 19))    # Juneteenth
    assert not is_trading_day(date(2026, 7, 3))     # 7/4 토요일 → 7/3 관측
    assert not is_trading_day(date(2026, 9, 7))     # Labor
    assert not is_trading_day(date(2026, 11, 26))   # Thanksgiving
    assert not is_trading_day(date(2026, 12, 25))   # Christmas
    assert is_trading_day(date(2026, 11, 27))       # 추수감사절 다음 날(반장)


def test_weekends_and_known_closures():
    assert not is_trading_day(date(2026, 11, 21))   # 토
    assert not is_trading_day(date(2018, 12, 5))    # 부시 장례
    assert not is_trading_day(date(2012, 10, 29))   # 샌디


def test_add_and_count_trading_days():
    assert add_trading_days(date(2025, 8, 19), 21) == date(2025, 9, 18)
    assert add_trading_days(date(2025, 8, 19), 63) == date(2025, 11, 17)
    assert add_trading_days(date(2025, 8, 19), 126) == date(2026, 2, 19)
    assert add_trading_days(date(2026, 2, 19), 126) == date(2026, 8, 20)
    assert trading_day_count(date(2026, 8, 19), date(2026, 11, 18)) == 64
    assert trading_day_count(date(2025, 8, 19), date(2026, 8, 18)) == 250
    assert next_trading_day(date(2026, 11, 25)) == date(2026, 11, 27)
    assert prev_trading_day(date(2026, 11, 30)) == date(2026, 11, 27)


def test_market_times_handle_dst():
    # 여름(EDT): 개장 13:30Z, 마감 20:00Z
    assert market_open_at(date(2026, 7, 15)).hour == 13
    assert market_close_at(date(2026, 7, 15)).hour == 20
    # 겨울(EST): 14:30Z / 21:00Z
    assert market_open_at(date(2026, 11, 19)).hour == 14
    assert market_close_at(date(2026, 11, 19)).hour == 21


def test_three_timestamps_match_document():
    as_of = date(2026, 11, 19)
    cut = data_cutoff_at(as_of, "postclose")
    assert cut == pd.Timestamp("2026-11-18T22:00:00Z").to_pydatetime()
    assert signal_published_at(cut) == pd.Timestamp("2026-11-18T22:02:11Z").to_pydatetime()
    assert order_cutoff_at(as_of) == pd.Timestamp("2026-11-19T14:25:00Z").to_pydatetime()
    pre = data_cutoff_at(as_of, "premarket")
    assert pre == pd.Timestamp("2026-11-19T12:50:00Z").to_pydatetime()
    # 신호 발행(21:52 KST)에서 주문 마감까지의 창: 겨울 93분 / 여름 33분 (2.2.2)
    assert round((order_cutoff_at(as_of) - signal_published_at(pre)).total_seconds() / 60) == 93
    summer = data_cutoff_at(date(2026, 7, 15), "premarket")
    assert round((order_cutoff_at(date(2026, 7, 15)) - signal_published_at(summer)).total_seconds() / 60) == 33


def test_fold_summary_reproduces_document_table():
    s = fold_summary(date(2026, 11, 19))
    assert s["holdout_start"] == "2026-08-19"
    assert s["horizons"]["1m"]["count"] == 4
    assert s["horizons"]["3m"]["count"] == 3
    assert s["horizons"]["6m"]["count"] == 2
    assert s["blocked"] == ["6m"]
    starts = [f["start"] for f in s["horizons"]["3m"]["folds"]]
    assert starts == ["2025-08-19", "2025-11-19", "2026-02-19", "2026-05-19"]


def test_fold_starts_roll_off_weekends():
    # 19일이 토요일인 분기는 다음 거래일(월)로 밀린다
    s = fold_summary(date(2027, 5, 20))   # 홀드아웃 2027-02-20(토) → 02-22
    assert s["holdout_start"] == "2027-02-22"
    for h in s["horizons"].values():
        for f in h["folds"]:
            assert is_trading_day(date.fromisoformat(f["start"]))


def test_window_can_extend_to_15_months():
    s = fold_summary(date(2026, 11, 19), window_months=15)
    assert s["horizons"]["6m"]["count"] == 3      # 15개월 창이면 6M 3폴드 (4.2.1 복귀 경로)


def test_et_day_end_and_next_decision():
    end = et_day_end(date(2026, 11, 12))
    assert end == pd.Timestamp("2026-11-13T04:59:59Z").to_pydatetime()
    assert first_decision_after(end) == pd.Timestamp("2026-11-13T12:50:00Z").to_pydatetime()
