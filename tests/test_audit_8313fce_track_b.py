"""test_audit_8313fce_track_b — 감사(8313fce) 트랙 B 수정 고정: 항목 03·04·05·06·07·08.

각 테스트는 **"고치기 전 코드에서 실패했을 동작"** 을 하나씩 잠근다.

- 03: EV/EBIT 주식수 복원이 표본 시점 이후 공시를 읽지 않는다 · EPS/순이익 같은 제출본 · 분할 환산 후 비교
- 04: V 규칙(감점 −4/−10)은 v1.3.2 그대로, 값은 `config/params_v001.yaml` 한 곳에서 온다 ·
      워치리스트 행에 감점 전 V·원시 안전마진·부호 있는 괴리·표본 품질이 남는다
- 05: 성장률 0% 클립 표시 + WACC 민감도 · 짧은 이력은 5년 DCF 경로로 점수를 주지 않는다
- 06: 최신 EV/EBIT 독립 계산(무효면 보류) · window 는 실제 유효 표본 범위 · 공개 산출물까지 전달
- 07: 접수 시각 누락 → 접수일 23:59:59 ET 폴백 + `available_at <= cutoff` 최종 타임스탬프 강제
- 08: 영업 ΔNWC(단기금융부채 제외) · 부채 우선순위(중복·누락·미보고/0) · 비지배지분 차감 · 세율 연도 쌍
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest
import yaml

from engine.data.xbrl_fundamentals import (
    _ttm_net_income,
    annual_facts,
    annual_revisions,
    build_fundamentals,
    debt_components,
    ev_ebit_history,
    operating_nwc,
    valuation_multiplier,
)
from engine.metrics.fundamentals import AnnualFundamentals
from engine.metrics.valuation import (
    ValuationParams,
    load_valuation_params,
    v_score,
    valuation_params,
)
from engine.screener import Candidate, score_candidate
from engine.utils.dates import bar_available_at
from scripts.run_screen import build_candidates

USD = 1e6
AS_OF = date(2026, 11, 20)


# ───────────────────────────── 공통 픽스처 빌더 ─────────────────────────────
def _annual(fy: int, val: float, filed: str, accn: str | None, form: str = "10-K") -> dict:
    return {"start": f"{fy}-01-01", "end": f"{fy}-12-31", "val": val, "form": form,
            "fp": "FY", "filed": filed, **({"accn": accn} if accn else {})}


def _instant(fy: int, val: float, filed: str, accn: str | None, form: str = "10-K") -> dict:
    return {"end": f"{fy}-12-31", "val": val, "form": form, "fp": "FY",
            "filed": filed, **({"accn": accn} if accn else {})}


def _facts(entries: dict[str, list[dict]]) -> dict:
    out: dict = {"facts": {"us-gaap": {}}}
    for tag, rows in entries.items():
        unit = "shares" if ("Shares" in tag and "EarningsPerShare" not in tag) else (
            "USD/shares" if tag == "EarningsPerShareDiluted" else "USD")
        out["facts"]["us-gaap"][tag] = {"units": {unit: rows}}
    return out


def _frame(start: str, end: str, price: float = 100.0) -> pd.DataFrame:
    idx = pd.bdate_range(start, end)
    return pd.DataFrame({"sadj_close": price, "raw_close": price,
                         "available_at": [bar_available_at(d.date()) for d in idx]}, index=idx)


def _company(shares: float = 100.0, minority: float = 0.0, ebit: list[float] | None = None,
             fcff_years: int = 5) -> AnnualFundamentals:
    ebit = ebit or [1_200.0, 1_140.0, 1_080.0, 1_020.0, 960.0][:fcff_years]
    n = len(ebit)
    return AnnualFundamentals(
        ebit=ebit, revenue=[5_000.0] * n, net_income=[300.0] * n, ocf=[500.0] * n,
        capex=[600.0] * n, depreciation=[200.0] * n, total_assets=[6_000.0] * n,
        non_interest_liabilities=[2_000.0] * n, excess_cash=[100.0] * n,
        total_debt=[1_000.0] * n, cash=[800.0] * n, equity=[3_000.0] * n,
        diluted_shares=[shares] * n, interest_expense=[40.0] * n, tax_rate=0.21,
        marketable_securities=[300.0] * n, minority_interest=minority,
    )


# ═══════════════════════════ 03. 주식수 복원의 미래 참조 ═══════════════════════════
SHARE_PAYLOAD = _facts({
    "OperatingIncomeLoss": [_annual(y, 100 * USD, f"{y + 1}-02-10", f"EB-{y}") for y in range(2018, 2023)],
    "NetIncomeLoss": [_annual(y, 50 * USD, f"{y + 1}-02-10", f"NI-{y}") for y in range(2018, 2023)],
    "EarningsPerShareDiluted": [_annual(y, 5.0, f"{y + 1}-02-10", f"NI-{y}") for y in range(2018, 2023)],
    "LongTermDebtNoncurrent": [_instant(y, 0.0, f"{y + 1}-02-10", f"EB-{y}") for y in range(2018, 2023)],
    "CashAndCashEquivalentsAtCarryingValue": [_instant(y, 0.0, f"{y + 1}-02-10", f"EB-{y}")
                                              for y in range(2018, 2023)],
    # 주식수 태그는 최근 2개년만 — 그 이전은 EPS 복원 경로(0.3~3배 검사 대상)
    "WeightedAverageNumberOfDilutedSharesOutstanding": [
        {"start": "2021-01-01", "end": "2021-12-31", "val": 10 * 1e6, "form": "10-K",
         "fp": "FY", "filed": "2022-02-10", "accn": "SH-2021"},
        {"start": "2022-01-01", "end": "2022-12-31", "val": 10 * 1e6, "form": "10-K",
         "fp": "FY", "filed": "2023-02-10", "accn": "SH-2022"},
    ],
})


def _with_future_restatement(payload: dict, shares_m: float = 40.0) -> dict:
    """결정일(2026-11-20) **이후** 제출된 10-K/A — 과거 표본을 흔들면 안 된다."""
    p = {**payload, "facts": {"us-gaap": {k: {"units": {u: list(rows) for u, rows in v["units"].items()}}
                                          for k, v in payload["facts"]["us-gaap"].items()}}}
    p["facts"]["us-gaap"]["WeightedAverageNumberOfDilutedSharesOutstanding"]["units"]["shares"].append(
        {"start": "2022-01-01", "end": "2022-12-31", "val": shares_m * 1e6, "form": "10-K/A",
         "fp": "FY", "filed": "2027-03-01", "accn": "FUTURE-AMEND"})
    return p


def test_ref_shares_ignores_filings_after_the_sample_date():
    """(03) 미래 10-K/A 한 줄이 과거 표본을 지우지 않는다 — 기준 주식수는 **그 시점** 값."""
    frame = _frame("2019-06-03", "2023-06-30")
    base = ev_ebit_history(SEARCH := SHARE_PAYLOAD, frame, AS_OF, include_samples=True)
    shifted = ev_ebit_history(_with_future_restatement(SEARCH), frame, AS_OF, include_samples=True)
    assert base["ev_ebit_samples"] == shifted["ev_ebit_samples"] > 0
    assert base["ev_ebit_years"] == shifted["ev_ebit_years"]
    assert base["ev_ebit_median_10y"] == pytest.approx(shifted["ev_ebit_median_10y"])
    # EPS 로 복원한 해는 출처가 남는다
    implied = [r for r in base["sample_rows"] if r["shares_source"] == "eps_implied"]
    assert implied, "EPS 복원 경로 표본이 없다 — 픽스처가 의도대로 만들어지지 않았다"
    assert all(r["eps_pairing"] == "same_filing" for r in implied)


def test_eps_implied_shares_compared_after_split_alignment():
    """(03) 비교는 **분할 환산 후** — 환산 전 비교면 분할 전 EPS 행이 전부 탈락했을 것이다."""
    payload = {**SHARE_PAYLOAD, "facts": {"us-gaap": {
        k: {"units": {u: list(rows) for u, rows in v["units"].items()}}
        for k, v in SHARE_PAYLOAD["facts"]["us-gaap"].items()}}}
    # FY2018~2020 EPS·순이익은 **분할 전** 기준(주식수 2.5백만 주), 이후 2.5:1 분할이 있었다고 본다.
    rows = payload["facts"]["us-gaap"]
    rows["EarningsPerShareDiluted"]["units"]["USD/shares"] = [
        _annual(y, 5.0 * 2.5, f"{y + 1}-02-10", f"NI-{y}") for y in range(2018, 2021)]
    rows["EarningsPerShareDiluted"]["units"]["USD/shares"] += [
        _annual(y, 5.0, f"{y + 1}-02-10", f"NI-{y}") for y in range(2021, 2023)]
    splits = [{"ex_date": date(2021, 6, 1), "ratio": 2.5}]
    hist = ev_ebit_history(payload, _frame("2019-06-03", "2023-06-30"), AS_OF,
                           splits=splits, include_samples=True)
    early = [r for r in hist["sample_rows"] if r["fy"].startswith("2018") or r["fy"].startswith("2019")]
    assert early and all(r["shares_source"] == "eps_implied" for r in early)
    # 2.5백만 주 × 2.5 = 6.25백만 주 — 그 시점 기준(2.5백만)의 0.3~3배 안이지만 환산 전이면 1/2.5 로 탈락한다
    assert all(r["ev_ebit"] == pytest.approx((100.0 * 10.0) / 100.0, rel=0.4) for r in early)


def test_eps_and_net_income_from_the_same_filing_are_preferred():
    """(03) EPS 와 순이익은 같은 제출본을 먼저 쓴다 — 섞이면 주식수가 틀어진다."""
    payload = _facts({
        "OperatingIncomeLoss": [_annual(2020, 100 * USD, "2021-02-10", "OP-2020")],
        # 최신 개정(10-K/A)은 EPS 만 바꿨다 → EPS·순이익을 그대로 나누면 20백만 주(2배)가 된다
        "EarningsPerShareDiluted": [_annual(2020, 5.0, "2021-02-10", "NI-2020"),
                                     _annual(2020, 2.5, "2021-06-10", "AMEND-2020", form="10-K/A")],
        "NetIncomeLoss": [_annual(2020, 50 * USD, "2021-02-10", "NI-2020")],
        "WeightedAverageNumberOfDilutedSharesOutstanding": [
            {"start": "2020-01-01", "end": "2020-12-31", "val": 10 * 1e6, "form": "10-K",
             "fp": "FY", "filed": "2021-02-10", "accn": "NI-2020"}],
        "LongTermDebtNoncurrent": [_instant(2020, 0.0, "2021-02-10", "OP-2020")],
        "CashAndCashEquivalentsAtCarryingValue": [_instant(2020, 0.0, "2021-02-10", "OP-2020")],
    })
    held = {k: {"units": {u: list(rows) for u, rows in v["units"].items()}}
            for k, v in payload["facts"]["us-gaap"].items()}
    del held["WeightedAverageNumberOfDilutedSharesOutstanding"]        # 주식수 태그 없음 → EPS 복원
    payload = {"facts": {"us-gaap": held}}
    hist = ev_ebit_history(payload, _frame("2021-06-20", "2021-12-30"), date(2022, 1, 10),
                           include_samples=True)
    rows = hist.get("sample_rows") or []
    assert rows, "EPS 복원 표본이 하나도 없다"
    assert all(r["eps_pairing"] == "same_filing" for r in rows)
    assert all(r["shares"] == pytest.approx(10.0) for r in rows)      # 50m / 5.0 = 10m 주


# ═══════════════════════════ 04. V 규칙 = yaml 한 경로 ═══════════════════════════
def test_params_yaml_is_the_single_source_for_v_rules(tmp_path):
    """(04) yaml 을 바꾸면 실제로 점수·감점이 바뀐다(예전에는 죽은 설정이었다)."""
    path = tmp_path / "params.yaml"
    path.write_text(yaml.safe_dump({"valuation": {
        "margin_high": 0.20, "margin_floor": -0.10, "v_penalty_hist": 3.0,
        "v_penalty_dcf": 1.5, "hist_divergence_max": 0.10, "dcf_sensitivity_max": 0.20,
    }}), encoding="utf-8")
    p = load_valuation_params(path)
    assert p.margin_high == pytest.approx(0.20)
    assert p.v_penalty_hist == pytest.approx(3.0)
    assert v_score(0.20, params=p) == pytest.approx(100.0)      # 확장된 상단이 반영된다
    assert v_score(-0.10, params=p) == pytest.approx(15.0)
    assert v_score(0.0, penalties=p.v_penalty_hist, params=p) == pytest.approx(47.0)  # 50 − 3
    # 기본값은 v1.3.2 그대로(사용자 결정: −4/−10 유지)
    default = valuation_params()
    assert (default.v_penalty_dcf, default.v_penalty_hist) == (4.0, 10.0)
    assert (default.dcf_sensitivity_max, default.hist_divergence_max) == (0.40, 0.20)


def test_config_valuation_section_has_no_dead_v_keys():
    """(04) `valuation:` 의 V 관련 키는 전부 `ValuationParams` 필드다(중복 정의·죽은 키 금지)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    cfg = yaml.safe_load((root / "config" / "params_v001.yaml").read_text(encoding="utf-8"))
    wired = set(ValuationParams.__dataclass_fields__)
    documented_fixed = {"history_years", "per_share_output", "net_debt_adjust",
                        "suspended_sectors", "suspend_reason"}   # [고정] 계약 표기
    unknown = set(cfg["valuation"]) - wired - documented_fixed
    assert not unknown, f"어떤 코드도 읽지 않는 V 설정이 남아 있다: {sorted(unknown)}"
    # 예전 이름(죽은 키)은 사라졌다
    for gone in ("v_score_minus20", "v_score_minus200", "dcf_penalty_points",
                 "historical_penalty_points", "historical_divergence_max",
                 "historical_percentile_penalty", "margin_full_score", "margin_zero_score"):
        assert gone not in cfg["valuation"]


