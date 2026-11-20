"""test_report — 폴드별 전략 성과·비용 민감도·신호 0건 폴드 (기획서 4.6, Phase 2 DoD)."""

from __future__ import annotations

import json

import pytest

from engine.backtest.demo import build_demo_scenario
from engine.backtest.report import (COST_SENSITIVITY_BPS, cost_sensitivity, fold_report, max_drawdown,
                                    strategy_metrics, write_fold_portfolio, write_fold_report)
from engine.backtest.runner import run_scenario


def curve(values: list[float], exposure: float = 0.5) -> list[dict]:
    return [{"date": f"2026-06-{i + 1:02d}", "equity": v, "exposure": exposure}
            for i, v in enumerate(values)]


def test_max_drawdown_and_metrics():
    values = [100.0, 110.0, 99.0, 120.0, 90.0, 130.0]
    assert max_drawdown(values) == pytest.approx(90 / 120 - 1)
    m = strategy_metrics(curve(values), [{"pnl_pct": 0.10, "exit_price": 10, "shares": 1},
                                        {"pnl_pct": -0.05, "exit_price": 10, "shares": 1}],
                         initial_cash=100.0)
    assert m["total_return"] == pytest.approx(0.30)
    assert m["mdd"] == pytest.approx(-0.25)
    assert m["win_rate"] == 0.5
    assert m["profit_factor"] == pytest.approx(2.0)
    assert m["avg_exposure"] == pytest.approx(0.5)
    assert m["sharpe"] is not None and m["sortino"] is not None
    assert m["calmar"] is not None and m["turnover"] is not None


def test_metrics_with_single_point_are_none():
    m = strategy_metrics(curve([100.0]), [], initial_cash=100.0)
    assert m["cagr"] is None and m["mdd"] is None and m["win_rate"] is None


def test_benchmark_comparison():
    ours = curve([100, 101, 102, 103])
    bench = curve([100, 100.5, 101, 101.5])
    m = strategy_metrics(ours, [], initial_cash=100.0, benchmark_curve=bench)
    assert m["benchmark"]["total_return"] == pytest.approx(0.015)
    assert m["excess_cagr"] > 0


def test_cost_sensitivity_runs_four_levels_and_decays():
    scenario = build_demo_scenario()
    runs = {bps: run_scenario(scenario, slippage_bps=float(bps)).result() for bps in COST_SENSITIVITY_BPS}

    def _run(slippage_bps: float, **_kwargs):
        return run_scenario(scenario, slippage_bps=slippage_bps).result()

    out = cost_sensitivity(_run, COST_SENSITIVITY_BPS)
    assert list(out) == ["0bp", "5bp", "10bp", "20bp"]
    returns = [out[f"{bps}bp"]["total_return"] for bps in COST_SENSITIVITY_BPS]
    assert returns == sorted(returns, reverse=True)          # 슬리피지가 커지면 수익이 줄어든다
    assert returns[0] > returns[-1]
    # 같은 폴드를 네 번 돌린 결과가 서로 다르다 = 비용이 실제로 반영된다
    assert len(set(r["total_return"] for r in out.values())) > 1


def test_fold_report_zero_signals_is_completed():
    report = fold_report("F0", "3m", [], portfolio=None)
    assert report["status"] == "completed"
    assert report["n_signals"] == 0
    assert report["strategy"]["cagr"] is None
    assert any("신호 0건" in note for note in report["notes"])
    assert report["cost_sensitivity"] == {}


def test_fold_report_with_portfolio(tmp_path):
    scenario = build_demo_scenario()
    result = run_scenario(scenario, slippage_bps=10).result()
    report = fold_report("F1", "3m", scenario["signals"], portfolio=result,
                         sensitivity={"10bp": {"cagr": 0.1}})
    assert report["status"] == "completed" and report["n_signals"] == len(scenario["signals"])
    assert report["strategy"]["n_trades"] >= 0
    assert report["portfolio"]["digest"].startswith("sha256:")
    assert report["portfolio"]["equity_curve"]
    path = write_fold_report(tmp_path, "F1", report)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["fold_id"] == "F1" and stored["status"] == "completed"
    ppath = write_fold_portfolio(tmp_path, "F1", result)
    assert ppath.exists() and "equity_curve" in json.loads(ppath.read_text(encoding="utf-8"))


def test_metrics_exposure_and_turnover_bounded():
    scenario = build_demo_scenario()
    result = run_scenario(scenario, slippage_bps=10).result()
    m = strategy_metrics(result.equity_curve, result.trades, initial_cash=result.params["initial_cash"])
    assert 0.0 <= m["avg_exposure"] <= 1.0
    assert m["turnover"] is None or m["turnover"] >= 0
