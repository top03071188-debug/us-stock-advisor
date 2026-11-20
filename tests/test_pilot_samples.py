"""test_pilot_samples — 파일럿 유효 표본과 평균 노출도 (R11).

감사 재현: `pilot_eval.json` 이 `n_graded 0`, `n_eff 2`(둘 다 WATCH·`no_action`)였고, WATCH 15건만
넣으면 `n_eff 15` 로 **지표 pass: true** 가 나왔다. `avg_exposure` 는 일일 실행이 넘기지 않아
영구 "보류" 였고, `summary()` 는 현재 `invested_pct` 를 평균 자리에 대입했다.

유효 표본은 **체결(`buy_filled`) + 3개월 만기 채점 완료**만 센다. 발행 신호 수는 따로 남긴다.
"""

from __future__ import annotations

from datetime import date

import pytest

from engine.ledger import live
from engine.ledger.pilot import (VERDICT_CANDIDATE, VERDICT_HOLD, eligible_samples, evaluate_pilot,
                                 pilot_summary)


def record(sid: str, ticker: str, as_of: str = "2026-01-05") -> dict:
    return {"signal_id": sid, "ticker": ticker, "as_of": as_of, "type": "WATCH", "grade": "B"}


def graded(sid: str, ret: float = 0.05, excess: float = 0.03, *, complete: bool = True,
           mdd: float = -0.03) -> dict:
    return {"signal_id": sid, "horizons": {"3m": {"return": ret, "excess_return": excess,
                                                  "mdd": mdd, "complete": complete}}}


def executions(sids: list[str], action: str = "buy_filled") -> dict:
    return {sid: {"quant_track": {"execution_event": action, "fill_price": 100.0}} for sid in sids}


# ── 표본 정의 ────────────────────────────────────────────────────────────────
def test_watch_only_does_not_increase_n_eff():
    records = [record(f"w{i}", f"T{i}") for i in range(15)]
    graded_rows = [graded(f"w{i}") for i in range(15)]
    ev = evaluate_pilot(records, graded_rows,
                        executions=executions([r["signal_id"] for r in records], action="no_action"),
                        portfolio={"total_return": 0.10, "avg_exposure": 0.4},
                        benchmark_return=0.05, risk_free=0.0)
    assert ev["n_eff"] == 0
    assert ev["n_records"] == 15 and ev["n_eligible"] == 0
    assert ev["verdict"] == VERDICT_HOLD
    assert "계산 불가" in ev["verdict_reason_ko"]
    metrics = {m["key"]: m for m in ev["metrics"]}
    assert metrics["n_eff"]["pass"] is False
    assert metrics["net_after_cost"]["value"] is None and metrics["net_after_cost"]["pass"] is None
    assert metrics["tail_loss"]["value"] is None


def test_untraded_records_without_executions_are_not_samples():
    """체결 정보가 없으면 표본 0건 — 레코드가 있다고 통과시키지 않는다."""
    records = [record(f"s{i}", f"T{i}") for i in range(15)]
    graded_rows = [graded(f"s{i}") for i in range(15)]
    ev = evaluate_pilot(records, graded_rows, portfolio={"total_return": 0.1, "avg_exposure": 0.4},
                        benchmark_return=0.05)
    assert ev["n_eff"] == 0 and ev["verdict"] == VERDICT_HOLD


def test_filled_but_not_matured_is_not_a_sample():
    records = [record("s1", "AAA")]
    rows = [graded("s1", complete=False)]
    assert eligible_samples(records, rows, executions(["s1"])) == []
    ev = evaluate_pilot(records, rows, executions=executions(["s1"]),
                        portfolio={"total_return": 0.1, "avg_exposure": 0.4}, benchmark_return=0.05)
    assert ev["n_eff"] == 0 and ev["verdict"] == VERDICT_HOLD


def test_filled_and_matured_count():
    records = [record(f"s{i}", f"T{i}") for i in range(15)]
    rows = [graded(f"s{i}") for i in range(15)]
    samples = eligible_samples(records, rows, executions([r["signal_id"] for r in records]))
    assert len(samples) == 15
    ev = evaluate_pilot(records, rows, executions=executions([r["signal_id"] for r in records]),
                        portfolio={"total_return": 0.10, "avg_exposure": 0.45},
                        benchmark_return=0.06, risk_free=0.01)
    assert ev["n_eff"] == 15 and ev["n_eligible"] == 15
    assert ev["metrics"][0]["pass"] is True