def test_watchlist_row_carries_raw_margin_and_unpenalised_v():
    """(04) 감점 전 V(`v_raw`)와 원시 안전마진이 행에 남는다 — 감점 효과를 나눠 볼 수 있다."""
    c = Candidate(ticker="AAA", sector="Information Technology", market_cap_usd=5e11,
                  adv20_usd=1e9, price=100.0, history_rows=1300, exchange="NYSE",
                  margin=-0.40, v_penalties=10.0, fundamentals=_company(), peg=1.2, revenue_cagr=0.10,
                  ev_ebit_samples=45, ev_ebit_years=5, ev_ebit_window="2016-01-04~2026-11-19",
                  ev_ebit_reason=None, hist_divergence_signed=-0.62, growth=0.0,
                  growth_raw_cagr=-0.0427, growth_clipped_to_zero=True, effective_fcff_years=5,
                  normalization_method="adjusted", dcf_sensitivity=0.0, wacc_sensitivity=0.15)
    row = score_candidate(c)
    assert row["margin"] == pytest.approx(-0.40)
    assert row["v_raw"] == pytest.approx(v_score(-0.40), abs=0.01)
    assert row["v"] == pytest.approx(v_score(-0.40, 10.0), abs=0.01)
    assert row["v_raw"] > row["v"]
    assert row["hist_divergence_signed"] == pytest.approx(-0.62)
    assert row["ev_ebit_samples"] == 45 and row["ev_ebit_years"] == 5
    assert row["ev_ebit_window"] == "2016-01-04~2026-11-19"
    assert row["growth_clipped_to_zero"] is True
    assert row["wacc_sensitivity"] == pytest.approx(0.15)


