"""engine.decide — 2층 의사결정(신호·리스크 가드·거시 레짐·포지션 사이징).

기획서 §3 / §7 Phase 2. 계산은 전부 결정론이며, 같은 스냅샷 + 같은 파라미터면 같은 결과다.
가격 기준은 모듈마다 고정이다(4.5 LA-12): 판단(B2·B3)은 `split_adj_ohlc`,
주문·체결·손절은 `raw_as_traded` + `atr_pct` 비율.
"""

from __future__ import annotations

from engine.decide.macro import RegimeView, classify_regime, regime_adjustments
from engine.decide.signals import EntryContext, evaluate_entry, evaluate_exit, evaluate_holdings, timing_score
from engine.decide.sizing import SizeResult, recheck_planned_loss, size_position, stop_distance_for
from engine.decide.veto import VetoInputs, VetoResult, load_veto_rules, veto_check

__all__ = [
    "RegimeView",
    "classify_regime",
    "regime_adjustments",
    "EntryContext",
    "evaluate_entry",
    "evaluate_exit",
    "evaluate_holdings",
    "timing_score",
    "SizeResult",
    "size_position",
    "stop_distance_for",
    "recheck_planned_loss",
    "VetoInputs",
    "VetoResult",
    "load_veto_rules",
    "veto_check",
]
