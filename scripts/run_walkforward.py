#!/usr/bin/env python3
"""scripts/run_walkforward.py — 폴드 계획 + Phase 2 전략 성과·포트폴리오 리포트.

1) `--plan-only` (Phase 1): 폴드 표(`data/walkforward/folds/summary.json`)와
   `data/calibration/attempts.json` 만 만든다. 이 파일은 **날짜 계획**이므로 `action` 은
   `"planned"`(또는 `"deferred"`)다 — 보정을 돌렸다는 뜻의 `"calibrate"` 가 아니다(감사 10).
   `sufficient` 판정은 `--min-folds` 를 존중한다.
2) `--portfolio-demo` (Phase 2): 수집 데이터가 없어도 `신호 → 분할 계획 → 장 전 주문 →
   모의 계좌 → 전략 지표 → 슬리피지 0/5/10/20bp 민감도` 전 경로를 재현한다.
   결과는 `out/walkforward/demo/` 에 쓴다(gitignore — 보유·수량 필드가 들어간다).
3) `--scenario <json>`: 같은 경로를 외부 시나리오(실데이터로 만든 폴드)로 실행한다.

    python scripts/run_walkforward.py --today 2026-11-19 --plan-only
    python scripts/run_walkforward.py --portfolio-demo
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.backtest.demo import build_demo_scenario  # noqa: E402
from engine.backtest.report import (COST_SENSITIVITY_BPS, cost_sensitivity, fold_calibration_table,  # noqa: E402
                                    fold_report, write_fold_portfolio, write_fold_report)
from engine.backtest.runner import run_scenario  # noqa: E402
from engine.backtest.walk_forward import plan_and_attempts, write_fold_output  # noqa: E402


def write_plan(args) -> int:
    today = date.fromisoformat(args.today) if args.today else date.today()
    plan = plan_and_attempts(today, min_folds=args.min_folds)
    paths = write_fold_output(args.out, plan["summary"], plan["attempts"])
    attempts_dir = Path("data/calibration")
    attempts_dir.mkdir(parents=True, exist_ok=True)
    (attempts_dir / "attempts.json").write_text(
        json.dumps(plan["attempts"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "today": today.isoformat(),
        "holdout_start": plan["summary"]["holdout_start"],
        "min_folds": args.min_folds,
        "counts": {k: v["count"] for k, v in plan["summary"]["horizons"].items()},
        "actions": {a["horizon"]: a["action"] for a in plan["attempts"]["attempts"]},
        "blocked": plan["summary"]["blocked"],
        "calibration_table": fold_calibration_table(plan["summary"]),
        **paths,
    }, ensure_ascii=False))
    return 0


def run_portfolio_report(scenario: dict, out_dir: str | Path, fold_id: str = "demo",
                         initial_cash: float = 1_000_000.0) -> dict:
    """시나리오 → 모의 계좌 → 전략 지표 + 비용 민감도 + 폴드 리포트(파일)."""
    out_dir = Path(out_dir)
    def _run(slippage_bps: float, **_kwargs):
        return run_scenario(scenario, slippage_bps=slippage_bps, initial_cash=initial_cash).result()

    base = _run(float(COST_SENSITIVITY_BPS[2]))

    sensitivity = cost_sensitivity(_run, COST_SENSITIVITY_BPS)
    report = fold_report(fold_id, horizon="3m", signals=scenario.get("signals", []),
                         portfolio=base, sensitivity=sensitivity,
                         notes=["합성 시나리오(실데이터 아님) — 경로 재현용이다." if scenario.get("is_demo") else ""])
    report_path = write_fold_report(out_dir, fold_id, report)
    portfolio_path = write_fold_portfolio(out_dir, fold_id, base)
    return {
        "fold_id": fold_id,
        "signals": len(scenario.get("signals", [])),
        "status": report["status"],
        "strategy": {k: report["strategy"][k] for k in
                     ("total_return", "cagr", "mdd", "sharpe", "sortino", "calmar",
                      "avg_exposure", "turnover", "win_rate", "n_trades")},
        "cost_sensitivity": {k: v.get("cagr") for k, v in sensitivity.items()},
        "digest": base.digest(),
        "report_path": str(report_path),
        "portfolio_path": str(portfolio_path),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--today", default=None, help="YYYY-MM-DD (기본: 오늘)")
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--portfolio-demo", action="store_true", help="합성 시나리오로 Phase 2 경로 재현")
    ap.add_argument("--scenario", default=None, help="시나리오 JSON 경로 (실데이터 폴드)")
    ap.add_argument("--min-folds", type=int, default=3)
    ap.add_argument("--out", default="data/walkforward")
    ap.add_argument("--initial-cash", type=float, default=1_000_000.0)
    args = ap.parse_args(argv)

    rc = 0
    if args.plan_only or (not args.portfolio_demo and not args.scenario):
        rc = write_plan(args)
    if args.portfolio_demo:
        summary = run_portfolio_report(build_demo_scenario(), Path("out/walkforward"),
                                       fold_id="demo", initial_cash=args.initial_cash)
        print(json.dumps({"portfolio_demo": summary}, ensure_ascii=False))
    if args.scenario:
        scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
        summary = run_portfolio_report(scenario, Path(args.out) / "portfolio",
                                       fold_id=scenario.get("fold_id", "fold"), initial_cash=args.initial_cash)
        print(json.dumps({"scenario": summary}, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