def test_run_screen_reads_quality_fields_from_valuation_inputs():
    """(04) `collect` 의 유니버스 행이 아니라 `valuation_inputs`(상세)에서 표본 품질을 읽는다."""
    from engine.data.fixtures import synthetic_history
    from engine.data.prices import build_price_frame

    hist = synthetic_history("FIXQ", days=1300, end=date(2026, 11, 19), pattern="flat")
    panel = build_price_frame("FIXQ", hist, None)
    data = {
        "universe": [{"ticker": "FIXQ", "sector": "Information Technology", "market_cap_usd": 2.4e12,
                      "adv20_usd": 9e9, "iv_mult": 0.8}],
        "panels": {"FIXQ": panel},
        "valuation_inputs": {"FIXQ": {"iv_mult": 0.8, "v_penalties": 10.0,
                                      "hist_divergence_signed": -0.76,
                                      "ev_ebit_samples": 41, "ev_ebit_years": 5,
                                      "ev_ebit_window": "2016-02-01~2026-11-19",
                                      "ev_ebit_reason": None, "growth": 0.0,
                                      "growth_raw_cagr": -0.0427, "growth_clipped_to_zero": True,
                                      "effective_fcff_years": 5, "normalization_method": "adjusted",
                                      "dcf_sensitivity": 0.0, "wacc_sensitivity": 0.1515,
                                      "terminal_g_sensitivity": 0.0589}},
    }
    cand = build_candidates(data)[0]
    assert cand.ev_ebit_samples == 41 and cand.ev_ebit_years == 5
    assert cand.ev_ebit_window == "2016-02-01~2026-11-19"
    assert cand.hist_divergence_signed == pytest.approx(-0.76)
    assert cand.growth_clipped_to_zero is True
    assert cand.wacc_sensitivity == pytest.approx(0.1515)
    assert cand.margin is not None and cand.margin < 0


