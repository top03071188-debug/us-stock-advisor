"""engine.metrics.valuation — 내재가치를 **전부 USD/share 로 통일**한다.

순서 [고정] (기획서 3.2.3):
  FCFF → EV_DCF → (순부채·비지배지분 차감) → 지분가치 → ÷ 희석주식수 = USD/share
  EPV = NOPAT_norm / WACC → 같은 조정 → ÷ 주식수
  Graham = sqrt(22.5 × EPS × BPS)  (이미 USD/share)
  IV_median = median(IV_DCF_base, IV_EPV, IV_Graham)

은행·보험·REIT 는 업종 전용 산식이 완성될 때까지 **가치평가 보류**(`valuation_suspended`).

[고정] v1.3.3 04: V 규칙 값은 **`config/params_v001.yaml` 의 `valuation:` 절 하나**에서 온다
(`ValuationParams`·`valuation_params()`). 예전에는 yaml 의 `v_score_minus20`·`dcf_penalty_points`
같은 키를 아무 코드도 읽지 않고 같은 값을 이 파일 상수에 중복 정의해, 설정을 바꿔도 동작이 변하지 않았다.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, fields
from functools import lru_cache
from pathlib import Path

import yaml

from engine.models import PerShareValue, UnitMismatchError

SUSPENDED_SECTORS = {
    "financials", "financial", "banks", "bank", "insurance", "insurers",
    "reits", "reit", "real estate", "부동산", "은행", "보험",
}
SUSPEND_REASON = "SECTOR_FORMULA_PENDING"

#: `config/params_v001.yaml` — V 규칙의 **단일 출처**(v1.3.3 04).
PARAMS_PATH = Path(__file__).resolve().parents[2] / "config" / "params_v001.yaml"


@dataclass(frozen=True)
class ValuationParams:
    """`config/params_v001.yaml` 의 `valuation:` 절 → V 점수·감점·민감도 규칙.

    기본값은 v1.3.2 값과 **같다**(사용자 결정: V 감점 −4/−10과 3구간 식 유지).
    파일이 없거나 키가 빠지면 기본값을 쓰되, 있으면 **코드가 실제로 읽는다**.
    """

    discount_rate_min: float = 0.08
    discount_rate_max: float = 0.15
    terminal_growth: float = 0.025
    growth_cap: float = 0.15
    margin_high: float = 0.30            # 안전마진 +30% → V 100
    margin_mid: float = 0.0              # 0% → V 50
    margin_floor: float = -0.20          # −20% → V 15
    margin_floor_min: float = -2.00      # −200% 이하 → V 0
    v_score_high: float = 100.0
    v_score_mid: float = 50.0
    v_score_floor: float = 15.0
    v_score_min: float = 0.0
    dcf_sensitivity_max: float = 0.40    # 이 이상이면 V 신뢰도 C + 감점 −4
    v_penalty_dcf: float = 4.0
    hist_divergence_max: float = 0.20    # 모델 IV 와 역사 EV/EBIT 내재가치의 허구 괴리
    v_penalty_hist: float = 10.0
    ev_ebit_percentile_hot: float = 90.0  # S1 과열(밸류)
    wacc_sensitivity_pm: float = 0.01    # 성장률 0%일 때 WACC ±1.0%p 민감도
    terminal_g_sensitivity_pm: float = 0.005   # 영구성장률 ±0.5%p

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def load_valuation_params(path: str | Path | None = None) -> ValuationParams:
    """yaml `valuation:` 절 → `ValuationParams`. 모르는 키는 무시하고, 빠진 키는 기본값."""
    target = Path(path) if path is not None else PARAMS_PATH
    known = {f.name for f in fields(ValuationParams)}
    try:
        with open(target, encoding="utf-8") as fh:
            raw = (yaml.safe_load(fh) or {}).get("valuation") or {}
    except (OSError, yaml.YAMLError):
        return ValuationParams()
    values: dict[str, float] = {}
    for key, value in raw.items():
        if key not in known or value is None:
            continue
        try:
            values[key] = float(value)
        except (TypeError, ValueError):
            continue
    return ValuationParams(**values)


@lru_cache(maxsize=1)
def valuation_params() -> ValuationParams:
    """프로세스 1회 로딩 — 호출자는 `params=` 로 명시할 수도 있다(테스트·보정)."""
    return load_valuation_params()


@dataclass(frozen=True)
class ValuationInputs:
    """모두 백만 달러·백만 주 단위로 맞춰 넣는다(단위가 섞이면 결과가 $/주가 아니다)."""

    ebit: float
    tax_rate: float
    depreciation: float
    capex: float
    delta_nwc: float
    net_debt: float
    minority_interest: float
    diluted_shares: float
    wacc: float
    terminal_growth: float = 0.025
    growth: float = 0.05
    eps: float | None = None
    bps: float | None = None


def classify_sector(sector: str | None) -> dict:
    if not sector:
        return {"valuation_suspended": False, "sector": None, "reason": None}
    norm = str(sector).strip().lower()
    suspended = any(tok in norm for tok in SUSPENDED_SECTORS) or norm in SUSPENDED_SECTORS
    return {
        "valuation_suspended": suspended,
        "sector": sector,
        "reason": SUSPEND_REASON if suspended else None,
    }


def nopat(ebit: float, tax_rate: float) -> float:
    return ebit * (1.0 - tax_rate)


def fcff(ebit: float, tax_rate: float, depreciation: float, capex: float, delta_nwc: float) -> float:
    return nopat(ebit, tax_rate) + depreciation - capex - delta_nwc


def dcf_ev(fcff_ref: float, growth: float, wacc: float, g: float, years: int = 5) -> dict:
    """FCFF 5년 할인 + 터미널밸류(EV, 백만 달러)."""
    if wacc <= g:
        raise ValueError("WACC 는 영구성장률보다 커야 한다")
    pv_explicit = 0.0
    flows = []
    for t in range(1, years + 1):
        f = fcff_ref * (1.0 + growth) ** t
        flows.append(f)
        pv_explicit += f / (1.0 + wacc) ** t
    fcff_terminal = flows[-1] * (1.0 + g)
    tv = fcff_terminal / (wacc - g)
    pv_tv = tv / (1.0 + wacc) ** years
    return {
        "ev": pv_explicit + pv_tv,
        "pv_explicit": pv_explicit,
        "pv_terminal": pv_tv,
        "terminal_value": tv,
        "fcff_flows": flows,
    }


def epv_ev(nopat_norm: float, wacc: float) -> float:
    return nopat_norm / wacc


def equity_per_share(ev: float, net_debt: float, minority: float, shares: float) -> float:
    if shares <= 0:
        raise ValueError("희석주식수는 0보다 커야 한다")
    return (ev - net_debt - minority) / shares


def graham_number(eps: float, bps: float) -> float:
    prod = 22.5 * float(eps) * float(bps)
    if prod <= 0:
        raise ValueError("Graham Number 는 EPS·BPS 가 양수일 때만 계산한다")
    return math.sqrt(prod)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def median_of(values: list[PerShareValue]) -> PerShareValue:
    """USD/share 만 받는다. EV·시총·주식수가 섞이면 `UnitMismatchError`."""
    if not values:
        raise UnitMismatchError("중앙값에 넣을 내재가치가 없다")
    for v in values:
        if not isinstance(v, PerShareValue):
            raise UnitMismatchError(
                f"PerShareValue 가 아닌 값({v!r})이 중앙값에 들어왔다 — EV·주식수를 그대로 넣지 않는다."
            )
    median = statistics.median([v.value for v in values])
    return PerShareValue(median, method="median(" + ",".join(v.method for v in values) + ")")


def value_company(inp: ValuationInputs, sector: str | None = None,
                  discount_min: float | None = None, discount_max: float | None = None,
                  growth_cap: float | None = None, params: ValuationParams | None = None) -> dict:
    """기획서 3.2.3 의 전체 파이프라인. 반환값은 전부 USD/share (단위 태그 포함).

    할인율 범위·영구성장률·성장 상한은 `params`(기본: `config/params_v001.yaml`)에서 온다.
    """
    p = params or valuation_params()
    discount_min = p.discount_rate_min if discount_min is None else discount_min
    discount_max = p.discount_rate_max if discount_max is None else discount_max
    growth_cap = p.growth_cap if growth_cap is None else growth_cap
    sector_info = classify_sector(sector)
    if sector_info["valuation_suspended"]:
        return {
            "valuation_suspended": True,
            "reason": SUSPEND_REASON,
            "sector": sector,
            "unit": "USD/share",
            "iv_low": None, "iv_median": None, "iv_high": None, "margin": None,
            "methods": {},
        }

    wacc = _clamp(inp.wacc, discount_min, discount_max)
    growth = _clamp(inp.growth, 0.0, growth_cap)
    terminal_growth = inp.terminal_growth if inp.terminal_growth is not None else p.terminal_growth
    base_fcff = fcff(inp.ebit, inp.tax_rate, inp.depreciation, inp.capex, inp.delta_nwc)
    if base_fcff <= 0:
        raise ValueError("정상화 FCFF 가 0 이하 — DCF 를 적용할 수 없다")

    def iv(g: float) -> float:
        ev = dcf_ev(base_fcff, g, wacc, terminal_growth)["ev"]
        return equity_per_share(ev, inp.net_debt, inp.minority_interest, inp.diluted_shares)

    iv_dcf_base = iv(growth)
    iv_dcf_bear = iv(growth * 0.5)
    iv_dcf_bull = iv(growth * 1.5)

    nopat_norm = nopat(inp.ebit, inp.tax_rate)
    iv_epv = equity_per_share(
        epv_ev(nopat_norm, wacc), inp.net_debt, inp.minority_interest, inp.diluted_shares
    )

    methods: dict[str, float] = {}
    if iv_dcf_base > 0:
        methods["dcf_base"] = iv_dcf_base
    if iv_epv > 0:
        methods["epv"] = iv_epv
    if inp.eps and inp.bps and inp.eps > 0 and inp.bps > 0:
        methods["graham"] = graham_number(inp.eps, inp.bps)
    if not methods:
        raise ValueError("사용 가능한 내재가치 방법이 0개 — V 신뢰도 C 로도 계산 불가")

    med = median_of([PerShareValue(v, m) for m, v in methods.items()])
    bear_set = [x for x in (iv_dcf_bear, iv_epv, methods.get("graham")) if x and x > 0]
    bull_set = [x for x in (iv_dcf_bull, iv_epv, methods.get("graham")) if x and x > 0]
    iv_low = statistics.median(bear_set) * 0.85
    iv_high = statistics.median(bull_set) * 1.15

    return {
        "valuation_suspended": False,
        "reason": None,
        "sector": sector,
        "unit": "USD/share",
        "wacc": wacc,
        "growth": growth,
        "fcff_normalized": base_fcff,
        "ev_dcf_base": dcf_ev(base_fcff, growth, wacc, terminal_growth)["ev"],
        "ev_dcf_bear": dcf_ev(base_fcff, growth * 0.5, wacc, terminal_growth)["ev"],
        "ev_dcf_bull": dcf_ev(base_fcff, growth * 1.5, wacc, terminal_growth)["ev"],
        "ev_epv": epv_ev(nopat_norm, wacc),
        "iv_dcf": iv_dcf_base,
        "iv_epv": iv_epv,
        "iv_graham": methods.get("graham"),
        "iv_median": med.value,
        "iv_low": iv_low,
        "iv_high": iv_high,
        "methods": methods,
        "method_count": len(methods),
        "v_confidence": "C" if len(methods) == 1 else "B",
    }


def margin_of_safety(iv_median: float, price: float) -> float:
    """`(IV_median − price) / IV_median`. price 는 현재 주식수 기준(sadj) 가격이어야 한다."""
    if iv_median == 0:
        raise ValueError("IV_median 이 0")
    return (iv_median - price) / iv_median


#: v1.3.3 04: 아래 상수는 `ValuationParams` 기본값의 **별칭**이다 — 값은 yaml 한 곳에서 온다.
#: (예전에는 yaml 키와 상수가 같은 값을 중복 정의해 설정을 바꿔도 동작이 변하지 않았다.)
DEFAULTS = ValuationParams()
V_PENALTY_DCF = DEFAULTS.v_penalty_dcf          # DCF 민감도 큼 — V 신뢰도 C 강등과 함께 적용
V_PENALTY_HIST = DEFAULTS.v_penalty_hist        # 10년 역사적 EV/EBIT 중앙값 대비 IV 20% 이상 괴리
DCF_SENSITIVITY_MAX = DEFAULTS.dcf_sensitivity_max   # 성장률 변화에 IV 가 ±40% 이상 흔들리면 민감도 큼
HIST_DIVERGENCE_MAX = DEFAULTS.hist_divergence_max   # IV 와 역사적 밸류에이션 내재가치의 괴리 허용폭
EV_EBIT_PERCENTILE_HOT = DEFAULTS.ev_ebit_percentile_hot   # S1 과열(밸류)


def v_score(margin: float, penalties: float = 0.0, params: ValuationParams | None = None) -> float:
    """안전마진 → 0~100 (v1.3.2 식 그대로, v1.3.3에서 파라미터화).

    0% = 50, +30% = 100 / 0% = 50, −20% = 15 / −20% = 15, −200% = 0.
    −20% 아래를 0 으로 눌러 버리면 고평가 종목이 전부 같은 0점이 되어
    (ADBE −40% 와 GOOGL −349%) 변별력이 사라지고 감점(−4/−10)도 반영될 자리가 없었다.
    """
    p = params or valuation_params()
    if margin >= p.margin_mid:
        slope = (p.v_score_high - p.v_score_mid) / (p.margin_high - p.margin_mid)
        score = p.v_score_mid + (margin - p.margin_mid) * slope
    elif margin >= p.margin_floor:
        slope = (p.v_score_floor - p.v_score_mid) / (p.margin_floor - p.margin_mid)
        score = p.v_score_mid + (margin - p.margin_mid) * slope
    else:
        slope = (p.v_score_min - p.v_score_floor) / (p.margin_floor_min - p.margin_floor)
        score = p.v_score_floor + (margin - p.margin_floor) * slope
    return _clamp(score - penalties, 0.0, 100.0)


def dcf_sensitivity(iv_bear: float, iv_bull: float, iv_base: float) -> float:
    """성장률 변화에 IV 가 **편측으로** 얼마나 흔들리는가(§3.2.3).

    `abs(iv_bull − iv_bear) / (2 × iv_base)` — 성장률 0.5·1.5배 시나리오의 폭을
    기본값 기준의 ±비율로 환산한다. 0.40 이상이면 "±40% 이상 변동" = 민감도 큼.
    """
    if iv_base <= 0:
        return float("inf")
    return abs(float(iv_bull) - float(iv_bear)) / (2.0 * float(iv_base))


def dcf_rate_sensitivity(base_fcff: float, growth: float, wacc: float, terminal_growth: float,
                         net_debt: float, minority: float, shares: float,
                         dwacc: float = 0.01, dg: float = 0.005, years: int = 5) -> dict:
    """성장률이 0 으로 잘렸을 때의 **다른 축** 민감도 — WACC ±1.0%p · 영구성장률 ±0.5%p (v1.3.3 05).

    성장률 0%면 bear(0.5배)=bull(1.5배)=base 라 `dcf_sensitivity` 가 항상 0 이 되어
    "검증 통과"처럼 읽했다. DCF 시나리오가 붕괴한 경우에도 가치가 할인율·영구성장률 가정에
    얼마나 흔들리는지 같은 척도(편측 ±비율)로 보인다.
    """
    def _iv(w: float, g: float) -> float | None:
        try:
            ev = dcf_ev(base_fcff, growth, w, g, years=years)["ev"]
        except (ValueError, ZeroDivisionError):
            return None
        return equity_per_share(ev, net_debt, minority, shares)

    base = _iv(wacc, terminal_growth) or 0.0
    out: dict = {"wacc_sensitivity": None, "terminal_g_sensitivity": None,
                 "rate_sensitivity": None, "wacc_low_iv": None, "wacc_high_iv": None,
                 "g_low_iv": None, "g_high_iv": None}
    w_lo, w_hi = _iv(wacc - dwacc, terminal_growth), _iv(wacc + dwacc, terminal_growth)
    if base > 0 and w_lo is not None and w_hi is not None:
        out["wacc_low_iv"], out["wacc_high_iv"] = w_lo, w_hi
        out["wacc_sensitivity"] = dcf_sensitivity(w_lo, w_hi, base)
    g_lo, g_hi = _iv(wacc, terminal_growth - dg), _iv(wacc, terminal_growth + dg)
    if base > 0 and g_lo is not None and g_hi is not None:
        out["g_low_iv"], out["g_high_iv"] = g_lo, g_hi
        out["terminal_g_sensitivity"] = dcf_sensitivity(g_lo, g_hi, base)
    legs = [v for v in (out["wacc_sensitivity"], out["terminal_g_sensitivity"]) if v is not None]
    out["rate_sensitivity"] = max(legs) if legs else None
    return out
