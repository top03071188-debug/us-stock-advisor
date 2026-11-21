"""test_walkforward_calibrate — 스냅샷 보정 · 스트레스 백테스트 · 유니버스 이력 빌더 (실가동 배선).

검증하는 계약:

1. `build_snapshot_evaluator` — 스냅샷 재생 → 채점 → **필수 메타가 붙은** `FoldObservation`.
   후보 파라미터가 실제로 엔진에 닿아 폴드 점수가 달라진다(수락·기각 둘 다).
2. `run_stress_backtest` — 3구간 실측 지표 + `universe_history`·`delisted_securities` 미검증 시
   **게이트는 미검증**(`stress_pass=None`, `gate_passed=False`)이고 실측은 `reference_metrics` 로만 남는다.
3. `scripts/build_universe_history.py` — HTML 파서(순수 함수)·이력 재구성·`validate_universe`.
4. `scripts/run_walkforward.py --calibrate/--stress/--quarterly/--semiannual` CLI.
5. `scripts/run_daily.py` 의 `stress_payload()` 가 이미 계산된 스트레스 리포트를 덮어쓰지 않는다.
"""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from engine.backtest.calibration import calibrate
from engine.backtest.pipeline_runner import (FoldPlan, build_offline_stress_panels,
                                             build_snapshot_evaluator, grade_signal,
                                             run_calibration_cycle, run_stress_backtest,
                                             verify_universe_inputs, write_versioned_params)
from engine.backtest.pipeline_runner import indicator_table
from engine.backtest.snapshot import load_snapshot_dataset, make_snapshot
from engine.data import fixtures
from engine.data.prices import build_price_frame
from engine.data.universe import UNIVERSE_COLUMNS, load_universe_history, membership_at, validate_universe
from engine.decide.pipeline import load_params
from engine.utils.dates import add_trading_days, prev_trading_day
from scripts import build_universe_history, run_walkforward

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 11, 20)
HOLDOUT_START = date(2026, 8, 20)
#: `build_folds(TODAY, horizons=(63,))` 의 적격 폴드 시작일(2026-08-20 홀드아웃 기준).
FOLD_STARTS = (date(2025, 11, 19), date(2026, 2, 19), date(2026, 5, 19))
HORIZON = 63
#: 스냅샷 합성 유니버스 — 마지막 종목(TSTC)만 **앞으로 하락**해 채점이 나빠진다.
#: IV 배수 1.30 → 안전마진 23.1% (기준 20% 통과 · 후보 24% 에서 탈락).
SPECS = {
    "TSTA": {"multiple": 1.40, "forward": 0.0035},
    "TSTB": {"multiple": 1.55, "forward": 0.0030},
    "TSTC": {"multiple": 1.30, "forward": -0.0030},
}
SNAPSHOT_END_PAD = 1300


# ────────────────────────────── 공통 도우미 ──────────────────────────────
def snapshot_panels(as_of: date) -> dict:
    """스냅샷용 합성 가격 — 긴 상승 뒤 12% 눌림(`dip`)으로 B2·B4 가 성립한다."""
    end = prev_trading_day(as_of)
    return {ticker: build_price_frame(ticker, fixtures.synthetic_history(
                ticker, days=SNAPSHOT_END_PAD, end=end, pattern="dip"), None)
            for ticker in (*SPECS, "SPY")}


def forward_panel(ticker: str, base_date: date, base_price: float, drift: float, seed: int):
    """채점용 **미래 가격** 패널 — 스냅샷 마지막 봉의 가격에서 이어 붙인다(척도 일치)."""
    idx = pd.bdate_range(start=base_date, end=base_date + timedelta(days=420))
    rng = np.random.default_rng(seed)
    daily = np.full(len(idx), drift) + rng.normal(0, 0.004, len(idx))
    close = pd.Series(base_price * np.exp(np.cumsum(daily)), index=idx)
    hist = pd.DataFrame({"Open": close, "High": close * 1.002, "Low": close * 0.998, "Close": close,
                         "Adj Close": close, "Volume": 1e6}, index=idx)
    return build_price_frame(ticker, hist, None)