def test_public_output_carries_ev_ebit_sample_quality():
    """(04·06) 공개 산출물(watchlist.json·tickers/*.json)에서 표본 수·기간·사유를 볼 수 있다."""
    import scripts.build_public_data as bpd

    watch = {"as_of": "2026-11-20", "count": 1, "watchlist": [{
        "ticker": "FIXP", "sector": "Information Technology", "q": 70.0, "v": 3.3, "v_raw": 13.3,
        "margin": -0.4, "g": 55.0, "rank_score": 48.0, "v_penalties": 10.0,
        "hist_divergence_signed": -0.78, "ev_ebit_percentile": 6.6, "ev_ebit_samples": 45,
        "ev_ebit_years": 5, "ev_ebit_window": "2016-01-04~2026-11-19", "ev_ebit_reason": None,
        "growth": 0.0, "growth_raw_cagr": -0.0427, "growth_clipped_to_zero": True,
        "effective_fcff_years": 5, "normalization_method": "adjusted",
        "dcf_sensitivity": 0.0, "wacc_sensitivity": 0.1515, "terminal_g_sensitivity": 0.0589,
        "valuation_suspended": False}]}
    rules = bpd.load_allowlist()
    out = bpd.build(None, None, None, rules, watch=watch)
    row = out["watchlist.json"]["watchlist"][0]
    for key in ("margin", "v_raw", "hist_divergence_signed", "ev_ebit_samples", "ev_ebit_years",
                "ev_ebit_window", "ev_ebit_reason", "growth_clipped_to_zero", "wacc_sensitivity"):
        assert key in row, f"watchlist.json 에 {key} 가 없다"
    ticker = out["tickers/FIXP.json"]
    assert ticker["valuation_quality"]["ev_ebit_samples"] == 45
    assert ticker["valuation_quality"]["ev_ebit_window"] == "2016-01-04~2026-11-19"


# ═══════════════════════════ 05. 성장률 0% · 짧은 이력 ═══════════════════════════
def _declining() -> AnnualFundamentals:
    """FCFF 가 줄어 원 CAGR 이 음수 — 0% 로 잘리는 케이스(감사 GOOGL 과 같은 모양)."""
    return _company(ebit=[900.0, 950.0, 1_000.0, 1_050.0, 1_100.0])


def test_growth_clip_is_explicit_and_wacc_sensitivity_replaces_zero():
    """(05) 0% 클립 사실과 원 CAGR 을 남기고, 성장 시나리오가 붕괴하면 WACC 민감도로 검증한다."""
    mult, det = valuation_multiplier(_declining(), 100.0, "Information Technology", 1.0, 0.042)
    assert mult is not None
    assert det["growth"] == 0.0
    assert det["growth_raw_cagr"] < 0
    assert det["growth_clipped_to_zero"] is True
    assert det["dcf_sensitivity"] == pytest.approx(0.0)          # bear == bull (구조적 한계)
    assert det["wacc_sensitivity"] > 0.05                        # 다른 축으로는 검증된다
    assert det["terminal_g_sensitivity"] is not None
    assert det["rate_sensitivity"] == pytest.approx(
        max(det["wacc_sensitivity"], det["terminal_g_sensitivity"]))


def test_growth_not_clipped_keeps_flag_false():
    mult, det = valuation_multiplier(_company(), 100.0, "Information Technology", 1.0, 0.042)
    assert mult is not None
    assert det["growth_clipped_to_zero"] is False
    assert det["growth_raw_cagr"] == pytest.approx(det["fcff_cagr"], rel=1e-3)


def test_short_history_drops_five_year_dcf():
    """(05) 2~4년 이력은 5년 DCF 모델로 점수를 주지 않는다(EPV·Graham 만, 신뢰도 C)."""
    short = _company(fcff_years=3)
    mult, det = valuation_multiplier(short, 100.0, "Information Technology", 1.0, 0.042)
    assert mult is not None
    assert det["effective_fcff_years"] == 3
    assert det["dcf_dropped"] == "SHORT_HISTORY"
    assert det["normalization_method"] == "unadjusted_median"
    assert "dcf_base" not in det["methods"]
    assert det["v_confidence"] == "C"
    assert det["iv_dcf"]["base"] is None


def test_five_year_history_is_normalized_not_dropped():
    mult, det = valuation_multiplier(_company(), 100.0, "Information Technology", 1.0, 0.042)
    assert mult is not None
    assert det["effective_fcff_years"] == 5
    assert det["normalization_method"] == "adjusted"
    assert "dcf_dropped" not in det
    assert "dcf_base" in det["methods"]