def test_overlapping_signals_still_cluster():
    """같은 종목이 63거래일 안에 겹치면 하나로 묶는다(4.7.3 ①)."""
    records = [record("a", "AAA", "2026-01-05"), record("b", "AAA", "2026-02-05")]
    rows = [graded("a"), graded("b")]
    assert len(eligible_samples(records, rows, executions(["a", "b"]))) == 2   # 표본 2건
    ev = evaluate_pilot(records, rows, executions=executions(["a", "b"]),
                        portfolio={"total_return": 0.1, "avg_exposure": 0.4}, benchmark_return=0.05)
    assert ev["n_eff"] == 1                                                     # 클러스터 1개
    assert ev["n_eligible"] == 2


# ── 평균 노출도(history) ──────────────────────────────────────────────────────
def test_avg_exposure_from_history(tmp_path):
    rows = [{"as_of": "2026-01-05", "cash_pct": 0.6, "invested_pct": 0.4, "equity_index": 1.0,
             "n_positions": 1},
            {"as_of": "2026-01-06", "cash_pct": 0.5, "invested_pct": 0.5, "equity_index": 1.0,
             "n_positions": 1},
            {"as_of": "2026-01-07", "cash_pct": 0.4, "invested_pct": 0.6, "equity_index": 1.0,
             "n_positions": 2}]
    for row in rows:
        live.append_portfolio_history(row, tmp_path)
    stored = live.read_portfolio_history(tmp_path)
    assert [r["invested_pct"] for r in stored] == [0.4, 0.5, 0.6]
    assert live.avg_exposure_from_history(stored, date(2026, 1, 5), date(2026, 1, 7)) == pytest.approx(0.5)
    assert live.avg_exposure_from_history(stored, date(2026, 1, 6), date(2026, 1, 7)) == pytest.approx(0.55)
    assert live.avg_exposure_from_history([], None, None) is None

    # 같은 as_of 는 다시 쓰지 않는다(멱등)
    live.append_portfolio_history(rows[-1], tmp_path)
    assert len(live.read_portfolio_history(tmp_path)) == 3


def test_summary_uses_history_average_not_current_value():
    graded_rows = [graded("s1")]
    portfolio = {"as_of": "2026-01-07", "cash_pct": 0.4, "invested_pct": 0.6, "equity_index": 1.0,
                 "avg_exposure": 0.5, "n_positions": 2}
    summary = live.summary(graded_rows, portfolio, pilot=None)
    assert summary["strategy"]["avg_exposure"] == pytest.approx(0.5)
    assert summary["strategy"]["invested_pct"] == pytest.approx(0.6)
    # 값이 없으면 None — 현재값을 평균 자리에 대입하지 않는다
    portfolio_without = dict(portfolio)
    portfolio_without.pop("avg_exposure")
    assert live.summary(graded_rows, portfolio_without, pilot=None)["strategy"]["avg_exposure"] is None


# ── 4지표가 실제로 계산된다 ────────────────────────────────────────────────────
def test_pilot_all_four_computed(tmp_path):
    records = [record(f"s{i}", f"T{i}") for i in range(16)]
    rows = [graded(f"s{i}", 0.06, 0.04) for i in range(16)]
    for day, exposure in (("2026-01-05", 0.4), ("2026-01-06", 0.5)):
        live.append_portfolio_history(
            {"as_of": day, "cash_pct": 1 - exposure, "invested_pct": exposure,
             "equity_index": 1.05, "n_positions": 2}, tmp_path)
    exposure = live.avg_exposure_from_history(live.read_portfolio_history(tmp_path),
                                              date(2026, 1, 5), date(2026, 1, 6))
    ev = evaluate_pilot(records, rows, executions=executions([r["signal_id"] for r in records]),
                        portfolio={"total_return": 0.10, "avg_exposure": exposure},
                        benchmark_return=0.06, risk_free=0.01)
    metrics = {m["key"]: m for m in ev["metrics"]}
    assert ev["n_eff"] == 16
    for key in ("n_eff", "net_after_cost", "cash_equivalent", "tail_loss"):
        assert metrics[key]["value"] is not None, f"{key} 가 계산되지 않았다"
        assert metrics[key]["pass"] is True, f"{key}: {metrics[key]}"
    assert ev["verdict"] == VERDICT_CANDIDATE
    assert ev["verdict"] != VERDICT_HOLD
    summary = pilot_summary(ev)
    assert summary["n_records"] == 16 and summary["n_eff"] == 16
    assert all("note_ko" in m for m in summary["metrics"])