def make_fold_snapshot(root: Path, as_of: date, panels: dict, multiples: dict | None = None):
    """그 시점의 판단 입력을 고정한 스냅샷(합성) — 세 종목 모두 BUY 가 나오는 모양."""
    accepted = datetime.combine(as_of - timedelta(days=10), time(20, 0), tzinfo=timezone.utc)
    rows, filings, news = [], {}, {}
    factors = dict(multiples or {ticker: spec["multiple"] for ticker, spec in SPECS.items()})
    for ticker in SPECS:
        last = float(panels[ticker].frame["sadj_close"].iloc[-1])
        rows.append({"ticker": ticker, "sector": "Information Technology",
                     "iv_median": last * float(factors[ticker]), "q": 76.0, "g": 58.0,
                     "news_state": "read_done", "adv20_usd": 9e9, "days_to_earnings": 40,
                     "market_cap_usd": 2e11, "exchange": "NYSE"})
        filings[ticker] = [
            {"form": "10-Q", "filed_date": (as_of - timedelta(days=10)).isoformat(),
             "acceptance_datetime": accepted.isoformat().replace("+00:00", "Z"), "items": [],
             "url": f"https://www.sec.gov/{ticker}/10q", "accession": f"{ticker}-10q"},
            {"form": "10-K", "filed_date": (as_of - timedelta(days=40)).isoformat(),
             "acceptance_datetime": (accepted - timedelta(days=30)).isoformat().replace("+00:00", "Z"),
             "items": [], "url": f"https://www.sec.gov/{ticker}/10k", "accession": f"{ticker}-10k"},
        ]
        news[ticker] = []
    data = {"as_of": as_of.isoformat(), "universe": rows, "macro": {}, "macro_previous": {},
            "filings": filings, "news": news,
            "fundamentals": {t: fixtures._fundamentals(1.0) for t in SPECS},
            "valuation_inputs": {t: {} for t in SPECS}, "source": "fixtures"}
    frames = {f"prices_{t}": p.frame for t, p in panels.items()}
    cutoff = datetime.combine(prev_trading_day(as_of), time(22, 0), tzinfo=timezone.utc)
    return make_snapshot(root, cutoff, frames, as_of=as_of.isoformat(),
                         inputs={k: v for k, v in data.items() if k != "panels"},
                         extra={"mode": "postclose"})


def grading_provider(panels_by_fold: dict, drifts: dict | None = None):
    """폴드별 채점 패널 — 스냅샷 마지막 봉 가격에서 종목별 드리프트로 이어 붙인다."""
    table = dict(drifts or {ticker: spec["forward"] for ticker, spec in SPECS.items()})
    table["SPY"] = 0.0004

    def _provider(plan) -> dict:
        panels = panels_by_fold[plan.fold_id]
        base_date = prev_trading_day(plan.start)
        out = {}
        for i, (ticker, panel) in enumerate(sorted(panels.items())):
            last = float(panel.frame["raw_close"].iloc[-1])
            out[ticker] = forward_panel(ticker, base_date, last, table.get(ticker, 0.0), 7 + i)
        return out

    return _provider


@pytest.fixture
def folds(tmp_path) -> list[FoldPlan]:
    plans = []
    for i, start in enumerate(FOLD_STARTS, start=1):
        snap = make_fold_snapshot(tmp_path / "snapshots" / f"F{i}", start, snapshot_panels(start))
        plans.append(FoldPlan(f"F{i}", HORIZON, start, add_trading_days(start, HORIZON),
                              snap.root, snap.snapshot_id, start))
    return plans


@pytest.fixture
def grading_panels(folds) -> dict:
    panels_by_fold = {}
    for plan in folds:
        panels_by_fold[plan.fold_id] = load_snapshot_dataset(plan.root)["panels"]
    return grading_provider(panels_by_fold)