# ═══════════════════════════ 06. 최신 EV/EBIT 독립 계산 ═══════════════════════════
def _ev_payload(last_ebit: float | None = 100.0, last_debt: bool = True) -> dict:
    """2018~2022 회계연도 — 2022 만 EBIT/부채를 바꿔 '최신 재무 무효' 상황을 만든다."""
    entries: dict[str, list[dict]] = {
        "OperatingIncomeLoss": [_annual(y, 100 * USD, f"{y + 1}-02-10", f"OP-{y}")
                                for y in range(2018, 2022)],
        "WeightedAverageNumberOfDilutedSharesOutstanding": [
            {"start": f"{y}-01-01", "end": f"{y}-12-31", "val": 10 * 1e6, "form": "10-K",
             "fp": "FY", "filed": f"{y + 1}-02-10", "accn": f"SH-{y}"} for y in range(2018, 2023)],
        "CashAndCashEquivalentsAtCarryingValue": [_instant(y, 0.0, f"{y + 1}-02-10", f"OP-{y}")
                                                  for y in range(2018, 2023)],
        "LongTermDebtNoncurrent": [_instant(y, 0.0, f"{y + 1}-02-10", f"OP-{y}")
                                   for y in range(2018, 2022)],
    }
    if last_ebit is not None:
        entries["OperatingIncomeLoss"].append(_annual(2022, last_ebit * USD, "2023-02-10", "OP-2022"))
    if last_debt:
        # 부채는 2021 까지만 — `last_debt=False` 면 최신 회계연도 부채가 "미보고"(결측)가 된다
        entries["LongTermDebtNoncurrent"].append(_instant(2022, 0.0, "2023-02-10", "OP-2022"))
    return _facts(entries)


def test_current_ev_ebit_is_none_when_latest_ebit_is_invalid():
    """(06) 최신 EBIT 이 음수면 과거 표본을 '현재'로 재사용하지 않는다 — 백분위 보류."""
    frame = _frame("2019-06-03", "2023-06-30")
    hist = ev_ebit_history(_ev_payload(last_ebit=-50.0), frame, date(2023, 6, 30))
    assert hist["ev_ebit_current"] is None
    assert hist["ev_ebit_percentile"] is None
    assert hist["ev_ebit_reason"] == "current_ebit_nonpositive"
    assert hist["ev_ebit_median_10y"] is not None            # 과거 중앙값은 남는다(정보)
    assert hist["ev_ebit_window"].endswith("2022-02-10") or hist["ev_ebit_window"]           # 실제 표본 범위
    assert hist["ev_ebit_window"].split("~")[1] < "2023-02-10"


def test_current_ev_ebit_is_none_when_latest_debt_is_missing():
    """(06) 부채 결측도 '무효' — 과거 값을 현재 배수로 쓰지 않는다."""
    frame = _frame("2019-06-03", "2023-06-30")
    hist = ev_ebit_history(_ev_payload(last_ebit=100.0, last_debt=False), frame, date(2023, 6, 30))
    assert hist["ev_ebit_current"] is None
    assert hist["ev_ebit_reason"] == "current_no_debt"
    assert hist["ev_ebit_percentile"] is None
    assert hist["ev_ebit_samples"] > 0          # 과거 표본은 남아 중앙값을 볼 수 있다
    assert hist["ev_ebit_median_10y"] is not None


def test_current_ev_ebit_computed_when_latest_is_valid():
    frame = _frame("2019-06-03", "2023-06-30")
    hist = ev_ebit_history(_ev_payload(), frame, date(2023, 6, 30))
    assert hist["ev_ebit_current"] == pytest.approx(100.0 * 10.0 / 100.0)
    assert hist["ev_ebit_percentile"] is not None
    assert hist["ev_ebit_reason"] is None
    # window 는 계획 날짜가 아니라 **유효 표본**의 양 끝
    first, last = hist["ev_ebit_window"].split("~")
    assert first >= "2019-06-03" and last <= "2023-06-30"


# ═══════════════════════════ 07. 접수 시각 누락 폴백 ═══════════════════════════
def test_missing_acceptance_uses_et_day_end_and_final_cutoff():
    """(07) 접수 시각이 없으면 접수일 **23:59:59 ET** — 같은 날 22:00Z 결정에는 안 들어간다."""
    payload = _facts({
        "Revenues": [_annual(2025, 1_000 * USD, "2026-11-19", None)],
        "OperatingIncomeLoss": [_annual(2025, 200 * USD, "2026-11-19", None)],
    })
    late = datetime(2026, 11, 19, 22, 0, tzinfo=timezone.utc)
    facts, stats = annual_facts(payload, late)
    assert date(2025, 12, 31) not in facts.get("revenue", {})
    assert stats["acceptance_missing"] >= 1
    # ET 하루가 끝난 뒤(11-20 05:00Z)에는 보인다
    after = annual_facts(payload, datetime(2026, 11, 20, 5, 0, tzinfo=timezone.utc))[0]
    assert date(2025, 12, 31) in after["revenue"]
    # 날짜만 주는 호출(오프라인·테스트)은 그날 하루 끝까지로 본다
    by_date = annual_facts(payload, date(2026, 11, 19))[0]
    assert date(2025, 12, 31) in by_date["revenue"]


