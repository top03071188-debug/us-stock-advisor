"""engine.data.xbrl_fundamentals — SEC XBRL companyfacts → `AnnualFundamentals`(최근 10 회계연도).

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
- ΔNWC 는 **영업 기준**((유동자산 − 현금 − 시장성단기투자) − **영업 유동부채** = 유동부채 − 단기금융부채)의
  연도별 변화이며, 각 연도 **매출의 5%** 로 자른다. 모든 단위는 **백만 달러**로 통일한다(v1.3.3 08-a).
- 순부채 = 이자부채 − 현금 − 시장성단기투자(§3.2.3). 주당 가치는 `engine.metrics.valuation` 이 계산한다
  (이 모듈은 **입력·정상화·배관**만 담당하고 계산식을 다시 쓰지 않는다).

[고정] v1.3.3 감사 수정(항목 03·05·06·07·08)
- **03**: EPS 로 주식수를 복원할 때 비교 기준(`ref_shares`)은 **그 표본 시점에 알 수 있었던** 가장 최근
  주식수다(전체 이력의 최신 회계연도를 cutoff 없이 읽지 않는다). EPS·순이익은 같은 회계연도·같은 제출본을
  우선하고, **분할 환산을 한 뒤에** 0.3~3배 범위를 비교한다. 출처는 `shares_source`
  (`reported`·`eps_implied`·`missing`)에 남긴다.
- **05**: 성장률은 `growth_raw_cagr`(클립 전)와 `growth_clipped_to_zero` 를 함께 돌려주고, 유효 FCFF 연수
  (`effective_fcff_years`)가 5년에 못 미치면 5년 DCF 경로로 점수를 주지 않는다(`dcf_dropped=SHORT_HISTORY`).
- **06**: 최신 `EV/EBIT` 은 과거 표본 루프와 **독립적으로** 마지막 봉에서 계산한다. 그 시점 재무가 무효면
  과거 표본을 "현재"로 재사용하지 않고 현재 배수·백분위를 `None`(보류)으로 둔다. `ev_ebit_window` 는
  계획 날짜가 아니라 **실제 유효 표본의 첫날~마지막날**이다.
- **07**: 접수 시각이 없으면 `engine.utils.dates.sec_conservative_available_at`(접수일 23:59:59 ET)로 폴백하고,
  연차·TTM·개정 이력 **모두** `available_at <= cutoff` 를 타임스탬프로 강제한다.
- **08**: 영업 ΔNWC 는 단기금융부채를 빼고, 총부채는 `debt_components()` 의 우선순위 하나로만 계산하며
  (중복 합산·누락·미보고/0 혼동 금지), 비지배지분을 기업가치에서 차감하고, 유효세율은 **같은 연도 쌍**만 쓴다.

단위: `AnnualFundamentals` 는 **백만 달러·백만 주**다. XBRL 은 달러·주 단위이므로 1e6 으로 나눈다.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from engine.data.sec_edgar import acceptance_available_at
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
    # v1.3.3 08-b: 총부채 우선순위용 **성분별** 태그. 합계(`long_term_debt_total`)가 유동분을 포함하므로
    # 비유동+유동+단기차입을 그대로 더하면 중복되고, 하나만 고르면 누락된다 — `debt_components()` 가 정리한다.
    "long_term_debt_total": ["LongTermDebt", "LongTermDebtAndCapitalLeaseObligations"],
    "long_term_debt_current": ["LongTermDebtCurrent"],
    "short_term_borrowings": ["ShortTermBorrowings", "OtherShortTermBorrowings"],
    "debt_current": ["DebtCurrent"],
    # v1.3.3 08-c: 비지배지분 — 모회사 귀속 자본과 구분해 기업가치에서 차감한다.
    "minority_interest": ["MinorityInterest"],
    "equity_including_nci": ["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
    "diluted_shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    # v1.3.2: 10년 EV/EBIT 백분위용 — 주식수 태그가 최근 몇 년만 있는 기업(예: GOOGL)의 폴백
    "eps_diluted": ["EarningsPerShareDiluted"],
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
    "diluted_shares", "interest_expense", "pretax_income", "tax_expense", "eps_diluted",
})

#: HG-11(XBRL 완전성 ≥ 90%) 판정에 쓰는 필수 항목 — 게이트·점수 계산이 실제로 읽는 것들.
REQUIRED_METRICS = (
    "revenue", "ebit", "net_income", "ocf", "capex", "depreciation",
    "total_assets", "equity", "diluted_shares",
)

#: v1.3.3 05: 5년 DCF 모델이 전제하는 최소 FCFF 연수. 이보다 짧으면 DCF 를 폐기한다(짧은 이력 가드).
MIN_DCF_YEARS = 5

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


def _cutoff_instant(decision_at: date | datetime | None) -> tuple[datetime | None, datetime | None]:
    """(UTC 타임스탬프 cutoff, 표시용 값) — 모든 `available_at` 비교의 **단일 기준**.

    datetime 이면 그 순간, 날짜만 주면 **그날 23:59:59 ET**(출처 시간대 하루의 끝)다.
    `None` 이면 제한 없음(수집·테스트용). 날짜 비교(`filed <= cutoff`)만으로 통과시키지 않는다(07).
    """
    if isinstance(decision_at, datetime):
        dt = decision_at if decision_at.tzinfo else decision_at.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc), dt.astimezone(timezone.utc)
    if decision_at is None:
        return None, None
    end = datetime.combine(decision_at, time(23, 59, 59), tzinfo=ZoneInfo("America/New_York"))
    return end.astimezone(timezone.utc), decision_at


def _blocked(available: datetime | None, cutoff: datetime | None) -> bool:
    """`available` 가 cutoff 뒤면 True(예외 없는 최종 타임스탬프 비교)."""
    if cutoff is None or available is None:
        return False
    return available > cutoff


def _within_a_year(start: date, end: date) -> bool:
    days = (end - start).days
    return 330 <= days <= 400


def annual_facts(payload: dict, decision_at: date | datetime,
                 acceptance: dict[str, datetime | str] | None = None
                 ) -> tuple[dict[str, dict[date, Fact]], dict]:
    """companyfacts → `{metric: {회계연도 말일: Fact}}` + 진단.

    시점 규칙(R06·v1.3.3 07): 접수 시각이 있으면 `available_at <= cutoff`, 없으면 접수일
    **23:59:59 ET**(`engine.utils.dates.sec_conservative_available_at` — `sec_edgar.acceptance_available_at`
    도 같은 함수로 위임한다)를 쓰고 **같은 타임스탬프 비교**를 거친다
    (날짜 문자열만 비교해 그날 마감 뒤 접수가 섞이지 않게).
    같은 (metric, 회계연도)는 **결정 시점까지 공개된 최신 값**을 쓰되, 태그 우선순위가 앞선 값은 덮지 않는다.
    """
    gaap = ((payload.get("facts") or {}).get("us-gaap") or {})
    decision_dt, decision_date = _decision_bounds(decision_at)
    cutoff_instant, _ = _cutoff_instant(decision_at)
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
                    # 공용 경로 하나 — 접수 시각이 있으면 그것, 없으면 접수일 23:59:59 ET(감사 07).
                    available = acceptance_available_at(filed_raw, acc)
                    if acc is None and decision_dt is not None:
                        stats["acceptance_missing"] += 1
                    if _blocked(available, cutoff_instant):
                        continue
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
def debt_components(values: dict[str, float | None]) -> dict:
    """한 회계연도의 **이자부채 성분** → `{total, long_term, short_term, current_financial, policy, reported}`.

    v1.3.3 08-b: 태그를 순서대로 하나만 고르면(`LongTermDebtCurrent` vs `ShortTermBorrowings`) 누락되고,
    전부 더하면 `LongTermDebt`(유동분 포함)와 `LongTermDebtCurrent` 가 **중복**된다. 그래서 우선순위를 하나로 정한다.

    1. **장기부채**: `LongTermDebt`(또는 `…AndCapitalLeaseObligations`, 유동분 포함 합계)가 보고되면 그것을 쓴다
       (`policy="total_includes_current"`). 없으면 `LongTermDebtNoncurrent` + `LongTermDebtCurrent` 를 더한다
       (`policy="components_summed"`).
    2. **단기차입**: `DebtCurrent`(유동부채 중 이자부채 합계)가 있으면 **이미 포함된 유동성장기부채를 빼서**
       단기차입만 남긴다. 없으면 `ShortTermBorrowings` 를 쓴다.
    3. `current_financial` = **유동부채 안에 있는 금융부채**(유동성장기부채 + 단기차입) — 영업 ΔNWC 에서 뺄 값.
    4. 이자부채 태그가 하나도 없으면 `total=nan`, `reported=False` — **미보고와 0 을 구분**한다.

    단위는 입력과 같다(호출자가 백만 달러로 바꿔 쓴다).
    """
    def _v(key: str) -> float | None:
        value = values.get(key)
        if value is None:
            return None
        out = float(value)
        return out if np.isfinite(out) else None

    total_lt = _v("long_term_debt_total")
    noncurrent = _v("long_term_debt_noncurrent")
    current_lt = _v("long_term_debt_current")
    st_borrow = _v("short_term_borrowings")
    debt_current = _v("debt_current")

    reported = any(v is not None for v in (total_lt, noncurrent, current_lt, st_borrow, debt_current))
    if not reported:
        return {"total": float("nan"), "long_term": float("nan"), "short_term": float("nan"),
                "current_financial": float("nan"), "policy": "missing", "reported": False}

    if total_lt is not None:
        long_term = total_lt
        policy = "total_includes_current"
        current_lt_counted = current_lt   # 합계 안에 이미 들어 있다
    else:
        long_term = float(sum(v for v in (noncurrent, current_lt) if v is not None)) \
            if any(v is not None for v in (noncurrent, current_lt)) else 0.0
        policy = "components_summed"
        current_lt_counted = current_lt

    if debt_current is not None:
        # `DebtCurrent` 는 유동성장기부채까지 포함한 유동 이자부채 합계다 — 이미 센 부분을 뺀다.
        short_term = max(0.0, debt_current - (current_lt_counted or 0.0))
    elif st_borrow is not None:
        short_term = st_borrow
    else:
        short_term = 0.0

    current_financial = (current_lt_counted or 0.0) + short_term
    return {
        "total": float(long_term + short_term),
        "long_term": float(long_term),
        "short_term": float(short_term),
        "current_financial": float(current_financial),
        "policy": policy,
        "reported": True,
    }


def _debt_values_from_facts(facts: dict[str, dict[date, Fact]], fy: date) -> dict:
    """`annual_facts`/`annual_series` 결과에서 **한 회계연도의 부채 성분**을 꺼내 `debt_components` 로 넘긴다.

    `long_term_debt` 지표는 태그 우선순위상 `LongTermDebtNoncurrent` 가 먼저지만, 그 태그가 없는 기업은
    `LongTermDebt`(유동분 포함 합계)가 들어온다 — **어느 태그였는지**로 성분을 가른다.
    """
    def _get(metric: str) -> tuple[float | None, str | None]:
        fact = (facts.get(metric) or {}).get(fy)
        if fact is None:
            return None, None
        value = float(fact.value)
        return (value if np.isfinite(value) else None), fact.tag

    total, total_tag = _get("long_term_debt_total")
    long_term_metric, long_term_tag = _get("long_term_debt")
    if total is None and long_term_metric is not None and long_term_tag != "LongTermDebtNoncurrent":
        total = long_term_metric           # `LongTermDebt` = 유동분 포함 합계
    noncurrent = long_term_metric if long_term_tag == "LongTermDebtNoncurrent" else None
    current_lt, _ = _get("long_term_debt_current")
    st_borrow, _ = _get("short_term_borrowings")
    debt_current, _ = _get("debt_current")
    return debt_components({
        "long_term_debt_total": total,
        "long_term_debt_noncurrent": noncurrent,
        "long_term_debt_current": current_lt,
        "short_term_borrowings": st_borrow,
        "debt_current": debt_current,
    })


def _debt_values_visible(revisions: dict[str, dict[date, list[dict]]], fy: date,
                         cutoff: datetime) -> dict:
    """시점 `cutoff` 에 알 수 있었던 한 회계연도의 부채 성분 — 역사 `EV/EBIT` 경로용(같은 우선순위)."""
    def _get(metric: str) -> float | None:
        rev = _visible(revisions, metric, fy, cutoff)
        if rev is None:
            return None
        value = float(rev["value"])
        return value if np.isfinite(value) else None

    total = _get("long_term_debt_total")
    return debt_components({
        "long_term_debt_total": total,
        "long_term_debt_noncurrent": _get("long_term_debt"),
        "long_term_debt_current": _get("long_term_debt_current"),
        "short_term_borrowings": _get("short_term_borrowings"),
        "debt_current": _get("debt_current"),
    })


def _debt_row_at_series(facts: dict[str, dict[date, Fact]], fy: date) -> dict:
    """현재 경로(`build_fundamentals`)용 얇은 별칭."""
    return _debt_values_from_facts(facts, fy)


def operating_nwc(current_assets: list[float], current_liabilities: list[float],
                  cash: list[float], marketable: list[float],
                  short_term_debt: list[float] | None = None) -> list[float]:
    """영업 운전자본(백만 달러) = (유동자산 − 현금 − 시장성단기투자) − **영업 유동부채**.

    금융항목(현금·시장성 증권)을 빼야 ΔNWC 가 "영업"의 변화가 된다 — GOOGL 2025 는
    (유동자산−유동부채) 변화가 +28,704 이지만 현금·시장성 증권이 만든 변화라 영업 ΔNWC 는 −2,482 다.
    부채 쪽도 같다(v1.3.3 08-a): **단기차입금·유동성장기부채는 영업 유동부채가 아니므로** 빼지 않으면
    차입 한 도에 FCFF 가 흔들린다(단기차입 +100 → NWC −100 → ΔNWC −100 → FCFF +100).
    `short_term_debt` 가 없으면 기존 동작(0)으로 돌아간다.
    """
    out: list[float] = []
    std = list(short_term_debt or [])
    for i, (ca, cl, c, m) in enumerate(zip(current_assets, current_liabilities, cash, marketable,
                                          strict=False)):
        if not (np.isfinite(ca) and np.isfinite(cl)):
            out.append(float("nan"))
            continue
        cc = float(c) if np.isfinite(c) else 0.0
        mm = float(m) if np.isfinite(m) else 0.0
        sd = float(std[i]) if i < len(std) and np.isfinite(std[i]) and float(std[i]) > 0 else 0.0
        operating_liabilities = max(0.0, float(cl) - sd)
        out.append(float(ca) - cc - mm - operating_liabilities)
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
    cutoff_instant, _ = _cutoff_instant(decision_at)
    flows: list[tuple[date, date, float]] = []
    for e in rows:
        if str(e.get("form") or "") not in ("10-K", "10-Q", "10-K/A", "10-Q/A"):
            continue
        start, end = _as_date(e.get("start")), _as_date(e.get("end"))
        val = e.get("val")
        if start is None or end is None or val is None:
            continue
        filed_raw = str(e.get("filed") or "")
        if not filed_raw:
            continue
        # v1.3.3 07: 접수 시각이 없으면 접수일 23:59:59 ET 로 폴백하고 **타임스탬프 비교**를 강제한다.
        acc = (acceptance or {}).get(str(e.get("accn") or ""))
        available = acceptance_available_at(filed_raw, acc)
        if _blocked(available, cutoff_instant):
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
                       years: int = 10, splits: list[dict] | None = None
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
    # 이자부채는 `debt_components()` 로만 계산한다(아래) — 태그 우선순위를 두 곳에 두지 않는다.
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

    # v1.3.3 08-b: 총부채·유동금융부채는 **`debt_components()` 한 경로**로만 계산한다(우선순위·중복 방지).
    debt_rows = [_debt_row_at_series(facts, d) for d in fy]
    total_debt = [r["total"] for r in debt_rows]                     # 원 단위(달러) — 아래에서 백만 달러로 바꿔 넣는다
    current_financial = [r["current_financial"] for r in debt_rows]
    # 이자부채 태그가 아예 없는 해(제약·서비스 일부)는 가장 최근에 보고된 값으로 채운다(0 으로 만들지 않는다).
    total_debt = _carry_forward(total_debt)
    current_financial = _carry_forward(current_financial)
    diag["debt_policy"] = [r["policy"] for r in debt_rows]
    diag["debt_reported"] = [bool(r["reported"]) for r in debt_rows]
    nil = [max(0.0, li - td) if np.isfinite(li) and np.isfinite(td) else float("nan")
           for li, td in zip(liabilities, total_debt, strict=False)]
    base_rev = revenue[0] if np.isfinite(revenue[0]) else 0.0
    excess_cash = [max(0.0, c - 0.02 * base_rev) if np.isfinite(c) else float("nan") for c in cash]

    # 유효세율 — **같은 연도 쌍**(세전이익·세금이 **둘 다** 있는 해)만 합산한다(v1.3.3 08-d).
    # 세금 태그가 3년치뿐인데 세전이익 5년을 분모로 두면 0.20 → 0.12 로 내려간다.
    eff = 0.21
    pretax5, tax5 = pretax[:NORMALIZATION_YEARS], tax[:NORMALIZATION_YEARS]
    pairs = [(float(p), float(t)) for p, t in zip(pretax5, tax5, strict=False)
             if np.isfinite(p) and np.isfinite(t) and float(p) > 0]
    if pairs:
        p_sum = float(sum(p for p, _ in pairs))
        t_sum = float(sum(t for _, t in pairs))
        if p_sum > 0:
            eff = float(min(0.35, max(0.0, t_sum / p_sum)))
    diag["tax_pairs"] = len(pairs)

    # v1.3.3 08-c: 비지배지분 — `MinorityInterest` 우선, 없으면 (총자본 − 모회사 귀속자본)으로 복원.
    nci_raw = col("minority_interest")
    equity_nci = col("equity_including_nci")
    nci = [n if np.isfinite(n) else (e_n - e if np.isfinite(e_n) and np.isfinite(e) else float("nan"))
           for n, e_n, e in zip(nci_raw, equity_nci, equity, strict=False)]
    minority_interest = max(0.0, _to_millions(nci[0])) if np.isfinite(nci[0]) else 0.0

    # R03: 영업 ΔNWC 5년 시계열(6년 운전자본이 필요) + 시장성 단기투자
    cash_m = [_to_millions(v) for v in cash]
    marketable = _marketable_series(series, fy, cash_m, diag)
    revenue_m = [_to_millions(v) for v in revenue]
    # v1.3.3 08-a: 유동부채에서 **단기금융부채(단기차입·유동성장기부채)** 를 뺀 영업 유동부채만 쓴다.
    nwc = operating_nwc([_to_millions(v) for v in current_assets],
                        [_to_millions(v) for v in current_liabilities], cash_m, marketable,
                        [_to_millions(v) for v in current_financial])
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
        minority_interest=minority_interest,
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


def annual_revisions(payload: dict, acceptance: dict[str, datetime | str] | None = None,
                     decision_at: date | datetime | None = None
                     ) -> dict[str, dict[date, list[dict]]]:
    """companyfacts → `{metric: {회계연도 말일: [개정 이력]}}`.

    `annual_facts` 는 결정 시점의 **최신 값 하나**만 남기지만, 10년 시계열을 과거 시점마다
    재현하려면 "그 시점에 알 수 있었던 값"이 필요하다. 그래서 여기서는 같은 태그·기간·form
    필터를 쓰되 **모든 개정(10-K/A·재작성 비교치 포함)을 `available_at` 과 함께** 남긴다.

    `decision_at` 을 주면 **그 이후에 공개된 개정은 버린다**(v1.3.3 07 — 최종 타임스탬프 강제).
    표본 시점별 컷은 호출자가 `_visible(..., cutoff)` 로 다시 건다.
    """
    gaap = ((payload.get("facts") or {}).get("us-gaap") or {})
    cutoff_instant, _ = _cutoff_instant(decision_at)
    out: dict[str, dict[date, list[dict]]] = {}
    for metric, tags in ANNUAL_TAGS.items():
        per: dict[date, list[dict]] = {}
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
                        continue
                    try:
                        value = float(val)
                    except (TypeError, ValueError):
                        continue
                    accn = str(e.get("accn") or "") or None
                    acc = (acceptance or {}).get(accn) if accn else None
                    # v1.3.3 07: 접수 시각이 없으면 접수일 **23:59:59 ET** (UTC 23:59:59 가 아니라 하루 끝)
                    available = acceptance_available_at(filed_raw, acc)
                    if _blocked(available, cutoff_instant):
                        continue
                    per.setdefault(end, []).append({
                        "available_at": available, "value": value, "rank": rank,
                        "accn": accn, "filed": _as_date(filed_raw),
                    })
        if per:
            out[metric] = per
    return out


def _visible(revisions: dict[str, dict[date, list[dict]]], metric: str, fy: date,
             cutoff: datetime) -> dict | None:
    """`fy` 회계연도의 `metric` 중 **`cutoff` 까지 공개된 최신 값**(같으면 태그 우선순위)."""
    best: dict | None = None
    for rev in (revisions.get(metric) or {}).get(fy, []):
        if rev["available_at"] > cutoff:
            continue
        if (best is None or rev["available_at"] > best["available_at"]
                or (rev["available_at"] == best["available_at"] and rev["rank"] < best["rank"])):
            best = rev
    return best


def _visible_any(revisions: dict[str, dict[date, list[dict]]], metric: str, fy: date,
                 cutoff: datetime) -> list[dict]:
    """`fy` 회계연도의 `metric` 중 `cutoff` 까지 공개된 **모든** 개정(최신순)."""
    rows = [rev for rev in (revisions.get(metric) or {}).get(fy, []) if rev["available_at"] <= cutoff]
    return sorted(rows, key=lambda r: (r["available_at"], -r["rank"]), reverse=True)


def ref_shares_at(revisions: dict[str, dict[date, list[dict]]], cutoff: datetime,
                  splits: list[dict] | None, decision_date: date) -> tuple[float | None, dict]:
    """시점 `cutoff` 에 **알 수 있었던 가장 최근 유효 주식수**(백만 주, 결정 시점 분할 기준).

    v1.3.3 03: 예전에는 전체 개정 이력의 **최신 회계연도 마지막 행**을 cutoff 없이 읽어,
    결정일 **이후** 제출된 10-K/A 가 과거 표본의 주식수 검사 기준을 바꿨다(미래 정보 참조).
    이제는 표본 시점마다 그 시점까지 공개된 최신 회계연도의 주식수를 쓰고, **분할 환산 후** 값을 돌려준다.
    """
    for fy in sorted(revisions.get("diluted_shares") or {}, reverse=True):
        rev = _visible(revisions, "diluted_shares", fy, cutoff)
        if rev is None:
            continue
        raw = _norm_share_count(float(rev["value"]))
        if not np.isfinite(raw) or raw <= 0:
            continue
        value, basis = _share_basis(raw, fy, rev.get("filed"), splits, decision_date)
        if np.isfinite(value) and value > 0:
            return _to_millions(value), {"fy": fy.isoformat(), "basis": basis,
                                         "accn": rev.get("accn")}
    return None, {}


def _paired_eps_ni(revisions: dict[str, dict[date, list[dict]]], fy: date,
                   cutoff: datetime) -> tuple[dict, dict, str] | None:
    """같은 회계연도의 **희석 EPS·순이익** — 같은 제출본을 우선하고, 없으면 각각의 cutoff 최신 개정.

    v1.3.3 03: 서로 다른 제출본의 EPS 와 순이익을 나누면 그 사이 개정·재작성이 섞여 주식수가 틀어진다.
    """
    eps_rows = _visible_any(revisions, "eps_diluted", fy, cutoff)
    ni_rows = _visible_any(revisions, "net_income", fy, cutoff)
    if not eps_rows or not ni_rows:
        return None
    ni_by_accn = {r["accn"]: r for r in ni_rows if r.get("accn")}
    same = [e for e in eps_rows if e.get("accn") and e["accn"] in ni_by_accn]
    if same:
        return same[0], ni_by_accn[same[0]["accn"]], "same_filing"
    return eps_rows[0], ni_rows[0], "latest_revision"


def _fy_row_at(revisions: dict[str, dict[date, list[dict]]], fy: date, cutoff: datetime,
               splits: list[dict] | None, decision_date: date,
               ref_shares: float | None = None, *, require_core: bool = False) -> dict:
    """시점 `cutoff` 에 알 수 있었던 `fy` 의 `EBIT·순부채·주식수`(백만 달러·백만 주) + 근거·유효성.

    주식수: 공시 태그가 있으면 `reported`, 없으면 `순이익 / 희석 EPS` 로 복원해 `eps_implied`,
    둘 다 안 되면 `missing`(v1.3.3 03 — 출처를 `shares_source` 에 남긴다).
    복원값은 **분할 환산 뒤** `ref_shares`(그 시점에 알려진 최근 주식수)의 0.3~3배와 비교한다.

    순부채는 `debt_components()` 로만 계산하므로 중복·누락이 없다.
    `require_core=True`(현재 배수용)면 부채·현금 같은 핵심 항목의 **결측을 무효**로 본다.
    과거 표본(`False`)에서는 결측을 0 으로 보고 `debt_policy` 에 `missing_as_zero` 로 남겨
    "모른다"와 "0"을 구분한다(이력 전체를 날려 백분위를 통째로 잃지 않게 하기 위해서다).
    """
    out: dict = {"fy": fy, "ebit": float("nan"), "net_debt": float("nan"),
                 "diluted_shares": float("nan"), "shares_source": "missing",
                 "shares_basis": None, "eps_pairing": None, "valid": False, "reason": None}
    ebit = _visible(revisions, "ebit", fy, cutoff)
    ebit_value = ebit["value"] if ebit else None
    if ebit_value is None:
        pretax = _visible(revisions, "pretax_income", fy, cutoff)
        interest = _visible(revisions, "interest_expense", fy, cutoff)
        if pretax is not None and interest is not None:
            ebit_value = pretax["value"] + interest["value"]
    if ebit_value is None:
        out["reason"] = "no_ebit"
        return out

    shares_value: float | None = None
    share_filed = None
    shares_rev = _visible(revisions, "diluted_shares", fy, cutoff)
    if shares_rev is not None and float(shares_rev["value"]) > 0:
        shares_value = _norm_share_count(float(shares_rev["value"]))
        share_filed = shares_rev.get("filed")
        out["shares_source"] = "reported"
    else:
        paired = _paired_eps_ni(revisions, fy, cutoff)
        if paired is not None:
            eps, ni, pairing = paired
            out["eps_pairing"] = pairing
            if float(eps["value"]) > 0 and float(ni["value"]) > 0:
                implied = float(ni["value"]) / float(eps["value"])       # 주 단위
                # ♀ 분할 환산 **후**에 기준 주식수와 비교한다(환산 전 비교는 기준이 섞인다).
                implied_adj, basis = _share_basis(implied, fy, eps.get("filed"), splits, decision_date)
                ratio_ok = True
                if ref_shares is not None and ref_shares > 0 and np.isfinite(implied_adj):
                    ratio_ok = 0.3 <= (implied_adj / 1e6) / float(ref_shares) <= 3.0
                if np.isfinite(implied_adj) and implied_adj > 0 and ratio_ok:
                    shares_value = implied
                    share_filed = eps.get("filed")
                    out["shares_source"] = "eps_implied"
                    out["shares_basis"] = basis
                    out["implied_ratio"] = round((implied_adj / 1e6) / float(ref_shares), 4) \
                        if (ref_shares and ref_shares > 0) else None
    if shares_value is None or not np.isfinite(shares_value) or shares_value <= 0:
        out["reason"] = "no_shares"
        return out

    debt = _debt_values_visible(revisions, fy, cutoff)
    cash_rev = _visible(revisions, "cash", fy, cutoff)
    if not debt["reported"] and require_core:
        out["reason"] = "no_debt"
        return out
    if cash_rev is None and require_core:
        out["reason"] = "no_cash"
        return out
    cash = float(cash_rev["value"]) if cash_rev is not None else 0.0
    debt_total = float(debt["total"]) if debt["reported"] else 0.0
    if not debt["reported"]:
        out["debt_policy"] = "missing_as_zero"
    marketable = _visible(revisions, "marketable_securities", fy, cutoff)
    if marketable is None:
        agg = _visible(revisions, "cash_and_st_investments", fy, cutoff)
        marketable_value = float(agg["value"]) - cash if (agg is not None and float(agg["value"]) >= cash) else 0.0
    else:
        marketable_value = float(marketable["value"])
    shares_adj, basis = _share_basis(shares_value, fy, share_filed, splits, decision_date)
    if not np.isfinite(shares_adj) or shares_adj <= 0:
        out["reason"] = "no_shares"
        return out
    out.update({
        "ebit": _to_millions(float(ebit_value)),
        "net_debt": _to_millions(debt_total - cash - marketable_value),
        "diluted_shares": _to_millions(shares_adj),
        "shares_basis": out["shares_basis"] or basis,
        "debt_policy": out.get("debt_policy") or debt["policy"],
        "valid": True,
    })
    return out


def ev_ebit_history(payload: dict, frame, decision_at: date | datetime,
                    acceptance: dict[str, datetime | str] | None = None,
                    splits: list[dict] | None = None, *, min_valid_years: int = 3,
                    step_days: int = 21, lookback_years: int = 10,
                    include_samples: bool = False) -> dict:
    """10년 역사적 `EV/EBIT` 분포 → 현재 백분위·중앙값·역사적 내재 주당가치 입력.

    [고정] 룩어헤드 차단: 스냅샷 날짜 `t` 의 **그 봉이 공개된 시각**(`available_at`)까지 접수된
    재무만 쓴다. 값도 그 시점에 보이던 개정본을 고르므로, 나중에 제출된 10-K·재작성이
    과거 판단에 섞이지 않는다. 시가총액은 `sadj_close(t) × 주식수(결정 시점 기준)` — 가격도
    주식수도 같은 분할 조정 기준이라 과거 시점에도 일관된다.

    유효 이력이 `min_valid_years`(3)년 미만이면 계산하지 않고 `None` 을 돌려준다
    (6~9년차 상장 종목은 그 이력으로 계산하고, 정말 짧은 이력만 폴백한다).
    """
    empty = {"ev_ebit_percentile": None, "ev_ebit_current": None, "ev_ebit_median_10y": None,
             "ev_ebit_samples": 0, "ev_ebit_years": 0, "ev_ebit_window": None,
             "iv_hist_ev_ebit": None, "reason": "insufficient_history",
             "ev_ebit_reason": "insufficient_history"}
    if frame is None or getattr(frame, "empty", True) or "sadj_close" not in frame.columns:
        return empty
    cutoff_decision, _ = _cutoff_instant(decision_at)
    revisions = annual_revisions(payload, acceptance, decision_at)
    # EBIT 태그가 없는 기업(제약·산업재 다수)은 세전이익+이자비용으로 근사를 쓰므로 회계연도 후보에 함께 넣는다.
    fiscal_years = sorted(set(revisions.get("ebit") or {}) | set(revisions.get("pretax_income") or {}))
    if not fiscal_years:
        return empty
    decision_date = decision_at.date() if isinstance(decision_at, datetime) else decision_at

    prices = frame[["sadj_close", "available_at"]].dropna(subset=["sadj_close"])
    if prices.empty:
        return empty
    window_start = pd.Timestamp(prices.index[-1]) - pd.DateOffset(years=lookback_years)
    if prices.index[0] > window_start:
        window_start = prices.index[0]
    window = prices.loc[prices.index >= window_start]
    if window.empty:
        return empty
    picks = list(window.index[::max(1, int(step_days))])
    if picks[-1] != window.index[-1]:
        picks.append(window.index[-1])

    samples: list[float] = []
    sample_rows: list[dict] = []
    used_years: set[date] = set()
    current: float | None = None
    def _bar_cutoff(t) -> datetime:
        bar = prices.loc[t]
        cutoff = pd.Timestamp(bar["available_at"])
        return (cutoff.tz_localize("UTC") if cutoff.tzinfo is None
                else cutoff.tz_convert("UTC")).to_pydatetime()

    def _row_at(t, cutoff: datetime, *, allow_older: bool = True, require_core: bool = False) -> dict:
        """그 봉 시점의 재무 행 — 주식수 기준도 **그 시점**에 있던 값(v1.3.3 03).

        `allow_older=True`(과거 표본)면 최신 유효 회계연도까지 거슬러 올라가고,
        `False`(현재 배수)면 **그 봉 시점의 최신 회계연도 하나만** 본다 — 오래된 연도를 "현재"로 쓰지 않는다.
        `require_core=True` 면 부채·현금 결측을 무효로 본다(현재 배수).
        """
        ref, _ = ref_shares_at(revisions, cutoff, splits, decision_date)
        row: dict = {"valid": False, "reason": "no_fy"}
        for fy in sorted((d for d in fiscal_years if d < t.date()), reverse=True):
            row = _fy_row_at(revisions, fy, cutoff, splits, decision_date, ref, require_core=require_core)
            if row["valid"] or not allow_older:
                return row
        return row

    def _ratio(price: float, row: dict) -> float | None:
        ebit_m, nd, sh = row["ebit"], row["net_debt"], row["diluted_shares"]
        if not (np.isfinite(ebit_m) and ebit_m > 0 and np.isfinite(sh) and sh > 0 and np.isfinite(nd)):
            return None
        ratio = (float(price) * float(sh) + float(nd)) / float(ebit_m)
        return float(ratio) if np.isfinite(ratio) and ratio > 0 else None

    samples: list[float] = []
    sample_rows: list[dict] = []
    used_years: set[date] = set()
    valid_dates: list[pd.Timestamp] = []
    for t in picks:
        cutoff = _bar_cutoff(t)
        if cutoff_decision is not None and cutoff > cutoff_decision:
            continue
        row = _row_at(t, cutoff)
        if not row["valid"]:
            continue
        ratio = _ratio(float(prices.loc[t]["sadj_close"]), row)
        if ratio is None:
            continue
        samples.append(ratio)
        used_years.add(row["fy"])
        valid_dates.append(pd.Timestamp(t))
        if include_samples:
            sample_rows.append({"date": t.date().isoformat(), "fy": row["fy"].isoformat(),
                                "ev_ebit": round(ratio, 4), "ebit": row["ebit"],
                                "shares": row["diluted_shares"], "net_debt": row["net_debt"],
                                "shares_source": row["shares_source"],
                                "shares_basis": row["shares_basis"],
                                "eps_pairing": row["eps_pairing"],
                                "debt_policy": row.get("debt_policy")})

    # ♀ v1.3.3 06: 최신 배수는 **과거 표본 루프와 독립적으로** 마지막 봉에서 계산한다.
    # 그 시점 재무가 유효하지 않으면(EBIT≤0·부채/현금/주식수 결측) 과거 표본을 "현재"로 재사용하지 않는다.
    last_t = prices.index[-1]
    last_cutoff = _bar_cutoff(last_t)
    last_row = _row_at(last_t, last_cutoff, allow_older=False, require_core=True)
    current: float | None = None
    current_reason: str | None = None
    if cutoff_decision is not None and last_cutoff > cutoff_decision:
        current_reason = "current_bar_after_cutoff"
    elif not last_row["valid"]:
        current_reason = f"current_{last_row['reason'] or 'invalid'}"
    else:
        current = _ratio(float(prices.loc[last_t]["sadj_close"]), last_row)
        if current is None:
            current_reason = "current_ebit_nonpositive"

    window_span = (f"{valid_dates[0].date().isoformat()}~{valid_dates[-1].date().isoformat()}"
                   if valid_dates else None)
    if len(used_years) < min_valid_years or len(samples) < 12:
        return {**empty, "ev_ebit_samples": len(samples), "ev_ebit_years": len(used_years),
                "ev_ebit_window": window_span,
                "ev_ebit_current": None if current is None else round(current, 4),
                **(dict(sample_rows=sample_rows) if include_samples else {})}

    median = float(statistics.median(samples))
    # 동률(`<=`) 포함 — 같은 배수가 여러 번 나오면 전부 "이하"로 센다(v1.3.3 06 명시).
    percentile = (round(100.0 * sum(1 for r in samples if r <= current) / len(samples), 2)
                  if current is not None else None)
    return {
        "ev_ebit_percentile": percentile,
        "ev_ebit_current": None if current is None else round(current, 4),
        "ev_ebit_median_10y": round(median, 4),
        "ev_ebit_samples": len(samples),
        "ev_ebit_years": len(used_years),
        "ev_ebit_window": window_span,
        "iv_hist_ev_ebit": None,   # 호출자가 정상화 EBIT·순부채·주식수로 채운다
        "reason": current_reason,
        "ev_ebit_reason": current_reason,
        **(dict(sample_rows=sample_rows) if include_samples else {}),
    }


def historical_iv(median_ev_ebit: float | None, ebit_norm: float | None, net_debt: float | None,
                  diluted_shares: float | None) -> float | None:
    """`iv_hist_ev_ebit = (median EV/EBIT × 정상화 EBIT − 순부채) ÷ 희석주식수` (USD/share)."""
    vals = (median_ev_ebit, ebit_norm, net_debt, diluted_shares)
    if any(v is None or not np.isfinite(float(v)) for v in vals):
        return None
    if float(median_ev_ebit) <= 0 or float(ebit_norm) <= 0 or float(diluted_shares) <= 0:
        return None
    return float((float(median_ev_ebit) * float(ebit_norm) - float(net_debt)) / float(diluted_shares))


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


def _value_without_dcf(inp, sector: str | None, params=None) -> dict:
    """정상화 FCFF ≤ 0 또는 **짧은 이력** 일 때 — DCF 를 폐기하고 EPV·Graham 만으로 중앙값을 낸다(§3.2.3 확장).

    계산식은 `engine.metrics.valuation` 의 공개 함수를 그대로 쓴다(이 모듈은 새 산식을 만들지 않는다).
    """
    from engine.metrics.valuation import (epv_ev, equity_per_share, graham_number, nopat,  # noqa: PLC0415
                                          valuation_params)

    p = params or valuation_params()
    wacc = _clamp(inp.wacc, p.discount_rate_min, p.discount_rate_max)
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
        "wacc": wacc, "growth": _clamp(inp.growth, 0.0, p.growth_cap),
        "fcff_normalized": float(nopat(inp.ebit, inp.tax_rate) + inp.depreciation - inp.capex - inp.delta_nwc),
        "iv_dcf": None,   # DCF 폐기 — 호출자가 {base,bear,bull}=None 으로 감싼다
        "iv_epv": methods.get("epv"), "iv_graham": methods.get("graham"),
        "iv_median": med, "iv_low": band * 0.85, "iv_high": band * 1.15,
        "methods": methods, "method_count": len(methods),
        "v_confidence": "C",   # DCF 를 폐기했으므로 남은 방법 수와 무관하게 C
        "median_method": median_method, "iv_band_basis": "no_dcf_band",
    }


def valuation_multiplier(f: AnnualFundamentals, price: float, sector: str | None,
                         beta: float | None, risk_free: float, growth_cap: float | None = None,
                         delta_nwc: float | None = None, hist: dict | None = None,
                         params=None) -> tuple[float | None, dict]:
    """`iv_mult` = IV_median(USD/share) ÷ 현재가. 계산 불가면 (None, 사유).

    `run_screen.build_candidates` 와 `pipeline.build_views` 는 `iv = price × iv_mult` 로 쓰므로
    **주당 내재가치를 여기서 한 번만** 계산해 배수로 넘긴다(단위는 USD/share 유지).
    방법별 IV·밴드·중앙값 선정 근거를 **버리지 않고** 함께 돌려준다(R04).

    `params` 는 `engine.metrics.valuation.ValuationParams`(기본: `config/params_v001.yaml`).
    v1.3.3: 성장률 클립 투명화(05)·짧은 이력 가드(05)·비지배지분 차감(08-c)·부채 미보고 표시(08-b).
    """
    from engine.metrics.valuation import (  # noqa: PLC0415
        ValuationInputs, classify_sector, dcf_rate_sensitivity, dcf_sensitivity,
        valuation_params, value_company,
    )

    p = params or valuation_params()
    cap = p.growth_cap if growth_cap is None else growth_cap
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

    # v1.3.3 05: 유효한 FCFF 연수 — 5년 모델에 못 미치면 DCF 를 폐기한다(짧은 이력 가드).
    fcff_window = [fcff_year(f, i) for i in range(min(MIN_DCF_YEARS, len(f.ebit)))]
    effective_fcff_years = sum(1 for v in fcff_window if np.isfinite(v))
    normalization_method = ("adjusted" if effective_fcff_years >= MIN_DCF_YEARS
                           else "unadjusted_median")

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
    growth = 0.0 if gross_growth is None else _clamp(gross_growth, 0.0, cap)
    # v1.3.3 05: 클립 전 원 CAGR 과 "0% 하한 적용" 사실을 함께 남긴다(화면 문구가 이것을 읽는다).
    growth_clipped_to_zero = bool(gross_growth is not None and np.isfinite(gross_growth) and gross_growth <= 0.0)
    growth_basis = "fcff_cagr" if gross_growth is not None else "unavailable"
    debt_value = f.total_debt[0] if f.total_debt else float("nan")
    debt_reporting = "reported" if np.isfinite(debt_value) else "missing"
    debt = float(debt_value) if np.isfinite(debt_value) else 0.0
    cash = f.cash[0] if f.cash and np.isfinite(f.cash[0]) else 0.0
    marketable = (f.marketable_securities[0]
                  if f.marketable_securities and np.isfinite(f.marketable_securities[0]) else 0.0)
    net_debt = float(debt - cash - marketable)   # §3.2.3: 시장성 단기투자도 차감
    minority = float(f.minority_interest) if np.isfinite(getattr(f, "minority_interest", 0.0) or 0.0) else 0.0
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
        net_debt=net_debt, minority_interest=minority, diluted_shares=shares,
        wacc=valuation_wacc(f, beta, risk_free, cost_of_debt),
        terminal_growth=p.terminal_growth, growth=growth, eps=eps, bps=bps,
    )
    detail: dict = {
        "price": float(price),
        "diluted_shares": float(shares), "net_debt": net_debt, "minority_interest": minority,
        "debt_reporting": debt_reporting,
        "ebit_normalized": ebit_med, "nopat_normalized": nopat_norm,
        "fcff_normalized": fcff_norm, "fcff_years": fcff_years,
        "depreciation_normalized": dep_med, "capex_normalized": capex_med,
        "delta_nwc_normalized": dnwc_norm, "delta_nwc_series": list(dnwc_vals),
        "growth": growth, "growth_basis": growth_basis, "fcff_cagr": gross_growth,
        "growth_raw_cagr": None if gross_growth is None else round(float(gross_growth), 6),
        "growth_clipped_to_zero": growth_clipped_to_zero,
        "effective_fcff_years": effective_fcff_years, "normalization_method": normalization_method,
        "marketable_securities": float(marketable),
        "normalization_years": years, "tax_rate": f.tax_rate,
    }
    if effective_fcff_years < MIN_DCF_YEARS:
        # v1.3.3 05: 5년 DCF 모델을 2~4년 이력에 그대로 돌리면 "같은 경로"로 점수가 나간다 → 폐기하고 C.
        try:
            res = _value_without_dcf(ValuationInputs(**common), sector, p)
        except (ValueError, ZeroDivisionError) as exc:
            return None, {**detail, "reason": f"SHORT_HISTORY:{type(exc).__name__}: {str(exc)[:80]}"}
        detail["dcf_dropped"] = "SHORT_HISTORY"
    elif fcff_norm is None or fcff_norm <= 0:
        # §3.2.3: 지분가치가 0 이하인 방법은 폐기한다 — DCF 를 빼고 나머지 방법으로 중앙값.
        try:
            res = _value_without_dcf(ValuationInputs(**common), sector, p)
        except (ValueError, ZeroDivisionError) as exc:
            return None, {**detail, "reason": f"FCFF_NONPOSITIVE:{type(exc).__name__}: {str(exc)[:80]}"}
        detail["dcf_dropped"] = "FCFF_NONPOSITIVE"
    else:
        try:
            res = value_company(ValuationInputs(**common), sector, params=p)
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

    # ── v1.3.2 §3.2.3 + v1.3.3: DCF 민감도 · WACC/g 민감도 · 10년 EV/EBIT 백분위 · V 감점 ─────────
    # ① DCF 민감도: 성장률 0.5·1.5배 시나리오의 편측 변동폭이 ±40% 이상이면 신뢰도 C + −4점.
    dcf = detail.get("iv_dcf") or {}
    sens: float | None = None
    if not detail.get("dcf_dropped") and all(dcf.get(k) is not None for k in ("base", "bear", "bull")):
        try:
            sens = float(dcf_sensitivity(dcf["bear"], dcf["bull"], dcf["base"]))
        except (TypeError, ValueError):
            sens = None
    # ①-b v1.3.3 05: 성장률이 0 으로 잘리면 bear==bull 이라 위 값이 항상 0 이 된다 —
    #      WACC ±1.0%p · 영구성장률 ±0.5%p 민감도를 별도 축으로 계산해 저장한다.
    rate_sens: dict = {}
    if not detail.get("dcf_dropped") and fcff_norm is not None and fcff_norm > 0:
        rate_sens = dcf_rate_sensitivity(
            float(fcff_norm), growth, float(common["wacc"]), p.terminal_growth,
            net_debt, minority, float(shares),
            dwacc=p.wacc_sensitivity_pm, dg=p.terminal_g_sensitivity_pm,
        )
    penalty_dcf = p.v_penalty_dcf if (sens is not None and sens >= p.dcf_sensitivity_max) else 0.0
    v_confidence = res.get("v_confidence")
    v_confidence_reason = res.get("v_confidence_reason")
    if penalty_dcf:
        v_confidence = "C"
        v_confidence_reason = "DCF_HIGH_SENSITIVITY"

    # ② 10년 EV/EBIT 중앙값 기반 내재가치와 모델 IV 가 20% 이상 괴리하면 −10점.
    hist = dict(hist or {})
    iv_hist = hist.get("iv_hist_ev_ebit")
    if iv_hist is None and hist.get("ev_ebit_median_10y") is not None:
        iv_hist = historical_iv(hist["ev_ebit_median_10y"], detail.get("ebit_normalized"),
                                net_debt, float(shares))
    divergence: float | None = None
    divergence_signed: float | None = None
    if iv_hist is not None and float(iv_hist) > 0:
        divergence_signed = (float(iv) - float(iv_hist)) / float(iv_hist)
        divergence = abs(divergence_signed)
    penalty_hist = p.v_penalty_hist if (divergence is not None and divergence >= p.hist_divergence_max) else 0.0
    percentile = hist.get("ev_ebit_percentile")
    detail.update({
        "dcf_sensitivity": round(sens, 4) if (sens is not None and np.isfinite(sens)) else None,
        "wacc_sensitivity": (round(rate_sens["wacc_sensitivity"], 4)
                             if rate_sens.get("wacc_sensitivity") is not None else None),
        "terminal_g_sensitivity": (round(rate_sens["terminal_g_sensitivity"], 4)
                                   if rate_sens.get("terminal_g_sensitivity") is not None else None),
        "rate_sensitivity": (round(rate_sens["rate_sensitivity"], 4)
                             if rate_sens.get("rate_sensitivity") is not None else None),
        "v_penalty_dcf": penalty_dcf,
        "v_penalty_hist": penalty_hist,
        "v_penalties": float(penalty_dcf + penalty_hist),
        "v_confidence": v_confidence,
        "v_confidence_reason": v_confidence_reason,
        "ev_ebit_percentile": percentile,
        "ev_ebit_current": hist.get("ev_ebit_current"),
        "ev_ebit_median_10y": hist.get("ev_ebit_median_10y"),
        "ev_ebit_samples": hist.get("ev_ebit_samples"),
        "ev_ebit_years": hist.get("ev_ebit_years"),
        "ev_ebit_window": hist.get("ev_ebit_window"),
        "ev_ebit_reason": hist.get("ev_ebit_reason", hist.get("reason")),
        "ev_ebit_overheated": (None if percentile is None
                                else bool(float(percentile) >= p.ev_ebit_percentile_hot)),
        "iv_hist_ev_ebit": None if iv_hist is None else round(float(iv_hist), 4),
        "hist_divergence": None if divergence is None else round(divergence, 4),
        "hist_divergence_signed": None if divergence_signed is None else round(divergence_signed, 4),
    })
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