def evaluator(plans, panels, **kw):
    return build_snapshot_evaluator(base_params=load_params(ROOT / "config" / "params_v001.yaml"),
                                    today=TODAY, holdout_start=HOLDOUT_START, fold_plans=plans,
                                    grading_panels=panels, code_version="test", **kw)


# ────────────────────────────── ① 스냅샷 기반 보정 ──────────────────────────────
def test_snapshot_evaluator_returns_observations_with_required_metadata(folds, grading_panels):
    observations = evaluator(folds, grading_panels)({})
    assert len(observations) == len(FOLD_STARTS)
    for observation in observations:
        assert observation.maturity_date and observation.holdout_start
        assert observation.signal_count == len(SPECS)
        assert observation.code_version == "test" and observation.snapshot_id
        assert observation.horizon_td == HORIZON
        # 홀드아웃 경계: 만기가 홀드아웃 시작 이전이다(LA-13)
        assert observation.maturity_date < HOLDOUT_START.isoformat()
        assert observation.agreement_mean is not None


def test_snapshot_evaluator_without_grading_prices_cannot_grade(folds):
    """채점 가격이 없으면 **빈 관측**이다 — 없는 채점을 만들어 내지 않는다."""
    assert evaluator(folds, None)({}) == []


def test_calibrate_accepts_when_bad_signal_leaves_and_stress_passes(folds, grading_panels):
    run = evaluator(folds, grading_panels)
    record = calibrate(run, load_params(ROOT / "config" / "params_v001.yaml"), min_folds=3,
                       min_signals=1, stress_pass=lambda _params: True)
    assert record["accepted"] is True, record["reason"]
    assert record["gain"] >= 2.0 and record["improved_folds"] >= 2
    assert float(record["params"]["min_margin"]) == pytest.approx(0.24)
    # 후보마다 폴드를 다시 돌렸다 — 후보별 Agreement 가 실제로 갈린다
    gains = {round(row["agreement_gain"], 2) for row in record["candidates"] if "agreement_gain" in row}
    assert gains and gains != {0.0}


def test_calibrate_rejects_when_no_candidate_improves(tmp_path):
    """세 종목이 같은 안전마진·같은 미래 경로면 후보가 갈리지 않는다 → GAIN_TOO_SMALL(기각)."""
    plans, panels_by_fold = [], {}
    for i, start in enumerate(FOLD_STARTS, start=1):
        snap = make_fold_snapshot(tmp_path / "even" / f"F{i}", start, snapshot_panels(start),
                                  multiples={ticker: 1.40 for ticker in SPECS})
        plans.append(FoldPlan(f"F{i}", HORIZON, start, add_trading_days(start, HORIZON),
                              snap.root, snap.snapshot_id, start))
        panels_by_fold[f"F{i}"] = load_snapshot_dataset(snap.root)["panels"]
    flat = grading_provider(panels_by_fold, drifts={ticker: 0.0030 for ticker in SPECS})
    record = calibrate(evaluator(plans, flat), load_params(ROOT / "config" / "params_v001.yaml"),
                       min_folds=3, min_signals=1, stress_pass=lambda _params: True)
    assert record["accepted"] is False
    assert record["reason"].startswith("GAIN_TOO_SMALL")


def test_calibrate_defers_without_snapshots(tmp_path):
    """스냅샷이 없으면 적격 폴드가 0 → INSUFFICIENT_FOLDS(빈 입력을 수락으로 바꾸지 않는다)."""
    run = build_snapshot_evaluator(tmp_path / "empty", params_version="params_v001",
                                   base_params=load_params(ROOT / "config" / "params_v001.yaml"),
                                   today=TODAY, holdout_start=HOLDOUT_START)
    record = calibrate(run, load_params(ROOT / "config" / "params_v001.yaml"), min_folds=3,
                       min_signals=1, stress_pass=lambda _params: True)
    assert record["accepted"] is False
    assert record["reason"] == "INSUFFICIENT_FOLDS"