def test_annual_revisions_drops_amendments_after_decision():
    payload = _facts({
        "OperatingIncomeLoss": [_annual(2025, 200 * USD, "2026-11-19", "OP-2025"),
                                _annual(2025, 210 * USD, "2026-12-01", "AMEND-2025", form="10-K/A")],
    })
    # 접수일 23:59:59 ET(11-20 04:59Z) 가 지난 시점 — 그때 알 수 있던 것은 OP-2025 뿐이다
    early = annual_revisions(payload, decision_at=datetime(2026, 11, 21, 0, 0, tzinfo=timezone.utc))
    assert [r["accn"] for r in early["ebit"][date(2025, 12, 31)]] == ["OP-2025"]
    # 접수일 당일 22:00Z 결정에는 그 공시가 아직 없었다(폴백이 하루 끝이므로)
    same_day = annual_revisions(payload, decision_at=datetime(2026, 11, 19, 22, 0, tzinfo=timezone.utc))
    assert "ebit" not in same_day


def test_ttm_net_income_uses_final_timestamp_comparison():
    """(07) TTM 도 접수일 23:59:59 ET 폴백 뒤 `<= cutoff` 비교를 거친다(예전엔 날짜만 봤다)."""
    entries = {
        "NetIncomeLoss": [
            {"start": "2025-01-01", "end": "2025-12-31", "val": 400 * USD, "form": "10-K",
             "fp": "FY", "filed": "2026-02-05", "accn": "FY2025"},
            {"start": "2025-01-01", "end": "2025-09-30", "val": 350 * USD, "form": "10-Q",
             "fp": "Q3", "filed": "2025-11-05", "accn": "Q3-2025"},
            {"start": "2026-01-01", "end": "2026-09-30", "val": 500 * USD, "form": "10-Q",
             "fp": "Q3", "filed": "2026-11-19"},                       # 접수 시각 없음
        ]
    }
    payload = _facts(entries)
    late = datetime(2026, 11, 19, 22, 0, tzinfo=timezone.utc)
    assert _ttm_net_income(payload, late, date(2025, 12, 31), None, 400.0) is None
    after = datetime(2026, 11, 20, 5, 30, tzinfo=timezone.utc)
    assert _ttm_net_income(payload, after, date(2025, 12, 31), None, 400.0) == pytest.approx(550.0)


# ═══════════════════════════ 08. NWC·부채·비지배지분·세율 ═══════════════════════════
def test_operating_nwc_excludes_short_term_financial_debt():
    """(08-a) 단기차입 +100 은 영업 NWC 를 바꾸지 않는다(FCFF 왜곡 차단)."""
    assert operating_nwc([600.0], [400.0], [100.0], [0.0]) == [100.0]
    assert operating_nwc([600.0], [500.0], [100.0], [0.0], [100.0]) == [100.0]
    # 단기금융부채가 유동부채보다 크면(이상치) 영업 유동부채는 0 으로 자른다(음수 부채 금지)
    assert operating_nwc([600.0], [100.0], [100.0], [0.0], [300.0]) == [500.0]


def test_debt_priority_prevents_double_count_and_double_missing():
    """(08-b) `LongTermDebt`(유동분 포함) + 유동성장기부채를 그대로 더하면 200 중복된다."""
    rows = {
        "total": debt_components({"long_term_debt_total": 1_200.0, "long_term_debt_current": 200.0,
                                  "short_term_borrowings": 100.0}),
        "components": debt_components({"long_term_debt_noncurrent": 1_000.0,
                                       "long_term_debt_current": 200.0,
                                       "short_term_borrowings": 100.0}),
    }
    assert rows["total"]["total"] == pytest.approx(1_300.0)      # 1,200 + 100 (유동분은 합계 안에)
    assert rows["components"]["total"] == pytest.approx(1_300.0)  # 1,000 + 200 + 100
    assert rows["total"]["policy"] == "total_includes_current"
    assert rows["components"]["policy"] == "components_summed"
    assert rows["total"]["current_financial"] == pytest.approx(300.0)
    # `DebtCurrent`(유동 이자부채 합계)와 유동성장기부채를 함께 보고해도 중복되지 않는다
    with_debt_current = debt_components({"long_term_debt_noncurrent": 1_000.0,
                                         "long_term_debt_current": 200.0, "debt_current": 200.0})
    assert with_debt_current["total"] == pytest.approx(1_000.0 + 200.0)
    assert with_debt_current["current_financial"] == pytest.approx(200.0)
    # 미보고(nan)와 0 은 다르다
    missing = debt_components({})
    assert not missing["reported"] and missing["total"] != missing["total"]
    zero = debt_components({"long_term_debt_total": 0.0})
    assert zero["reported"] and zero["total"] == 0.0


def test_minority_interest_is_subtracted_from_equity_value():
    """(08-c) 비지배지분을 기업가치에서 빼지 않으면 주당 가치가 과대평가된다."""
    _, base = valuation_multiplier(_company(minority=0.0), 100.0, "Information Technology", 1.0, 0.042)
    _, with_nci = valuation_multiplier(_company(minority=200.0), 100.0, "Information Technology", 1.0, 0.042)
    assert with_nci["minority_interest"] == pytest.approx(200.0)
    assert with_nci["iv_epv"] == pytest.approx(base["iv_epv"] - 200.0 / 100.0)
    assert with_nci["iv_dcf"]["base"] == pytest.approx(base["iv_dcf"]["base"] - 200.0 / 100.0)
    assert with_nci["iv_median"] < base["iv_median"]


