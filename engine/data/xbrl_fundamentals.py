"""engine.data.xbrl_fundamentals — SEC XBRL companyfacts → `AnnualFundamentals`(최근 5 회계연도).

[고정] look-ahead 차단(4.5)
- 연차 재무는 **`form` 이 10-K 계열 · `fp` = FY · `filed ≤ decision_at.date()`** 인 항목만 쓴다.
- 같은 회계연도 값이 정정(10-K/A)·재작성으로 여러 번 나오면 **가장 먼저 공개된 값**을 쓴다
  (그때 알 수 있었던 값을 재생하는 것이 원칙 — 사후 정정 수치는 그 시점에 없었다).
- 분기(10-Q) 값은 연차 집계에 섞지 않는다.

단위: `AnnualFundamentals` 는 **백만 달러·백만 주**다. XBRL 은 달러·주 단위이므로 1e6 으로 나눈다.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Iterable

import numpy as np
import pandas as pd

from engine.metrics.fundamentals import AnnualFundamentals

#: 지표 → XBRL 태그 **우선순위**. 같은 회계연도에 여러 태그가 있으면 앞의 태그를 쓴다.
ANNUAL_TAGS: dict[str, list[str]] = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ],
    "ebit": ["OperatingIncomeLoss"],    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "ocf": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets",
    ],
    "depreciation": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
        "Depreciation",
    ],
    "total_assets": ["Assets"],
    "liabilities": ["Liabilities"],
    "equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "long_term_debt": ["LongTermDebtNoncurrent", "LongTermDebt", "LongTermDebtAndCapitalLeaseObligations"],
    "short_term_debt": ["LongTermDebtCurrent", "ShortTermBorrowings", "DebtCurrent", "OtherShortTermBorrowings"],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
    "diluted_shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "interest_expense": [
        "InterestExpense",
        "InterestExpenseNonoperating",
        "InterestExpenseDebt",
        "InterestIncomeExpenseNet",
    ],
    "pretax_income": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ],
    "tax_expense": ["IncomeTaxExpenseBenefit"],
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
}

#: 기간(flow) 항목 — 시작·끝이 있어야 하고 1년(±)이어야 한다. 나머지는 시점(instant) 항목.
FLOW_METRICS = frozenset({
    "revenue", "ebit", "net_income", "ocf", "capex", "depreciation",
    "diluted_shares", "interest_expense", "pretax_income", "tax_expense",
})

#: HG-11(XBRL 완전성 ≥ 90%) 판정에 쓰는 필수 항목 — 게이트·점수 계산이 실제로 읽는 것들.
REQUIRED_METRICS = (
    "revenue", "ebit", "net_income", "ocf", "capex", "depreciation",
    "total_assets", "equity", "diluted_shares",
)

#: 연차 재무로 인정하는 form. 10-K/A(정정)도 포함하되 `filed` 필터가 먼저다.
ANNUAL_FORMS = ("10-K", "10-K/A", "20-F", "40-F")


def _as_date(value) -> date | None:
    if value in (None, ""):
        return None
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError):
        return None


def _within_a_year(start: date, end: date) -> bool:
    days = (end - start).days
    return 330 <= days <= 400


def annual_series(payload: dict, decision_date: date) -> dict[str, dict[date, float]]:
    """companyfacts → `{metric: {회계연도 말일: 값(원 단위)}}`.

    - `filed ≤ decision_date` 만(문자열 ISO 비교로 충분하다 — SEC 는 `YYYY-MM-DD`).
    - 같은 (metric, 회계연도)에 여러 항목이 있으면 **가장 이른 `filed`** 를 남긴다.
    - 태그 우선순위: 앞 태그가 채운 회계연도는 뒤 태그가 덮어쓰지 않는다.
    """
    gaap = ((payload.get("facts") or {}).get("us-gaap") or {})
    cutoff = decision_date.isoformat()
    out: dict[str, dict[date, float]] = {}
    for metric, tags in ANNUAL_TAGS.items():
        per_date: dict[date, tuple[str, float]] = {}
        for tag in tags:
            block = gaap.get(tag) or {}
            for unit, entries in (block.get("units") or {}).items():
                if unit not in ("USD", "USD/shares", "shares"):
                    continue
                for e in entries:
                    if str(e.get("form") or "") not in ANNUAL_FORMS:
                        continue
                    if str(e.get("fp") or "") != "FY":
                        continue
                    filed = str(e.get("filed") or "")
                    if not filed or filed > cutoff:
                        continue
                    end = _as_date(e.get("end"))
                    val = e.get("val")
                    if end is None or val is None:
                        continue
                    start = _as_date(e.get("start"))
                    if metric in FLOW_METRICS:
                        if start is None or not _within_a_year(start, end):
                            continue
                    elif start is not None and (end - start).days > 10:
                        continue  # 시점 항목인데 기간 항목으로 들어온 행은 버린다
                    try:
                        value = float(val)
                    except (TypeError, ValueError):
                        continue
                    prev = per_date.get(end)
                    if prev is None or filed < prev[0]:
                        per_date[end] = (filed, value)
        if per_date:
            out[metric] = {d: v for d, (_, v) in per_date.items()}
    return out


def fiscal_years(series: dict[str, dict[date, float]], limit: int = 5) -> list[date]:
    """최근 회계연도 말일(최신순). 매출·영업이익 기준, 없으면 자산 기준."""
    for metric in ("revenue", "ebit", "net_income", "total_assets"):
        dates = series.get(metric) or {}
        if dates:
            return sorted(dates, reverse=True)[:limit]
    return []


def _lookup(values: dict[date, float] | None, target: date, tolerance_days: int = 10) -> float:
    """해당 회계연도의 값. 정확히 없으면 ±`tolerance_days` 안의 가장 가까운 값, 없으면 NaN."""
    if not values:
        return float("nan")
    if target in values:
        return values[target]
    near = [(abs((d - target).days), v) for d, v in values.items() if abs((d - target).days) <= tolerance_days]
    if not near:
        return float("nan")
    return min(near, key=lambda x: x[0])[1]


def _to_millions(value: float) -> float:
    return value / 1e6 if np.isfinite(value) else float("nan")


def _sum_finite(*values: float) -> float:
    """유한한 항만 더한다(한 항목이 결측이면 0으로 보고 더한다 — 총부채 = 장기 + 단기)."""
    parts = [float(v) for v in values if np.isfinite(v)]
    return float(sum(parts)) if parts else float("nan")


def _norm_share_count(value: float) -> float:
    """주식수를 **주 단위**로 맞춘다.

    일부 기업(MCD 등)은 최근 공시에서 `shares` 단위 값을 **백만 주 / 천 주** 로 태깅한다.
    실제 상장사 희석주식수는 1e6~1e11 주이므로 그 범위 밖이면 단위를 되돌린다.
    """
    if not np.isfinite(value) or value <= 0:
        return float("nan")
    if value < 1e5:
        return value * 1e6      # 백만 주 단위
    if value < 1e6:
        return value * 1e3      # 천 주 단위
    return value


def _carry_forward(values: list[float]) -> list[float]:
    """최신순 리스트에서 결측을 **가장 최근에 보고된 값**으로 채운다(없으면 NaN 유지).

    이자비용·감가상각처럼 태그가 일부 연도에만 있는 항목의 근사치다. 새는 방향은
    보수적이다(부채가 늘어도 옛 이자비용을 쓰면 이자보상배율이 낮게 나온다).
    """
    known = [v for v in values if np.isfinite(v)]
    if not known:
        return values
    out: list[float] = []
    for v in values:
        out.append(v if np.isfinite(v) else known[0])
    return out


def build_fundamentals(payload: dict, decision_date: date,
                       years: int = 5) -> tuple[AnnualFundamentals | None, dict]:
    """companyfacts → (AnnualFundamentals, 진단).

    진단에는 `fiscal_years`, `completeness`, `missing`(지표별 결측 연도 수)가 들어간다.
    """
    series = annual_series(payload, decision_date)
    fy = fiscal_years(series, limit=years)
    diag: dict = {"fiscal_years": [d.isoformat() for d in fy], "completeness": 0.0, "missing": {}}
    if not fy:
        return None, diag

    def col(metric: str) -> list[float]:
        return [_lookup(series.get(metric), d) for d in fy]

    revenue = col("revenue")
    ebit = col("ebit")
    net_income = col("net_income")
    ocf = col("ocf")
    capex = col("capex")
    depreciation = _carry_forward(col("depreciation"))
    total_assets = col("total_assets")
    liabilities = col("liabilities")
    equity = col("equity")
    # 총부채 태그가 없는 기업은 회계항등식(자산 = 부채 + 자본)으로 채운다.
    liabilities = [li if np.isfinite(li) else (a - e if np.isfinite(a) and np.isfinite(e) else float("nan"))
                   for li, a, e in zip(liabilities, total_assets, equity, strict=False)]
    long_debt = _carry_forward(col("long_term_debt"))
    short_debt = _carry_forward(col("short_term_debt"))
    cash = col("cash")
    shares = [_norm_share_count(v) for v in col("diluted_shares")]
    interest = _carry_forward(col("interest_expense"))
    pretax = col("pretax_income")
    tax = col("tax_expense")
    # 영업이익 태그가 없는 기업(제약·산업재 다수)은 세전이익 + 이자비용으로 EBIT 를 근사한다.
    # 운영이익을 별도 태그하지 않는 회사도 재무제표상 EBIT 는 이 합으로 나온다.
    ebit = [e if np.isfinite(e) else (p + i if np.isfinite(p) and np.isfinite(i) else float("nan"))
            for e, p, i in zip(ebit, pretax, interest, strict=False)]
    current_assets = col("current_assets")
    current_liabilities = col("current_liabilities")

    total_debt = [_sum_finite(long_debt[i], short_debt[i]) for i in range(len(fy))]
    nil = [max(0.0, li - td) if np.isfinite(li) and np.isfinite(td) else float("nan")
           for li, td in zip(liabilities, total_debt, strict=False)]
    base_rev = revenue[0] if np.isfinite(revenue[0]) else 0.0
    excess_cash = [max(0.0, c - 0.02 * base_rev) if np.isfinite(c) else float("nan") for c in cash]

    # 유효세율 — 5년 합계 세금/세전이익, 0~35% 로 자른다(음수·이상치는 기본 21% 로).
    eff = 0.21
    if any(np.isfinite(p) and np.isfinite(t) and p > 0 for p, t in zip(pretax, tax, strict=False)):
        p_sum = float(np.nansum([p for p in pretax if np.isfinite(p) and p > 0]))
        t_sum = float(np.nansum([t for p, t in zip(pretax, tax, strict=False) if np.isfinite(p) and p > 0 and np.isfinite(t)]))
        if p_sum > 0:
            eff = float(min(0.35, max(0.0, t_sum / p_sum)))

    f = AnnualFundamentals(
        ebit=[_to_millions(v) for v in ebit],
        revenue=[_to_millions(v) for v in revenue],
        net_income=[_to_millions(v) for v in net_income],
        ocf=[_to_millions(v) for v in ocf],
        capex=[_to_millions(v) for v in capex],
        depreciation=[_to_millions(v) for v in depreciation],
        total_assets=[_to_millions(v) for v in total_assets],
        non_interest_liabilities=[_to_millions(v) for v in nil],
        excess_cash=[_to_millions(v) for v in excess_cash],
        total_debt=[_to_millions(v) for v in total_debt],
        cash=[_to_millions(v) for v in cash],
        equity=[_to_millions(v) for v in equity],
        diluted_shares=[_to_millions(v) for v in shares],
        interest_expense=[_to_millions(v) for v in interest],
        tax_rate=round(eff, 4),
    )

    cells = 0
    filled = 0
    missing: dict[str, int] = {}
    for dict_metric in REQUIRED_METRICS:
        vals = ({"revenue": revenue, "ebit": ebit, "net_income": net_income, "ocf": ocf,
                 "capex": capex, "depreciation": depreciation, "total_assets": total_assets,
                 "equity": equity, "diluted_shares": shares})[dict_metric]
        n_missing = sum(1 for v in vals if not np.isfinite(v))
        missing[dict_metric] = n_missing
        cells += len(vals)
        filled += len(vals) - n_missing
    diag["completeness"] = round(filled / cells, 4) if cells else 0.0
    diag["missing"] = missing
    diag["years_available"] = len(fy)
    diag["nwc_series"] = _nwc_delta(current_assets, current_liabilities, revenue[:2])
    return f, diag


def _nwc_delta(current_assets: list[float], current_liabilities: list[float],
               revenue: list[float]) -> float:
    """ΔNWC(백만 달러) — 최근 1년 (유동자산 − 유동부채) 변화. 없으면 0(보수 아님, DCF 입력)."""
    if len(current_assets) < 2 or len(current_liabilities) < 2:
        return 0.0
    if not all(np.isfinite(x) for x in current_assets[:2] + current_liabilities[:2]):
        return 0.0
    delta = (current_assets[0] - current_liabilities[0]) - (current_assets[1] - current_liabilities[1])
    delta_m = delta / 1e6
    cap = 0.05 * (revenue[0] if revenue and np.isfinite(revenue[0]) else 0.0)
    if cap > 0:
        delta_m = max(-cap, min(cap, delta_m))
    return float(delta_m)


def revenue_cagr(f: AnnualFundamentals) -> float | None:
    """최근 5개 회계연도의 매출 CAGR(4년). 값이 없거나 시작이 0 이하면 None."""
    rev = f.revenue
    if len(rev) < 5 or not np.isfinite(rev[0]) or not np.isfinite(rev[-1]) or rev[-1] <= 0 or rev[0] <= 0:
        return None
    return float((rev[0] / rev[-1]) ** (1.0 / (len(rev) - 1)) - 1.0)


def peg_ratio(f: AnnualFundamentals, price: float, growth: float | None) -> float | None:
    """PEG = (주가/EPS) ÷ (성장률 %p). EPS≤0 이나 성장률≤0 이면 None."""
    if growth is None or growth <= 0 or price is None or not np.isfinite(price) or price <= 0:
        return None
    if not f.net_income or not f.diluted_shares:
        return None
    ni, sh = f.net_income[0], f.diluted_shares[0]
    if not np.isfinite(ni) or not np.isfinite(sh) or sh <= 0 or ni <= 0:
        return None
    eps = ni / sh
    if eps <= 0:
        return None
    pe = price / eps
    return float(pe / (growth * 100.0))


def beta_from_prices(stock: pd.Series, benchmark: pd.Series, window: int = 252) -> float | None:
    """일간 수익률 회귀 베타(clipped 0.3~2.5). 데이터가 모자라면 None."""
    try:
        s = pd.Series(stock).astype(float).pct_change().dropna().tail(window)
        b = pd.Series(benchmark).astype(float).pct_change().dropna().tail(window)
        joined = pd.concat([s, b], axis=1, join="inner").dropna()
        if len(joined) < 60:
            return None
        var = float(joined.iloc[:, 1].var(ddof=1))
        if not np.isfinite(var) or var <= 0:
            return None
        cov = float(joined.iloc[:, 0].cov(joined.iloc[:, 1]))
        return float(min(2.5, max(0.3, cov / var)))
    except Exception:  # noqa: BLE001 — 베타는 보조값이다
        return None


def valuation_multiplier(f: AnnualFundamentals, price: float, sector: str | None,
                         beta: float | None, risk_free: float, growth_cap: float = 0.15,
                         delta_nwc: float = 0.0) -> tuple[float | None, dict]:
    """`iv_mult` = IV_median(USD/share) ÷ 현재가. 계산 불가면 (None, 사유).

    `run_screen.build_candidates` 와 `pipeline.build_views` 는 `iv = price × iv_mult` 로 쓰므로
    **주당 내재가치를 여기서 한 번만** 계산해 배수로 넘긴다(단위는 USD/share 유지).
    """
    from engine.metrics.valuation import ValuationInputs, classify_sector, value_company  # noqa: PLC0415

    if not price or not np.isfinite(price) or price <= 0:
        return None, {"reason": "no_price"}
    if classify_sector(sector)["valuation_suspended"]:
        return None, {"reason": "SECTOR_FORMULA_PENDING"}
    if len(f.ebit) == 0 or not np.isfinite(f.ebit[0]):
        return None, {"reason": "no_ebit"}
    shares = f.diluted_shares[0] if f.diluted_shares else float("nan")
    if not np.isfinite(shares) or shares <= 0:
        return None, {"reason": "no_shares"}

    gross_growth = revenue_cagr(f)
    growth = 0.0 if gross_growth is None else float(max(0.0, min(growth_cap, gross_growth)))
    debt = f.total_debt[0] if np.isfinite(f.total_debt[0]) else 0.0
    interest = f.interest_expense[0] if f.interest_expense and np.isfinite(f.interest_expense[0]) else 0.0
    cost_of_debt = 0.05 if debt <= 0 else float(min(0.10, max(0.02, interest / debt)))
    inp = ValuationInputs(
        ebit=f.ebit[0],
        tax_rate=f.tax_rate,
        depreciation=f.depreciation[0] if np.isfinite(f.depreciation[0]) else 0.0,
        capex=f.capex[0] if np.isfinite(f.capex[0]) else 0.0,
        delta_nwc=delta_nwc,
        net_debt=(debt - (f.cash[0] if np.isfinite(f.cash[0]) else 0.0)),
        minority_interest=0.0,
        diluted_shares=shares,
        wacc=_wacc(f, beta, risk_free, cost_of_debt),
        terminal_growth=0.025,
        growth=growth,
        eps=(f.net_income[0] / shares if np.isfinite(f.net_income[0]) else None),
        bps=(f.equity[0] / shares if np.isfinite(f.equity[0]) else None),
    )
    try:
        res = value_company(inp, sector)
    except (ValueError, ZeroDivisionError) as exc:
        return None, {"reason": f"{type(exc).__name__}: {str(exc)[:80]}"}
    iv = res.get("iv_median")
    if iv is None or not np.isfinite(iv) or iv <= 0:
        return None, {"reason": "iv_unavailable"}
    return float(iv / price), {"iv_median": float(iv), "wacc": res.get("wacc"), "growth": growth,
                               "methods": sorted(res.get("methods") or {})}


def _wacc(f: AnnualFundamentals, beta: float | None, risk_free: float, cost_of_debt: float) -> float:
    """(E/V)Re + (D/V)Rd(1−t). 베타가 없으면 1.0(시장 평균)으로 둔다."""
    e = max(f.equity[0] if f.equity and np.isfinite(f.equity[0]) else 0.0, 0.0)
    d = max(f.total_debt[0] if np.isfinite(f.total_debt[0]) else 0.0, 0.0)
    v = e + d
    if v <= 0:
        return 0.09
    b = 1.0 if beta is None else float(beta)
    re = float(risk_free) + b * 0.05
    return float((e / v) * re + (d / v) * cost_of_debt * (1 - f.tax_rate))


def coverage(payloads: Iterable[dict], decision_date: date) -> dict:
    """여러 종목 payload 의 연차 추출 성공 여부 요약(진단용)."""
    ok = 0
    rows = list(payloads)
    for p in rows:
        if fiscal_years(annual_series(p, decision_date)):
            ok += 1
    return {"tickers": len(rows), "with_annual": ok, "ratio": round(ok / len(rows), 3) if rows else 0.0}


def safe_float(value) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return out if math.isfinite(out) else float("nan")
