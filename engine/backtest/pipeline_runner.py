"""engine.backtest.pipeline_runner — 백테스트·스트레스·자동보정의 **실가동 배선**.

계획(`walk_forward.plan_and_attempts`)과 판정 함수(`calibration.calibrate`,
`stress.stress_report`) 사이에서 **실제로 계산을 돌리는** 부분이다.

(A) `build_snapshot_evaluator` — 스냅샷 재생 → 채점 → `FoldObservation`.
    `calibrate()` 는 후보마다 이 러너를 부르므로 후보 파라미터가 실제로 엔진에 닿아야 한다
    (`calibration.map_candidate_to_params`).
(B) `run_stress_backtest` — 2022/2020/2018Q4 세 구간을 일별로 돌려 실측 지표를 만든다.
(C) `run_calibration_cycle` — 홀드아웃 확인 → 보정 1회 → 시도 기록 → (수락 시) 새 파라미터 버전 +
    `reports/calibration.md` PR 본문.

[고정 — "데이터가 없으면 통과가 아니다"]
- (B)는 `universe_history`·`delisted_securities` 가 **검증**됐을 때만 `stress_report(uh, delisted, results=...)`
  로 게이트를 판정한다. 미확보·불완전이면 `stress_report(None, None)` 안전핀을 **그대로** 쓰고,
  실측 수치는 `reference_metrics` + `reference_only: true` 로만 남긴다(게이트·성적표에 쓰지 않는다).
- (B)에서 가격만 있는 실행은 기초 점수(Q·V·G)를 **참조용 중립 가정**으로 채운다(`entry_basis`).
  그래서 그 결과도 `reference_only` 다 — 실데이터 기초가 붙기 전에는 게이트가 아니다.
- (C)는 스트레스가 `gate_passed=True` 가 아니면 후보를 채택하지 않는다(`stress_pass` 콜러블이 False).
- 홀드아웃 경계(`maturity_date < holdout_start`)를 넘는 폴드는 목적함수에 넣지 않는다.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from engine.backtest.calibration import (FoldObservation, calibrate, map_candidate_to_params,
                                         observations_from_fold_results, rebalance_weights,
                                         write_attempts)
from engine.backtest.execution import band_from_dict, opening_fill
from engine.backtest.holdout import HoldoutLedger
from engine.backtest.replay import replay
from engine.backtest.runner import run_scenario
from engine.backtest.snapshot import Snapshot, load_snapshot, load_snapshot_dataset
from engine.backtest.stress import (STRESS_WINDOWS, extended_plan, stress_report,
                                    write_stress_report)
from engine.backtest.walk_forward import Fold, agreement_components, summarize_fold
from engine.data.universe import (DELISTED_COLUMNS, UNIVERSE_COLUMNS, coverage, membership_at,
                                  validate_universe)
from engine.decide.pipeline import load_params
from engine.decide.signals import EntryContext, build_entry_plan, evaluate_entry
from engine.decide.veto import VetoInputs, load_veto_rules, veto_check
from engine.ledger.portfolio import Bar, BuyOrder, PortfolioSimulator, TrancheOrder
from engine.metrics.technical import atr_pct, bollinger_pct_b, dma, rsi14
from engine.models import PriceKind
from engine.utils.dates import build_folds, fold_summary, trading_days

#: 보정 대상 평면 키 — 그 밖의 키는 `map_candidate_to_params` 가 거절한다.
FLAT_KEYS = ("w_quality", "w_valuation", "w_timing", "w_macro", "w_growth",
             "min_margin", "rsi_buy", "dma50_dev", "structural_risk_block", "risk_off_multiplier")
WEIGHT_KEYS = ("w_quality", "w_valuation", "w_timing", "w_macro", "w_growth")
DEFAULT_WEIGHTS = {"quality": 0.30, "valuation": 0.25, "timing": 0.20, "macro": 0.15, "growth": 0.10}

#: 스트레스 구간 지표 이름(공개 산출물 키 — 원시 OHLC 시계열은 넣지 않는다).
METRIC_KEYS = ("strategy_mdd", "benchmark_mdd", "v06_fired", "new_buys_during_crash",
               "loss_ratio", "n_signal_days", "n_orders", "n_fills", "n_closed_trades",
               "strategy_total_return", "benchmark_total_return", "max_exposure",
               "final_equity_ratio", "n_universe_at_start", "n_days", "v06_day_count",
               "determinism_check")

#: 가격만 있을 때 쓰는 **참조용** 기초 가정 — 게이트·성적표에 쓰지 않는다.
REFERENCE_FUNDAMENTALS = {"q": 70.0, "v": 70.0, "g": 50.0, "iv_multiple": 1.30,
                          "structural_risk": 0.0}
REFERENCE_ENTRY_BASIS = (
    "price_only_neutral_proxy — 가격만으로 Q·V·G 를 채울 수 없어 중립 가정"
    "(Q 70 · IV = 가격×1.30 · structural_risk 0)으로 2층 게이트를 돌렸다. "
    "이 실행의 수치는 참고용이며 게이트·성적표에 쓰지 않는다."
)
UNIVERSE_MISSING_NOTE = (
    "universe_history·delisted_securities 미확보/불완전 — 생존편향이 남아 있어 "
    "게이트 통과로 계산하지 않는다(기획서 4.3)."
)


# ────────────────────────────── 공통 유틸 ──────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _mdd(values: Iterable[float]) -> float | None:
    """최대 낙폭(MDD) — 고점 대비 최저 비율(음수). 값이 없으면 None."""
    peak = None
    worst = None
    for value in values:
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if not np.isfinite(v):
            continue
        peak = v if peak is None else max(peak, v)
        if peak and peak > 0:
            drawdown = v / peak - 1.0
            worst = drawdown if worst is None else min(worst, drawdown)
    return None if worst is None else round(float(worst), 6)


def _ret(values: Iterable[float]) -> float | None:
    clean = [float(v) for v in values if v is not None and np.isfinite(float(v))]
    if len(clean) < 2 or clean[0] <= 0:
        return None
    return float(clean[-1] / clean[0] - 1.0)


def indicator_table(panel) -> pd.DataFrame:
    """한 종목의 **시점별 지표표** — 각 행은 그날까지의 값만 쓴다(미래 참조 없음).

    `snapshot_indicators()` 를 날마다 부르면 전체 창을 다시 계산하므로, 롤링 계산을 한 번만 하고
    날짜로 조회한다. 계산식은 `engine.metrics.technical` 의 라이브 함수와 같다.
    """
    sadj = panel.series(PriceKind.split_adj_ohlc)
    frame = sadj.frame
    out = pd.DataFrame(index=pd.DatetimeIndex(frame.index))
    for column in ("sadj_close", "raw_open", "raw_high", "raw_low", "raw_close"):
        if column not in frame.columns:
            raise ValueError(f"{getattr(panel, 'ticker', '?')}: 가격 컬럼 {column} 이 없다")
        out[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
    out["split_factor"] = (pd.to_numeric(frame["split_factor"], errors="coerce").fillna(1.0).astype(float)
                           if "split_factor" in frame.columns else 1.0)
    out["atr_pct"] = atr_pct(sadj)
    out["dma50"] = dma(sadj, 50)
    out["dma200"] = dma(sadj, 200)
    out["rsi14"] = rsi14(sadj)
    out["pct_b"] = bollinger_pct_b(sadj)
    line = out["dma200"]
    out["dma200_slope"] = line / line.shift(20) - 1.0
    out["vol60"] = out["sadj_close"].pct_change().rolling(60, min_periods=21).std(ddof=1)
    out = out.dropna(subset=["sadj_close", "raw_close"])
    out.index = pd.DatetimeIndex(out.index).normalize()
    return out


def _row_on(table: pd.DataFrame | None, day: date) -> pd.Series | None:
    """그날(정확히)의 지표 행 — 없으면 None(휴장·결측)."""
    if table is None or table.empty:
        return None
    stamp = pd.Timestamp(day)
    if stamp not in table.index:
        return None
    return table.loc[stamp]


def _bars_on(tables: Mapping[str, pd.DataFrame], day: date, tickers: Iterable[str],
             sectors: Mapping[str, str] | None = None) -> dict[str, Bar]:
    """그날 봉 묶음 — 봉이 없는 종목은 빠진다(체결·평가에서 제외)."""
    bars: dict[str, Bar] = {}
    for ticker in tickers:
        row = _row_on(tables.get(ticker), day)
        if row is None or not np.isfinite(float(row["raw_close"])):
            continue
        bars[ticker] = Bar(
            date=day,
            raw_open=float(row["raw_open"]),
            raw_high=float(row["raw_high"]),
            raw_low=float(row["raw_low"]),
            raw_close=float(row["raw_close"]),
            sadj_close=float(row["sadj_close"]),
            dma200=float(row["dma200"]) if np.isfinite(row["dma200"]) else None,
            atr_pct=float(row["atr_pct"]) if np.isfinite(row["atr_pct"]) else None,
            rsi14=float(row["rsi14"]) if np.isfinite(row["rsi14"]) else None,
            sector=(sectors or {}).get(ticker, ""),
        )
    return bars


# ────────────────────────────── (A) 스냅샷 워크포워드 ──────────────────────────────
@dataclass
class FoldPlan:
    """적격 폴드 하나에 대응하는 스냅샷 — 재생할 시점과 채점 만기를 함께 고정한다."""

    fold_id: str
    horizon_td: int
    start: date
    maturity: date
    root: Path
    snapshot_id: str
    as_of: date

    @property
    def key(self) -> tuple:
        return (self.horizon_td, self.fold_id, self.maturity.isoformat())

    def as_dict(self) -> dict:
        return {"fold_id": self.fold_id, "horizon_td": self.horizon_td,
                "start": self.start.isoformat(), "maturity": self.maturity.isoformat(),
                "snapshot_root": str(self.root), "snapshot_id": self.snapshot_id,
                "as_of": self.as_of.isoformat()}


def list_snapshots(root: str | Path = "data/snapshots") -> tuple[list[Snapshot], list[str]]:
    """스냅샷 폴더 목록(decision_at 오름차순) + 읽지 못한 폴더 사유.

    `manifest.json` 이 없는 폴더·깨진 매니페스트는 **조용히 건너뛰지 않고** 사유를 남긴다.
    """
    base = Path(root)
    snapshots: list[Snapshot] = []
    problems: list[str] = []
    if not base.exists():
        return snapshots, [f"missing_root:{base}"]
    for folder in sorted(p for p in base.iterdir() if p.is_dir()):
        if not (folder / "manifest.json").exists():
            problems.append(f"no_manifest:{folder.name}")
            continue
        try:
            snapshots.append(load_snapshot(folder))
        except (OSError, ValueError, KeyError) as exc:
            problems.append(f"broken_manifest:{folder.name}:{type(exc).__name__}")
    snapshots.sort(key=lambda s: (s.decision_at, str(s.root)))
    return snapshots, problems


def plan_fold_snapshots(snapshots: Iterable[Snapshot], folds: Mapping[int, list[Fold]],
                        tolerance_days: int = 10) -> list[FoldPlan]:
    """적격 폴드(`maturity < holdout_start`)마다 그 시작일 부근의 스냅샷을 고른다.

    한 폴드에 스냅샷이 여럿이면 **가장 이른** 것을 쓴다(폴드 시작일에 가장 가까운 판단).
    스냅샷이 없는 폴드는 계획에서 빠진다 — 없는 폴드를 만들어 내지 않는다.
    """
    pool = list(snapshots)
    plans: list[FoldPlan] = []
    for horizon_td in sorted(folds):
        for fold in folds[horizon_td]:
            if not fold.eligible:
                continue
            candidates = [s for s in pool
                          if fold.start <= _as_date(s.as_of) <= fold.start + timedelta(days=tolerance_days)]
            if not candidates:
                continue
            chosen = min(candidates, key=lambda s: (s.as_of, str(s.root)))
            plans.append(FoldPlan(fold_id=fold.fold_id, horizon_td=int(fold.horizon_td),
                                  start=fold.start, maturity=fold.maturity, root=Path(chosen.root),
                                  snapshot_id=chosen.snapshot_id, as_of=_as_date(chosen.as_of)))
    return plans


def grade_signal(signal: dict, tables: Mapping[str, pd.DataFrame],
                 benchmark_table: pd.DataFrame | None, horizon_td: int,
                 slippage_bps: float = 10.0) -> dict | None:
    """신호 1건을 만기까지 채점한다 — `agreement_components`(4.2.4) 그대로.

    가격 기준 [고정]: 판단·채점은 `split_adj_ohlc`, 체결가는 `raw_as_traded`.
    밴드(`target_band`)는 `base_price`(raw) 기준이므로 base 봉의 `sadj/raw` 비율로 한 기준에 맞춘다.
    만기 봉이 데이터에 없으면 **None**(채점 불가 — 없는 채점을 만들지 않는다).
    """
    ticker = str(signal.get("ticker") or "").upper()
    band = signal.get("target_band") or {}
    table = tables.get(ticker)
    base_bar = _as_date(band.get("base_bar_date")) or _as_date(signal.get("as_of"))
    base_price = band.get("base_price")
    if table is None or table.empty or base_bar is None or not base_price:
        return None
    stamp = pd.Timestamp(base_bar)
    base_pos = int(table.index.searchsorted(stamp, side="right")) - 1
    if base_pos < 0:
        return None
    entry_pos = base_pos + 1
    end_pos = base_pos + 1 + int(horizon_td)
    if end_pos >= len(table):
        return None
    base_row = table.iloc[base_pos]
    if float(base_row["raw_close"]) <= 0:
        return None
    basis = float(base_row["sadj_close"]) / float(base_row["raw_close"])
    window = table.iloc[entry_pos:end_pos + 1]
    fill_raw = float(table.iloc[entry_pos]["raw_open"]) or float(base_price)
    entry_band = band_from_dict(signal.get("entry_band"))
    if entry_band is not None:
        fill = opening_fill(entry_band, fill_raw, slippage_bps)
        if fill.get("execution_event") == "buy_filled":
            fill_raw = float(fill["fill_price"])
    fill_sadj = fill_raw * basis
    base_sadj = float(base_price) * basis
    end_sadj = float(window["sadj_close"].iloc[-1])
    realized_mdd = float(min(window["sadj_close"] / float(window["sadj_close"].iloc[0]) - 1.0))
    benchmark_return = None
    if benchmark_table is not None and not benchmark_table.empty:
        marks = benchmark_table.index
        first_idx = int(marks.searchsorted(table.index[entry_pos], side="left"))
        last_idx = int(marks.searchsorted(table.index[end_pos], side="right")) - 1
        if 0 <= first_idx < len(marks) and last_idx > first_idx:
            first = float(benchmark_table["sadj_close"].iloc[first_idx])
            last = float(benchmark_table["sadj_close"].iloc[last_idx])
            if first > 0:
                benchmark_return = last / first - 1.0
    predicted = signal.get("expected_mdd")
    predicted_mdd = float(predicted) if predicted is not None else -0.09
    lo = float(band.get("lo", base_price))
    hi = float(band.get("hi", base_price))
    agreement = agreement_components(
        direction=1, base_price=base_sadj, fill_price=fill_sadj,
        band_lo=base_sadj * (lo / float(base_price)),
        band_hi=base_sadj * (hi / float(base_price)),
        end_price=end_sadj, predicted_mdd=predicted_mdd, realized_mdd=realized_mdd,
        p_entry=fill_sadj, p_min=float(window["sadj_close"].min()),
        p_max=float(window["sadj_close"].max()), benchmark_return=benchmark_return,
    )
    excess = None if benchmark_return is None else (end_sadj / fill_sadj - 1.0) - benchmark_return
    return {
        "ticker": ticker,
        "signal_id": signal.get("signal_id"),
        "entry_score": float(signal.get("entry_score") or 0.0),
        "excess_return": 0.0 if excess is None else round(float(excess), 6),
        "mdd": round(realized_mdd, 6),
        "agreement": agreement,
        "fill_price": round(fill_raw, 6),
        "end_price": round(end_sadj, 6),
        "maturity_bar": table.index[end_pos].date().isoformat(),
    }


def _engine_params_for(candidate: Mapping | None, base_params: Mapping) -> dict:
    """평면 후보 → 엔진 중첩 설정. 가중치는 base 로 채운 뒤 합 1 로 맞춘다(감사 10)."""
    flat = {key: float(value) for key, value in (candidate or {}).items() if key in FLAT_KEYS}
    if not flat:
        return copy.deepcopy(dict(base_params))
    if any(key in flat for key in WEIGHT_KEYS):
        current = (base_params or {}).get("entry_weights") or {}
        weights = {f"w_{name}": float(current.get(name, DEFAULT_WEIGHTS[name])) for name in DEFAULT_WEIGHTS}
        weights.update({key: value for key, value in flat.items() if key in WEIGHT_KEYS})
        weights = rebalance_weights(weights)
        flat.update({key: weights[key] for key in WEIGHT_KEYS})
    return map_candidate_to_params(flat, copy.deepcopy(dict(base_params)))


def build_snapshot_evaluator(snapshot_root: str | Path = "data/snapshots", *,
                             base_params: dict | str | Path | None = None,
                             today: date | None = None, holdout_start: date | None = None,
                             horizons: tuple[int, ...] = (21, 63, 126),
                             grading_panels: Mapping[str, object] | Callable[[FoldPlan], Mapping[str, object]] | None = None,
                             benchmark: str = "SPY", code_version: str | None = None,
                             params_version: str | None = None,
                             snapshots: Iterable[Snapshot] | None = None,
                             fold_plans: Iterable[FoldPlan] | None = None,
                             tolerance_days: int = 10, slippage_bps: float = 10.0,
                             universe_verified: bool = False,
                             diagnostics: dict | None = None) -> Callable[[dict], list[FoldObservation]]:
    """스냅샷 기반 `FoldEvaluator` — **후보마다** 재생·채점을 다시 돌린다(4.2.7·R10c).

    `grading_panels` 는 채점 가격(스냅샷 이후 봉)이다. 스냅샷은 자기 시점까지만 담으므로
    미래 가격은 따로 준다. 폴드마다 가격 기준일이 달라 **폴드별 패널**이 필요하면
    `Callable[[FoldPlan], Mapping]` 로 넘긴다(첫 호출 결과를 폴드별로 캐시한다).

    적격 폴드나 스냅샷이 0건, 또는 채점 가격이 없으면 `evaluate()` 는 **빈 목록**을 돌려주고
    `calibrate()` 가 `INSUFFICIENT_FOLDS` 로 보류 사유를 남긴다(빈 입력을 수락으로 바꾸지 않는다).
    """
    if base_params is None or isinstance(base_params, (str, Path)):
        base = load_params(base_params or "config/params_v001.yaml")
    else:
        base = copy.deepcopy(dict(base_params))
    today = today or date.today()
    if fold_plans is not None:
        plans = list(fold_plans)
    else:
        pool = list(snapshots) if snapshots is not None else list_snapshots(snapshot_root)[0]
        folds = build_folds(today, horizons=tuple(horizons), holdout_start=holdout_start)
        plans = plan_fold_snapshots(pool, folds, tolerance_days=tolerance_days)
    if holdout_start is not None:
        plans = [p for p in plans if p.maturity < holdout_start]
    params_version = params_version or str(base.get("version") or "params_v001")
    code_version = code_version or "unknown"
    table_cache: dict[str, dict[str, pd.DataFrame]] = {}

    def _tables_for(plan: FoldPlan | None, root: Path | None) -> dict[str, pd.DataFrame]:
        """폴드별 채점 지표표 — 콜러블 패널이면 폴드 키로 캐시한다."""
        if grading_panels is None:
            return {}
        key = plan.key if plan is not None else ("static",)
        if key in table_cache:
            return table_cache[key]
        source = grading_panels(plan) if callable(grading_panels) and plan is not None else grading_panels
        if callable(source) or not source:
            table_cache[key] = {}
            return {}
        table_cache[key] = {str(t).upper(): indicator_table(p) for t, p in source.items() if p is not None}
        if not table_cache[key] and root is not None and plan is not None:
            data = load_snapshot_dataset(root)
            table_cache[key] = {str(t).upper(): indicator_table(p)
                                for t, p in (data.get("panels") or {}).items()}
        return table_cache[key]

    _tables_for(None, None)
    if diagnostics is not None:
        diagnostics.update({
            "n_folds_planned": len(plans),
            "folds": [p.as_dict() for p in plans],
            "grading_panel_available": grading_panels is not None,
            "params_version": params_version,
        })

    def evaluate(candidate: dict) -> list[FoldObservation]:
        engine_params = _engine_params_for(candidate, base)
        out: list[FoldObservation] = []
        for plan in plans:
            try:
                snap = load_snapshot(plan.root)
                result = replay(snap, engine_params, params_version=params_version,
                                code_commit=code_version)
            except (OSError, ValueError, KeyError):
                continue
            tables = _tables_for(plan, plan.root)
            if not tables:
                continue
            bench_table = tables.get(str(benchmark).upper())
            signals = [s for s in (result.signals or [])
                       if s.get("decision") == "BUY" or s.get("type") == "BUY"]
            graded = [g for g in (grade_signal(s, tables, bench_table, plan.horizon_td, slippage_bps)
                                  for s in signals) if g is not None]
            fold = Fold(plan.fold_id, plan.start, plan.horizon_td, plan.maturity,
                        eligible=True, observed_days=plan.horizon_td, truncated=False)
            summary = summarize_fold(fold, graded, universe_verified=universe_verified)
            out.extend(observations_from_fold_results(
                [summary],
                holdout_start=holdout_start.isoformat() if isinstance(holdout_start, date) else None,
                code_version=code_version, snapshot_id=plan.snapshot_id))
        return out

    evaluate.plans = plans  # type: ignore[attr-defined] — CLI·감사용 진단
    return evaluate


# ────────────────────────────── (B) 스트레스 백테스트 ──────────────────────────────
def verify_universe_inputs(universe_history: pd.DataFrame | None,
                           delisted: pd.DataFrame | None,
                           windows: Iterable[dict] = STRESS_WINDOWS,
                           delisted_complete: bool | None = None,
                           index_size: int = 500, tolerance: float = 0.02) -> dict:
    """게이트에 쓸 수 있는 유니버스·상장폐지 데이터인가.

    [고정] 무료 API 로는 상장폐지 종목의 최종가·`delisting_return` 을 **완전히** 확보할 수 없다.
    그래서 상장폐지 데이터는 ① 필수 컬럼 ② 상장폐지일 파싱 ③ **모든 행의 수익률 확인**
    ④ 커버리지 시작일 ⑤ 완전성 선언(`delisted_complete`)이 모두 맞아야 검증으로 본다.
    선언이 없으면 파일만으로 "완전"을 주장하지 않는다.

    유니버스 이력도 **구성종목 수**를 본다 — 공개 변경 이력은 "선별된 변경"이라 빠진 구간이 있고,
    어떤 시점 구성종목 수가 지수 규모(500)에서 ±2% 를 벗어나면 그 이력은 불완전하다.
    """
    reasons: list[str] = []
    items = list(windows)
    earliest = min((w["start"] for w in items), default=date(2018, 10, 1))
    stamp = datetime.combine(earliest, datetime.min.time(), tzinfo=timezone.utc)
    uh_ok = False
    membership_counts: dict[str, int] = {}
    if universe_history is None or getattr(universe_history, "empty", True):
        reasons.append("UNIVERSE_HISTORY_MISSING")
    else:
        missing = [c for c in UNIVERSE_COLUMNS[:4] if c not in universe_history.columns]
        if missing:
            reasons.append(f"UNIVERSE_HISTORY_COLUMNS:{missing}")
        else:
            check = validate_universe(universe_history)
            if not check.get("ok"):
                reasons.append(f"UNIVERSE_INVALID:{check.get('reasons')}")
            else:
                cov = coverage(universe_history, stamp)
                if not cov.get("covered"):
                    reasons.append(f"UNIVERSE_COVERAGE:{cov.get('reason')}")
                elif not membership_at(universe_history, stamp):
                    reasons.append("UNIVERSE_EMPTY_AT_WINDOW")
                else:
                    uh_ok = True
                for window in items:
                    day = window["start"]
                    count = len(membership_at(
                        universe_history,
                        datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)))
                    membership_counts[day.isoformat()] = count
                    if index_size and abs(count - index_size) / index_size > tolerance:
                        reasons.append(f"UNIVERSE_MEMBER_COUNT:{day.isoformat()}:{count}/{index_size}")
                        uh_ok = False
    delisted_ok = False
    if delisted is None or getattr(delisted, "empty", True):
        reasons.append("DELISTED_MISSING")
    else:
        missing = [c for c in ("ticker", "delist_date", "delisting_return") if c not in delisted.columns]
        if missing:
            reasons.append(f"DELISTED_COLUMNS:{missing}")
        else:
            dates = pd.to_datetime(delisted["delist_date"], errors="coerce")
            returns = pd.to_numeric(delisted["delisting_return"], errors="coerce")
            unknown = int(returns.isna().sum())
            bad_dates = int(dates.isna().sum())
            if bad_dates:
                reasons.append(f"DELISTED_BAD_DATE:{bad_dates}")
            if unknown:
                reasons.append(f"DELISTED_UNKNOWN_LIQUIDATION:{unknown}")
            if delisted_complete is not True:
                reasons.append("DELISTED_COMPLETENESS_UNDECLARED")
            if not bad_dates and not unknown and delisted_complete is True:
                delisted_ok = True
            if not bad_dates and dates.max() < pd.Timestamp(earliest):
                reasons.append("DELISTED_NOT_COVERING_WINDOW")
                delisted_ok = False
    return {
        "universe_verified": bool(uh_ok and delisted_ok),
        "universe_history_verified": bool(uh_ok),
        "delisted_verified": bool(delisted_ok),
        "reasons": reasons,
        "membership_counts": membership_counts,
        "index_size": int(index_size),
        "tolerance": float(tolerance),
        "required_columns": list(UNIVERSE_COLUMNS),
        "delisted_columns": list(DELISTED_COLUMNS),
        "coverage_required_from": earliest.isoformat(),
    }


def _reference_context(ticker: str, stamp: pd.Timestamp, row: pd.Series, *, sector: str,
                       provider: Callable[[str, date], dict] | None,
                       reference: Mapping) -> EntryContext | None:
    """그날의 2층 판단 입력 — 지표는 그날까지의 값만, 기초는 provider(없으면 중립 가정)."""
    if not np.isfinite(row["sadj_close"]) or float(row["sadj_close"]) <= 0:
        return None
    values = dict(reference)
    if provider is not None:
        supplied = provider(ticker, stamp.date()) or {}
        values.update({k: v for k, v in supplied.items() if v is not None})
    iv_median = values.get("iv_median")
    if iv_median is None:
        iv_median = float(row["sadj_close"]) * float(values.get("iv_multiple") or 1.30)
    return EntryContext(
        ticker=ticker,
        raw_close=float(row["raw_close"]),
        sadj_close=float(row["sadj_close"]),
        split_factor=float(row["split_factor"]) if np.isfinite(row["split_factor"]) else 1.0,
        sector=sector,
        dma50=float(row["dma50"]) if np.isfinite(row["dma50"]) else None,
        dma200=float(row["dma200"]) if np.isfinite(row["dma200"]) else None,
        dma200_slope=float(row["dma200_slope"]) if np.isfinite(row["dma200_slope"]) else None,
        rsi14=float(row["rsi14"]) if np.isfinite(row["rsi14"]) else None,
        pct_b=float(row["pct_b"]) if np.isfinite(row["pct_b"]) else None,
        atr_pct=float(row["atr_pct"]) if np.isfinite(row["atr_pct"]) else None,
        iv_median=float(iv_median),
        q=float(values.get("q")) if values.get("q") is not None else None,
        v=float(values.get("v")) if values.get("v") is not None else None,
        g=float(values.get("g")) if values.get("g") is not None else None,
        structural_risk=float(values.get("structural_risk") or 0.0),
        in_watchlist=True,
        held=False,
        news_state=str(values.get("news_state") or "read_done"),
        evidence=[{"type": "fundamental", "metric": "quality_composite", "value_kind": "score_0_100"},
                  {"type": "sec_filing", "form": "10-K", "accession": "stress_reference"},
                  {"type": "market", "metric": "atr_pct", "value_kind": "ratio"}],
    )


def _order_from_plan(signal_id: str, ticker: str, committed_at: datetime, plan: Mapping,
                     sector: str) -> BuyOrder:
    """`build_entry_plan()` 결과 → 장 전 커밋 주문.

    `build_entry_plan` 의 `entry_band` 는 `EntryBand.as_dict()`(sadj 기준) 형식이므로
    `band_from_dict` 로 그대로 되살린다 — raw 이름(`stop_min`)을 기대하는 구 `order_from_plan`
    경로를 쓰지 않는다(두 형식을 섮으면 주문 범위가 틀어진다).
    """
    tranches = [TrancheOrder(index=int(row["tranche"]), weight=float(row["weight"]),
                             limit_price=None if int(row["tranche"]) == 1 else float(row["price_ref"]),
                             active=bool(row.get("active", True)), note=row.get("note"))
                for row in plan.get("tranches") or []]
    return BuyOrder(
        signal_id=signal_id, ticker=ticker, committed_at=committed_at,
        atr_pct=plan.get("atr_pct"), plan_weight=float(plan.get("plan_weight", 0.0)),
        tranches=tranches, sector=sector, band=band_from_dict(plan.get("entry_band")),
        stop_distance=plan.get("stop_distance"), iv_high=None,
    )


def _v06_params() -> tuple[float, float]:
    """V06 임계값 — 규칙 본문(`config/veto_rules.yaml`) 하나만 읽는다."""
    for rule in load_veto_rules()["rules"]:
        if rule["code"] == "V06":
            params = rule.get("params") or {}
            return float(params.get("vix_max", 35.0)), float(params.get("hy_oas_max_bp", 700.0))
    return 35.0, 700.0


def _v06_vetoes(vix: float | None, hy_oas_bp: float | None) -> list[dict]:
    """V06(시장 급변)만 판정한다 — 다른 Veto 는 그 시점 입력이 없어 켜지 않는다.

    V12(근거 부족)가 같이 발동하지 않도록 **최소 근거**를 넘긴다. 실제 차단 사유는 V06 뿐이다.
    """
    evidence = [{"type": "fundamental"}, {"type": "sec_filing"}]
    result = veto_check(VetoInputs(vix=vix, hy_oas_bp=hy_oas_bp, evidence=evidence))
    return [v for v in result.vetoes if v["code"] == "V06"]


def _series_value(series: pd.Series | None, day: date) -> float | None:
    if series is None or len(series) == 0:
        return None
    stamp = pd.Timestamp(day)
    idx = series.index[series.index <= stamp]
    if len(idx) == 0:
        return None
    value = series.loc[idx[-1]]
    return float(value) if pd.notna(value) else None


def simulate_window(window: Mapping, *, tables: Mapping[str, pd.DataFrame],
                    params: Mapping, benchmark_table: pd.DataFrame | None = None,
                    vix_series: pd.Series | None = None,
                    hy_series: pd.Series | None = None,
                    provider: Callable[[str, date], dict] | None = None,
                    reference: Mapping = REFERENCE_FUNDAMENTALS,
                    members_by_day: Mapping[date, set[str]] | None = None,
                    sectors: Mapping[str, str] | None = None,
                    regimes: Mapping[date, str] | None = None,
                    initial_cash: float = 1_000_000.0, slippage_bps: float = 10.0,
                    max_tickers: int | None = None, benchmark: str = "SPY") -> dict:
    """한 스트레스 구간을 일별로 돌린다 — 신호 → 분할 계획 → 장 전 주문 → 모의 계좌.

    매 거래일 아침, **전일 종가까지의 지표**로 `evaluate_entry`→`build_entry_plan` 을 돌려
    주문을 커밋하고, 같은 날 개장 경매에서 체결한다. 주문 가능 여부가 보유·주문 상태에 따라
    달라지므로 시뮬레이터를 하루씩 진행하며 주문을 만들고, 그렇게 모은 일별 봉·주문을
    **`run_scenario()` 로 다시 한 번** 돌려 지표를 낸다(기획서 4.12의 같은 경로).
    두 실행의 자산곡선이 다르면 `determinism_check=False` 로 드러낸다.
    """
    days = trading_days(window["start"], window["end"])
    # 벤치마크는 **비교 기준**이다 — 매수 대상 유니버스에 넣지 않는다.
    base_members = set(tables) - {str(benchmark).upper()}
    if max_tickers:
        base_members = set(sorted(base_members)[:int(max_tickers)])
    sim = PortfolioSimulator(initial_cash=initial_cash, slippage_bps=slippage_bps)
    vix_max, hy_max = _v06_params()
    v06_days: list[str] = []
    signal_days = 0
    n_orders = 0
    prev_day: date | None = None
    prev_rows: dict[str, pd.Series] = {}
    scenario_days: list[dict] = []
    scenario_orders: list[dict] = []
    for day in days:
        members = (set((members_by_day or {}).get(day, base_members)) & set(tables)) - {str(benchmark).upper()}
        if max_tickers:
            members = set(sorted(members)[:int(max_tickers)])
        regime = str((regimes or {}).get(day, "neutral"))
        vix = _series_value(vix_series, day)
        hy = _series_value(hy_series, day)
        if (vix is not None and vix > vix_max) or (hy is not None and hy > hy_max):
            v06_days.append(day.isoformat())
        new_orders = []
        if prev_day is not None:
            vetoes = _v06_vetoes(_series_value(vix_series, prev_day), _series_value(hy_series, prev_day))
            committed_at = datetime.combine(prev_day, datetime.min.time(),
                                            tzinfo=timezone.utc) + timedelta(hours=22)
            open_tickers = {o.ticker for o in sim.orders if o.status in ("pending", "active")}
            for ticker in sorted(members):
                if ticker in sim.positions or ticker in open_tickers:
                    continue
                row = prev_rows.get(ticker)
                if row is None:
                    continue
                ctx = _reference_context(ticker, pd.Timestamp(prev_day), row,
                                         sector=(sectors or {}).get(ticker, ""),
                                         provider=provider, reference=reference)
                if ctx is None:
                    continue
                ctx.vetoes = vetoes
                evaluation = evaluate_entry(ctx, regime=regime, params=params)
                if evaluation.get("signal") != "BUY":
                    continue
                plan = build_entry_plan(ctx, evaluation, regime=regime, params=params)
                if not plan.get("placeable"):
                    continue
                new_orders.append(_order_from_plan(f"{prev_day.isoformat()}-{ticker}-BUY-001",
                                                   ticker, committed_at, plan, sector=ctx.sector))
            if new_orders:
                signal_days += 1
                n_orders += len(new_orders)
                scenario_orders.extend({"day": day, "order": copy.deepcopy(order)} for order in new_orders)
        bars = _bars_on(tables, day, members, sectors)
        scenario_days.append({
            "date": day,
            "bars": {ticker: {"raw_open": bar.raw_open, "raw_high": bar.raw_high,
                              "raw_low": bar.raw_low, "raw_close": bar.raw_close,
                              "sadj_close": bar.sadj_close, "dma200": bar.dma200,
                              "atr_pct": bar.atr_pct, "rsi14": bar.rsi14, "sector": bar.sector}
                     for ticker, bar in bars.items()},
            "regime": regime,
        })
        sim.process_day(day, bars, new_orders=new_orders, regime=regime)
        prev_day = day
        prev_rows = {ticker: table.loc[pd.Timestamp(day)] for ticker, table in tables.items()
                     if pd.Timestamp(day) in table.index}
    incremental = sim.result()
    replay_run = run_scenario({"days": scenario_days, "orders": scenario_orders},
                              slippage_bps=slippage_bps, initial_cash=initial_cash)
    result = replay_run.result()
    determinism = ([round(float(row["equity"]), 4) for row in incremental.equity_curve]
                   == [round(float(row["equity"]), 4) for row in result.equity_curve])
    curve = result.equity_curve
    trades = result.trades
    fills = sum(1 for event in result.events if event.get("event") == "buy_filled")
    last_day = days[-1] if days else window["end"]
    marked = [float(t["pnl_pct"]) for t in trades if t.get("pnl_pct") is not None]
    for position in result.final_state.get("positions") or []:
        row = _row_on(tables.get(position.get("ticker")), last_day)
        if row is not None and position.get("avg_cost"):
            marked.append(float(row["raw_close"]) / float(position["avg_cost"]) - 1.0)
    bench_values = []
    if benchmark_table is not None and not benchmark_table.empty:
        rows = benchmark_table.loc[(benchmark_table.index >= pd.Timestamp(window["start"]))
                                   & (benchmark_table.index <= pd.Timestamp(window["end"]))]
        bench_values = list(rows["sadj_close"].astype(float))
    return {
        "strategy_mdd": _mdd([row["equity"] for row in curve]),
        "benchmark_mdd": _mdd(bench_values),
        "v06_fired": bool(v06_days),
        "new_buys_during_crash": int(fills),
        "loss_ratio": (round(float(np.mean([1.0 if value < 0 else 0.0 for value in marked])), 4)
                       if marked else None),
        "n_signal_days": int(signal_days),
        "n_orders": int(n_orders),
        "n_fills": int(fills),
        "n_closed_trades": int(len(trades)),
        "strategy_total_return": (round(float(curve[-1]["equity"] / curve[0]["equity"] - 1.0), 6)
                                  if len(curve) > 1 and curve[0]["equity"] else None),
        "benchmark_total_return": (round(_ret(bench_values), 6) if bench_values else None),
        "max_exposure": (round(max(float(row["exposure"]) for row in curve), 6) if curve else None),
        "final_equity_ratio": (round(float(curve[-1]["equity"] / initial_cash), 6) if curve else None),
        "n_universe_at_start": int(len((members_by_day or {}).get(days[0], base_members)) if days else 0),
        "n_days": int(len(days)),
        "v06_days": v06_days[:20],
        "v06_day_count": len(v06_days),
        "determinism_check": bool(determinism),
    }


def run_stress_backtest(*, panels: Mapping[str, object], params: Mapping | str | Path | None = None,
                        benchmark: str = "SPY", vix: pd.Series | None = None,
                        hy_oas: pd.Series | None = None,
                        universe_history: pd.DataFrame | None = None,
                        delisted: pd.DataFrame | None = None,
                        delisted_complete: bool | None = None,
                        windows: Iterable[dict] = STRESS_WINDOWS,
                        provider: Callable[[str, date], dict] | None = None,
                        reference: Mapping | None = None,
                        regimes: Mapping[date, str] | None = None,
                        sectors: Mapping[str, str] | None = None,
                        initial_cash: float = 1_000_000.0, slippage_bps: float = 10.0,
                        max_tickers: int | None = None, data_source: str = "injected",
                        extra_note_ko: str | None = None) -> dict:
    """3구간 스트레스 백테스트 → `stress.json` 페이로드.

    `universe_history`·`delisted_securities` 가 **검증**되면 `stress_report(uh, delisted, results=...)`
    로 정상 판정하고, 아니면 안전핀(`stress_report(None, None)`)을 유지한 채 실측 수치를
    `reference_metrics` 로만 붙인다.
    """
    if params is None or isinstance(params, (str, Path)):
        params = load_params(params or "config/params_v001.yaml")
    ref = dict(REFERENCE_FUNDAMENTALS)
    if reference:
        ref.update(reference)
    items = list(windows)
    tables = {str(t).upper(): indicator_table(p) for t, p in (panels or {}).items() if p is not None}
    bench = tables.get(str(benchmark).upper())
    verdict = verify_universe_inputs(universe_history, delisted, items, delisted_complete)
    members_by_day: dict[date, set[str]] | None = None
    universe_source = "panels"
    if verdict["universe_verified"] and universe_history is not None:
        universe_source = "universe_history"
        members_by_day = {}
        for window in items:
            for day in trading_days(window["start"], window["end"]):
                members_by_day[day] = membership_at(
                    universe_history,
                    datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc))
    measured = {window["key"]: simulate_window(
        window, tables=tables, params=params, benchmark_table=bench, vix_series=vix,
        hy_series=hy_oas, provider=provider, reference=ref, members_by_day=members_by_day,
        sectors=sectors, regimes=regimes, initial_cash=initial_cash,
        slippage_bps=slippage_bps, max_tickers=max_tickers,
        benchmark=benchmark) for window in items}
    results = {}
    for window in items:
        row = measured[window["key"]]
        results[window["key"]] = {key: row[key] for key in
                                  ("strategy_mdd", "benchmark_mdd", "v06_fired",
                                   "new_buys_during_crash", "loss_ratio")
                                  if row.get(key) is not None}
    if verdict["universe_verified"]:
        payload = stress_report(universe_history, delisted, results=results)
    else:
        # [고정] 안전핀 — 입력이 불완전하면 게이트는 미검증이다. 실측은 참고로만 붙인다.
        payload = stress_report(None, None)
    payload["reference_only"] = not verdict["universe_verified"]
    payload["reference_note_ko"] = (None if verdict["universe_verified"] else
                                    UNIVERSE_MISSING_NOTE + " " + REFERENCE_ENTRY_BASIS)
    payload["entry_basis"] = "xbrl_point_in_time" if provider is not None else "price_only_neutral_proxy"
    payload["regime_source"] = ("injected" if regimes else "neutral_default")
    payload["regime_note_ko"] = (None if regimes else
                                 "거시 레짐 미주입 — 전 구간 neutral 로 돌렸다(verified 아님). "
                                 "FRED 빈티지 레짐을 붙이기 전까지 이 수치는 참고용이다.")
    payload["entry_basis_ko"] = None if provider is not None else REFERENCE_ENTRY_BASIS
    payload["universe_source"] = universe_source
    payload["verdict"] = verdict
    payload["data_source"] = data_source
    payload["generated_at"] = _now_iso()
    payload["benchmark"] = str(benchmark).upper()
    payload["n_windows_measured"] = len(measured)
    for row in payload.get("results") or []:
        extra = measured.get(row.get("key")) or {}
        row["reference_only"] = not verdict["universe_verified"]
        row["reference_metrics"] = {key: extra.get(key) for key in METRIC_KEYS if extra.get(key) is not None}
        for key in ("n_signal_days", "n_orders", "n_fills"):
            row[key] = extra.get(key)
        if not row.get("metrics"):
            row["metrics"] = dict(row["reference_metrics"])
    if extra_note_ko:
        payload["data_note_ko"] = extra_note_ko
    return payload


# ────────────────────────────── (C) 보정 1회 실행 ──────────────────────────────
def next_params_version(params_dir: str | Path = "config") -> tuple[int, str]:
    """다음 파라미터 버전 번호·이름 — 기존 파일은 절대 덮어쓰지 않는다."""
    highest = 0
    for path in Path(params_dir).glob("params_v*.yaml"):
        digits = "".join(ch for ch in path.stem.replace("params_v", "") if ch.isdigit())
        if digits:
            highest = max(highest, int(digits))
    number = highest + 1
    return number, f"params_v{number:03d}"


def _patch_params_text(text: str, changes: Mapping[tuple[str, ...], float]) -> tuple[str, list[str]]:
    """YAML **주석을 살린 채** 값만 바꾼다 — 검토 가능한 diff 를 남기기 위해서다.

    `changes` 의 키는 섹션 경로(`("entry_weights", "valuation")`,
    `("entry_gates", "margin_of_safety", "neutral")`)다. 찾지 못한 키는 사유 목록으로 돌려준다.
    """
    out: list[str] = []
    found: list[str] = []
    pending = dict(changes)
    section: str | None = None
    subsection: str | None = None
    for line in text.splitlines():
        # 인라인 주석(`margin_of_safety:   # [보정]`)을 띄고 구조를 판단한다 — 주석 많은 YAML 이 기본이다.
        code = line.split("#", 1)[0].rstrip()
        stripped = code.strip()
        indent = len(code) - len(code.lstrip())
        if stripped and stripped.endswith(":") and indent == 0:
            section, subsection = stripped[:-1], None
        elif stripped and stripped.endswith(":") and indent == 2:
            subsection = stripped[:-1]
        replaced = False
        if stripped and ":" in stripped:
            key = stripped.split(":", 1)[0].strip()
            for pattern, value in list(pending.items()):
                if pattern[0] != section:
                    continue
                if len(pattern) == 2 and (pattern[1] != key or indent != 2):
                    continue
                if len(pattern) == 3 and (subsection != pattern[1] or key != pattern[2] or indent != 4):
                    continue
                comment = "  #" + line.split("#", 1)[1] if "#" in line else ""
                out.append(f"{' ' * indent}{key}: {float(value):g}{comment}")
                found.append(".".join(pattern))
                pending.pop(pattern)
                replaced = True
                break
        if not replaced:
            out.append(line)
    missing = [".".join(key) for key in pending]
    return "\n".join(out) + "\n", missing


def write_versioned_params(params_path: str | Path, engine_params: Mapping, flat: Mapping[str, float],
                           version: str, today: date, record: Mapping) -> Path:
    """수락된 보정을 **새 버전 파일**로 쓴다(기존 `params_v001.yaml` 은 건드리지 않는다)."""
    path = Path(params_path)
    base_text = path.read_text(encoding="utf-8")
    changes: dict[tuple[str, ...], float] = {}
    for key, value in (flat or {}).items():
        if key in WEIGHT_KEYS:
            changes[("entry_weights", key[len("w_"):])] = float(value)
        elif key == "min_margin":
            margins = ((engine_params.get("entry_gates") or {}).get("margin_of_safety") or {})
            for regime, margin in margins.items():
                changes[("entry_gates", "margin_of_safety", str(regime))] = float(margin)
        elif key == "rsi_buy":
            changes[("entry_gates", "rsi_buy")] = float(value)
        elif key == "dma50_dev":
            changes[("entry_gates", "dma50_deviation")] = float(value)
        elif key == "structural_risk_block":
            changes[("entry_gates", "structural_risk_block")] = float(value)
        elif key == "risk_off_multiplier":
            changes[("macro", "risk_off_multiplier")] = float(value)
    patched, missing = _patch_params_text(base_text, changes)
    patched = patched.replace("version: params_v001", f"version: {version}", 1)
    patched = patched.replace('created_at: "2026-11-19"', f'created_at: "{today.isoformat()}"', 1)
    header = (
        f"# {version} — {today.isoformat()} 워크포워드 보정 채택본 (자동 생성)\n"
        f"# 근거: reports/calibration.md · Agreement 평균 +{float(record.get('gain') or 0.0):.2f}점 · "
        f"개선 폴드 {record.get('improved_folds')}개 · 스트레스 게이트 통과\n"
        f"# 원본: {path.name} (덮어쓰지 않음)\n"
    )
    if missing:
        header += f"# 주의: 주석 보존 패치에서 찾지 못한 키 — 수동 확인 필요: {', '.join(missing)}\n"
    out_path = path.parent / f"{version}.yaml"
    out_path.write_text(header + patched, encoding="utf-8")
    return out_path


def _calibration_markdown(summary: Mapping) -> str:
    """`reports/calibration.md` — 그대로 PR 본문으로 쓰는 사람이 읽는 보고서."""
    record = summary.get("record") or {}
    stress = summary.get("stress") or {}
    lines = [f"# 워크포워드 보정 리포트 — {summary.get('as_of')}", "", "## 판정", ""]
    lines += [f"- 결과: **{'채택' if record.get('accepted') else '보류/기각'}** (`{record.get('reason')}`)",
              f"- 채점된 폴드: {record.get('n_folds')}개 (최소 {summary.get('min_folds')}개)",
              f"- 폴드당 최소 신호: {summary.get('min_signals')}건",
              f"- 홀드아웃: {(summary.get('holdout') or {}).get('note_ko') or '-'}",
              f"- 스트레스 게이트: `gate_passed={stress.get('gate_passed')}` "
              f"(verified_all={stress.get('verified_all')})", ""]
    if record.get("accepted"):
        lines += ["## 채택된 변경", "",
                  f"- Agreement 평균 +{float(record.get('gain') or 0):.2f}점 · "
                  f"개선 폴드 {record.get('improved_folds')}개 · 목적함수 {record.get('objective')}",
                  f"- 새 파라미터 버전: `{summary.get('params_version_new')}` "
                  f"(원본 `{summary.get('params_version_base')}` 는 그대로 둔다)", ""]
        changed = summary.get("changed") or {}
        if changed:
            lines += ["| 키 | 이전 | 이후 |", "| --- | --- | --- |"]
            lines += [f"| {key} | {value[0]} | {value[1]} |" for key, value in changed.items()]
            lines.append("")
    else:
        lines += ["## 보류·기각 사유", "", f"- `{record.get('reason')}`",
                  "- 후보를 채택하지 않았으므로 파라미터 파일을 만들지 않는다.", ""]
    candidates = (record.get("candidates") or [])[:8]
    if candidates:
        lines += ["## 후보 상위", "", "| 후보 | Agreement 이득 | 개선 폴드 | 목적함수 |",
                  "| --- | --- | --- | --- |"]
        for row in candidates:
            label = ", ".join(f"{k}={v}" for k, v in (row.get("params") or {}).items()
                              if k in FLAT_KEYS) or "(오류)"
            lines.append(f"| {label} | {row.get('agreement_gain')} | {row.get('improved_folds')} | "
                         f"{row.get('objective')} |")
        lines.append("")
    lines += ["## 입력", "",
              f"- 스냅샷 {summary.get('n_snapshots')}개 · 스냅샷이 있는 적격 폴드 "
              f"{summary.get('n_folds_planned')}개",
              f"- 채점 가격 패널: {'있음' if summary.get('grading_panel_available') else '없음'}",
              f"- 파라미터: `{summary.get('params_version_base')}`", "", "## 다음 단계", "",
              "1. 이 리포트를 PR 본문으로 올리고 사람이 검토한다(자동 적용 금지).",
              ("2. 스트레스 게이트가 `gate_passed=false` 인 동안에는 채택하지 않는다(기획서 4.3)."
               if not stress.get("gate_passed") else
               "2. 스트레스 게이트 통과 — 병합 후 다음 배치부터 새 버전이 적용된다."), ""]
    return "\n".join(lines)


def run_calibration_cycle(*, today: date | None = None,
                          snapshot_root: str | Path = "data/snapshots",
                          params_path: str | Path = "config/params_v001.yaml",
                          params_dir: str | Path = "config",
                          min_folds: int = 3, min_signals: int = 25,
                          horizons: tuple[int, ...] = (21, 63, 126),
                          holdout_root: str | Path = "data/walkforward/holdout",
                          attempts_root: str | Path = "data/calibration",
                          walkforward_root: str | Path = "data/walkforward",
                          reports_dir: str | Path = "reports",
                          stress_payload: Mapping | None = None,
                          grading_panels: Mapping[str, object] | None = None,
                          write: bool = True) -> dict:
    """보정 1회 실행 — 홀드아웃 확인 → 후보 탐색 → 기록 → (채택 시) 새 버전·PR 본문.

    스트레스 게이트가 `gate_passed=True` 가 아니면 `stress_pass` 콜러블이 False 를 돌려주므로
    `calibrate()` 가 `STRESS_FAILED` 로 후보를 기각한다 — 미검증 스트레스를 통과로 바꾸지 않는다.
    """
    today = today or date.today()
    stress = dict(stress_payload or _read_stress_payload(walkforward_root))
    base_params = load_params(params_path)
    base_version = str(base_params.get("version") or Path(str(params_path)).stem)
    holdout = HoldoutLedger(holdout_root).can_open(today)
    # 홀드아웃 시작일은 폴드 계획과 **같은 규칙**으로 계산해 관측 메타에 고정한다(LA-13).
    holdout_start = date.fromisoformat(fold_summary(today, min_folds=min_folds)["holdout_start"])
    diagnostics: dict = {}
    evaluator = build_snapshot_evaluator(
        snapshot_root, base_params=base_params, today=today, holdout_start=holdout_start,
        horizons=horizons, grading_panels=grading_panels, params_version=base_version,
        diagnostics=diagnostics)

    def stress_pass(_candidate: Mapping) -> bool:
        # 게이트가 미검증이면 **통과가 아니다** — 후보를 채택하지 않는다(감사 10).
        return bool(stress.get("gate_passed") is True)

    record = calibrate(evaluator, base_params, min_folds=min_folds, min_signals=min_signals,
                       stress_pass=stress_pass)
    record = {
        **record,
        "as_of": today.isoformat(),
        "kind": "calibrate",
        "params_version": base_version,
        "holdout": holdout,
        "stress": {"gate_passed": stress.get("gate_passed"),
                   "verified_all": stress.get("verified_all"),
                   "stress_pass": stress.get("stress_pass"),
                   "reference_only": stress.get("reference_only")},
        "n_snapshots": len(list_snapshots(snapshot_root)[0]),
        "n_folds_planned": diagnostics.get("n_folds_planned"),
        "folds": diagnostics.get("folds"),
        "holdout_start": holdout_start.isoformat(),
    }
    _, new_version = next_params_version(params_dir)
    summary: dict = {
        "as_of": today.isoformat(), "accepted": bool(record.get("accepted")),
        "record": record, "min_folds": min_folds, "min_signals": min_signals,
        "holdout": holdout, "stress": record["stress"],
        "params_version_base": base_version, "params_version_new": None,
        "n_snapshots": record["n_snapshots"], "n_folds_planned": record["n_folds_planned"],
        "grading_panel_available": diagnostics.get("grading_panel_available"),
        "changed": {}, "stress_ok": bool(stress.get("gate_passed") is True),
    }
    if record.get("accepted"):
        candidate = record.get("candidate") or {}
        flat = {key: value for key, value in (candidate.get("params") or {}).items() if key in FLAT_KEYS}
        engine_params = _engine_params_for(candidate.get("params") or record.get("params"), base_params)
        previous = _flat_from_params(base_params)
        summary["changed"] = {key: [round(float(previous.get(key, 0.0)), 4), round(float(value), 4)]
                              for key, value in flat.items()
                              if abs(float(value) - float(previous.get(key, value))) > 1e-12}
        summary["params_version_new"] = new_version
        if write:
            summary["params_path"] = str(write_versioned_params(
                params_path, engine_params, flat, new_version, today, record))
    if write:
        summary["attempts_path"] = write_attempts(attempts_root, record)
        summary["walkforward_attempts_path"] = _append_walkforward_attempts(walkforward_root, record)
        report_path = Path(reports_dir) / "calibration.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(_calibration_markdown(summary), encoding="utf-8")
        summary["report_path"] = str(report_path)
    return summary


def _flat_from_params(params: Mapping) -> dict[str, float]:
    """엔진 설정 → 평면 보정 키(비교·기록용)."""
    weights = (params or {}).get("entry_weights") or {}
    gates = (params or {}).get("entry_gates") or {}
    margins = gates.get("margin_of_safety") or {}
    out = {f"w_{key}": float(value) for key, value in weights.items()}
    for key, target in (("rsi_buy", "rsi_buy"), ("dma50_deviation", "dma50_dev"),
                        ("structural_risk_block", "structural_risk_block")):
        if gates.get(key) is not None:
            out[target] = float(gates[key])
    if isinstance(margins, dict) and margins.get("neutral") is not None:
        out["min_margin"] = float(margins["neutral"])
    risk_off = ((params or {}).get("macro") or {}).get("risk_off_multiplier")
    if risk_off is not None:
        out["risk_off_multiplier"] = float(risk_off)
    return out


def _read_stress_payload(walkforward_root: str | Path) -> dict:
    path = Path(walkforward_root) / "stress.json"
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and payload.get("windows"):
                return payload
        except (OSError, json.JSONDecodeError):
            pass
    return stress_report(None, None)


def _append_walkforward_attempts(walkforward_root: str | Path, record: Mapping) -> str:
    """`data/walkforward/attempts.json` 에 보정 시도를 **덧붙인다**(계획 항목은 보존)."""
    root = Path(walkforward_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "attempts.json"
    payload: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        payload = loaded if isinstance(loaded, dict) else {"attempts": loaded}
    history = list(payload.get("calibrations") or [])
    history.append({key: record.get(key) for key in
                    ("as_of", "kind", "accepted", "reason", "n_folds",
                     "params_version", "gain", "improved_folds", "stress")})
    payload["calibrations"] = history
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


# ────────────────────────────── 오프라인·라이브 입력 ──────────────────────────────
DEMO_TICKERS: dict[str, tuple[str, str]] = {
    "DEMA": ("Technology", "dip"), "DEMB": ("Health Care", "down"),
    "DEMC": ("Industrials", "dip"), "DEMD": ("Energy", "down"),
    "DEME": ("Consumer Discretionary", "dip"), "DEMF": ("Financials", "flat"),
}


#: 합성 스트레스 구간 — 스트레스 창과 같은 기간에 하락 충격을 넣는다(오프라인 검증용).
DEMO_EVENTS: tuple[tuple[str, str, float], ...] = (
    ("2018-10-01", "2018-12-24", -0.0042),
    ("2020-02-20", "2020-03-23", -0.0180),
    ("2020-03-24", "2020-04-30", 0.0100),
    ("2022-01-03", "2022-06-16", -0.0022),
    ("2022-08-16", "2022-10-14", -0.0028),
)


def _demo_hist(idx: pd.DatetimeIndex, drift: np.ndarray, noise: float,
               rng: np.random.Generator) -> pd.DataFrame:
    """일별 드리프트·노이즈 → 합성 OHLC(Yahoo 형태). 고정 시드라 실행마다 같다."""
    daily = drift + rng.normal(0, noise, len(idx))
    close = pd.Series(100.0 * np.exp(np.cumsum(daily)), index=idx)
    open_ = close.shift(1).fillna(close.iloc[0]) * np.exp(rng.normal(0, noise / 3.0, len(idx)))
    high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0, noise / 2.0, len(idx))))
    low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0, noise / 2.0, len(idx))))
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close,
                         "Adj Close": close * 0.99,
                         "Volume": rng.integers(2_000_000, 9_000_000, len(idx)).astype(float)}, index=idx)


def build_offline_stress_panels(start: str = "2016-01-04", end: str = "2024-06-28",
                                tickers: Mapping[str, tuple[str, str]] | None = None,
                                seed: int = 11) -> tuple[dict[str, object], pd.Series]:
    """오프라인 스트레스 실행용 **합성** 패널 — 실데이터가 아니다(`data_source: offline_demo`).

    `--stress --offline-demo` 가 네트워크 없이 전 경로(신호→주문→모의 계좌→지표)를 검증할 때만 쓴다.
    실제 성과로 쓰지 않는다. 스트레스 창과 같은 달에 하락 충격을 넣어 신호·손절 경로가 실제로 돌게 한다.
    """
    from engine.data.prices import build_price_frame  # noqa: PLC0415 — 무거운 임포트를 지연

    universe = dict(tickers or DEMO_TICKERS)
    idx = pd.bdate_range(start=start, end=end)
    rng = np.random.default_rng(seed)
    base_drift = np.full(len(idx), 0.00035)
    for begin, finish, drift in DEMO_EVENTS:
        mask = (idx >= pd.Timestamp(begin)) & (idx <= pd.Timestamp(finish))
        base_drift[mask] = drift
    panels: dict[str, object] = {}
    for scale, (ticker, (_sector, pattern)) in enumerate(sorted(universe.items())):
        factor = {"down": 1.25, "dip": 1.0, "flat": 0.7}.get(pattern, 1.0)
        hist = _demo_hist(idx, base_drift * factor, 0.010 + 0.002 * (scale % 3), rng)
        panels[ticker] = build_price_frame(ticker, hist, None)
    panels["SPY"] = build_price_frame("SPY", _demo_hist(idx, base_drift * 0.75, 0.008, rng), None)
    vix = pd.Series(19.0, index=idx)
    vix.loc[(idx >= pd.Timestamp("2018-10-08")) & (idx <= pd.Timestamp("2018-11-16"))] = 24.0
    vix.loc[(idx >= pd.Timestamp("2018-12-03")) & (idx <= pd.Timestamp("2018-12-24"))] = 30.0
    vix.loc[(idx >= pd.Timestamp("2020-02-24")) & (idx <= pd.Timestamp("2020-04-17"))] = 52.0
    vix.loc[(idx >= pd.Timestamp("2022-01-24")) & (idx <= pd.Timestamp("2022-02-08"))] = 33.0
    vix.loc[(idx >= pd.Timestamp("2022-09-26")) & (idx <= pd.Timestamp("2022-10-14"))] = 36.5
    return panels, vix


def collect_stress_inputs(*, as_of: date | None = None,
                          universe_path: str | Path = "config/universe.yaml",
                          benchmark: str = "SPY", history_years: int = 10,
                          max_tickers: int | None = None) -> dict:
    """라이브 수집 — 10년 가격(종목 + 벤치마크 + VIX). 실패는 사유와 함께 돌려준다."""
    from engine.data.collect import collect_prices, load_universe  # noqa: PLC0415
    from engine.data.prices import build_price_frame  # noqa: PLC0415
    import yfinance as yf  # noqa: PLC0415

    as_of = as_of or date.today()
    diag: dict = {"mode": "live", "sources": {}, "degraded": []}
    config = load_universe(universe_path)
    rows = list(config.get("tickers") or [])
    tickers = [row["ticker"] for row in rows]
    sectors = {row["ticker"]: row.get("sector") or "" for row in rows}
    if max_tickers:
        tickers = tickers[:int(max_tickers)]
    panels = collect_prices(tickers + [benchmark], as_of, datetime.now(timezone.utc), diag,
                            history_years=history_years)
    vix = None
    try:
        hist = yf.Ticker("^VIX").history(period=f"{history_years}y", auto_adjust=False, actions=False)
        if hist is not None and len(hist):
            frame = build_price_frame("^VIX", hist, None).frame["sadj_close"].dropna()
            frame.index = pd.DatetimeIndex(frame.index).normalize()
            vix = frame
    except Exception as exc:  # noqa: BLE001 — VIX 실패는 스트레스 실패가 아니다(V06 미판정)
        diag["degraded"].append(f"vix:{type(exc).__name__}")
    return {"panels": panels, "vix": vix, "sectors": sectors, "benchmark": benchmark,
            "diagnostics": diag, "tickers_requested": len(tickers)}


def write_stress_payload(payload: Mapping, root: str | Path = "data/walkforward") -> Path:
    """`data/walkforward/stress.json` 저장 — `stress.write_stress_report` 그대로."""
    return write_stress_report(dict(payload), root)


def stress_summary_for_cli(payload: Mapping) -> dict:
    """CLI 출력용 요약(원시 시계열 없음)."""
    return {
        "data_source": payload.get("data_source"),
        "reference_only": payload.get("reference_only"),
        "universe_verified": payload.get("universe_verified"),
        "gate_passed": payload.get("gate_passed"),
        "stress_pass": payload.get("stress_pass"),
        "entry_basis": payload.get("entry_basis"),
        "verdict": (payload.get("verdict") or {}).get("reasons"),
        "windows": [{"key": row.get("key"), "verified": row.get("verified"), "pass": row.get("pass"),
                     "reference_metrics": row.get("reference_metrics")}
                    for row in payload.get("results") or []],
    }


def fold_plan_count(folds: Mapping[int, list[Fold]]) -> int:
    """적격 폴드 수(계획 기준) — 반기 확장의 `folds_available` 입력."""
    return sum(1 for horizon in folds for fold in folds[horizon] if fold.eligible)


def extended_summary(today: date, folds_available: int, universe_verified: bool) -> dict:
    """반기 확장 계획 요약 — `extended_plan` 그대로(폴드 수·검증 여부)."""
    return extended_plan(today, folds_available=folds_available, universe_verified=universe_verified)


__all__ = [
    "FoldPlan", "build_offline_stress_panels", "build_snapshot_evaluator",
    "collect_stress_inputs", "extended_summary", "fold_plan_count", "grade_signal",
    "indicator_table", "list_snapshots", "next_params_version", "plan_fold_snapshots",
    "run_calibration_cycle", "run_stress_backtest", "simulate_window", "stress_summary_for_cli",
    "verify_universe_inputs", "write_stress_payload", "write_versioned_params",
]
