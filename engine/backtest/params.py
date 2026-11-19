"""engine.backtest.params — 시점별 활성 파라미터 선택 (LA-8).

`data/params_history.json` 은 `[{version, adopted_at, values, ...}]` 형태이고, 폴드는
**그 시점에 active 였던 버전**만 쓴다. 이후 보정값을 과거에 적용하면 과적합이 된다.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd


def active_params(history: list[dict], decision_at: datetime) -> dict:
    if not history:
        raise ValueError("params_history 가 비었다")
    rows = sorted(history, key=lambda h: pd.Timestamp(h["adopted_at"]))
    chosen = None
    for h in rows:
        ts = pd.Timestamp(h["adopted_at"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        if ts <= decision_at:
            chosen = h
    if chosen is None:
        raise ValueError(f"{decision_at} 시점에 active 였던 파라미터가 없다(미래 값만 있음)")
    return chosen


def version_at(history: list[dict], decision_at: datetime) -> str:
    return active_params(history, decision_at)["version"]
