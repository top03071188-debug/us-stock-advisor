"""engine.backtest.report — 폴드별 전략 성과 리포트 (기획서 4.6·4.12).

전략 지표는 **신호 목록이 아니라 현금·보유를 추적한 모의 계좌의 자산 곡선**에서 낸다(4.6).
같은 폴드에서 슬리피지 0 / 5 / 10 / 20bp 를 각각 돌려 비용 민감도를 함께 남긴다.

**신호 0건 폴드도 정상 완료**다 — `status: completed`, 지표 None. 실패로 처리하지 않는다(3.1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

TRADING_DAYS = 252
COST_SENSITIVITY_BPS = (0, 5, 10, 20)


def _safe(x) -> float | None:
    if x is None:
        return None
    if isinstance(x, float) and (np.isnan(x) or np.isinf(x)):
        return None
    return round(float(x), 6)


def max_drawdown(equity: list[float]) -> float | None:
    if len(equity) < 2:
        return None
    peak = float(equity[0])
    worst = 0.0
    for v in equity:
        peak = max(peak, float(v))
        if peak > 0:
            worst = min(worst, float(v) / peak - 1.0)
    return worst


def strategy_metrics(equity_curve: list[dict], trades: list[dict] | None = None,
                     initial_cash: float | None = None,
                     benchmark_curve: list[dict] | None = None) -> dict:
    """자산 곡선 → CAGR·MDD·Sharpe·Sortino·Calmar·회전율·평균 노출도·승률·손익비 (4.6)."""
    equity = [float(row["equity"]) for row in equity_curve if row.get("equity") is not None]
    if len(equity) < 2:
        return {
            "days": len(equity), "total_return": None, "cagr": None, "mdd": None,
            "sharpe": None, "sortino": None, "calmar": None, "turnover": None,
            "avg_exposure": None, "win_rate": None, "profit_factor": None,
            "n_trades": len(trades or []), "benchmark": None, "excess_cagr": None,
        }
    start = float(initial_cash) if initial_cash else equity[0]
    end = equity[-1]
    n = len(equity)
    total_return = end / start - 1.0
    years = n / TRADING_DAYS
    cagr = (end / start) ** (1.0 / years) - 1.0 if years > 0 and start > 0 and end > 0 else None
    rets = np.diff(equity) / np.array(equity[:-1], dtype=float)
    sharpe = None
    sortino = None
    if len(rets) > 2 and rets.std(ddof=1) > 0:
        sharpe = float(rets.mean() / rets.std(ddof=1) * np.sqrt(TRADING_DAYS))
        downside = rets[rets < 0]
        if len(downside) > 1 and downside.std(ddof=1) > 0:
            sortino = float(rets.mean() / downside.std(ddof=1) * np.sqrt(TRADING_DAYS))
    mdd = max_drawdown(equity)
    calmar = None if (mdd is None or mdd == 0 or cagr is None) else cagr / abs(mdd)
    exposure = [float(row.get("exposure") or 0.0) for row in equity_curve]
    traded = sum(abs(float(t.get("shares", 0.0)) * float(t.get("exit_price", 0.0))) for t in (trades or []))
    avg_equity = float(np.mean(equity))
    turnover = (traded / avg_equity / years) if avg_equity > 0 and years > 0 else None
    pnls = [float(t["pnl_pct"]) for t in (trades or []) if t.get("pnl_pct") is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    out = {
        "days": n,
        "start_equity": _safe(start),
        "end_equity": _safe(end),
        "total_return": _safe(total_return),
        "cagr": _safe(cagr),
        "mdd": _safe(mdd),
        "sharpe": _safe(sharpe),
        "sortino": _safe(sortino),
        "calmar": _safe(calmar),
        "turnover": _safe(turnover),
        "avg_exposure": _safe(float(np.mean(exposure)) if exposure else 0.0),
        "win_rate": _safe(len(wins) / len(pnls)) if pnls else None,
        "profit_factor": _safe(sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else None,
        "n_trades": len(pnls),
        "benchmark": None,
        "excess_cagr": None,
    }
    if benchmark_curve:
        b_eq = [float(r["equity"]) for r in benchmark_curve if r.get("equity") is not None]
        if len(b_eq) >= 2:
            b_total = b_eq[-1] / b_eq[0] - 1.0
            b_years = len(b_eq) / TRADING_DAYS
            b_cagr = (b_eq[-1] / b_eq[0]) ** (1.0 / b_years) - 1.0 if b_years > 0 and b_eq[0] > 0 else None
            out["benchmark"] = {"total_return": _safe(b_total), "cagr": _safe(b_cagr),
                                "mdd": _safe(max_drawdown(b_eq))}
            if cagr is not None and b_cagr is not None:
                out["excess_cagr"] = _safe(cagr - b_cagr)
    return out


def cost_sensitivity(run, bps_values: tuple[int, ...] = COST_SENSITIVITY_BPS, **kwargs) -> dict:
    """같은 폴드를 슬리피지 0/5/10/20bp 로 각각 실행한다(4.6).

    `run(slippage_bps=..., **kwargs)` 는 `PortfolioResult` 를 돌려주는 함수다.
    """
    out: dict[str, dict] = {}
    for bps in bps_values:
        result = run(slippage_bps=float(bps), **kwargs)
        out[f"{bps}bp"] = strategy_metrics(result.equity_curve, result.trades,
                                           initial_cash=getattr(result, "initial_cash", None) or
                                           result.params.get("initial_cash"))
    return out


@dataclass
class FoldReport:
    fold_id: str
    horizon: str
    status: str
    n_signals: int
    strategy: dict
    signals: list[dict]
    portfolio: dict | None
    cost_sensitivity: dict
    notes: list[str]

    def as_dict(self) -> dict:
        return {
            "fold_id": self.fold_id,
            "horizon": self.horizon,
            "status": self.status,
            "n_signals": self.n_signals,
            "strategy": self.strategy,
            "signals": self.signals,
            "portfolio": self.portfolio,
            "cost_sensitivity": self.cost_sensitivity,
            "notes": self.notes,
        }


def fold_report(fold_id: str, horizon: str, signals: list[dict], portfolio=None,
                benchmark_curve: list[dict] | None = None,
                sensitivity: dict | None = None, notes: list[str] | None = None) -> dict:
    """폴드 리포트 1건. 신호 0건이어도 `completed` 로 기록한다(실패 아님)."""
    notes = list(notes or [])
    if portfolio is not None and not hasattr(portfolio, "equity_curve"):
        portfolio = portfolio.result()
    if not signals:
        notes.append("신호 0건 — 정상 완료(3.1). 빈 결과를 실패로 처리하지 않는다.")
    strategy = strategy_metrics(portfolio.equity_curve if portfolio else [], portfolio.trades if portfolio else [],
                                initial_cash=portfolio.params.get("initial_cash") if portfolio else None,
                                benchmark_curve=benchmark_curve)
    report = FoldReport(
        fold_id=fold_id,
        horizon=horizon,
        status="completed",
        n_signals=len(signals),
        strategy=strategy,
        signals=signals,
        portfolio=None if portfolio is None else {
            "final_state": portfolio.final_state,
            "equity_curve": portfolio.equity_curve,
            "digest": portfolio.digest(),
        },
        cost_sensitivity=sensitivity or {},
        notes=notes,
    )
    return report.as_dict()


def write_fold_report(root: str | Path, fold_id: str, report: dict) -> Path:
    """`{root}/folds/{fold_id}/report.json` — 공개 커밋에는 넣지 않는다(보유 필드 포함)."""
    p = Path(root) / "folds" / str(fold_id) / "report.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return p


def write_fold_portfolio(root: str | Path, fold_id: str, portfolio) -> Path:
    """`{root}/folds/{fold_id}/portfolio.json` — 4.12 의 일별 자산 곡선(로컬 산출물)."""
    p = Path(root) / "folds" / str(fold_id) / "portfolio.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(portfolio.to_json(), encoding="utf-8")
    return p