def test_effective_tax_rate_uses_matched_year_pairs():
    """(08-d) 세금이 3년만 있으면 세전이익도 그 3년만 분모로 쓴다(0.12 → 0.20)."""
    years = [2018, 2019, 2020, 2021, 2022]
    entries = {
        "Revenues": [_annual(y, 100 * USD, f"{y + 1}-02-10", f"A-{y}") for y in years],
        "OperatingIncomeLoss": [_annual(y, 20 * USD, f"{y + 1}-02-10", f"A-{y}") for y in years],
        "NetIncomeLoss": [_annual(y, 15 * USD, f"{y + 1}-02-10", f"A-{y}") for y in years],
        "NetCashProvidedByUsedInOperatingActivities": [_annual(y, 18 * USD, f"{y + 1}-02-10", f"A-{y}")
                                                       for y in years],
        "PaymentsToAcquirePropertyPlantAndEquipment": [_annual(y, 4 * USD, f"{y + 1}-02-10", f"A-{y}")
                                                       for y in years],
        "DepreciationDepletionAndAmortization": [_annual(y, 3 * USD, f"{y + 1}-02-10", f"A-{y}")
                                                 for y in years],
        "Assets": [_instant(y, 200 * USD, f"{y + 1}-02-10", f"A-{y}") for y in years],
        "Liabilities": [_instant(y, 110 * USD, f"{y + 1}-02-10", f"A-{y}") for y in years],
        "StockholdersEquity": [_instant(y, 90 * USD, f"{y + 1}-02-10", f"A-{y}") for y in years],
        "CashAndCashEquivalentsAtCarryingValue": [_instant(y, 10 * USD, f"{y + 1}-02-10", f"A-{y}")
                                                  for y in years],
        "WeightedAverageNumberOfDilutedSharesOutstanding": [
            {"start": f"{y}-01-01", "end": f"{y}-12-31", "val": 10 * 1e6, "form": "10-K",
             "fp": "FY", "filed": f"{y + 1}-02-10", "accn": f"A-{y}"} for y in years],
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": [
            _annual(y, 500 * USD, f"{y + 1}-02-10", f"A-{y}") for y in years],
        # 세금은 최근 3년만 보고
        "IncomeTaxExpenseBenefit": [_annual(y, 100 * USD, f"{y + 1}-02-10", f"A-{y}")
                                    for y in (2020, 2021, 2022)],
    }
    f, diag = build_fundamentals(_facts(entries), AS_OF)
    assert f is not None
    assert diag["tax_pairs"] == 3
    assert f.tax_rate == pytest.approx(0.20, abs=1e-6)      # 300 / 1,500 (예전엔 300/2,500 = 0.12)


def test_minority_interest_read_from_xbrl():
    """(08-c) `MinorityInterest` 또는 (총자본 − 모회사 자본)으로 비지배지분을 복원한다."""
    years = [2018, 2019, 2020, 2021, 2022]
    base = {tag: [] for tag in ("Revenues", "OperatingIncomeLoss", "NetIncomeLoss",
                                "NetCashProvidedByUsedInOperatingActivities",
                                "PaymentsToAcquirePropertyPlantAndEquipment",
                                "DepreciationDepletionAndAmortization")}
    for y in years:
        base["Revenues"].append(_annual(y, 100 * USD, f"{y + 1}-02-10", f"A-{y}"))
        base["OperatingIncomeLoss"].append(_annual(y, 20 * USD, f"{y + 1}-02-10", f"A-{y}"))
        base["NetIncomeLoss"].append(_annual(y, 15 * USD, f"{y + 1}-02-10", f"A-{y}"))
        base["NetCashProvidedByUsedInOperatingActivities"].append(_annual(y, 18 * USD, f"{y + 1}-02-10", f"A-{y}"))
        base["PaymentsToAcquirePropertyPlantAndEquipment"].append(_annual(y, 4 * USD, f"{y + 1}-02-10", f"A-{y}"))
        base["DepreciationDepletionAndAmortization"].append(_annual(y, 3 * USD, f"{y + 1}-02-10", f"A-{y}"))
    base["Assets"] = [_instant(y, 200 * USD, f"{y + 1}-02-10", f"A-{y}") for y in years]
    base["Liabilities"] = [_instant(y, 110 * USD, f"{y + 1}-02-10", f"A-{y}") for y in years]
    base["StockholdersEquity"] = [_instant(y, 87 * USD, f"{y + 1}-02-10", f"A-{y}") for y in years]
    base["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"] = [
        _instant(y, 90 * USD, f"{y + 1}-02-10", f"A-{y}") for y in years]
    base["CashAndCashEquivalentsAtCarryingValue"] = [_instant(y, 10 * USD, f"{y + 1}-02-10", f"A-{y}")
                                                     for y in years]
    base["WeightedAverageNumberOfDilutedSharesOutstanding"] = [
        {"start": f"{y}-01-01", "end": f"{y}-12-31", "val": 10 * 1e6, "form": "10-K",
         "fp": "FY", "filed": f"{y + 1}-02-10", "accn": f"A-{y}"} for y in years]
    f, _ = build_fundamentals(_facts(base), AS_OF)
    assert f is not None
    assert f.minority_interest == pytest.approx(3.0)      # 90 − 87 (백만 달러)
    assert f.equity[0] == pytest.approx(87.0)             # Graham BPS 는 모회사 귀속 자본


