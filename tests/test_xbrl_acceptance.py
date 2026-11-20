"""test_xbrl_acceptance — 접수 시각(`acceptanceDateTime`) 기준 시점 필터와 최신 유효값(R06).

검사 항목:
- 같은 날 **마감 뒤** 접수된 10-K 는 그날 결정에 들어가지 않는다(문자열 `filed` 비교의 누수 차단)
- 장전 결정에서도 같은 날 늦게 접수된 공시는 보이지 않는다
- 정정(10-K/A)·재작성 비교치는 **공개된 뒤** 결정부터 반영된다(최초값 고정 금지)
- 접수 시각 인덱스가 없으면 `filed` 날짜로 폴백하고 진단에 `xbrl_acceptance_missing` 을 남긴다
- `sec_edgar.acceptance_index` 가 submissions 의 `accn` → 접수 시각 맵을 만든다
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from engine.data.sec_edgar import acceptance_index
from engine.data.xbrl_fundamentals import annual_facts, annual_series, build_fundamentals

USD = 1e6
YEARS = [2021, 2022, 2023, 2024, 2025]


def _facts(entries: dict[str, list[dict]], units: str = "USD") -> dict:
    out: dict = {"facts": {"us-gaap": {}}}
    for tag, rows in entries.items():
        out["facts"]["us-gaap"][tag] = {"units": {units: rows}}
    return out


def _annual(tag_year: int, val: float, filed: str, accn: str, form: str = "10-K") -> dict:
    return {"start": f"{tag_year}-01-01", "end": f"{tag_year}-12-31", "val": val, "form": form,
            "fp": "FY", "filed": filed, "accn": accn}


def _balance(tag_year: int, val: float, filed: str, accn: str) -> dict:
    return {"end": f"{tag_year}-12-31", "val": val, "form": "10-K", "fp": "FY",
            "filed": filed, "accn": accn}


def _company(revenue_filings: dict[int, tuple[str, str]] | None = None) -> dict:
    """건강한 회사 5년치 — `revenue_filings` 로 연도별 (filed, accn) 을 바꾼다."""
    filings = revenue_filings or {y: (f"{y + 1}-02-10", f"ACC-{y}") for y in YEARS}
    revenue = [_annual(y, 10_000 * USD, *filings[y]) for y in YEARS]
    entries = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": revenue,
        "OperatingIncomeLoss": [_annual(y, 2_000 * USD, *filings[y]) for y in YEARS],
        "NetIncomeLoss": [_annual(y, 1_500 * USD, *filings[y]) for y in YEARS],
        "NetCashProvidedByUsedInOperatingActivities": [_annual(y, 1_800 * USD, *filings[y]) for y in YEARS],
        "PaymentsToAcquirePropertyPlantAndEquipment": [_annual(y, 400 * USD, *filings[y]) for y in YEARS],
        "DepreciationDepletionAndAmortization": [_annual(y, 300 * USD, *filings[y]) for y in YEARS],
        "Assets": [_balance(y, 20_000 * USD, *filings[y]) for y in YEARS],
        "Liabilities": [_balance(y, 11_000 * USD, *filings[y]) for y in YEARS],
        "StockholdersEquity": [_balance(y, 9_000 * USD, *filings[y]) for y in YEARS],
        "LongTermDebtNoncurrent": [_balance(y, 3_000 * USD, *filings[y]) for y in YEARS],
        "CashAndCashEquivalentsAtCarryingValue": [_balance(y, 1_200 * USD, *filings[y]) for y in YEARS],
        "InterestExpense": [_annual(y, 150 * USD, *filings[y]) for y in YEARS],
        "AssetsCurrent": [_balance(y, 6_000 * USD, *filings[y]) for y in YEARS],
        "LiabilitiesCurrent": [_balance(y, 4_000 * USD, *filings[y]) for y in YEARS],
        "WeightedAverageNumberOfDilutedSharesOutstanding": [
            {"start": f"{y}-01-01", "end": f"{y}-12-31", "val": 1_000 * 1e6, "form": "10-K",
             "fp": "FY", "filed": filings[y][0], "accn": filings[y][1]} for y in YEARS],
    }
    return _facts(entries)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_post_close_filing_excluded():
    """10-K 가 같은 날 23:30Z 에 접수됐다면 22:00Z 결정에는 없었다(문자열 filed 비교의 누수)."""
    payload = _company({**{y: (f"{y + 1}-02-10", f"ACC-{y}") for y in YEARS},
                        2025: ("2026-02-05", "ACC-POSTCLOSE")})
    acceptance = {"ACC-POSTCLOSE": _dt("2026-02-05T23:30:00Z")}
    facts, _ = annual_facts(payload, _dt("2026-02-05T22:00:00Z"), acceptance)
    assert date(2025, 12, 31) not in facts["revenue"]
    # 접수 전 결정(전날)도 물론 볼 수 없다
    series = annual_series(payload, _dt("2026-02-05T22:00:00Z"), acceptance)
    assert max(series["revenue"]) == date(2024, 12, 31)


def test_premarket_same_day_filing_excluded():
    """장전 결정(12:50Z)에서도 같은 날 18:00Z 접수분은 보이지 않는다(최대 11시간 누수 차단)."""
    payload = _company({**{y: (f"{y + 1}-02-10", f"ACC-{y}") for y in YEARS},
                        2025: ("2026-02-05", "ACC-LATER")})
    acceptance = {"ACC-LATER": _dt("2026-02-05T18:00:00Z")}
    series = annual_series(payload, _dt("2026-02-05T12:50:00Z"), acceptance)
    assert max(series["revenue"]) == date(2024, 12, 31)
    # 18:00Z 이후 결정이면 들어온다
    later = annual_series(payload, _dt("2026-02-05T18:30:00Z"), acceptance)
    assert max(later["revenue"]) == date(2025, 12, 31)


def test_amendment_visible_after_its_acceptance():
    """최초 1,000(02-05 접수) / 정정 5,000(05-10 접수) — 각 시점의 최신 유효값을 쓴다."""
    payload = _company()
    rows = payload["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]
    rows[4] = _annual(2025, 1_000 * USD, "2026-02-05", "ACC-ORIG")
    rows.append(_annual(2025, 5_000 * USD, "2026-05-10", "ACC-AMEND", form="10-K/A"))
    acceptance = {"ACC-ORIG": _dt("2026-02-05T21:00:00Z"),
                  "ACC-AMEND": _dt("2026-05-10T20:00:00Z")}
    assert annual_series(payload, _dt("2026-03-01T12:50:00Z"), acceptance)["net_income"][
        date(2025, 12, 31)] == pytest.approx(1_000 * USD)
    assert annual_series(payload, _dt("2026-06-01T12:50:00Z"), acceptance)["net_income"][
        date(2025, 12, 31)] == pytest.approx(5_000 * USD)


def test_two_filings_same_day_later_acceptance_wins():
    """같은 날 두 번 제출되면 접수 시각이 늦은 쪽이 그 시점의 최신값이다."""
    payload = _company()
    rows = payload["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]
    rows[4] = _annual(2025, 1_000 * USD, "2026-02-05", "ACC-1500")
    rows.append(_annual(2025, 1_200 * USD, "2026-02-05", "ACC-1800", form="10-K/A"))
    acceptance = {"ACC-1500": _dt("2026-02-05T15:00:00Z"), "ACC-1800": _dt("2026-02-05T18:00:00Z")}
    assert annual_series(payload, _dt("2026-02-05T16:00:00Z"), acceptance)["net_income"][
        date(2025, 12, 31)] == pytest.approx(1_000 * USD)
    assert annual_series(payload, _dt("2026-02-05T19:00:00Z"), acceptance)["net_income"][
        date(2025, 12, 31)] == pytest.approx(1_200 * USD)


def test_acceptance_missing_falls_back_with_flag():
    """접수 시각 인덱스가 없으면 filed 날짜로 폴백하되 **조용히** 넘어가지 않는다."""
    payload = _company()
    f, diag = build_fundamentals(payload, _dt("2026-11-20T02:34:00Z"))
    assert f is not None
    assert diag["xbrl_acceptance_missing"] is True
    # 날짜만 주면(오프라인·테스트) 플래그를 세우지 않는다
    _, diag_date = build_fundamentals(payload, date(2026, 11, 20))
    assert diag_date["xbrl_acceptance_missing"] is False


def test_acceptance_index_joins_companyfacts_accn():
    """submissions 의 접수 시각 맵이 companyfacts `accn` 과 그대로 조인된다."""
    submissions = {"filings": {"recent": {
        "accessionNumber": ["0001652044-26-000018", "0001652044-26-000084"],
        "acceptanceDateTime": ["2026-02-05T21:04:11.000Z", "2026-10-29T20:05:03.000Z"],
    }}}
    index = acceptance_index(submissions)
    assert index["0001652044-26-000018"] == _dt("2026-02-05T21:04:11Z")
    payload = _company({**{y: (f"{y + 1}-02-10", f"ACC-{y}") for y in YEARS},
                        2025: ("2026-02-05", "0001652044-26-000018")})
    series = annual_series(payload, _dt("2026-02-05T21:00:00Z"), index)
    assert max(series["revenue"]) == date(2024, 12, 31)   # 21:04 접수 → 21:00 결정엔 없다
    assert max(annual_series(payload, _dt("2026-02-05T21:05:00Z"), index)["revenue"]) == date(2025, 12, 31)
