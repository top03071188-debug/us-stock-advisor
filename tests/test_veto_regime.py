"""test_veto_regime — 리스크 가드 12종과 거시 레짐 분류 (기획서 3.6·3.7)."""

from __future__ import annotations

import pytest

from engine.decide.macro import ADJUSTMENTS, classify_regime, regime_adjustments
from engine.decide.veto import CODE_ORDER, VetoInputs, load_veto_rules, veto_check, veto_summary

GOOD_EVIDENCE = [{"type": "fundamental", "metric": "FCF_margin_5y", "value": 0.22},
                 {"type": "sec_filing", "form": "10-Q", "url": "https://www.sec.gov/x"}]


def clean(**kw) -> VetoInputs:
    base = dict(days_to_earnings=12, close_lag_business_days=0, filing_items=[], going_concern=False,
                adv20_usd=5e9, vix=15.0, hy_oas_bp=300.0, projected_sector_weight=0.10,
                projected_position_weight=0.02, thesis_break=False, structural_risk=10.0,
                one_day_return=0.01, split_announced=False, news_injection=False,
                evidence=GOOD_EVIDENCE)
    base.update(kw)
    return VetoInputs(**base)


def test_rules_file_defines_all_twelve():
    rules = load_veto_rules()
    codes = [r["code"] for r in rules["rules"]]
    assert codes == CODE_ORDER
    assert {r["group"] for r in rules["rules"]} >= {"hard_gate", "disclosure", "liquidity", "macro",
                                                    "limit", "thesis", "data", "evidence"}


def test_clean_input_has_no_veto():
    out = veto_check(clean())
    assert out.vetoes == [] and out.blocked is False


@pytest.mark.parametrize("kwargs, code", [
    ({"days_to_earnings": -1}, "V01"),
    ({"days_to_earnings": 2}, "V01"),
    ({"close_lag_business_days": 3}, "V02"),
    ({"filing_items": ["4.02"]}, "V03"),
    ({"filing_items": ["5.02"], "filing_items_verified": ["5.02"]}, "V03"),
    ({"going_concern": True}, "V04"),
    ({"adv20_usd": 1e7}, "V05"),
    ({"vix": 40.0}, "V06"),
    ({"hy_oas_bp": 750.0}, "V06"),
    ({"projected_sector_weight": 0.30}, "V07"),
    ({"projected_position_weight": 0.06}, "V08"),
    ({"thesis_break": True}, "V09"),
    ({"structural_risk": 75.0}, "V09"),
    ({"one_day_return": 0.55}, "V10"),
    ({"news_injection": True}, "V11"),
    ({"evidence": []}, "V12"),
    ({"evidence": [{"type": "news"}]}, "V12"),
])
def test_each_veto_triggers(kwargs, code):
    out = veto_check(clean(**kwargs))
    assert code in out.codes, out.codes
    assert code in out.blocking


def test_unverified_502_does_not_trigger_v03():
    out = veto_check(clean(filing_items=["5.02"], filing_items_verified=[]))
    assert "V03" not in out.codes


def test_split_announced_suppresses_v10():
    out = veto_check(clean(one_day_return=0.55, split_announced=True))
    assert "V10" not in out.codes


def test_failed_evidence_verification_triggers_v12():
    out = veto_check(clean(evidence=[{"type": "fundamental", "quote_verified": False}]))
    assert "V12" in out.codes


def test_multiple_vetoes_sorted_and_summarized():
    out = veto_check(clean(days_to_earnings=1, vix=50.0, projected_position_weight=0.09, evidence=[]))
    assert out.codes == ["V01", "V06", "V08", "V12"]
    summary = veto_summary(out)
    assert summary["blocked"] is True
    assert summary["by_group"] == {"disclosure": 1, "macro": 1, "limit": 1, "evidence": 1}
    assert len(summary["messages_ko"]) == 4


def test_veto_messages_are_korean():
    out = veto_check(clean(adv20_usd=1.0))
    assert "유동성" in out.vetoes[0]["message_ko"]


# ── 거시 레짐 ────────────────────────────────────────────────────────────────
def world(**kw) -> dict:
    base = {"dff": 4.0, "cpi_yoy": 3.0, "t10y2y": 0.6, "hy_oas_bp": 300, "vix": 15.0, "dtwexbg": 118.0}
    base.update(kw)
    return base


PREV = {"dff": 4.5, "cpi_yoy": 3.0, "dtwexbg": 121.0}      # 실질금리 하락 · 달러 약세


def test_risk_on_regime():
    view = classify_regime(world(), PREV)
    assert view.regime == "risk_on"
    assert view.votes_risk_on >= 3 and view.verified is True
    assert view.multiplier == 1.0 and view.margin_of_safety == 0.15
    assert view.max_new_positions is None


def test_risk_off_regime():
    vals = world(t10y2y=-0.5, hy_oas_bp=650, vix=30.0, dtwexbg=130.0)
    prev = {"dff": 3.0, "cpi_yoy": 3.0, "dtwexbg": 121.0}     # 실질금리 상승
    view = classify_regime(vals, prev)
    assert view.regime == "risk_off"
    assert view.multiplier == 0.4 and view.margin_of_safety == 0.25
    assert view.max_new_positions == 10


def test_mixed_signals_fall_back_to_neutral():
    vals = world(t10y2y=0.1, hy_oas_bp=420, vix=20.0, dtwexbg=122.0)
    prev = {"dff": 4.0, "cpi_yoy": 3.0, "dtwexbg": 121.0}
    view = classify_regime(vals, prev)
    assert view.regime == "neutral"
    assert view.multiplier == 0.7 and view.margin_of_safety == 0.20
    assert view.max_new_positions == 20


def test_missing_inputs_mark_unverified():
    view = classify_regime({"vix": 15.0})                       # 1개만 투표 가능
    assert view.verified is False
    assert view.regime == "neutral"
    assert {"real_rate", "t10y2y", "credit", "dollar"} <= set(view.inputs_missing)


def test_extreme_flag_separate_from_regime():
    view = classify_regime(world(vix=40.0, hy_oas_bp=720), PREV)
    assert view.extreme is True
    assert view.regime in ("risk_on", "neutral", "risk_off")


def test_regime_adjustments_and_unknown():
    assert regime_adjustments("neutral")["multiplier"] == 0.7
    assert set(ADJUSTMENTS) == {"risk_on", "neutral", "risk_off"}
    with pytest.raises(ValueError):
        regime_adjustments("bull")
