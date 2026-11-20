"""test_split_dilution — 분할을 희석으로 오인하던 HG-09 판정(R05).

실측(SEC companyfacts)을 축약한 픽스처로 고정한다:
- NVDA FY2023 2,507 → FY2026 24,514 (10:1, 2024-06-10) → **−2.22%** (통과해야 함)
- WMT  FY2023 2,734 → FY2026  8,022 (3:1, 2024-02-26) → **−2.19%** (통과해야 함)
- AVGO FY2022   423 → FY2025  4,853 (10:1, 2024-07-15) → **+14.7%** (정당 탈락, 고치면 안 됨)
- 결정일 이후 분할은 환산에 쓰지 않는다(룩어헤드 차단)
- 기준을 모르면 `HG-09_UNVERIFIED` 로 **탈락**한다(모르면 통과 금지)
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from engine.data.prices import build_price_frame
from engine.data.xbrl_fundamentals import build_fundamentals, split_events_from_panel
from engine.metrics.fundamentals import dilution_3y, hard_gate_checks

USD = 1e6
DECISION = date(2026, 11, 20)


def _share_rows(rows: list[tuple[str, float, str, str, str]]) -> list[dict]:
    """(회계연도 말일, 백만 주, filed, accn, form) → XBRL `shares` 항목."""
    out = []
    for end, shares, filed, accn, form in rows:
        start = (date.fromisoformat(end) - timedelta(days=365)).isoformat()
        out.append({"start": start, "end": end, "val": shares * 1e6, "form": form,
                    "fp": "FY", "filed": filed, "accn": accn})
    return out


def _payload(shares: list[dict], years: list[str]) -> dict:
    """주식수만 바꾼 최소 companyfacts — 나머지는 고정값. 10-K 는 결산 약 40일 뒤 제출."""
    def annual(end: str, val: float, filed: str, accn: str) -> dict:
        start = (date.fromisoformat(end) - timedelta(days=365)).isoformat()
        return {"start": start, "end": end, "val": val, "form": "10-K",
                "fp": "FY", "filed": filed, "accn": accn}

    def instant(end: str, val: float, filed: str, accn: str) -> dict:
        return {"end": end, "val": val, "form": "10-K", "fp": "FY", "filed": filed, "accn": accn}

    filings = {end: ((date.fromisoformat(end) + timedelta(days=40)).isoformat(), f"ACC-{end}")
               for end in years}
    entries = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": [
            annual(end, 100_000 * USD, *filings[end]) for end in years],
        "OperatingIncomeLoss": [annual(end, 20_000 * USD, *filings[end]) for end in years],
        "NetIncomeLoss": [annual(end, 15_000 * USD, *filings[end]) for end in years],
        "NetCashProvidedByUsedInOperatingActivities": [
            annual(end, 18_000 * USD, *filings[end]) for end in years],
        "PaymentsToAcquirePropertyPlantAndEquipment": [
            annual(end, 4_000 * USD, *filings[end]) for end in years],
        "DepreciationDepletionAndAmortization": [annual(end, 3_000 * USD, *filings[end]) for end in years],
        "Assets": [instant(end, 200_000 * USD, *filings[end]) for end in years],
        "Liabilities": [instant(end, 110_000 * USD, *filings[end]) for end in years],
        "StockholdersEquity": [instant(end, 90_000 * USD, *filings[end]) for end in years],
        "LongTermDebtNoncurrent": [instant(end, 30_000 * USD, *filings[end]) for end in years],
        "CashAndCashEquivalentsAtCarryingValue": [instant(end, 12_000 * USD, *filings[end]) for end in years],
        "InterestExpense": [annual(end, 1_500 * USD, *filings[end]) for end in years],
        "AssetsCurrent": [instant(end, 60_000 * USD, *filings[end]) for end in years],
        "LiabilitiesCurrent": [instant(end, 40_000 * USD, *filings[end]) for end in years],
        "WeightedAverageNumberOfDilutedSharesOutstanding": shares,
    }
    return {"facts": {"us-gaap": {tag: {"units": {"shares" if tag.endswith("SharesOutstanding") else "USD": rows}}
                                  for tag, rows in entries.items()}}}


NVDA_YEARS = ["2020-01-26", "2021-01-31", "2022-01-30", "2023-01-29", "2024-01-28",
              "2025-01-26", "2026-01-25"]
NVDA_SHARES = _share_rows([
    ("2020-01-26", 2_470.0, "2020-02-21", "ACC-FY2020", "10-K"),
    ("2021-01-31", 2_510.0, "2021-02-26", "ACC-FY2021", "10-K"),
    ("2022-01-30", 2_535.0, "2022-02-25", "ACC-FY2022", "10-K"),
    ("2023-01-29", 2_507.0, "2023-02-24", "ACC-FY2023", "10-K"),            # as-reported(분할 전)
    ("2023-01-29", 25_070.0, "2025-02-26", "ACC-FY2023-R", "10-K"),         # 재작성 비교치(분할 후)
    ("2024-01-28", 2_494.0, "2024-02-21", "ACC-FY2024", "10-K"),            # as-reported(분할 전)
    ("2024-01-28", 24_940.0, "2025-02-26", "ACC-FY2024-R", "10-K"),         # 재작성 비교치
    ("2025-01-26", 24_804.0, "2025-02-26", "ACC-FY2025", "10-K"),
    ("2026-01-25", 24_514.0, "2026-02-25", "ACC-FY2026", "10-K"),
])
NVDA_SPLIT = [{"ex_date": date(2024, 6, 10), "ratio": 10.0}]


def test_nvda_split_not_dilution():
    """재작성 비교치가 결정 시점까지 공개돼 있으면 그것만으로 −2.22% — HG-09 가 걸리면 안 된다."""
    payload = _payload(NVDA_SHARES, NVDA_YEARS)
    f, diag = build_fundamentals(payload, DECISION)
    assert f is not None
    assert f.diluted_shares[0] == pytest.approx(24_514.0)
    assert f.diluted_shares[2] == pytest.approx(24_940.0)   # FY2024 은 재작성 비교치
    assert f.diluted_shares[3] == pytest.approx(25_070.0)   # FY2023 은 재작성 비교치
    assert f.shares_basis[3] == "as_reported"
    assert dilution_3y(f) == pytest.approx(-0.0222, abs=1e-4)
    assert "HG-09" not in hard_gate_checks(f, {})
    assert "HG-09_UNVERIFIED" not in hard_gate_checks(f, {})


def test_nvda_split_factor_used_before_restatement():
    """재작성치가 아직 없던 시점(2024-08-01)에는 10:1 분할을 곱해 같은 기준으로 맞춘다."""
    payload = _payload(NVDA_SHARES, NVDA_YEARS)
    f, _ = build_fundamentals(payload, date(2024, 8, 1), splits=NVDA_SPLIT)
    assert f is not None
    assert f.diluted_shares_reported[0] == pytest.approx(2_494.0)   # FY2024 as-reported
    assert f.diluted_shares[0] == pytest.approx(24_940.0)
    assert f.shares_basis[0] == "split_adjusted"
    # FY2021 도 같은 분할로 환산돼 기준이 섞이지 않는다
    assert f.diluted_shares[3] == pytest.approx(25_100.0)
    assert dilution_3y(f) == pytest.approx(24_940 / 25_100 - 1, abs=1e-6)


def test_wmt_split_not_dilution():
    """3:1 분할 — 2,734 → 8,022 는 −2.19% 로 통과해야 한다(과거 +193% 오탈락)."""
    years = ["2021-01-31", "2022-01-31", "2023-01-31", "2024-01-31", "2025-01-31", "2026-01-31"]
    shares = _share_rows([
        ("2021-01-31", 2_847.0, "2021-03-19", "ACC-FY2021", "10-K"),
        ("2022-01-31", 2_805.0, "2022-03-18", "ACC-FY2022", "10-K"),
        ("2023-01-31", 2_734.0, "2023-03-17", "ACC-FY2023", "10-K"),
        ("2023-01-31", 8_202.0, "2024-03-15", "ACC-FY2023-R", "10-K"),
        ("2024-01-31", 8_108.0, "2024-03-15", "ACC-FY2024", "10-K"),
        ("2025-01-31", 8_081.0, "2025-03-14", "ACC-FY2025", "10-K"),
        ("2026-01-31", 8_022.0, "2026-03-13", "ACC-FY2026", "10-K"),
    ])
    payload = _payload(shares, years)
    f, _ = build_fundamentals(payload, DECISION)
    assert f is not None
    assert f.diluted_shares[0] == pytest.approx(8_022.0)
    assert dilution_3y(f) == pytest.approx(-0.0219, abs=1e-4)
    assert hard_gate_checks(f, {}) == []


def test_avgo_still_fails():
    """AVGO 는 VMware 유상증자로 실제 희석 +14.7% — 분할 보정 후에도 HG-09 로 탈락해야 한다."""
    years = ["2021-10-31", "2022-10-30", "2023-10-29", "2024-11-03", "2025-11-02"]
    shares = _share_rows([
        ("2021-10-31", 429.0, "2021-12-17", "ACC-FY2021", "10-K"),
        ("2022-10-30", 423.0, "2022-12-16", "ACC-FY2022", "10-K"),
        ("2022-10-30", 4_230.0, "2024-12-20", "ACC-FY2022-R", "10-K"),
        ("2023-10-29", 4_272.0, "2023-12-15", "ACC-FY2023", "10-K"),
        ("2024-11-03", 4_778.0, "2024-12-18", "ACC-FY2024", "10-K"),
        ("2025-11-02", 4_853.0, "2025-12-19", "ACC-FY2025", "10-K"),
    ])
    payload = _payload(shares, years)
    f, _ = build_fundamentals(payload, DECISION, splits=[{"ex_date": date(2024, 7, 15), "ratio": 10.0}])
    assert f is not None
    assert f.diluted_shares[0] == pytest.approx(4_853.0)
    assert f.diluted_shares[3] == pytest.approx(4_230.0)
    assert dilution_3y(f) == pytest.approx(0.1472, abs=1e-3)
    assert "HG-09" in hard_gate_checks(f, {})


def test_split_after_decision_not_applied():
    """ex-date 가 결정일 이후면 환산에 쓰지 않는다(룩어헤드 차단)."""
    payload = _payload(NVDA_SHARES, NVDA_YEARS)
    f, _ = build_fundamentals(payload, date(2024, 6, 1), splits=NVDA_SPLIT)   # ex-date 06-10 전
    assert f is not None
    assert f.diluted_shares[0] == pytest.approx(2_494.0)
    assert f.shares_basis[0] == "as_reported"


def test_unknown_basis_blocks():
    """주식수 기준을 모르면 HG-09 를 **통과시키지 않는다**(nan → HG-09_UNVERIFIED)."""
    years = ["2022-01-31", "2023-01-31", "2024-01-31", "2025-01-31", "2026-01-31"]
    payload = _payload([], years)      # 주식수 태그 자체가 없다
    f, diag = build_fundamentals(payload, DECISION)
    assert f is not None
    assert all(b == "unverified" for b in f.shares_basis)
    assert diag["shares_used"][0]["basis"] == "unverified"
    assert "HG-09_UNVERIFIED" in hard_gate_checks(f, {})


def test_split_events_from_panel_recover_ratio():
    """가격 패널의 `split_factor` 만으로 분할 이력(2:1)을 복원한다 — 별도 API 호출 없음."""
    idx = pd.bdate_range("2024-05-01", periods=10)
    close = pd.Series([100.0, 101, 100, 102, 50.0, 51, 50, 49, 50, 51], index=idx)
    hist = pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Adj Close": close, "Volume": 1_000_000.0})
    panel = build_price_frame("SPL", hist, pd.Series({idx[4]: 2.0}))
    events = split_events_from_panel(panel)
    assert len(events) == 1
    assert events[0]["ex_date"] == idx[4].date()
    assert events[0]["ratio"] == pytest.approx(2.0)
    assert split_events_from_panel(None) == []