# ═══════════════════════ 통합 경계 (트랙 A ↔ 트랙 B) ═══════════════════════
def test_sec_acceptance_helper_is_one_path_to_dates():
    """(07) `sec_edgar` 의 접수 시각 폴백은 `engine.utils.dates` 의 **같은 정의**를 쓴다."""
    from engine.data import sec_edgar
    from engine.utils.dates import et_day_end, sec_conservative_available_at

    # 이름만 다른 같은 함수 — 별도 구현이 생기면 이 테스트가 먼저 깨진다.
    assert sec_edgar.acceptance_fallback_available_at is sec_edgar.acceptance_available_at
    filed = date(2026, 11, 19)
    assert sec_edgar.acceptance_available_at(filed) == sec_conservative_available_at(filed, None)
    assert sec_edgar.acceptance_available_at(filed) == et_day_end(filed)
    # 접수 시각이 있으면 그 시각 그대로, 없으면 ET 하루 끝 — 두 경로 모두 dates 가 만든다.
    assert sec_edgar.acceptance_available_at("2026-11-19", "2026-11-19T21:30:00.000Z") == \
        sec_conservative_available_at(filed, "2026-11-19T21:30:00.000Z")
    stamp = datetime(2026, 11, 19, 23, 50, tzinfo=timezone.utc)
    assert sec_edgar.acceptance_available_at(filed, stamp) == stamp


def test_pipeline_record_carries_track_b_valuation_fields(tmp_path):
    """(통합) `build_payload` 의 `record["valuation"]` 에 트랙 B 필드가 실제 값으로 배관된다."""
    from pathlib import Path

    from engine.data import fixtures
    from engine.decide.pipeline import build_payload, load_params

    data = fixtures.dataset(AS_OF)
    ticker = next(iter(data["valuation_inputs"]))
    detail = {
        "iv_median": 120.0, "iv_low": 110.0, "iv_high": 130.0, "iv_band_basis": "bear_bull",
        "price": 100.0, "wacc": 0.095, "median_method": "epv", "methods": {"epv": 120.0},
        "growth": 0.0, "growth_raw_cagr": -0.0412, "growth_clipped_to_zero": True,
        "effective_fcff_years": 5, "normalization_method": "adjusted",
        "dcf_sensitivity": 0.0, "wacc_sensitivity": 0.1375, "terminal_g_sensitivity": 0.0525,
        "rate_sensitivity": 0.1375, "hist_divergence_signed": -0.61, "ev_ebit_current": 21.7,
        "ev_ebit_percentile": 42.0, "ev_ebit_samples": 118, "ev_ebit_years": 10,
        "ev_ebit_window": "2016-11-21~2026-11-19", "ev_ebit_reason": None,
        "minority_interest": 150.0, "debt_reporting": "reported", "v_penalties": 10.0,
    }
    data["valuation_inputs"][ticker] = detail
    times = {"data_cutoff_at": "2026-11-19T22:00:00Z", "signal_published_at": "2026-11-20T02:34:58Z",
             "order_cutoff_at": "2026-11-20T14:25:00Z", "target_session": "2026-11-20"}
    payload = build_payload(data, as_of=AS_OF, mode="postclose",
                            params=load_params(Path("config/params_v001.yaml")),
                            params_version="params_v001", code_commit="test",
                            snapshot_id="sha256:test", status="sample", three_times=times)
    record = next(s for s in payload["signals"] if s["ticker"] == ticker)
    val = record["valuation"]
    # 안전마진은 픽스처 패널의 분할조정 종가 기준이다 — 레코드의 price 와 같은 봉을 쓴다.
    assert val["margin"] == pytest.approx((120.0 - val["price"]) / 120.0, abs=1e-4)
    assert val["v_raw"] > 0                                     # 감점 전 V
    assert val["v_penalties"] == pytest.approx(10.0)             # 감점은 v1.3.2 그대로
    assert val["growth_raw_cagr"] == pytest.approx(-0.0412)
    assert val["growth_clipped_to_zero"] is True
    assert val["effective_fcff_years"] == 5
    assert val["normalization_method"] == "adjusted"
    assert val["wacc_sensitivity"] == pytest.approx(0.1375)
    assert val["terminal_g_sensitivity"] == pytest.approx(0.0525)
    assert val["hist_divergence_signed"] == pytest.approx(-0.61)
    assert val["ev_ebit_samples"] == 118 and val["ev_ebit_years"] == 10
    assert val["ev_ebit_window"] == "2016-11-21~2026-11-19"
    assert val["ev_ebit_reason"] is None
    assert val["minority_interest"] == pytest.approx(150.0)


def test_public_meta_and_today_carry_new_run_fields():
    """(01·02) 공개 `meta.json` 에 배치 시작·발행 지연·발행 회차가 실제로 실린다."""
    import scripts.build_public_data as bpd

    signals = {
        "as_of": "2026-11-20", "status": "normal", "mode": "postclose",
        "run_started_at": "2026-11-20T05:49:00Z", "signal_published_at": "2026-11-20T05:49:49Z",
        "publish_lag_seconds": 6.1, "published_after_order_cutoff": False,
        "inputs_hash": "sha256:" + "a" * 64,
        "diagnostics": {"wacc_risk_free": {"source": "FRED:DGS10", "value": 0.051,
                                           "fallback": False, "note_ko": None}},
        "signals": [{"ticker": "FIXP", "signal_id": "2026-11-20-FIXP-WATCH-002", "revision": 2,
                     "supersedes": "2026-11-20-FIXP-WATCH-001", "decision": "WATCH"}],
    }
    rules = bpd.load_allowlist()
    out = bpd.build(signals, None, None, rules)
    meta = out["meta.json"]
    assert meta["run_started_at"] == "2026-11-20T05:49:00Z"
    assert meta["publish_lag_seconds"] == pytest.approx(6.1)
    assert meta["published_after_order_cutoff"] is False
    assert meta["wacc_risk_free"]["fallback"] is False
    assert out["signals/today.json"]["signals"][0]["revision"] == 2
    assert out["signals/today.json"]["signals"][0]["supersedes"].endswith("-001")
