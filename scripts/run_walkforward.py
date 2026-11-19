#!/usr/bin/env python3
"""scripts/run_walkforward.py — 폴드 계획(1M/3M/6M) 생성과 보정 시도 기록.

Phase 1 에서는 `--plan-only` 로 폴드 표(`data/walkforward/folds/summary.json`)와
`data/calibration/attempts.json` 만 만든다. 폴드 재생(수집 데이터 필요)은 Phase 2.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.backtest.walk_forward import plan_and_attempts, write_fold_output  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--today", default=None, help="YYYY-MM-DD (기본: 오늘)")
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--min-folds", type=int, default=3)
    ap.add_argument("--out", default="data/walkforward")
    args = ap.parse_args(argv)

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
        "counts": {k: v["count"] for k, v in plan["summary"]["horizons"].items()},
        "blocked": plan["summary"]["blocked"],
        **paths,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