def test_calibration_cycle_requires_stress_gate_and_writes_records(tmp_path, folds, grading_panels):
    """스트레스 게이트가 미검증이면 후보를 채택하지 않고, 그래도 사유를 남긴다."""
    params_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", params_dir)
    before = (params_dir / "params_v001.yaml").read_text(encoding="utf-8")
    stress = {"gate_passed": False, "verified_all": False, "stress_pass": None, "reference_only": True}
    summary = run_calibration_cycle(
        today=TODAY, snapshot_root=tmp_path / "snapshots", params_path=params_dir / "params_v001.yaml",
        params_dir=params_dir, min_folds=3, min_signals=1, grading_panels=grading_panels,
        holdout_root=tmp_path / "walkforward" / "holdout", attempts_root=tmp_path / "calibration",
        walkforward_root=tmp_path / "walkforward", reports_dir=tmp_path / "reports",
        stress_payload=stress)
    record = summary["record"]
    assert record["accepted"] is False
    assert record["reason"] == "STRESS_FAILED"
    assert summary["params_version_new"] is None
    assert not list(params_dir.glob("params_v002.yaml")), "미검증 스트레스로 파라미터를 만들었다"
    assert (params_dir / "params_v001.yaml").read_text(encoding="utf-8") == before
    assert (tmp_path / "reports" / "calibration.md").exists()
    attempts = json.loads((tmp_path / "calibration" / "attempts.json").read_text(encoding="utf-8"))
    assert attempts["attempts"][-1]["reason"] == "STRESS_FAILED"
    walkforward_attempts = json.loads((tmp_path / "walkforward" / "attempts.json").read_text(encoding="utf-8"))
    assert walkforward_attempts["calibrations"][-1]["accepted"] is False


def test_calibration_cycle_writes_new_params_version_when_accepted(tmp_path, folds, grading_panels):
    params_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", params_dir)
    stress = {"gate_passed": True, "verified_all": True, "stress_pass": True, "reference_only": False}
    summary = run_calibration_cycle(
        today=TODAY, snapshot_root=tmp_path / "snapshots", params_path=params_dir / "params_v001.yaml",
        params_dir=params_dir, min_folds=3, min_signals=1, grading_panels=grading_panels,
        holdout_root=tmp_path / "walkforward" / "holdout", attempts_root=tmp_path / "calibration",
        walkforward_root=tmp_path / "walkforward", reports_dir=tmp_path / "reports",
        stress_payload=stress)
    assert summary["record"]["accepted"] is True
    new_path = Path(summary["params_path"])
    assert new_path.name == "params_v002.yaml" and new_path.exists()
    assert (params_dir / "params_v001.yaml").read_text(encoding="utf-8").count("version: params_v001") == 1
    text = new_path.read_text(encoding="utf-8")
    assert "version: params_v002" in text
    assert "# [보정]" in text, "주석을 살린 패치가 아니다"
    updated = load_params(new_path)
    assert updated["version"] == "params_v002"
    assert updated["entry_gates"]["margin_of_safety"]["neutral"] == pytest.approx(0.24)
    assert updated["entry_gates"]["margin_of_safety"]["risk_off"] == pytest.approx(0.29)
    report = (tmp_path / "reports" / "calibration.md").read_text(encoding="utf-8")
    assert "채택" in report and "params_v002" in report


