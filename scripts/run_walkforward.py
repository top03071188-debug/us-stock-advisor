#!/usr/bin/env python3
"""scripts/run_walkforward.py — 폴드 계획 · 보정 · 스트레스 · 포트폴리오 리포트.

모드(모두 함께 줄 수 있다):

1) `--plan-only` (Phase 1): 폴드 표(`data/walkforward/folds/summary.json`)와
   `data/calibration/attempts.json` 만 만든다. 이 파일은 **날짜 계획**이므로 `action` 은
   `"planned"`(또는 `"deferred"`)다 — 보정을 돌렸다는 뜻의 `"calibrate"` 가 아니다(감사 10).
2) `--portfolio-demo` (Phase 2): 수집 데이터가 없어도 `신호 → 분할 계획 → 장 전 주문 →
   모의 계좌 → 전략 지표 → 슬리피지 민감도` 전 경로를 재현한다(`out/walkforward/demo/`).
3) `--scenario <json>`: 같은 경로를 외부 시나리오(실데이터로 만든 폴드)로 실행한다.
4) `--calibrate`: 스냅샷을 재생·채점해 후보를 탐색하고 시도·리포트를 남긴다
   (`--snapshots`, `--params`, `--min-folds`, `--min-signals`).
5) `--stress`: 2022/2020/2018Q4 세 구간을 실제로 돌려 `data/walkforward/stress.json` 을 만든다.
   기본은 **라이브 수집**(10년 가격·VIX), `--offline-demo` 는 합성 패널로 경로만 검증한다.
6) `--quarterly`: 분기 워크포워드 통합 — 계획 + 홀드아웃 확인 + 스냅샷 보정 + `reports/calibration.md`.
7) `--semiannual`: 반기 통합 — 계획 + 5년 확장 계획 + 스트레스 백테스트(`stress.json`).

    python scripts/run_walkforward.py --today 2026-11-19 --plan-only
    python scripts/run_walkforward.py --portfolio-demo
    python scripts/run_walkforward.py --quarterly
    python scripts/run_walkforward.py --semiannual --offline-demo
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.backtest.demo import build_demo_scenario  # noqa: E402
from engine.backtest.pipeline_runner import (build_offline_stress_panels, collect_stress_inputs,  # noqa: E402
                                             extended_summary, fold_plan_count,
                                             run_calibration_cycle, run_stress_backtest,
                                             stress_summary_for_cli, write_stress_payload)
from engine.backtest.report import (COST_SENSITIVITY_BPS, cost_sensitivity, fold_calibration_table,  # noqa: E402
                                    fold_report, write_fold_portfolio, write_fold_report)
from engine.backtest.runner import run_scenario  # noqa: E402
from engine.backtest.walk_forward import plan_and_attempts, write_fold_output  # noqa: E402
from engine.data.universe import load_delisted, load_universe_history  # noqa: E402
from engine.utils.dates import build_folds  # noqa: E402


def _previous_json(path: Path) -> dict:
    """기존 JSON(깨졌으면 빈 dict) — 계획을 다시 쓰면서 **보정 이력을 지우지 않는다**."""
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_plan(args) -> int:
    today = args.today_date
    plan = plan_and_attempts(today, min_folds=args.min_folds)
    attempts_dir = Path("data/calibration")
    attempts_dir.mkdir(parents=True, exist_ok=True)
    calibration_path = attempts_dir / "attempts.json"
    kept_records = [row for row in (_previous_json(calibration_path).get("attempts") or [])
                    if isinstance(row, dict) and row.get("kind") == "calibrate"]
    calibration_path.write_text(json.dumps(
        {**plan["attempts"], "attempts": list(plan["attempts"]["attempts"]) + kept_records},
        ensure_ascii=False, indent=2), encoding="utf-8")
    walkforward_path = Path(args.out) / "attempts.json"
    kept_calibrations = _previous_json(walkforward_path).get("calibrations") or []
    paths = write_fold_output(args.out, plan["summary"], plan["attempts"])
    merged = _previous_json(walkforward_path)
    merged["calibrations"] = kept_calibrations
    walkforward_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "today": today.isoformat(),
        "holdout_start": plan["summary"]["holdout_start"],
        "min_folds": args.min_folds,
        "counts": {k: v["count"] for k, v in plan["summary"]["horizons"].items()},
        "actions": {a["horizon"]: a["action"] for a in plan["attempts"]["attempts"]},
        "blocked": plan["summary"]["blocked"],
        "calibration_table": fold_calibration_table(plan["summary"]),
        "kept_calibrations": len(kept_calibrations or kept_records),
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


# ────────────────────────────── 보정·스트레스 ──────────────────────────────
def run_calibrate(args, stress_payload: dict | None = None) -> int:
    summary = run_calibration_cycle(
        today=args.today_date, snapshot_root=args.snapshots, params_path=args.params,
        params_dir=Path(args.params).parent, min_folds=args.min_folds,
        min_signals=args.min_signals, reports_dir=args.reports_dir,
        grading_panels=_grading_panels(args), stress_payload=stress_payload)
    record = summary.get("record") or {}
    print(json.dumps({
        "calibrate": {
            "today": summary["as_of"],
            "accepted": record.get("accepted"),
            "reason": record.get("reason"),
            "n_folds": record.get("n_folds"),
            "n_folds_planned": summary.get("n_folds_planned"),
            "n_snapshots": summary.get("n_snapshots"),
            "stress_gate": summary.get("stress"),
            "holdout": {"allowed": (summary.get("holdout") or {}).get("allowed"),
                        "reason": (summary.get("holdout") or {}).get("reason")},
            "params_version_new": summary.get("params_version_new"),
            "report_path": summary.get("report_path"),
            "attempts_path": summary.get("attempts_path"),
            "walkforward_attempts_path": summary.get("walkforward_attempts_path"),
        }
    }, ensure_ascii=False))
    return 0


def _grading_panels(args):
    """채점용 가격 패널 — `--grading-panels <dir>` 가 있으면 그 폴더의 CSV 를 읽는다.

    스냅샷은 그 시점까지만 데이터를 담으므로 **만기 이후 가격**은 따로 필요하다. 가장 최근
    스냅샷 폴더를 주면 과거 폴드의 만기 가격까지 덮는다. 없으면 채점할 수 없고, 평가기는
    빈 관측을 돌려준다(없는 채점을 만들지 않는다).
    """
    root = getattr(args, "grading_panels", None)
    if not root:
        return None
    from engine.backtest.snapshot import _panel_from_csv  # noqa: PLC0415 — 내부 로더 재사용

    base = Path(root)
    panels = {}
    for path in sorted(base.glob("prices_*.csv")):
        ticker = path.stem[len("prices_"):]
        try:
            panels[ticker] = _panel_from_csv(ticker, path)
        except (OSError, ValueError, KeyError):
            continue
    return panels or None


def _universe_inputs(args) -> tuple[object | None, object | None, bool | None]:
    """유니버스 이력·상장폐지 데이터(없으면 None) + 완전성 선언."""
    history = load_universe_history(args.universe_history) if Path(args.universe_history).exists() else None
    delisted = load_delisted(args.delisted) if Path(args.delisted).exists() else None
    complete: bool | None = True if args.delisted_complete else None
    if complete is None and delisted is not None:
        meta = Path(args.delisted).with_suffix(".meta.json")
        if meta.exists():
            try:
                complete = bool(json.loads(meta.read_text(encoding="utf-8")).get("complete"))
            except (OSError, json.JSONDecodeError):
                complete = None
    return history, delisted, complete


def run_stress(args) -> int:
    history, delisted, complete = _universe_inputs(args)
    if args.offline_demo:
        panels, vix = build_offline_stress_panels()
        payload = run_stress_backtest(
            panels=panels, params=args.params, vix=vix, universe_history=history,
            delisted=delisted, delisted_complete=complete, data_source="offline_demo",
            max_tickers=args.stress_tickers,
            extra_note_ko="합성 패널(offline_demo) — 경로 검증용이며 실제 성과가 아니다.")
    else:
        collected = collect_stress_inputs(as_of=args.today_date, max_tickers=args.stress_tickers)
        if not collected["panels"]:
            print(json.dumps({"stress": {"status": "no_data",
                                          "diagnostics": collected["diagnostics"]}},
                             ensure_ascii=False), file=sys.stderr)
            return 4
        payload = run_stress_backtest(
            panels=collected["panels"], params=args.params, vix=collected["vix"],
            sectors=collected["sectors"], universe_history=history, delisted=delisted,
            delisted_complete=complete, data_source="live",
            max_tickers=args.stress_tickers,
            extra_note_ko="라이브 수집(10년 가격·VIX) — 유니버스·상장폐지 검증 전에는 참고용이다.")
        payload["collection"] = {"tickers_requested": collected["tickers_requested"],
                                 "degraded": collected["diagnostics"].get("degraded"),
                                 "sources": collected["diagnostics"].get("sources")}
    path = write_stress_payload(payload, Path(args.stress_out).parent)
    print(json.dumps({"stress": {**stress_summary_for_cli(payload), "path": str(path)}},
                     ensure_ascii=False))
    return 0


def run_quarterly(args) -> int:
    write_plan(args)
    return run_calibrate(args)


def run_semiannual(args) -> int:
    write_plan(args)
    folds = build_folds(args.today_date)
    plan = extended_summary(args.today_date, folds_available=fold_plan_count(folds),
                            universe_verified=bool(Path(args.universe_history).exists()
                                                   and Path(args.delisted).exists()))
    print(json.dumps({"extended": plan}, ensure_ascii=False))
    return run_stress(args)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--today", default=None, help="YYYY-MM-DD (기본: 오늘)")
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--portfolio-demo", action="store_true", help="합성 시나리오로 Phase 2 경로 재현")
    ap.add_argument("--scenario", default=None, help="시나리오 JSON 경로 (실데이터 폴드)")
    ap.add_argument("--min-folds", type=int, default=3)
    ap.add_argument("--min-signals", type=int, default=25, help="폴드당 최소 신호 수(4.2.7)")
    ap.add_argument("--out", default="data/walkforward")
    ap.add_argument("--initial-cash", type=float, default=1_000_000.0)
    # ── 보정 ──
    ap.add_argument("--calibrate", action="store_true", help="스냅샷 기반 보정 1회")
    ap.add_argument("--snapshots", default="data/snapshots", help="스냅샷 루트")
    ap.add_argument("--params", default="config/params_v001.yaml", help="기준 파라미터")
    ap.add_argument("--grading-panels", default=None,
                    help="채점용 가격 CSV 폴더(prices_<TICKER>.csv) — 폴드 만기 이후 가격이 담긴 "
                         "폴더(예: 가장 최근 스냅샷 data/snapshots/2026-11-20). 없으면 채점하지 않는다")
    ap.add_argument("--reports-dir", default="reports")
    # ── 스트레스 ──
    ap.add_argument("--stress", action="store_true", help="3구간 스트레스 백테스트")
    ap.add_argument("--offline-demo", action="store_true", help="합성 패널로 스트레스 경로만 검증")
    ap.add_argument("--stress-out", default="data/walkforward/stress.json")
    ap.add_argument("--stress-tickers", type=int, default=None, help="스트레스에 쓸 종목 수 상한")
    ap.add_argument("--universe-history", default="data/universe_history.csv")
    ap.add_argument("--delisted", default="data/delisted_securities.csv")
    ap.add_argument("--delisted-complete", action="store_true",
                    help="상장폐지 데이터가 완전하다고 **선언**한다(파일만으로는 판정하지 않는다)")
    # ── 통합 ──
    ap.add_argument("--quarterly", action="store_true", help="분기 워크포워드 통합")
    ap.add_argument("--semiannual", action="store_true", help="반기 확장·스트레스 통합")
    args = ap.parse_args(argv)
    args.today_date = date.fromisoformat(args.today) if args.today else date.today()

    explicit = any((args.plan_only, args.portfolio_demo, args.scenario, args.calibrate,
                    args.stress, args.quarterly, args.semiannual))
    rc = 0
    if args.plan_only or not explicit:
        rc = write_plan(args)
    if args.portfolio_demo:
        summary = run_portfolio_report(build_demo_scenario(), Path("out/walkforward"),
                                       fold_id="demo", initial_cash=args.initial_cash)
        print(json.dumps({"portfolio_demo": summary}, ensure_ascii=False))
    if args.scenario:
        scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
        summary = run_portfolio_report(scenario, Path(args.out) / "portfolio",
                                       fold_id=scenario.get("fold_id", "fold"),
                                       initial_cash=args.initial_cash)
        print(json.dumps({"scenario": summary}, ensure_ascii=False))
    if args.stress:
        rc = run_stress(args) or rc
    if args.calibrate:
        rc = run_calibrate(args) or rc
    if args.quarterly:
        rc = run_quarterly(args) or rc
    if args.semiannual:
        rc = run_semiannual(args) or rc
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
