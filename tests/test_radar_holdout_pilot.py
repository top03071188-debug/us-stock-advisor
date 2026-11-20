"""test_radar_holdout_pilot — 13F 레이더 · 홀드아웃 잠금 · 스트레스 미검증 · 파일럿 4지표."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from engine.backtest import holdout, stress
from engine.data.holdings_13f import (
    Filing13F,
    Holding13F,
    compute_deltas,
    filings_available,
    parse_filing,
    parse_information_table,
    radar_payload,
    watermark,
)
from engine.ledger.pilot import VERDICT_CANDIDATE, VERDICT_HOLD, evaluate_pilot, n_eff, tail_loss

XML = """<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>MICROSOFT CORP</nameOfIssuer>
    <cusip>594918104</cusip>
    <value>1250000</value>
    <shrsOrPrnAmt><sshPrnamt>3010</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <putCall></putCall>
  </infoTable>
  <infoTable>
    <nameOfIssuer>NVIDIA CORP</nameOfIssuer>
    <cusip>67066G104</cusip>
    <value>640000</value>
    <shrsOrPrnAmt><sshPrnamt>1300</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <putCall>Put</putCall>
  </infoTable>
</informationTable>
"""


def _filing(period_end: str, filed: str, holdings: list[tuple[str, str, float]]) -> Filing13F:
    return Filing13F(
        filer_cik="0001234567", filer_name="Example Capital",
        period_end=date.fromisoformat(period_end),
        acceptance_datetime=datetime.fromisoformat(filed).replace(tzinfo=timezone.utc),
        holdings=[Holding13F(cusip=c, issuer=i, value_usd=v) for c, i, v in holdings],
    )


# ────────────────────────── 13F ──────────────────────────
def test_parse_information_table_with_namespace():
    rows = parse_information_table(XML)
    assert [r.cusip for r in rows] == ["594918104", "67066G104"]
    assert rows[0].value_usd == pytest.approx(1_250_000_000.0)   # 13F value 는 천 달러 단위
    assert rows[1].put_call == "Put"
    assert rows[0].issuer == "MICROSOFT CORP"


def test_parse_filing_requires_period_end():
    with pytest.raises(Exception):
        parse_filing({"filer_cik": "1"})


def test_45_day_lag_gate():
    filing = _filing("2026-06-30", "2026-08-10T20:00:00", [("594918104", "MSFT", 1e9)])
    assert filing.available_from() == date(2026, 8, 14)          # 분기말 + 45일(제출 기한)
    before = datetime(2026, 8, 9, tzinfo=timezone.utc)           # 제출 전
    after = datetime(2026, 8, 11, tzinfo=timezone.utc)           # 제출 후(기한보다 이르지만 알 수 있다)
    assert filings_available([filing], before) == []
    assert filings_available([filing], after) == [filing]

    # 접수 시각이 없으면 제출 기한(분기말 +45일)부터 쓸 수 있다
    no_acceptance = Filing13F(filer_cik="0001234567", filer_name="Example Capital",
                              period_end=date(2026, 6, 30))
    assert filings_available([no_acceptance], datetime(2026, 8, 13, tzinfo=timezone.utc)) == []
    assert filings_available([no_acceptance], datetime(2026, 8, 15, tzinfo=timezone.utc)) == [no_acceptance]


def test_watermark_says_45_days_before_quarter_end():
    filing = _filing("2026-06-30", "2026-08-10T20:00:00", [("594918104", "MSFT", 1e9)])
    mark = watermark(datetime(2026, 11, 20, tzinfo=timezone.utc), [filing])
    assert mark["watermark_ko"] == "45일 전 분기말 기준"
    assert mark["period_end"] == "2026-06-30"
    assert mark["lag_days"] == 45
    assert mark["stale"] is True                                  # 143일 → 지연 표시
    empty = watermark(datetime(2026, 11, 20, tzinfo=timezone.utc), [])
    assert empty["period_end"] is None and empty["stale"] is True


def test_deltas_new_increase_reduce_exit():
    prev = _filing("2026-03-31", "2026-05-10T12:00:00",
                   [("AAA", "Alpha", 100.0), ("BBB", "Beta", 100.0), ("CCC", "Gamma", 100.0)])
    cur = _filing("2026-06-30", "2026-08-10T12:00:00",
                  [("AAA", "Alpha", 150.0), ("BBB", "Beta", 50.0), ("DDD", "Delta", 80.0)])
    rows = {r["cusip"]: r for r in compute_deltas(prev, cur)}
    assert rows["DDD"]["kind"] == "new"
    assert rows["AAA"]["kind"] == "increase" and rows["AAA"]["delta_pct"] == pytest.approx(0.5)
    assert rows["BBB"]["kind"] == "reduce" and rows["BBB"]["delta_pct"] == pytest.approx(-0.5)
    assert rows["CCC"]["kind"] == "exit"
    assert "shares" not in set(rows["AAA"]), "수량은 공개 필드가 아니다"


def test_radar_payload_marks_watchlist_overlap():
    prev = _filing("2026-03-31", "2026-05-10T12:00:00", [("AAA", "Alpha", 100.0)])
    cur = _filing("2026-06-30", "2026-08-10T12:00:00",
                  [("AAA", "Alpha", 150.0), ("MSFT", "MICROSOFT CORP", 90.0)])
    payload = radar_payload([prev, cur], datetime(2026, 11, 20, tzinfo=timezone.utc),
                            watchlist={"MSFT"})
    assert payload["watermark_ko"] == "45일 전 분기말 기준"
    assert payload["n_filers"] == 1
    assert payload["counts"]["new"] == 1
    assert "MICROSOFT CORP" in payload["watchlist_overlap"]


def test_radar_ignores_filing_not_yet_available():
    cur = _filing("2026-09-30", "2026-11-14T12:00:00", [("AAA", "Alpha", 100.0)])
    payload = radar_payload([cur], datetime(2026, 11, 20, tzinfo=timezone.utc))
    assert payload["n_filers"] == 1                       # 11-14 는 기한(11-14) 이후 → 사용 가능
    payload2 = radar_payload([cur], datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert payload2["n_filers"] == 0                      # 아직 모르는 분기


# ────────────────────────── 홀드아웃 ──────────────────────────
def test_holdout_window_matches_spec():
    window = holdout.window_for(date(2026, 11, 19))
    assert window.holdout_start == date(2026, 8, 19)
    assert window.holdout_end == date(2026, 11, 18)


def test_next_open_is_quarterly():
    assert holdout.next_open_date(date(2026, 11, 20)) == date(2027, 1, 1)
    assert holdout.next_open_date(date(2026, 8, 5)) == date(2026, 10, 1)
    assert holdout.is_open_day(date(2026, 10, 1)) is True
    assert holdout.is_open_day(date(2026, 10, 2)) is False


def test_boundary_rule_maturity_before_holdout_start():
    assert holdout.is_eligible_fold("2026-08-18", "2026-08-19") is True
    assert holdout.is_eligible_fold("2026-08-19", "2026-08-19") is False   # 경계는 제외
    folds = [
        {"fold_id": "F1", "maturity": "2026-08-19", "eligible": True},           # 위반
        {"fold_id": "F2", "maturity": "2026-08-18", "eligible": True},           # 정상
        {"fold_id": "F3", "maturity": "2026-09-30", "eligible": False},          # 정상(잘림)
        {"fold_id": "F4", "maturity": "2026-09-30", "eligible": False, "truncated": False},  # 표시 누락
    ]
    problems = holdout.validate_boundary(folds, "2026-08-19")
    assert any(p.startswith("F1") for p in problems)
    assert any(p.startswith("F4") for p in problems)
    assert not any(p.startswith("F2") for p in problems)


def test_holdout_opens_once_per_quarter(tmp_path):
    ledger = holdout.HoldoutLedger(tmp_path)
    assert ledger.can_open(date(2026, 11, 20))["allowed"] is False     # 개봉일 아님
    assert ledger.can_open(date(2027, 1, 1))["allowed"] is True
    path = ledger.record(date(2027, 1, 1), {"result": {"agreement": 61.2, "passed": True}})
    assert path.exists() and ledger.status(date(2027, 1, 2))["evaluations"] == 1
    again = ledger.can_open(date(2027, 1, 1))
    assert again["allowed"] is False and again["reason"] == "ALREADY_EVALUATED_THIS_QUARTER"
    with pytest.raises(holdout.HoldoutLocked):
        ledger.record(date(2027, 1, 1), {"result": {"agreement": 40.0, "passed": False}})


def test_holdout_evaluate_flags_conservative_mode():
    result = holdout.evaluate(52.0, threshold=55.0, previous_agreement=60.0)
    assert result["passed"] is False
    assert result["conservative_mode"] is True
    assert holdout.evaluate(None)["passed"] is None


# ────────────────────────── 스트레스 ──────────────────────────
def test_stress_without_universe_data_is_not_a_pass():
    report = stress.stress_report(None, None)
    assert report["verified_all"] is False
    assert report["stress_pass"] is None
    assert report["gate_passed"] is False                  # 빈 입력을 통과로 처리하지 않는다
    for row in report["results"]:
        assert row["verified"] is False and row["pass"] is None
        assert "미확보" in (row["reason_ko"] or "")
    assert "universe_history" in report["missing_inputs"]


def test_stress_with_data_computes_pass():
    report = stress.stress_report(object(), object(), results={
        "2022_rate_hike": {"strategy_mdd": -0.12, "benchmark_mdd": -0.20, "v06_fired": True},
        "2020_covid": {"new_buys_during_crash": 0},
        "2018q4": {"loss_ratio": 0.5},
    })
    assert report["verified_all"] is True
    assert report["stress_pass"] is True
    assert report["gate_passed"] is True


def test_extended_plan_reports_incomplete():
    plan = stress.extended_plan(date(2026, 11, 20), folds_available=6, universe_verified=False)
    assert plan["status"] == "incomplete"
    assert plan["label_ko"] == "장기 확장 미완료 — 6/20폴드"


# ────────────────────────── 파일럿 ──────────────────────────
def _record(signal_id: str, ticker: str, as_of: str) -> dict:
    return {"signal_id": signal_id, "ticker": ticker, "as_of": as_of}


def _graded(ticker: str, as_of: str, ret: float, excess: float, mdd: float = -0.03) -> dict:
    return {"signal_id": f"{as_of}-{ticker}", "ticker": ticker, "as_of": as_of, "action": "buy_filled",
            "horizons": {"3m": {"return": ret, "excess_return": excess, "mdd": mdd}}}


def test_n_eff_clusters_overlapping_signals():
    records = [
        _record("a", "MSFT", "2026-01-05"), _record("b", "MSFT", "2026-02-05"),   # 21거래일 → 같은 클러스터
        _record("c", "MSFT", "2026-06-01"),                                       # 63거래일 밖 → 새 클러스터
        _record("d", "AAPL", "2026-01-06"),
    ]
    result = n_eff(records)
    assert result["n_eff"] == 3
    assert result["n_records"] == 4
    assert len(result["by_ticker"]["MSFT"]) == 2


def test_pilot_holds_without_samples():
    ev = evaluate_pilot([], [])
    assert ev["verdict"] == VERDICT_HOLD
    assert "판정 보류" in ev["verdict_reason_ko"]
    keys = [m["key"] for m in ev["metrics"]]
    assert keys == ["n_eff", "net_after_cost", "cash_equivalent", "tail_loss"]


def test_pilot_holds_when_a_metric_cannot_be_computed():
    records = [_record(f"s{i}", f"T{i}", "2026-01-05") for i in range(20)]
    graded = [_graded(f"T{i}", "2026-01-05", 0.03, 0.01) for i in range(20)]
    ev = evaluate_pilot(records, graded, portfolio=None, benchmark_return=None)
    assert ev["verdict"] == VERDICT_HOLD          # 계좌 곡선·벤치마크가 없으면 판정하지 않는다


def test_pilot_candidate_when_all_four_pass():
    records = [_record(f"s{i}", f"T{i}", "2026-01-05") for i in range(20)]
    graded = [_graded(f"T{i}", "2026-01-05", 0.05, 0.03) for i in range(20)]
    ev = evaluate_pilot(records, graded,
                        portfolio={"total_return": 0.10, "avg_exposure": 0.45},
                        benchmark_return=0.06, risk_free=0.01)
    assert ev["n_eff"] == 20
    assert all(m["pass"] is True for m in ev["metrics"]), ev["metrics"]
    assert ev["verdict"] == VERDICT_CANDIDATE


def test_tail_loss_gate():
    assert tail_loss([0.1, 0.1, 0.1, 0.1, -0.02])["pass"] is True
    assert tail_loss([0.1, 0.1, 0.1, 0.1, -0.30])["pass"] is False
    assert tail_loss([])["pass"] is None
