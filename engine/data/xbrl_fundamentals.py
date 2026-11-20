"""engine.data.xbrl_fundamentals — SEC XBRL companyfacts → `AnnualFundamentals`(최근 6 회계연도).

[고정] look-ahead 차단(4.5, R06)
- 연차 재무는 **`form` 이 10-K 계열 · `fp` = FY** 인 항목만 쓴다. 분기(10-Q) 값은 연차 집계에 섞지 않는다.
- 시점 필터는 **접수 시각(`acceptanceDateTime`) ≤ decision_at** 이다. `filed` 는 표시·정렬용이다
  (같은 날 마감 뒤 접수된 10-K 가 그날 결정에 들어가면 안 된다).
- 같은 (metric, 회계연도)에 여러 행이 있으면 **결정 시점까지 공개된 최신 유효값**을 쓴다.
  10-K/A·재작성 비교치도 그 시점에 공개됐다면 그때 알 수 있었던 값이다(§4.2.2가 금지하는 것은 *사후* 정정치뿐).
- 접수 시각 인덱스가 없으면 `filed` 날짜 비교로 폴백하고 진단에 `xbrl_acceptance_missing: true` 를 남긴다
  (조용한 폴백 금지).

[고정] 주식수 기준(R05)
- 결정 시점까지 **알려진 분할**만 반영한다. ① 결정 시점까지 제출된 재작성 비교치가 있으면 그 값을 쓰고,
  ② 없으면 그 분할의 ex-date 가 제출일 이후일 때 `split_factor` 를 곱한다.
- 연도별 근거를 `shares_basis`(`as_reported`·`restated`·`split_adjusted`·`unverified`)에 남긴다.
- 분할 이력도 재작성치도 없어 기준을 모르면 `unverified` → HG-09 는 **탈락**한다(모르면 통과 금지).

[고정] 밸류에이션 정상화(R03)
- FCFF·NOPAT 는 **최근 5년 중앙값**으로 정상화하고(§3.2.3), 성장률은 **5년 FCFF CAGR** 을 0~15% 로 자른다.
- ΔNWC 는 **영업 기준**((유동자산 − 현금 − 시장성단기투자) − 유동부채)의 연도별 변화이며,
  각 연도 **매출의 5%** 로 자른다. 모든 단위는 **백만 달러**로 통일한다.
- 순부채 = 이자부채 − 현금 − 시장성단기투자(§3.2.3). 주당 가치는 `engine.metrics.valuation` 이 계산한다
  (이 모듈은 **입력·정상화·배관**만 담당하고 계산식을 다시 쓰지 않는다).

단위: `AnnualFundamentals` 는 **백만 달러·백만 주**다. XBRL 은 달러·주 단위이므로 1e6 으로 나눈다.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable

import numpy as np
import pandas as pd

from engine.metrics.fundamentals import AnnualFundamentals, sanitize_fundamentals

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
    # R03: 순부채·영업 ΔNWC 에서 차감하는 시장성 단기투자 + 대체 집계 태그
    "marketable_securities": [
        "MarketableSecuritiesCurrent",
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
        "ShortTermInvestments",
    ],
    "cash_and_st_investments": ["CashCashEquivalentsAndShortTermInvestments"],
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

#: 연차 재무로 인정하는 form. 10-K/A(정정)도 포함하되 접수 시각 필터가 먼저다.
ANNUAL_FORMS = ("10-K", "10-K/A", "20-F", "40-F")

#: 정상화·PEG 에 쓰는 창(년) — 점수·게이트는 항상 앞 5년만 본다.
NORMALIZATION_YEARS = 5
#: ΔNWC 상한(매출 대비) — §3.2.3
NWC_CAP_RATIO = 0.05


@dataclass(frozen=True)
class Fact:
    """한 (지표, 회계연도) 값과 그 출처(공시 접수)."""

    value: float
    accn: str | None = None
    filed: date | None = None
    form: str | None = None
    tag: str | None = None
    available_at: datetime | None = None


def _as_date(value) -> date | None:
    if value in (None, ""):
        return None
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError):
        return None


def _as_dt(value) -> datetime | None:
    """문자열·Timestamp → tz-aware UTC datetime."""
    if value in (None, ""):
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC").to_pydatetime()


def _decision_bounds(decision_at: date | datetime | None) -> tuple[datetime | None, date]:
    """decision → (UTC datetime 또는 None, 날짜). 날짜만 주면 그날 23:59:59 UTC 까지로 본다."""
    if isinstance(decision_at, datetime):
        dt = decision_at if decision_at.tzinfo else decision_at.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        return dt, dt.date()
    if decision_at is None:
        return None, date.max
    return None, decision_at


def _within_a_year(start: date, end: date) -> bool:
    days = (end - start).days
    return 330 <= days <= 400


def annual_facts(payload: dict, decision_at: date | datetime,
                 acceptance: dict[str, datetime | str] | None = None
                 ) -> tuple[dict[str, dict[date, Fact]], dict]:
    """companyfacts → `{metric: {회계연도 말일: Fact}}` + 진단.

    시점 규칙(R06): 접수 시각이 있으면 `acceptance[accn] ≤ decision_at`, 없으면 `filed ≤ decision_at.date()`.
    같은 (metric, 회계연도)는 **결정 시점까지 공개된 최신 값**을 쓰되, 태그 우선순위가 앞선 값은 덮지 않는다.
    """
    gaap = ((payload.get("facts") or {}).get("us-gaap") or {})
    decision_dt, decision_date = _decision_bounds(decision_at)
    cutoff = decision_date.isoformat()
    out: dict[str, dict[date, Fact]] = {}
    stats = {"rows": 0, "acceptance_missing": 0}
    for metric, tags in ANNUAL_TAGS.items():
        per_date: dict[date, tuple[int, Fact]] = {}
        for rank, tag in enumerate(tags):
            block = gaap.get(tag) or {}
            for unit, entries in (block.get("units") or {}).items():
                if unit not in ("USD", "USD/shares", "shares"):
                    continue
                for e in entries:
                    if str(e.get("form") or "") not in ANNUAL_FORMS:
                        continue
                    if str(e.get("fp") or "") != "FY":
                        continue
                    filed_raw = str(e.get("filed") or "")
                    if not filed_raw:
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
                    stats["rows"] += 1
                    accn = str(e.get("accn") or "") or None
                    acc = (acceptance or {}).get(accn) if accn else None
                    if acc is not None:
                        available = _as_dt(acc)
                        if decision_dt is not None and available is not None and available > decision_dt:
                            continue
                    else:
                        # 접수 시각을 모르면 `filed` 날짜로 폴백한다(그날 23:59:59 UTC 이후로 보수 처리).
                        if filed_raw > cutoff:
                            continue
                        available = datetime.combine(_as_date(filed_raw) or decision_date, time(23, 59, 59),
                                                     tzinfo=timezone.utc)
                        if decision_dt is not None:
                            stats["acceptance_missing"] += 1
                    prev = per_date.get(end)
                    fact = Fact(value=value, accn=accn, filed=_as_date(filed_raw),
                                form=str(e.get("form") or ""), tag=tag, available_at=available)
                    if prev is not None:
                        prev_rank, prev_fact = prev
                        if rank > prev_rank:
                            continue  # 뒤 태그는 앞 태그가 채운 회계연도를 덮지 않는다
                        if rank == prev_rank and (prev_fact.available_at or datetime.min.replace(
                                tzinfo=timezone.utc)) >= (available or datetime.min.replace(tzinfo=timezone.utc)):
                            continue  # 같은 태그면 결정 시점까지 공개된 **최신** 값
                    per_date[end] = (rank, fact)
        if per_date:
            out[metric] = {d: fact for d, (_, fact) in per_date.items()}
    return out, stats


def annual_series(payload: dict, decision_at: date | datetime,
                  acceptance: dict[str, datetime | str] | None = None) -> dict[str, dict[date, float]]:
    """companyfacts → `{metric: {회계연도 말일: 값(원 단위)}}` — `annual_facts` 의 값만 꺼낸 뷰."""
    facts, _ = annual_facts(payload, decision_at, acceptance)
    return {m: {d: f.value for d, f in per.items()} for m, per in facts.items()}


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


# ───────────────────────────── R05: 분할·재작성 기준 통일 ─────────────────────────────
def split_events_from_panel(panel) -> list[dict]:
    """`PricePanel.frame['split_factor']` → `[{ex_date, ratio}]` (ex-date 오름차순).

    `split_factor(t)` 는 t **이후** 분할비율의 곱이므로, 계수가 떨어진 날이 ex-date 이고
    `ratio = factor(직전) / factor(당일)` 이다(10:1 이면 10.0). 별도 API 호출을 하지 않는다.
    """
    frame = getattr(panel, "frame", None)
    if frame is None or "split_factor" not in getattr(frame, "columns", []):
        return []
    factors = frame["split_factor"].astype(float)
    events: list[dict] = []
    prev_val = None
    for ts, val in factors.items():
        if prev_val is not None and np.isfinite(val) and np.isfinite(prev_val) and val < prev_val * (1 - 1e-9):
            ex = ts.date() if hasattr(ts, "date") else _as_date(ts)
            events.append({"ex_date": ex, "ratio": float(prev_val / val)})
        prev_val = val
    return events


def _share_basis(value: float, fy_end: date, filed: date | None,
                 splits: list[dict] | None, decision_date: date) -> tuple[float, str]:
    """연도별 주식수를 **결정 시점 기준**으로 환산하고 근거를 돌려준다.

    - 그 회계연도 말일 이후·결정 시점 이전에 ex-date 가 있는 분할만 본다.
    - 값이 **그 분할의 ex-date 이후 제출된 공시**에서 왔으면 SEC 가 이미 분할 반영해 비교치를
      재작성한 값이므로 곱하지 않는다(`restated`). 그 전 제출본이면 곱한다(`split_adjusted`).
    """
    if not np.isfinite(value):
        return float("nan"), "unverified"
    if not splits:
        return value, "as_reported"
    factor = 1.0
    touched = False
    applied = False
    for s in splits:
        ex = s.get("ex_date")
        ratio = float(s.get("ratio") or 0.0)
        if ex is None or ratio <= 0:
            continue
        if ex <= fy_end or ex > decision_date:
            continue
        touched = True
        if filed is not None and filed >= ex:
            continue  # 그 제출본은 이미 분할 후 기준이다
        factor *= ratio
        applied = True
    if applied:
        return value * factor, "split_adjusted"
    if touched:
        return value, "restated"
    return value, "as_reported"


# ───────────────────────────── R03: 영업 ΔNWC·정상화 ─────────────────────────────
def operating_nwc(current_assets: list[float], current_liabilities: list[float],
                  cash: list[float], marketable: list[float]) -> list[float]:
    """영업 운전자본(백만 달러) = (유동자산 − 현금 − 시장성단기투자) − 유동부채.

    금융항목(현금·시장성 증권)을 빼야 ΔNWC 가 "영업"의 변화가 된다 — GOOGL 2025 는
    (유동자산−유동부채) 변화가 +28,704 이지만 현금·시장성 증권이 만든 변화라 영업 ΔNWC 는 −2,482 다.
    """
    out: list[float] = []
    for ca, cl, c, m in zip(current_assets, current_liabilities, cash, marketable, strict=False):
        if not (np.isfinite(ca) and np.isfinite(cl)):
            out.append(float("nan"))
            continue
        cc = float(c) if np.isfinite(c) else 0.0
        mm = float(m) if np.isfinite(m) else 0.0
        out.append(float(ca) - cc - mm - float(cl))
    return out


def delta_nwc_series(nwc: list[float], revenue: list[float],
                     cap_ratio: float = NWC_CAP_RATIO) -> list[float]:
    """연도별 ΔNWC(백만 달러, 최신순) — 각 연도 **매출의 5%** 로 자른다.

    `nwc` 와 `revenue` 는 같은 단위(백만 달러)여야 한다. 단위가 섞이면 상한이 사실상 사라진다
    (R03 의 원 결함: `delta/1e6` 와 달러 단위 `cap` 을 비교해 5% 상한이 걸리지 않았다).
    """
    out: list[float] = []
    for i in range(len(nwc) - 1):
        now, prev = nwc[i], nwc[i + 1]
        if not (np.isfinite(now) and np.isfinite(prev)):
            out.append(0.0)
            continue
        delta = float(now) - float(prev)
        rev = revenue[i] if i < len(revenue) else float("nan")
        cap = cap_ratio * float(rev) if np.isfinite(rev) and float(rev) > 0 else 0.0
        if cap > 0:
            delta = max(-cap, min(cap, delta))
        out.append(float(delta))
    return out


def _marketable_series(series: dict[str, dict[date, float]], fy: list[date],
                       cash: list[float], diag: dict) -> list[float]:
    """시장성 단기투자(백만 달러). 태그가 없으면 `현금+단기투자` − 현금으로 대체한다."""
    direct = [_to_millions(_lookup(series.get("marketable_securities"), d)) for d in fy]
    agg = [_to_millions(_lookup(series.get("cash_and_st_investments"), d)) for d in fy]
    out: list[float] = []
    missing = False
    for m, a, c in zip(direct, agg, cash, strict=False):
        if np.isfinite(m):
            out.append(float(m))
            continue
        if np.isfinite(a) and np.isfinite(c) and a >= c:
            out.append(float(a) - float(c))
            continue
        missing = True
        out.append(float("nan"))
    if missing:
        diag["nwc_no_marketable"] = True
    return out


def _cagr(first: float, last: float, periods: int) -> float | None:
    if periods <= 0 or not np.isfinite(first) or not np.isfinite(last):
        return None
    if first <= 0 or last <= 0:
        return None
    return float((first / last) ** (1.0 / periods) - 1.0)


def _ttm_net_income(payload: dict, decision_at: date | datetime, fy_end: date,
                    acceptance: dict[str, datetime | str] | None, fy_value: float) -> float | None:
    """TTM 순이익(백만 달러) = 최근 FY + 올해 누적(YTD) − 작년 같은 기간 누적.

    10-Q 는 3·6·9개월 누적을 모두 태깅하므로, Q4 를 따로 찾지 않고 이 식으로 4분기를 만든다.
    """
    gaap = ((payload.get("facts") or {}).get("us-gaap") or {})
    rows = (((gaap.get("NetIncomeLoss") or {}).get("units") or {}).get("USD") or [])
    decision_dt, decision_date = _decision_bounds(decision_at)
    flows: list[tuple[date, date, float]] = []
    for e in rows:
        if str(e.get("form") or "") not in ("10-K", "10-Q", "10-K/A", "10-Q/A"):
            continue
        start, end = _as_date(e.get("start")), _as_date(e.get("end"))
        val = e.get("val")
        if start is None or end is None or val is None:
            continue
        filed_raw = str(e.get("filed") or "")
        if not filed_raw or filed_raw > decision_date.isoformat():
            continue
        acc = (acceptance or {}).get(str(e.get("accn") or ""))
        if acc is not None and decision_dt is not None and (_as_dt(acc) or decision_dt) > decision_dt:
            continue
        try:
            flows.append((start, end, float(val) / 1e6))
        except (TypeError, ValueError):
            continue
    if not flows:
        return None
    # 올해 누적: fy_end 이후 종료, 60~330일 구간 중 가장 늦은 끝
    ytd = [f for f in flows if f[0] > fy_end and 60 <= (f[1] - f[0]).days <= 330]
    if not ytd:
        return None
    ytd_now = max(ytd, key=lambda f: (f[1], f[1] - f[0]))
    target_end = ytd_now[1] - timedelta(days=365)
    duration = (ytd_now[1] - ytd_now[0]).days
    prior = [f for f in flows
             if abs((f[1] - target_end).days) <= 10 and abs((f[1] - f[0]).days - duration) <= 15]
    if not prior:
        return None
    ytd_prior = max(prior, key=lambda f: (f[1], f[1] - f[0]))
    if not np.isfinite(fy_value):
        return None
    return float(fy_value + ytd_now[2] - ytd_prior[2])


def build_fundamentals(payload: dict, decision_at: date | datetime,
                       acceptance: dict[str, datetime | str] | None = None,
                       years: int = 6, splits: list[dict] | None = None
                       ) -> tuple[AnnualFundamentals | None, dict]:
    """companyfacts → (AnnualFundamentals, 진단).

    진단에는 `fiscal_years`(최대 `years`개)·`completeness`(앞 5년 기준)·`missing`(지표별 결측 연도 수)와
    R03/R05/R06 근거(`delta_nwc_series`·`operating_nwc`·`shares_used`·`fundamental_sources`·
    `xbrl_acceptance_missing`)가 들어간다.
    """
    decision_dt, decision_date = _decision_bounds(decision_at)
    facts, fstats = annual_facts(payload, decision_at, acceptance)
    series = {m: {d: f.value for d, f in per.items()} for m, per in facts.items()}
    fy = fiscal_years(series, limit=years)
    diag: dict = {"fiscal_years": [d.isoformat() for d in fy], "completeness": 0.0, "missing": {},
                  "xbrl_acceptance_missing": bool(decision_dt is not None and (
                      acceptance is None or fstats["acceptance_missing"] > 0))}
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
    interest = _carry_forward(col("interest_expense"))
    pretax = col("pretax_income")
    tax = col("tax_expense")
    # 영업이익 태그가 없는 기업(제약·산업재 다수)은 세전이익 + 이자비용으로 EBIT 를 근사한다.
    # 운영이익을 별도 태그하지 않는 회사도 재무제표상 EBIT 는 이 합으로 나온다.
    ebit = [e if np.isfinite(e) else (p + i if np.isfinite(p) and np.isfinite(i) else float("nan"))
            for e, p, i in zip(ebit, pretax, interest, strict=False)]
    current_assets = col("current_assets")
    current_liabilities = col("current_liabilities")

    # R05: 결정 시점 기준 주식수 — 공시 그대로 값과 환산값·근거를 함께 남긴다.
    shares_reported = [_norm_share_count(v) for v in col("diluted_shares")]
    shares: list[float] = []
    shares_basis: list[str] = []
    shares_used: list[dict] = []
    for d, raw in zip(fy, shares_reported, strict=False):
        fact = (facts.get("diluted_shares") or {}).get(d)
        filed = fact.filed if fact is not None else None
        value, basis = _share_basis(raw, d, filed, splits, decision_date)
        shares.append(_to_millions(value))
        shares_basis.append(basis if np.isfinite(value) else "unverified")
        shares_used.append({"fy": d.isoformat(), "raw": _to_millions(raw) if np.isfinite(raw) else None,
                            "adjusted": _to_millions(value) if np.isfinite(value) else None,
                            "basis": shares_basis[-1], "accn": fact.accn if fact is not None else None,
                            "filed": fact.filed.isoformat() if (fact is not None and fact.filed) else None})

    total_debt = [_sum_finite(long_debt[i], short_debt[i]) for i in range(len(fy))]
    nil = [max(0.0, li - td) if np.isfinite(li) and np.isfinite(td) else float("nan")
           for li, td in zip(liabilities, total_debt, strict=False)]
    base_rev = revenue[0] if np.isfinite(revenue[0]) else 0.0
    excess_cash = [max(0.0, c - 0.02 * base_rev) if np.isfinite(c) else float("nan") for c in cash]

    # 유효세율 — **앞 5년** 합계 세금/세전이익, 0~35% 로 자른다(음수·이상치는 기본 21% 로).
    eff = 0.21
    pretax5, tax5 = pretax[:NORMALIZATION_YEARS], tax[:NORMALIZATION_YEARS]
    if any(np.isfinite(p) and np.isfinite(t) and p > 0 for p, t in zip(pretax5, tax5, strict=False)):
        p_sum = float(np.nansum([p for p in pretax5 if np.isfinite(p) and p > 0]))
        t_sum = float(np.nansum([t for p, t in zip(pretax5, tax5, strict=False)
                                 if np.isfinite(p) and p > 0 and np.isfinite(t)]))
        if p_sum > 0:
            eff = float(min(0.35, max(0.0, t_sum / p_sum)))

    # R03: 영업 ΔNWC 5년 시계열(6년 운전자본이 필요) + 시장성 단기투자
    cash_m = [_to_millions(v) for v in cash]
    marketable = _marketable_series(series, fy, cash_m, diag)
    revenue_m = [_to_millions(v) for v in revenue]
    nwc = operating_nwc([_to_millions(v) for v in current_assets],
                        [_to_millions(v) for v in current_liabilities], cash_m, marketable)
    dnwc = delta_nwc_series(nwc, revenue_m)

    f = AnnualFundamentals(
        ebit=[_to_millions(v) for v in ebit],
        revenue=revenue_m,
        net_income=[_to_millions(v) for v in net_income],
        ocf=[_to_millions(v) for v in ocf],
        capex=[_to_millions(v) for v in capex],
        depreciation=[_to_millions(v) for v in depreciation],
        total_assets=[_to_millions(v) for v in total_assets],
        non_interest_liabilities=[_to_millions(v) for v in nil],
        excess_cash=[_to_millions(v) for v in excess_cash],
        total_debt=[_to_millions(v) for v in total_debt],
        cash=cash_m,
        equity=[_to_millions(v) for v in equity],
        diluted_shares=shares,
        interest_expense=[_to_millions(v) for v in interest],
        tax_rate=round(eff, 4),
        marketable_securities=marketable,
        operating_nwc=nwc,
        delta_nwc=dnwc,
        diluted_shares_reported=[_to_millions(v) if np.isfinite(v) else float("nan")
                                 for v in shares_reported],
        shares_basis=shares_basis,
    )
    f.ttm_eps = _ttm_eps(payload, decision_at, acceptance, fy[0], f)
    f.eps_cagr = eps_cagr(f)

    cells = 0
    filled = 0
    missing: dict[str, int] = {}
    for dict_metric in REQUIRED_METRICS:
        vals = ({"revenue": revenue, "ebit": ebit, "net_income": net_income, "ocf": ocf,
                 "capex": capex, "depreciation": depreciation, "total_assets": total_assets,
                 "equity": equity, "diluted_shares": shares})[dict_metric]
        window = vals[:NORMALIZATION_YEARS]
        n_missing = sum(1 for v in window if not np.isfinite(v))
        missing[dict_metric] = n_missing
        cells += len(window)
        filled += len(window) - n_missing
    diag["completeness"] = round(filled / cells, 4) if cells else 0.0
    diag["missing"] = missing
    diag["years_available"] = len(fy)
    diag["nwc_series"] = dnwc[0] if dnwc else 0.0
    diag["delta_nwc_series"] = dnwc
    diag["operating_nwc"] = nwc
    diag["marketable_securities"] = marketable
    diag["shares_used"] = shares_used
    diag["eps_cagr"] = f.eps_cagr
    diag["ttm_eps"] = None if not np.isfinite(f.ttm_eps) else round(float(f.ttm_eps), 4)
    diag["fundamental_sources"] = _fundamental_sources(facts, fy)
    return f, diag


def _fundamental_sources(facts: dict[str, dict[date, Fact]], fy: list[date]) -> list[dict]:
    """회계연도별 근거 공시 — 증거 카드의 `accn`·`filed`·`available_at` 연결에 쓴다(R04)."""
    out: list[dict] = []
    for d in fy:
        fact = (facts.get("net_income") or {}).get(d) or (facts.get("revenue") or {}).get(d) \
            or (facts.get("ebit") or {}).get(d)
        if fact is None:
            out.append({"fy": d.isoformat(), "accn": None, "form": None, "filed": None,
                        "available_at": None})
            continue
        out.append({
            "fy": d.isoformat(), "accn": fact.accn, "form": fact.form,
            "filed": fact.filed.isoformat() if fact.filed else None,
            "available_at": fact.available_at.isoformat().replace("+00:00", "Z")
            if fact.available_at else None,
        })
    return out


def eps_series(f: AnnualFundamentals, years: int = NORMALIZATION_YEARS) -> list[float]:
    """희석 EPS(USD) — 최근 5년(최신순). 주식수·순이익이 양수인 해만 값이 있다."""
    out: list[float] = []
    for i in range(min(years, len(f.net_income), len(f.diluted_shares))):
        ni, sh = f.net_income[i], f.diluted_shares[i]
        out.append(float(ni / sh) if np.isfinite(ni) and np.isfinite(sh) and sh > 0 else float("nan"))
    return out


def eps_cagr(f: AnnualFundamentals, years: int = NORMALIZATION_YEARS) -> float | None:
    """5년 EPS CAGR(4년 성장, 소수). 양 끝이 양수여야 한다."""
    eps = eps_series(f, years)
    if len(eps) < years:
        return None
    return _cagr(eps[0], eps[-1], len(eps) - 1)


def _ttm_eps(payload: dict, decision_at: date | datetime,
             acceptance: dict[str, datetime | str] | None, fy_end: date,
             f: AnnualFundamentals) -> float:
    ni = _ttm_net_income(payload, decision_at, fy_end, acceptance,
                         f.net_income[0] if f.net_income else float("nan"))
    shares = f.diluted_shares[0] if f.diluted_shares else float("nan")
    if ni is None or not np.isfinite(shares) or shares <= 0:
        return float("nan")
    return float(ni / shares)


def revenue_cagr(f: AnnualFundamentals) -> float | None:
    """최근 **5개 회계연도**의 매출 CAGR(4년). 값이 없거나 시작이 0 이하면 None."""
    rev = list(f.revenue)[:NORMALIZATION_YEARS]
    if len(rev) < 5:
        return None
    return _cagr(rev[0], rev[-1], len(rev) - 1)


def fcff_cagr(f: AnnualFundamentals) -> float | None:
    """최근 5년 FCFF CAGR(§3.2.3 성장률 입력). 어느 한쪽이 0 이하면 None."""
    fcffs = normalized_fcff_series(f)
    if len(fcffs) < 2:
        return None
    return _cagr(fcffs[0], fcffs[-1], len(fcffs) - 1)


def fcff_year(f: AnnualFundamentals, i: int, delta_nwc: float | None = None) -> float:
    """연도 i 의 FCFF(백만 달러) = EBIT(1−t) + 감가상각 − CAPEX − ΔNWC."""
    ebit = f.ebit[i] if i < len(f.ebit) else float("nan")
    if not np.isfinite(ebit):
        return float("nan")
    dep = f.depreciation[i] if i < len(f.depreciation) and np.isfinite(f.depreciation[i]) else 0.0
    capex = f.capex[i] if i < len(f.capex) and np.isfinite(f.capex[i]) else 0.0
    if delta_nwc is None:
        delta_nwc = f.delta_nwc[i] if i < len(f.delta_nwc) else 0.0
    return float(ebit * (1 - f.tax_rate) + dep - capex - delta_nwc)


def normalized_fcff_series(f: AnnualFundamentals, years: int = NORMALIZATION_YEARS) -> list[float]:
    """최근 5년 FCFF(최신순). ΔNWC 는 연도별 값(없으면 0)."""
    return [fcff_year(f, i) for i in range(min(years, len(f.ebit)))]


def _median(values: Iterable[float]) -> float | None:
    vals = [float(v) for v in values if v is not None and np.isfinite(v)]
    return float(statistics.median(vals)) if vals else None


def peg_ratio(f: AnnualFundamentals, price: float, growth: float | None,
              eps: float | None = None) -> float | None:
    """PEG = PER ÷ (성장률 %p).

    §3.2.4: PER 은 **TTM EPS**, 성장률은 **5년 EPS CAGR** 이다(R03). TTM EPS 가 없으면
    FY EPS 로 폴백하고 그 사실은 `peg_basis()` 로 남긴다. EPS≤0·성장률≤0 이면 None.
    """
    if growth is None or growth <= 0 or price is None or not np.isfinite(price) or price <= 0:
        return None
    if eps is None:
        eps = f.ttm_eps if np.isfinite(getattr(f, "ttm_eps", float("nan"))) else None
        if eps is None:
            if not f.net_income or not f.diluted_shares:
                return None
            ni, sh = f.net_income[0], f.diluted_shares[0]
            if not np.isfinite(ni) or not np.isfinite(sh) or sh <= 0 or ni <= 0:
                return None
            eps = ni / sh
    if eps is None or not np.isfinite(eps) or eps <= 0:
        return None
    return float((price / eps) / (growth * 100.0))


def peg_basis(f: AnnualFundamentals) -> str:
    """PEG 의 EPS 기준 — `ttm` 이면 TTM EPS, `fy_fallback` 이면 최근 FY EPS."""
    return "ttm" if np.isfinite(getattr(f, "ttm_eps", float("nan"))) else "fy_fallback"


def peg_growth(f: AnnualFundamentals) -> tuple[float | None, str]:
    """PEG 성장률 — EPS CAGR 우선, 없으면 매출 CAGR 폴백(근거를 함께 돌려준다)."""
    g = f.eps_cagr if f.eps_cagr is not None else eps_cagr(f)
    if g is not None and np.isfinite(g) and g > 0:
        return float(g), "eps_cagr"
    rev = revenue_cagr(f)
    if rev is not None and np.isfinite(rev) and rev > 0:
        return float(rev), "revenue_cagr_fallback"
    return None, "unavailable"


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


def valuation_wacc(f: AnnualFundamentals, beta: float | None, risk_free: float,
                   cost_of_debt: float) -> float:
    """(E/V)Re + (D/V)Rd(1−t) — **가치평가(V) 경로**의 WACC (§3.2.3).

    Q 점수 경로의 `qc_wacc`(β=1.0 고정, 3.2.2)와 정의가 다르다. 베타가 없으면 1.0(시장 평균).
    """
    e = max(f.equity[0] if f.equity and np.isfinite(f.equity[0]) else 0.0, 0.0)
    d = max(f.total_debt[0] if np.isfinite(f.total_debt[0]) else 0.0, 0.0)
    v = e + d
    if v <= 0:
        return 0.09
    b = 1.0 if beta is None else float(beta)
    re = float(risk_free) + b * 0.05
    return float((e / v) * re + (d / v) * cost_of_debt * (1 - f.tax_rate))


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


def _method_median(methods: dict[str, float]) -> tuple[float | None, str | None]:
    """방법별 IV 의 중앙값과 **어떤 방법이 중앙값을 정했는지**(§3.2.3 근거 표시)."""
    if not methods:
        return None, None
    items = sorted(methods.items(), key=lambda kv: kv[1])
    values = [v for _, v in items]
    med = statistics.median(values)
    hits = [name for name, v in items if abs(v - med) <= 1e-9]
    if len(hits) == 1:
        return float(med), hits[0]
    return float(med), "median(" + ",".join(name for name, _ in items) + ")"


def _value_without_dcf(inp, sector: str | None) -> dict:
    """정상화 FCFF ≤ 0 일 때 — DCF 를 폐기하고 EPV·Graham 만으로 중앙값을 낸다(§3.2.3 확장).

    계산식은 `engine.metrics.valuation` 의 공개 함수를 그대로 쓴다(이 모듈은 새 산식을 만들지 않는다).
    """
    from engine.metrics.valuation import (epv_ev, equity_per_share, graham_number, nopat)  # noqa: PLC0415

    wacc = _clamp(inp.wacc, 0.08, 0.15)
    iv_epv = equity_per_share(epv_ev(nopat(inp.ebit, inp.tax_rate), wacc),
                              inp.net_debt, inp.minority_interest, inp.diluted_shares)
    methods: dict[str, float] = {}
    if iv_epv > 0:
        methods["epv"] = float(iv_epv)
    if inp.eps and inp.bps and inp.eps > 0 and inp.bps > 0:
        methods["graham"] = float(graham_number(inp.eps, inp.bps))
    if not methods:
        raise ValueError("정상화 FCFF ≤ 0 이고 남은 방법이 0개 — V 신뢰도 C 로도 계산 불가")
    med, median_method = _method_median(methods)
    band = statistics.median(list(methods.values()))
    return {
        "valuation_suspended": False, "reason": None, "sector": sector, "unit": "USD/share",
        "wacc": wacc, "growth": _clamp(inp.growth, 0.0, 0.15),
        "fcff_normalized": float(nopat(inp.ebit, inp.tax_rate) + inp.depreciation - inp.capex - inp.delta_nwc),
        "iv_dcf": {"base": None, "bear": None, "bull": None},
        "iv_epv": methods.get("epv"), "iv_graham": methods.get("graham"),
        "iv_median": med, "iv_low": band * 0.85, "iv_high": band * 1.15,
        "methods": methods, "method_count": len(methods),
        "v_confidence": "C",   # DCF 를 폐기했으므로 남은 방법 수와 무관하게 C
        "median_method": median_method, "iv_band_basis": "no_dcf_band",
    }


def valuation_multiplier(f: AnnualFundamentals, price: float, sector: str | None,
                         beta: float | None, risk_free: float, growth_cap: float = 0.15,
                         delta_nwc: float | None = None) -> tuple[float | None, dict]:
    """`iv_mult` = IV_median(USD/share) ÷ 현재가. 계산 불가면 (None, 사유).

    `run_screen.build_candidates` 와 `pipeline.build_views` 는 `iv = price × iv_mult` 로 쓰므로
    **주당 내재가치를 여기서 한 번만** 계산해 배수로 넘긴다(단위는 USD/share 유지).
    방법별 IV·밴드·중앙값 선정 근거를 **버리지 않고** 함께 돌려준다(R04).
    """
    from engine.metrics.valuation import ValuationInputs, classify_sector, value_company  # noqa: PLC0415

    f = sanitize_fundamentals(f)   # 스냅샷 복원값(None)도 받아들인다

    if not price or not np.isfinite(price) or price <= 0:
        return None, {"reason": "no_price"}
    if classify_sector(sector)["valuation_suspended"]:
        return None, {"reason": "SECTOR_FORMULA_PENDING"}
    if len(f.ebit) == 0 or not np.isfinite(f.ebit[0]):
        return None, {"reason": "no_ebit"}
    shares = f.diluted_shares[0] if f.diluted_shares else float("nan")
    if not np.isfinite(shares) or shares <= 0:
        return None, {"reason": "no_shares"}

    years = min(NORMALIZATION_YEARS, len(f.ebit))
    # 전부 백만 달러 단위에서 계산한다(단위 혼용 금지 — R03 의 원 결함).
    ebit_med = _median(f.ebit[:years])
    dep_med = _median(f.depreciation[:years]) or 0.0
    capex_med = _median(f.capex[:years]) or 0.0
    dnwc_vals = list(f.delta_nwc[:years]) if f.delta_nwc else [float(delta_nwc or 0.0)] * years
    nopat_norm = (ebit_med or 0.0) * (1 - f.tax_rate) if ebit_med is not None else float("nan")
    fcff_years = [fcff_year(f, i, dnwc_vals[i] if i < len(dnwc_vals) else 0.0) for i in range(years)]
    fcff_norm = _median(fcff_years)
    dnwc_norm = _median(dnwc_vals) or 0.0

    gross_growth = fcff_cagr(f)
    growth = 0.0 if gross_growth is None else _clamp(gross_growth, 0.0, growth_cap)
    growth_basis = "fcff_cagr" if gross_growth is not None else "unavailable"
    debt = f.total_debt[0] if f.total_debt and np.isfinite(f.total_debt[0]) else 0.0
    cash = f.cash[0] if f.cash and np.isfinite(f.cash[0]) else 0.0
    marketable = (f.marketable_securities[0]
                  if f.marketable_securities and np.isfinite(f.marketable_securities[0]) else 0.0)
    net_debt = float(debt - cash - marketable)   # §3.2.3: 시장성 단기투자도 차감
    interest = f.interest_expense[0] if f.interest_expense and np.isfinite(f.interest_expense[0]) else 0.0
    cost_of_debt = 0.05 if debt <= 0 else float(min(0.10, max(0.02, interest / debt)))
    eps = (f.net_income[0] / shares) if np.isfinite(f.net_income[0]) else None
    bps = (f.equity[0] / shares) if np.isfinite(f.equity[0]) else None

    # 정상화 FCFF 를 `valuation.py` 의 입력으로 옮긴다. 그 모듈은 base_fcff = EBIT(1−t)+D&A−CAPEX−ΔNWC
    # 하나만 받으므로, ΔNWC 자리에 "중앙값 FCFF 와 중앙값 구성항의 차이"를 넣어 **중앙값 FCFF** 를 만든다.
    dnwc_effective = float(nopat_norm + dep_med - capex_med - fcff_norm) if fcff_norm is not None else 0.0
    common = dict(
        ebit=float(ebit_med) if ebit_med is not None else float("nan"),
        tax_rate=f.tax_rate, depreciation=dep_med, capex=capex_med, delta_nwc=dnwc_effective,
        net_debt=net_debt, minority_interest=0.0, diluted_shares=shares,
        wacc=valuation_wacc(f, beta, risk_free, cost_of_debt),
        terminal_growth=0.025, growth=growth, eps=eps, bps=bps,
    )
    detail: dict = {
        "price": float(price),
        "diluted_shares": float(shares), "net_debt": net_debt,
        "ebit_normalized": ebit_med, "nopat_normalized": nopat_norm,
        "fcff_normalized": fcff_norm, "fcff_years": fcff_years,
        "depreciation_normalized": dep_med, "capex_normalized": capex_med,
        "delta_nwc_normalized": dnwc_norm, "delta_nwc_series": list(dnwc_vals),
        "growth": growth, "growth_basis": growth_basis, "fcff_cagr": gross_growth,
        "marketable_securities": float(marketable),
        "normalization_years": years, "tax_rate": f.tax_rate,
    }
    if fcff_norm is None or fcff_norm <= 0:
        # §3.2.3: 지분가치가 0 이하인 방법은 폐기한다 — DCF 를 빼고 나머지 방법으로 중앙값.
        try:
            res = _value_without_dcf(ValuationInputs(**common), sector)
        except (ValueError, ZeroDivisionError) as exc:
            return None, {**detail, "reason": f"FCFF_NONPOSITIVE:{type(exc).__name__}: {str(exc)[:80]}"}
        detail["dcf_dropped"] = "FCFF_NONPOSITIVE"
    else:
        try:
            res = value_company(ValuationInputs(**common), sector)
        except (ValueError, ZeroDivisionError) as exc:
            return None, {**detail, "reason": f"{type(exc).__name__}: {str(exc)[:80]}"}
    iv = res.get("iv_median")
    if iv is None or not np.isfinite(iv) or iv <= 0:
        return None, {**detail, "reason": "iv_unavailable"}
    methods = dict(res.get("methods") or {})
    med, median_method = _method_median(methods)
    detail.update({
        "iv_mult": float(iv / price),
        "iv_median": float(iv),
        "iv_low": float(res.get("iv_low")) if res.get("iv_low") is not None else None,
        "iv_high": float(res.get("iv_high")) if res.get("iv_high") is not None else None,
        "iv_dcf": {
            "base": res.get("iv_dcf"), "bear": res.get("ev_dcf_bear"), "bull": res.get("ev_dcf_bull"),
        },
        "iv_epv": res.get("iv_epv"), "iv_graham": res.get("iv_graham"),
        "methods": methods, "method_count": len(methods),
        "v_confidence": res.get("v_confidence"),
        "median_method": median_method or res.get("median_method"),
        "iv_band_basis": res.get("iv_band_basis") or "bear_bull",
        "wacc": res.get("wacc"),
    })
    # DCF bear/bull: valuation.py 는 EV(백만 달러)로 돌려주므로 같은 순부채·주식수로 주당 값을 만든다.
    if res.get("ev_dcf_bear") is not None and res.get("ev_dcf_bull") is not None:
        def _per_share(ev: float) -> float:
            return float((float(ev) - net_debt) / float(shares))
        detail["iv_dcf"] = {"base": res.get("iv_dcf"),
                          "bear": _per_share(res["ev_dcf_bear"]),
                          "bull": _per_share(res["ev_dcf_bull"])}
    return float(iv / price), detail


def coverage(payloads: Iterable[dict], decision_at: date | datetime) -> dict:
    """여러 종목 payload 의 연차 추출 성공 여부 요약(진단용)."""
    ok = 0
    rows = list(payloads)
    for p in rows:
        if fiscal_years(annual_series(p, decision_at)):
            ok += 1
    return {"tickers": len(rows), "with_annual": ok, "ratio": round(ok / len(rows), 3) if rows else 0.0}


def safe_float(value) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return out if math.isfinite(out) else float("nan")