# ────────────────────────────── ② 스트레스 백테스트 ──────────────────────────────
def test_stress_backtest_measures_all_three_windows_but_gate_stays_unverified():
    panels, vix = build_offline_stress_panels()
    payload = run_stress_backtest(panels=panels, vix=vix, data_source="offline_demo")
    assert [row["key"] for row in payload["results"]] == ["2022_rate_hike", "2020_covid", "2018q4"]
    for row in payload["results"]:
        metrics = row["reference_metrics"]
        assert row["verified"] is False and row["pass"] is None
        assert row["reference_only"] is True
        assert isinstance(metrics["strategy_mdd"], float)
        assert isinstance(metrics["benchmark_mdd"], float)
        assert isinstance(metrics["v06_fired"], bool)
        assert isinstance(metrics["new_buys_during_crash"], int)
        assert metrics["n_days"] > 20
        # 일별 진행(process_day)과 run_scenario 재실행의 자산곡선이 같다(결정론·같은 경로)
        assert metrics["determinism_check"] is True
    # 안전핀: 데이터가 없으면 통과가 아니다
    assert payload["gate_passed"] is False
    assert payload["stress_pass"] is None
    assert payload["verified_all"] is False
    assert payload["reference_only"] is True
    assert "UNIVERSE_HISTORY_MISSING" in payload["verdict"]["reasons"]
    assert "DELISTED_MISSING" in payload["verdict"]["reasons"]
    # 실측이 실제로 돌았다(신호일·주문·체결이 0 이 아니다)
    assert sum(row["n_orders"] or 0 for row in payload["results"]) > 0
    # 2022 구간은 VIX 급등으로 V06 이 발동했다
    assert next(row for row in payload["results"] if row["key"] == "2022_rate_hike")[
        "reference_metrics"]["v06_fired"] is True


def _complete_universe(size: int = 500) -> pd.DataFrame:
    rows = [{"date": "2010-01-04", "ticker": f"T{i:03d}", "index_name": "SP500",
             "sector": "Industrials", "added_at": "2010-01-04", "removed_at": "",
             "announcement_at": "", "effective_at": "2010-01-04"} for i in range(size)]
    frame = pd.DataFrame(rows, columns=UNIVERSE_COLUMNS)
    for column in ("added_at", "removed_at", "announcement_at", "effective_at"):
        frame[column] = pd.to_datetime(frame[column], errors="coerce")
    return frame


def test_stress_backtest_judges_gate_only_with_verified_universe_and_delisted():
    panels, vix = build_offline_stress_panels()
    history = _complete_universe()
    delisted = pd.DataFrame([{"ticker": "OLD1", "cik": None, "delist_date": "2019-05-01",
                              "delist_type": "merger", "final_price": 12.0,
                              "delisting_return": -0.02, "source": "test"}])
    payload = run_stress_backtest(panels=panels, vix=vix, universe_history=history,
                                  delisted=delisted, delisted_complete=True, data_source="test")
    assert payload["universe_verified"] is True and payload["verified_all"] is True
    assert payload["reference_only"] is False
    assert payload["stress_pass"] is not None, "검증된 입력인데 판정을 비웠다"
    assert payload["gate_passed"] is (payload["stress_pass"] is True)
    assert payload["universe_source"] == "universe_history"
    for row in payload["results"]:
        assert row["verified"] is True and row["pass"] is not None


def test_stress_backtest_keeps_gate_closed_when_delisted_is_missing():
    panels, vix = build_offline_stress_panels()
    payload = run_stress_backtest(panels=panels, vix=vix, universe_history=_complete_universe(),
                                  delisted=None, data_source="test")
    assert payload["universe_verified"] is False
    assert payload["gate_passed"] is False and payload["stress_pass"] is None
    reasons = payload["verdict"]["reasons"]
    assert "DELISTED_MISSING" in reasons
    assert payload["verdict"]["universe_history_verified"] is True


def test_verify_universe_inputs_rejects_incomplete_membership_and_unknown_liquidations():
    partial = _complete_universe(size=400)          # 지수 규모(500)에서 20% 모자란다
    delisted = pd.DataFrame([{"ticker": "OLD1", "delist_date": "2019-05-01",
                              "delisting_return": None}])        # 수익률 미확보(불명 청산)
    verdict = verify_universe_inputs(partial, delisted, delisted_complete=True)
    assert verdict["universe_verified"] is False
    assert any(reason.startswith("UNIVERSE_MEMBER_COUNT") for reason in verdict["reasons"])
    assert any(reason.startswith("DELISTED_UNKNOWN_LIQUIDATION") for reason in verdict["reasons"])
    # 완전성 선언이 없으면 파일만으로 '완전'을 주장하지 않는다
    good = pd.DataFrame([{"ticker": "OLD1", "delist_date": "2019-05-01", "final_price": 12.0,
                          "delisting_return": -0.02}])
    assert verify_universe_inputs(_complete_universe(), good)["delisted_verified"] is False
    assert verify_universe_inputs(_complete_universe(), good, delisted_complete=True)[
        "universe_verified"] is True


