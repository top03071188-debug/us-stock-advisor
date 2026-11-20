"""test_quant_signals_schema — 데이터 계약 스키마·발행 레코드 검증 (기획서 2.2.2, Phase 2 DoD)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from engine.decide.macro import classify_regime
from engine.decide.signals import target_band
from engine.ledger.record import publish_record
from engine.models import QuantSignalSchemaError, validate_quant_signals
from engine.schema import load_schema, validate as validate_schema

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas" / "quant_signals.schema.json"
SAMPLE = ROOT / "data" / "samples" / "quant_signals.example.json"


def signal(**kw) -> dict:
    base = dict(
        signal_id="2026-11-19-MSFT-BUY-001", ticker="MSFT", type="BUY", grade="B", entry_score=69.4,
        as_of="2026-11-19", data_cutoff_at="2026-11-18T22:00:00Z",
        signal_published_at="2026-11-18T22:02:11Z", order_cutoff_at="2026-11-19T14:25:00Z",
        generated_price=412.35, target_band=target_band(522.0, 412.35), expected_mdd=-0.09,
        plan_weight=0.05, stop_distance=0.09,
        evidence=[{"type": "fundamental", "metric": "FCF_margin_5y"}],
    )
    base.update(kw)
    return publish_record(**base)


def payload(signals: list[dict] | None = None) -> dict:
    regime = classify_regime({"dff": 4.0, "cpi_yoy": 3.0, "t10y2y": 0.4, "hy_oas_bp": 380,
                              "vix": 20.0, "dtwexbg": 120.0}, {"dff": 4.4, "cpi_yoy": 3.0, "dtwexbg": 120.5})
    return {
        "schema_version": 1,
        "generated_at": "2026-11-19T07:02:11+09:00",
        "data_cutoff_at": "2026-11-18T22:00:00Z",
        "signal_published_at": "2026-11-18T22:02:11Z",
        "order_cutoff_at": "2026-11-19T14:25:00Z",
        "as_of": "2026-11-19",
        "params_version": "params_v001",
        "snapshot_id": "sha256:" + "a" * 64,
        "code_commit": "deadbee",
        "regime": regime.as_dict(),
        "signals": signals if signals is not None else [signal()],
        "blocked": [{"ticker": "OTHER", "reason": "V01"}],
        "watchlist": [{"ticker": "MSFT", "sector": "Information Technology", "q": 82, "v": 71, "g": 60}],
        "ledger_summary": {"n_graded": 0},
        "portfolio": {"as_of": "2026-11-19", "cash_pct": 0.98, "invested_pct": 0.02,
                      "positions": [{"ticker": "MSFT", "weight": 0.02, "sector": "Information Technology"}]},
    }


def test_schema_file_loads():
    schema = load_schema(SCHEMA)
    assert schema["title"] == "quant_signals"
    assert "signal" in schema["$defs"]


def test_valid_payload_passes():
    summary = validate_quant_signals(payload())
    assert summary["ok"] is True and summary["n_signals"] == 1
    assert summary["signal_published_at"] == "2026-11-18T22:02:11Z"


def test_sample_file_matches_schema():
    stored = json.loads(SAMPLE.read_text(encoding="utf-8"))
    assert validate_quant_signals(stored)["ok"] is True


def test_missing_required_field_fails():
    p = payload()
    del p["portfolio"]
    with pytest.raises(QuantSignalSchemaError) as exc:
        validate_quant_signals(p)
    assert any("portfolio" in e for e in exc.value.errors)


def test_three_times_order_is_enforced():
    p = payload()
    p["signals"][0]["signal_published_at"] = "2026-11-18T20:00:00Z"     # 마감보다 이르다
    with pytest.raises(QuantSignalSchemaError) as exc:
        validate_quant_signals(p)
    assert any("시각" in e for e in exc.value.errors)


def test_execution_event_must_be_null_at_publish():
    p = payload()
    p["signals"][0]["execution_event"] = "buy_filled"
    with pytest.raises(QuantSignalSchemaError):
        validate_quant_signals(p)


def test_entry_plan_must_be_provisional():
    p = payload()
    p["signals"][0]["entry_plan"][1]["provisional"] = False
    with pytest.raises(QuantSignalSchemaError) as exc:
        validate_quant_signals(p)
    assert any("잠정가" in e for e in exc.value.errors)


def test_entry_plan_cannot_carry_fill_price():
    p = payload()
    p["signals"][0]["entry_plan"][0]["fill_price"] = 415.10
    with pytest.raises(QuantSignalSchemaError):
        validate_quant_signals(p)


def test_position_cannot_carry_stop_price_at_publish():
    p = payload()
    p["signals"][0]["position"]["stop_price"] = 377.74
    with pytest.raises(QuantSignalSchemaError):
        validate_quant_signals(p)


def test_forbidden_public_keys_are_rejected():
    for key in ("raw_close", "avg_cost", "shares", "body_excerpt_1500"):
        p = payload()
        p["signals"][0][key] = 1
        with pytest.raises(QuantSignalSchemaError) as exc:
            validate_quant_signals(p)
        assert any("공개 금지" in e for e in exc.value.errors)


def test_bad_regime_and_as_of_fail_schema():
    p = payload()
    p["regime"]["regime"] = "bull"
    with pytest.raises(QuantSignalSchemaError):
        validate_quant_signals(p)
    p2 = payload()
    p2["as_of"] = "19/11/2026"
    with pytest.raises(QuantSignalSchemaError):
        validate_quant_signals(p2)


def test_duplicate_signal_ids_fail():
    p = payload([signal(), signal()])
    with pytest.raises(QuantSignalSchemaError) as exc:
        validate_quant_signals(p)
    assert any("중복" in e for e in exc.value.errors)


def test_zero_signals_is_valid():
    p = payload([])
    assert validate_quant_signals(p)["n_signals"] == 0


def test_schema_rejects_wrong_types():
    p = copy.deepcopy(payload())
    p["signals"][0]["entry_score"] = "high"
    with pytest.raises(QuantSignalSchemaError):
        validate_quant_signals(p)


def test_schema_validate_returns_paths():
    schema = load_schema(SCHEMA)
    errors = validate_schema({"schema_version": "1"}, schema)
    assert errors and errors[0].startswith("$")
