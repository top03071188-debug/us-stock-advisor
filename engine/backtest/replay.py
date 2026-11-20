"""engine.backtest.replay — 스냅샷에서 시점 T 의 1·2층 계산을 다시 실행한다.

이 모듈은 **결정론**만 책임진다: 같은 스냅샷 + 같은 파라미터면 같은 신호 JSON(타임스탬프 제외).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pandas as pd

from engine.backtest.execution import OrderNotPlaceable, build_entry_band
from engine.backtest.snapshot import Snapshot
from engine.metrics.technical import stop_distance, snapshot_indicators
from engine.models import PriceKind
from engine.screener import screen


@dataclass
class ReplayResult:
    as_of: str
    snapshot_id: str
    watchlist: list[dict]
    signals: list[dict]
    blocked: list[dict]
    digest: str

    def to_json(self) -> str:
        base = {
            "as_of": self.as_of,
            "snapshot_id": self.snapshot_id,
            "watchlist": self.watchlist,
            "signals": self.signals,
            "blocked": self.blocked,
        }
        return json.dumps(base, ensure_ascii=False, sort_keys=True, indent=2)


def _digest(payload: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def replay(snapshot: Snapshot, candidates: list, params: dict, panels: dict | None = None) -> ReplayResult:
    """스냅샷 시점의 1층 스크리닝 + 2층 신호(가격대) 계산.

    `panels[ticker]` 가 있으면 지표를 계산해 `atr_pct`·`dma200` 을 신호에 넣는다.
    없으면 후보의 `margin` 만으로 WATCH 신호를 만든다(최소 루프).
    """
    screened = screen(
        candidates,
        sectors_max=params.get("universe", {}).get("sectors_max_count", 6),
        watchlist_max=params.get("universe", {}).get("watchlist_max", 40),
    )
    entry_cfg = params.get("entry_gates", {})
    risk_cfg = params.get("risk", {})
    candidate_by_ticker = {c.ticker: c for c in candidates}
    signals: list[dict] = []
    blocked: list[dict] = []

    for row in screened["watchlist"]:
        ticker = row["ticker"]
        panel = (panels or {}).get(ticker)
        if panel is None:
            signals.append({**row, "type": "WATCH", "grade": "C", "reason": "지표 없음(최소 루프)"})
            continue
        ind = snapshot_indicators(panel, pd.Timestamp(snapshot.as_of).date())
        if not ind:
            continue
        sadj = panel.series(PriceKind.split_adj_ohlc)
        dist_series = stop_distance(sadj, risk_cfg.get("stop_atr_mult", 3.0), risk_cfg.get("stop_max", 0.20))
        dist = float(dist_series.iloc[-1]) if pd.notna(dist_series.iloc[-1]) else 0.20
        required = entry_cfg.get("margin_of_safety", {}).get("neutral", 0.20)
        candidate_iv = getattr(candidate_by_ticker.get(ticker), "iv_median", None)
        band = None
        if candidate_iv:
            try:
                band = build_entry_band(
                    iv_median=float(candidate_iv),
                    required_margin=required,
                    dma200_sadj=ind["dma200"] or ind["sadj_close"],
                    signal_price=ind["raw_close"],
                    stop_distance=dist,
                    dma200_mult=entry_cfg.get("dma200_mult", 0.97),
                ).as_dict()
            except (OrderNotPlaceable, TypeError, ValueError):
                band = None
        signal = {
            "ticker": ticker,
            "type": "WATCH" if band is None else "BUY",
            "grade": "B" if band is not None else "C",
            "entry_score": row["rank_score"],
            "components": {"quality": row["q"], "valuation": row["v"], "growth": row["g"]},
            "sadj_close": ind["sadj_close"],
            "price": ind["raw_close"],
            "atr_pct": ind["atr_pct"],
            "stop_distance": round(dist, 4),
            "dma200_sadj": ind["dma200"],
            "rsi14": ind["rsi14"],
            "entry_band": band,
            "snapshot_id": snapshot.snapshot_id,
            "as_of": snapshot.as_of,
        }
        if band is None:
            blocked.append({"ticker": ticker, "reason": "order_not_placeable"})
        signals.append(signal)

    payload = json.dumps(
        {"as_of": snapshot.as_of, "signals": signals, "blocked": blocked, "watchlist": screened["watchlist"]},
        ensure_ascii=False, sort_keys=True,
    )
    return ReplayResult(
        as_of=snapshot.as_of,
        snapshot_id=snapshot.snapshot_id,
        watchlist=screened["watchlist"],
        signals=signals,
        blocked=blocked,
        digest=_digest(payload),
    )