def test_grade_signal_returns_none_when_maturity_bar_is_missing():
    """만기 봉이 데이터에 없으면 채점하지 않는다 — 없는 채점을 만들지 않는다."""
    panel = forward_panel("TSTA", date(2027, 1, 4), 100.0, 0.001, 3)
    tables = {"TSTA": indicator_table(panel)}
    near_end = panel.frame.index[-10].date().isoformat()      # 만기(63거래일)까지 봉이 모자란다
    signal = {"ticker": "TSTA", "entry_score": 80.0, "as_of": near_end,
              "target_band": {"base_price": 100.0, "lo": 105.0, "hi": 110.0,
                              "base_bar_date": near_end}}
    assert grade_signal(signal, tables, None, HORIZON) is None


# ────────────────────────────── ③ 유니버스 이력 빌더 ──────────────────────────────
FIXTURE_CONSTITUENTS = """
<html><body><table>
<tr><th>Symbol</th><th>Security</th><th>GICS Sector</th><th>Date added</th></tr>
<tr><td>AAA</td><td>Alpha Co.</td><td>Information Technology</td><td>2010-01-04</td></tr>
<tr><td>BBB<sup>[1]</sup></td><td>Beta Inc.</td><td>Health Care</td><td>2015-06-19</td></tr>
<tr><td>CCC</td><td>Gamma Ltd.</td><td>Energy</td><td>2019-03-18</td></tr>
</table></body></html>
"""
FIXTURE_CHANGES = """
<html><body><table>
<tr><th>Effective Date</th><th colspan="2">Added</th><th colspan="2">Removed</th><th>Reason</th></tr>
<tr><th></th><th>Ticker</th><th>Security</th><th>Ticker</th><th>Security</th><th></th></tr>
<tr><td>2015-01-02</td><td>ZZZ</td><td>Zeta</td><td></td><td></td><td>new</td></tr>
<tr><td rowspan="2">2018-05-04</td><td>QQQ</td><td>Quux</td><td>ZZZ</td><td>Zeta</td><td>swap</td></tr>
<tr><td></td><td></td><td>YYY</td><td>Ypsilon</td><td>swap</td></tr>
<tr><td>2019-03-18</td><td>CCC</td><td>Gamma Ltd.</td><td>BBB</td><td>Beta Inc.</td><td>swap</td></tr>
</table></body></html>
"""


def test_parse_constituents_and_changes_handles_rowspan_and_footnotes():
    constituents = build_universe_history.parse_constituents_html(FIXTURE_CONSTITUENTS)
    assert [row["ticker"] for row in constituents] == ["AAA", "BBB", "CCC"]
    assert constituents[1]["sector"] == "Health Care"
    changes = build_universe_history.parse_changes_html(FIXTURE_CHANGES)
    assert {(row["effective_date"], row["added"], row["removed"]) for row in changes} == {
        ("2015-01-02", "ZZZ", ""), ("2018-05-04", "QQQ", "ZZZ"),
        ("2018-05-04", "", "YYY"), ("2019-03-18", "CCC", "BBB")}


