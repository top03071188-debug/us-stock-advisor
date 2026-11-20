"""engine.decide.macro — 거시 레짐 분류와 레짐별 노출·사이징 조정 (기획서 3.7).

[고정]
- 5개 신호(실질금리 추세·장단기차·신용 스프레드·변동성·달러 3개월 변화) 중 **3개 이상**이
  같은 방향이면 그 레짐, 아니면 Neutral.
- 레짐은 포지션 배수(1.0 / 0.7 / 0.4), 매수 안전마진 임계(15/20/25%),
  신규 매수 허용 종목 수(제한 없음/20/10)를 바꾼다. **상한(5%·25%·현금 최소)은 그대로**다.
- 입력이 모자라 3개 미만이면 `verified: false` — 빈 입력을 통과로 처리하지 않는다(4.3).
- `MACRO_EXTREME`(V06, VIX>35 또는 HY OAS>700bp)은 레짐과 별개로 `veto.py` 가 판단한다.
"""

from __future__ import annotations

from dataclasses import dataclass

REGIMES = ("risk_on", "neutral", "risk_off")

#: 3.7 표의 임계값. [고정] — 보정 대상이 아니다(보정은 Risk-Off 배수만).
THRESHOLDS = {
    "t10y2y": {"risk_on": 0.5, "risk_off": -0.2},          # %p
    "hy_oas_bp": {"risk_on": 350.0, "risk_off": 500.0},    # bp
    "vix": {"risk_on": 18.0, "risk_off": 25.0},
    "dollar_3m": {"risk_on": -0.02, "risk_off": 0.05},     # 3개월 변화율
    "real_rate_change": {"risk_on": -0.10, "risk_off": 0.10},  # 실질금리 3개월 변화(%p)
}

#: 레짐별 조정치(3.7). `max_new_positions=None` 은 제한 없음.
ADJUSTMENTS = {
    "risk_on": {"multiplier": 1.0, "margin_of_safety": 0.15, "max_new_positions": None, "score": 80.0},
    "neutral": {"multiplier": 0.7, "margin_of_safety": 0.20, "max_new_positions": 20, "score": 50.0},
    "risk_off": {"multiplier": 0.4, "margin_of_safety": 0.25, "max_new_positions": 10, "score": 20.0},
}


@dataclass(frozen=True)
class RegimeView:
    regime: str
    votes: dict[str, str]
    votes_risk_on: int
    votes_risk_off: int
    inputs_missing: tuple[str, ...]
    verified: bool
    extreme: bool = False

    @property
    def multiplier(self) -> float:
        return ADJUSTMENTS[self.regime]["multiplier"]

    @property
    def margin_of_safety(self) -> float:
        return ADJUSTMENTS[self.regime]["margin_of_safety"]

    @property
    def max_new_positions(self) -> int | None:
        return ADJUSTMENTS[self.regime]["max_new_positions"]

    @property
    def score(self) -> float:
        """M 점수(0~100) — `entry_score` 의 거시 항."""
        return ADJUSTMENTS[self.regime]["score"]

    def as_dict(self) -> dict:
        return {
            "regime": self.regime,
            "votes": dict(self.votes),
            "votes_risk_on": self.votes_risk_on,
            "votes_risk_off": self.votes_risk_off,
            "inputs_missing": list(self.inputs_missing),
            "verified": self.verified,
            "extreme": self.extreme,
            "multiplier": self.multiplier,
            "margin_of_safety": self.margin_of_safety,
            "max_new_positions": self.max_new_positions,
            "score": self.score,
        }


def _vote_threshold(value: float | None, low: float, high: float) -> str | None:
    """낮으면 risk_on, 높으면 risk_off, 사이면 neutral(3.7 표)."""
    if value is None:
        return None
    if value < low:
        return "risk_on"
    if value > high:
        return "risk_off"
    return "neutral"


def _real_rate(values: dict, previous: dict | None) -> float | None:
    dff = values.get("dff")
    cpi = values.get("cpi_yoy")
    if dff is None or cpi is None:
        return None
    real = float(dff) - float(cpi)
    prev = None
    if previous:
        pdff, pcpi = previous.get("dff"), previous.get("cpi_yoy")
        if pdff is not None and pcpi is not None:
            prev = float(pdff) - float(pcpi)
    return None if prev is None else real - prev


def classify_regime(values: dict | None, previous: dict | None = None,
                    extreme_vix: float = 35.0, extreme_hy_bp: float = 700.0) -> RegimeView:
    """`values` 는 최신 거시값, `previous` 는 같은 지표의 약 3개월 전 값(추세 계산용).

    값이 없는 지표는 투표에서 빠지고 `inputs_missing` 에 남는다. 3개 미만이면 `verified=False`.
    """
    values = dict(values or {})
    votes: dict[str, str] = {}
    missing: list[str] = []

    real_change = _real_rate(values, previous)
    v = _vote_threshold(real_change, THRESHOLDS["real_rate_change"]["risk_on"],
                        THRESHOLDS["real_rate_change"]["risk_off"])
    if v is None:
        missing.append("real_rate")
    else:
        votes["real_rate"] = v

    for key, name in (("t10y2y", "t10y2y"), ("hy_oas_bp", "credit"), ("vix", "volatility")):
        raw = values.get(key)
        th = THRESHOLDS["hy_oas_bp" if key == "hy_oas_bp" else key]
        v = _vote_threshold(float(raw) if raw is not None else None, th["risk_on"], th["risk_off"])
        if v is None:
            missing.append(name)
        else:
            votes[name] = v

    dollar = None
    if values.get("dtwexbg") is not None and previous and previous.get("dtwexbg"):
        prev_d = float(previous["dtwexbg"])
        if prev_d:
            dollar = float(values["dtwexbg"]) / prev_d - 1.0
    v = _vote_threshold(dollar, THRESHOLDS["dollar_3m"]["risk_on"], THRESHOLDS["dollar_3m"]["risk_off"])
    if v is None:
        missing.append("dollar")
    else:
        votes["dollar"] = v

    on = sum(1 for v in votes.values() if v == "risk_on")
    off = sum(1 for v in votes.values() if v == "risk_off")
    if on >= 3 and on > off:
        regime = "risk_on"
    elif off >= 3 and off > on:
        regime = "risk_off"
    else:
        regime = "neutral"

    vix = values.get("vix")
    hy = values.get("hy_oas_bp")
    extreme = bool((vix is not None and float(vix) > extreme_vix) or
                   (hy is not None and float(hy) > extreme_hy_bp))
    return RegimeView(regime=regime, votes=votes, votes_risk_on=on, votes_risk_off=off,
                      inputs_missing=tuple(missing), verified=len(votes) >= 3, extreme=extreme)


def regime_adjustments(regime: str) -> dict:
    """레짐 이름 → 배수·안전마진 임계·신규 종목 수·M 점수."""
    if regime not in ADJUSTMENTS:
        raise ValueError(f"알 수 없는 레짐: {regime!r} (허용: {REGIMES})")
    return dict(ADJUSTMENTS[regime])
