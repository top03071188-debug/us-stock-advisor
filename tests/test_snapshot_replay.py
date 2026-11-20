"""test_snapshot_replay — 스냅샷 입력 해시 · 재생 = 라이브 (R10a·R10b).

감사 재현: manifest 파일이 `prices_AAPL.csv` 하나뿐이라 재무·거시·뉴스가 바뀌어도 `snapshot_id`
가 같았고, `replay()` 는 밴드 유무만으로 BUY/WATCH 를 정해 B0~B6·Veto·등급을 무시했다.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from engine.backtest.replay import fixed_times_from_manifest, replay
from engine.backtest.snapshot import (INPUTS_FILE, load_snapshot, load_snapshot_dataset, make_snapshot,
                                      verify_snapshot)
from engine.data import fixtures
from engine.data.prices import build_price_frame
from engine.decide.pipeline import build_payload, load_params
from engine.metrics.fundamentals import AnnualFundamentals

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 11, 20)
DECISION_AT = datetime(2026, 11, 19, 22, 0, tzinfo=timezone.utc)


def frame(ticker: str = "AAA", days: int = 5) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in pd.bdate_range("2026-11-13", periods=days)])
    close = pd.Series([100.0] * days, index=idx)
    hist = pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close,
                         "Adj Close": close, "Volume": 1_000_000.0}, index=idx)
    return build_price_frame(ticker, hist, None).frame


def times_for(as_of: date = AS_OF) -> dict:
    return {"data_cutoff_at": "2026-11-19T22:00:00Z", "signal_published_at": "2026-11-19T22:02:11Z",
            "order_cutoff_at": "2026-11-20T14:25:00Z", "target_session": as_of.isoformat()}


# ── (a) 입력 전체를 해시한다 ───────────────────────────────────────────────────
def test_snapshot_id_changes_with_fundamentals(tmp_path):
    frames = {"prices_AAA": frame()}
    base_inputs = {"macro": {"vix": 18.4}, "fundamentals": {"AAA": {"ebit": [5000]}},
                   "news": {}, "filings": {}}
    a = make_snapshot(tmp_path / "a", DECISION_AT, frames, as_of=AS_OF.isoformat(), inputs=base_inputs)
    b = make_snapshot(tmp_path / "b", DECISION_AT, frames, as_of=AS_OF.isoformat(), inputs=base_inputs)
    assert a.snapshot_id == b.snapshot_id, "같은 입력이면 같은 snapshot_id 여야 한다"
    assert INPUTS_FILE in a.files

    for key in ("macro", "fundamentals", "news", "filings"):
        changed = json.loads(json.dumps(base_inputs))
        changed[key] = {**(changed.get(key) or {}), "_changed": 1}
        c = make_snapshot(tmp_path / f"c_{key}", DECISION_AT, frames, as_of=AS_OF.isoformat(),
                          inputs=changed)
        assert c.snapshot_id != a.snapshot_id, f"{key} 가 바뀌었는데 snapshot_id 가 같다"


def test_snapshot_verify_detects_inputs_tampering(tmp_path):
    snap = make_snapshot(tmp_path / "s", DECISION_AT, {"prices_AAA": frame()},
                         as_of=AS_OF.isoformat(), inputs={"macro": {"vix": 18.4}})
    assert verify_snapshot(snap.root)["ok"] is True
    path = snap.root / INPUTS_FILE
    path.write_text('{"macro":{"vix":99.0}}', encoding="utf-8")
    report = verify_snapshot(snap.root)
    assert report["ok"] is False
    assert any(p.startswith("hash_mismatch:inputs.json") for p in report["problems"])


# ── (b) 재생 데이터셋 복원 ────────────────────────────────────────────────────
def test_snapshot_roundtrip_dataset(tmp_path):
    fundamentals = AnnualFundamentals(
        ebit=[5000, 4900, 4800, 4700, 4600], revenue=[25000, 24000, 23000, 22000, 21000],
        net_income=[3100, 3000, 2900, 2800, 2700], ocf=[3800, 3700, 3600, 3500, 3400],
        capex=[700, 690, 680, 670, 660], depreciation=[600, 590, 580, 570, 560],
        total_assets=[20000, 19500, 19000, 18500, 18000],
        non_interest_liabilities=[4000, 3900, 3800, 3700, 3600],
        excess_cash=[1000, 950, 900, 850, 800], total_debt=[3000, 3000, 3100, 3100, 3200],
        cash=[1200, 1150, 1100, 1050, 1000], equity=[9000, 8800, 8600, 8400, 8200],
        diluted_shares=[1000, 1000, 1005, 1010, 1015], interest_expense=[150, 150, 155, 160, 165],
    )
    snap = make_snapshot(tmp_path / "s", DECISION_AT, {"prices_AAA": frame()},
                         as_of=AS_OF.isoformat(),
                         inputs={"fundamentals": {"AAA": fundamentals},
                                 "when": datetime(2026, 11, 19, 21, 0, tzinfo=timezone.utc),
                                 "nan_value": float("nan"), "tags": {"b", "a"}})
    data = load_snapshot_dataset(snap.root)
    restored = data["fundamentals"]["AAA"]
    assert isinstance(restored, AnnualFundamentals)
    assert restored.ebit == fundamentals.ebit
    assert "AAA" in data["panels"] and not data["panels"]["AAA"].frame.empty
    assert data["nan_value"] is None
    assert data["tags"] == ["a", "b"]


# ── (c) 라이브와 재생이 같은 결정 함수를 쓴다 ─────────────────────────────────
def test_live_and_replay_agree(tmp_path):
    data = fixtures.dataset(AS_OF)
    params = load_params(ROOT / "config" / "params_v001.yaml")
    live_payload = build_payload(
        data, as_of=AS_OF, mode="postclose", params=params, params_version="params_v001",
        code_commit="live", snapshot_id="sha256:live", status="sample",
        three_times=times_for(), held=[], sector_weights={},
    )
    assert live_payload["signals"], "픽스처에서 신호가 하나도 없다 — 비교할 대상이 없다"

    frames = {f"prices_{ticker}": panel.frame for ticker, panel in data["panels"].items()}
    inputs = {k: v for k, v in data.items() if k != "panels"}
    snap = make_snapshot(tmp_path / "snap", DECISION_AT, frames, as_of=AS_OF.isoformat(),
                         inputs=inputs, extra={"mode": "postclose"})
    replayed = replay(snap, params)

    assert fixed_times_from_manifest(snap.manifest)["target_session"] == AS_OF.isoformat()
    live_by_ticker = {s["ticker"]: s for s in live_payload["signals"]}
    replay_by_ticker = {s["ticker"]: s for s in replayed.signals}
    assert set(live_by_ticker) == set(replay_by_ticker)
    for ticker, live_sig in live_by_ticker.items():
        rep = replay_by_ticker[ticker]
        assert rep["trade_grade"] == live_sig["trade_grade"]
        assert rep["quant_intent"] == live_sig["quant_intent"]
        assert rep["gate_summary"] == live_sig["gate_summary"]
        assert rep["veto"] == live_sig["veto"]
        assert rep["entry_band"] == live_sig["entry_band"]
    assert {b["ticker"]: b["reason"] for b in replayed.blocked} == \
           {b["ticker"]: b["reason"] for b in live_payload["blocked"]}


def test_replay_digest_is_stable_and_ignores_timestamps(tmp_path):
    data = fixtures.dataset(AS_OF)
    params = load_params(ROOT / "config" / "params_v001.yaml")
    frames = {f"prices_{ticker}": panel.frame for ticker, panel in data["panels"].items()}
    inputs = {k: v for k, v in data.items() if k != "panels"}
    snap = make_snapshot(tmp_path / "snap", DECISION_AT, frames, as_of=AS_OF.isoformat(), inputs=inputs)
    first = replay(snap, params)
    second = replay(load_snapshot(snap.root), params)
    assert first.digest == second.digest
    assert first.to_json() == second.to_json()
    assert first.snapshot_id == snap.snapshot_id


def test_replay_uses_band_and_gates_not_a_minimal_loop(tmp_path):
    """재생 결과에 등급·실패 게이트·Veto 가 실린다(밴드 유무만 보던 최소 루프의 제거 확인)."""
    data = fixtures.dataset(AS_OF)
    params = load_params(ROOT / "config" / "params_v001.yaml")
    frames = {f"prices_{ticker}": panel.frame for ticker, panel in data["panels"].items()}
    inputs = {k: v for k, v in data.items() if k != "panels"}
    snap = make_snapshot(tmp_path / "snap", DECISION_AT, frames, as_of=AS_OF.isoformat(), inputs=inputs)
    result = replay(snap, params)
    grades = {s.get("trade_grade") for s in result.signals}
    assert grades - {"BUY", "STRONG_BUY", "WATCH", "NONE"} == set()
    assert any(s.get("gate_summary") for s in result.signals)
    assert result.regime.get("regime") in ("risk_on", "neutral", "risk_off")