def test_build_universe_rows_folds_intervals_and_drops_unresolved_removals():
    constituents = build_universe_history.parse_constituents_html(FIXTURE_CONSTITUENTS)
    changes = build_universe_history.parse_changes_html(FIXTURE_CHANGES)
    rows, stats = build_universe_history.build_universe_rows(constituents, changes)
    frame = build_universe_history.to_frame(rows)
    assert validate_universe(frame)["ok"] is True
    by_ticker = {row["ticker"]: row for row in rows}
    assert by_ticker["AAA"]["added_at"] == "2010-01-04"           # 현재 구성종목 표의 편입일
    assert by_ticker["BBB"]["removed_at"] == "2019-03-18"         # 편출 기록이 구간을 닫았다
    assert by_ticker["CCC"]["removed_at"] == ""                   # 현재 구성원
    assert by_ticker["ZZZ"]["removed_at"] == "2018-05-04"
    assert "QQQ" in stats["unresolved_removals"], "편출 기록 없는 과거 종목은 구간을 버리고 센다"
    assert "QQQ" not in by_ticker and "YYY" not in by_ticker
    at_2018 = membership_at(frame, datetime(2018, 6, 1, tzinfo=timezone.utc))
    assert {"AAA", "BBB"} <= at_2018 and "CCC" not in at_2018 and "ZZZ" not in at_2018
    assert "QQQ" not in at_2018, "편출일을 모르는 구간을 '아직 구성원'으로 남기지 않는다"


def test_build_universe_history_cli_writes_csv_and_reports_delisted_gap(tmp_path):
    constituents_path = tmp_path / "constituents.html"
    changes_path = tmp_path / "changes.html"
    constituents_path.write_text(FIXTURE_CONSTITUENTS, encoding="utf-8")
    changes_path.write_text(FIXTURE_CHANGES, encoding="utf-8")
    out = tmp_path / "universe_history.csv"
    report_path = tmp_path / "report.json"
    code = build_universe_history.main([
        "--offline-html", str(constituents_path), "--changes-html", str(changes_path),
        "--out", str(out), "--json-report", str(report_path), "--delisted", str(tmp_path / "d.json")])
    assert code == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "ok" and report["validation"]["ok"] is True
    assert report["delisted"]["exists"] is False and report["delisted"]["complete"] is False
    frame = load_universe_history(str(out))
    assert list(frame.columns) == UNIVERSE_COLUMNS and not frame.empty


