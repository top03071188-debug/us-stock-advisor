"""engine.backtest.walk_forward — 폴드 생성·자격 판정·채점·집계.

[고정] 4.2.1 / 4.2.7
- 채점 기간은 **거래일**(1M 21 / 3M 63 / 6M 126).
- `maturity_date < holdout_start` 인 폴드만 보정 코퍼스에 넣는다.
- 경계에서 잘린 폴드는 `truncated: true` + `observed_days` 로 **참고용**만.
- 적격 폴드가 `min_folds`(기본 3, `--min-folds` 로 변경) 미만인 기간은 보정 보류 → `attempts.json` 에 사유를 남긴다.
- `--plan-only` 의 `attempts.json` 은 **날짜 계획**이므로 `action: "planned"` 다(`"calibrate"` 아님).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np

from engine.utils.dates import Fold, fold_summary


@dataclass
class FoldResult:
    fold_id: str
    horizon_td: int
    start: date
    maturity: date
    eligible: bool
    truncated: bool
    observed_days: int
    signals: list[dict] = field(default_factory=list)
    agreement_mean: float | None = None
    ic: float | None = None
    win_rate: float | None = None
    mdd: float | None = None
    strategy_return: float | None = None
    universe_verified: bool = False

    def to_dict(self) -> dict:
        return {
            "fold_id": self.fold_id,
            "horizon_td": self.horizon_td,
            "start": self.start.isoformat(),
            "maturity": self.maturity.isoformat(),
            "eligible": self.eligible,
            "truncated": self.truncated,
            "observed_days": self.observed_days,
            "n_signals": len(self.signals),
            "agreement_mean": self.agreement_mean,
            "ic": self.ic,
            "win_rate": self.win_rate,
            "mdd": self.mdd,
            "strategy_return": self.strategy_return,
            "universe_verified": self.universe_verified,
        }


def agreement_components(direction: int, base_price: float, fill_price: float,
                         band_lo: float, band_hi: float, end_price: float,
                         predicted_mdd: float, realized_mdd: float,
                         p_entry: float, p_min: float, p_max: float,
                         benchmark_return: float | None = None) -> dict:
    """4.2.4 Agreement — 네 항목 전부 0~100.

    밴드는 `base_price` 기준 수익률로 환산해 같은 기준끼리 비교한다(달러 vs 수익률 혼합 금지).
    방향 점수 `D` 는 **초과수익**(`r_fill − r_SPY`)으로 판정한다(4.2.4). 벤치마크가 없으면
    `D` 는 `None` 이고 나머지 항목 가중치로 재정규화한다(0%로 갈음하지 않는다).
    """
    r_base = end_price / base_price - 1.0
    r_fill = end_price / fill_price - 1.0
    r_lo, r_hi = band_lo / base_price - 1.0, band_hi / base_price - 1.0
    width = r_hi - r_lo
    if width <= 0:
        band_score = None
    else:
        deviation = max(0.0, r_base - r_hi, r_lo - r_base)
        band_score = 100.0 * max(0.0, min(1.0, 1.0 - deviation / width))
    if benchmark_return is None:
        d = None
        excess = None
    else:
        excess = r_fill - float(benchmark_return)
        d = 100.0 if (excess > 0) == (direction > 0) else 0.0
    if abs(realized_mdd) <= 1e-9:
        dd = None
    else:
        dd = 100.0 * max(0.0, min(1.0, 1.0 - abs(abs(predicted_mdd) - abs(realized_mdd)) / max(abs(realized_mdd), 0.05)))
    if p_max <= p_min:
        timing = None
    else:
        if direction > 0:
            timing = 100.0 * max(0.0, min(1.0, 1.0 - (p_entry - p_min) / (p_max - p_min)))
        else:
            timing = 100.0 * max(0.0, min(1.0, 1.0 - (p_max - p_entry) / (p_max - p_min)))
    parts = {40.0: d, 25.0: band_score, 15.0: dd, 20.0: timing}
    total_w = sum(w for w, v in parts.items() if v is not None)
    total = sum(w * v for w, v in parts.items() if v is not None) / total_w if total_w else None
    return {
        "direction": d,
        "band": None if band_score is None else round(band_score, 1),
        "drawdown": None if dd is None else round(dd, 1),
        "timing": None if timing is None else round(timing, 1),
        "total": None if total is None else round(total, 1),
        "r_base": round(r_base, 4),
        "r_fill": round(r_fill, 4),
        "excess_return": None if excess is None else round(excess, 6),
    }


def spearman_ic(scores: list[float], returns: list[float]) -> float | None:
    if len(scores) < 3:
        return None
    s = np.asarray(scores, dtype=float)
    r = np.asarray(returns, dtype=float)
    if np.isnan(s).any() or np.isnan(r).any():
        return None
    s_rank = np.argsort(np.argsort(s))
    r_rank = np.argsort(np.argsort(r))
    if s_rank.std() == 0 or r_rank.std() == 0:
        return None
    return float(np.corrcoef(s_rank, r_rank)[0, 1])


def summarize_fold(fold: Fold, graded: list[dict], universe_verified: bool) -> FoldResult:
    """채점 결과 목록 → 폴드 요약. 신호 0건도 정상(평균 None)."""
    result = FoldResult(fold.fold_id, fold.horizon_td, fold.start, fold.maturity,
                        fold.eligible, fold.truncated, fold.observed_days,
                        signals=graded, universe_verified=universe_verified)
    totals = [g["agreement"]["total"] for g in graded if g.get("agreement", {}).get("total") is not None]
    if totals:
        result.agreement_mean = round(float(np.mean(totals)), 2)
    result.ic = spearman_ic([g["entry_score"] for g in graded], [g["excess_return"] for g in graded])
    if graded:
        result.win_rate = round(float(np.mean([1.0 if g["excess_return"] > 0 else 0.0 for g in graded])), 4)
        result.strategy_return = round(float(np.mean([g["excess_return"] for g in graded])), 4)
        result.mdd = round(float(min(g.get("mdd", 0.0) for g in graded)), 4)
    return result


def plan_and_attempts(today: date, min_folds: int = 3) -> dict:
    """폴드 요약 + **보정 계획** 기록(`calibration/attempts.json`). 6M 미달이면 보류 사유.

    `--plan-only` 는 날짜 계획만 만든다 — `action` 은 `"calibrate"`(보정을 돌렸다는 뜻)가 아니라
    **`"planned"`** 이다. `sufficient` 도 `min_folds`(CLI `--min-folds`)를 존중한다 — 예전에는 `>= 3`
    하드코딩으로 `--min-folds 5` 를 줘도 4·3폴드 구간이 `calibrate` 로 찍혔다(감사 10).
    """
    min_folds = int(min_folds)
    summary = fold_summary(today, min_folds=min_folds)
    attempts = {
        "as_of": today.isoformat(),
        "holdout_start": summary["holdout_start"],
        "kind": "plan",
        "min_folds": min_folds,
        "attempts": [],
    }
    for name, h in summary["horizons"].items():
        sufficient = bool(h["sufficient"])
        attempts["attempts"].append({
            "horizon": name,
            "horizon_td": h["horizon_td"],
            "eligible_folds": h["eligible_folds"],
            "count": h["count"],
            "min_folds": min_folds,
            "sufficient": sufficient,
            "action": "planned" if sufficient else "deferred",
            "reason": None if sufficient else "INSUFFICIENT_ELIGIBLE_FOLDS",
            "note": None if sufficient else (
                f"적격 폴드 {h['count']}/{min_folds} — "
                + ("보정 창을 15개월 이상으로 늘려 적격 폴드 3개를 확보한 뒤 연다(4.2.1)."
                   if name == "6m" else "표본이 부족하다.")
            ),
        })
    return {"summary": summary, "attempts": attempts}


def write_fold_output(root: str | Path, summary: dict, attempts: dict) -> dict:
    root = Path(root)
    (root / "folds").mkdir(parents=True, exist_ok=True)
    (root / "folds" / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "attempts.json").write_text(json.dumps(attempts, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"summary_path": str(root / "folds" / "summary.json"), "attempts_path": str(root / "attempts.json")}


def truncated_excluded_from_objective(results: list[FoldResult]) -> list[FoldResult]:
    """목적함수에 넣을 수 있는 결과만 — 잘린 폴드는 참고용(4.2.1)."""
    return [r for r in results if r.eligible and not r.truncated]