def test_build_universe_history_without_input_returns_3(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("network down")

    monkeypatch.setattr(build_universe_history, "fetch_html", boom)
    code = build_universe_history.main(["--out", str(tmp_path / "x.csv"),
                                        "--delisted", str(tmp_path / "d.csv")])
    assert code == 3


def test_write_versioned_params_patches_values_and_keeps_comments(tmp_path):
    params_path = tmp_path / "params_v001.yaml"
    shutil.copy(ROOT / "config" / "params_v001.yaml", params_path)
    engine = load_params(params_path)
    engine["entry_gates"]["margin_of_safety"] = {"risk_on": 0.19, "neutral": 0.24, "risk_off": 0.29}
    engine["entry_gates"]["rsi_buy"] = 38
    path = write_versioned_params(params_path, engine, {"min_margin": 0.24, "rsi_buy": 38},
                                  "params_v002", TODAY, {"gain": 3.5, "improved_folds": 2})
    text = path.read_text(encoding="utf-8")
    assert "rsi_buy: 38" in text and "neutral: 0.24" in text
    assert "[보정]" in text and "[고정]" in text
    updated = load_params(path)
    assert updated["entry_gates"]["margin_of_safety"]["neutral"] == pytest.approx(0.24)
    assert updated["entry_gates"]["rsi_buy"] == pytest.approx(38.0)
    assert updated["universe"]["index"] == "SP500"          # 나머지 설정은 그대로


# ────────────────────────────── ④ CLI ──────────────────────────────
@pytest.fixture
def cli_root(tmp_path, monkeypatch) -> Path:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    shutil.copytree(ROOT / "schemas", tmp_path / "schemas")
    (tmp_path / "data" / "snapshots").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_cli_stress_offline_demo_writes_stress_json(cli_root):
    assert run_walkforward.main(["--stress", "--offline-demo", "--today", TODAY.isoformat()]) == 0
    payload = json.loads((cli_root / "data" / "walkforward" / "stress.json").read_text(encoding="utf-8"))
    assert payload["data_source"] == "offline_demo"
    assert payload["gate_passed"] is False and payload["stress_pass"] is None
    assert len(payload["results"]) == 3
    assert all(row["reference_metrics"] for row in payload["results"])


def test_cli_calibrate_defers_without_snapshots(cli_root):
    assert run_walkforward.main(["--calibrate", "--today", TODAY.isoformat()]) == 0
    attempts = json.loads((cli_root / "data" / "calibration" / "attempts.json").read_text(encoding="utf-8"))
    assert attempts["attempts"][-1]["reason"] == "INSUFFICIENT_FOLDS"
    report = (cli_root / "reports" / "calibration.md").read_text(encoding="utf-8")
    assert "보류/기각" in report


def test_cli_quarterly_runs_plan_and_calibration(cli_root):
    assert run_walkforward.main(["--quarterly", "--today", TODAY.isoformat()]) == 0
    assert (cli_root / "data" / "walkforward" / "folds" / "summary.json").exists()
    assert (cli_root / "reports" / "calibration.md").exists()
    walkforward_attempts = json.loads(
        (cli_root / "data" / "walkforward" / "attempts.json").read_text(encoding="utf-8"))
    assert walkforward_attempts["calibrations"], "보정 시도가 attempts.json 에 없다"
    assert walkforward_attempts["attempts"], "계획 항목을 지웠다"


def test_cli_plan_rerun_keeps_calibration_history(cli_root):
    """`--quarterly` 뒤에 `--semiannual` 을 돌려도 보정 이력이 지워지지 않는다."""
    assert run_walkforward.main(["--quarterly", "--today", TODAY.isoformat()]) == 0
    records = json.loads((cli_root / "data" / "calibration" / "attempts.json").read_text(encoding="utf-8"))
    calibrations = [row for row in records["attempts"] if row.get("kind") == "calibrate"]
    assert calibrations, "보정 시도 기록이 없다"
    assert run_walkforward.main(["--semiannual", "--offline-demo", "--today", TODAY.isoformat()]) == 0
    records = json.loads((cli_root / "data" / "calibration" / "attempts.json").read_text(encoding="utf-8"))
    assert [row for row in records["attempts"] if row.get("kind") == "calibrate"] == calibrations
    assert records["kind"] == "plan" and records["attempts"]
    wf = json.loads((cli_root / "data" / "walkforward" / "attempts.json").read_text(encoding="utf-8"))
    assert wf["calibrations"], "walkforward attempts 의 보정 이력이 사라졌다"


def test_cli_semiannual_runs_extended_plan_and_stress(cli_root):
    assert run_walkforward.main(["--semiannual", "--offline-demo", "--today", TODAY.isoformat()]) == 0
    payload = json.loads((cli_root / "data" / "walkforward" / "stress.json").read_text(encoding="utf-8"))
    assert payload["results"] and payload["gate_passed"] is False
    summary = json.loads(
        (cli_root / "data" / "walkforward" / "folds" / "summary.json").read_text(encoding="utf-8"))
    assert summary["horizons"], "폴드 계획이 없다"


# ────────────────────────────── ⑤ run_daily 보존 ──────────────────────────────
def test_run_daily_stress_payload_keeps_existing_report(cli_root):
    from scripts import run_daily

    path = cli_root / "data" / "walkforward" / "stress.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    panels, vix = build_offline_stress_panels()
    from engine.backtest.pipeline_runner import write_stress_payload
    write_stress_payload(run_stress_backtest(panels=panels, vix=vix, data_source="offline_demo"), path.parent)
    before = json.loads(path.read_text(encoding="utf-8"))
    assert before["results"][0]["reference_metrics"]

    payload = run_daily.stress_payload()
    assert payload["generated_at"] == before["generated_at"]
    assert payload["results"][0]["reference_metrics"] == before["results"][0]["reference_metrics"]
    assert payload["gate_passed"] is False


def test_run_daily_stress_payload_falls_back_to_unverified_report(cli_root):
    from scripts import run_daily

    payload = run_daily.stress_payload()
    assert payload["verified_all"] is False and payload["gate_passed"] is False
    assert payload["stress_pass"] is None
    assert "universe_history" in payload["missing_inputs"]
