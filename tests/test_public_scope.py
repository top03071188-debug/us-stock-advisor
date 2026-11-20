"""test_public_scope — Pages 산출물에 원시 시세·보유 정보가 **0건**인지 검사 (Phase 3 DoD).

`build_public_data.py` 의 출력은 공개 Pages 에 그대로 올라간다. 그래서 이 테스트는
① 금지 키 0건 ② 파일당 200KB 이하 ③ 필요한 파일이 모두 있는지 ④ 차트에 원시 가격이 없는지를 본다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from engine.models import FORBIDDEN_KEYS
from scripts import build_public_data as bpd

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / "config" / "public_allowlist.yaml"
SAMPLE = ROOT / "data" / "samples" / "quant_signals.example.json"
FOLDS = ROOT / "data" / "walkforward" / "folds" / "summary.json"
MAX_BYTES = 200 * 1024


def _rules() -> dict:
    with open(ALLOWLIST, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


@pytest.fixture(scope="module")
def outputs() -> dict:
    signals = json.loads(SAMPLE.read_text(encoding="utf-8"))
    folds = json.loads(FOLDS.read_text(encoding="utf-8")) if FOLDS.exists() else {}
    records = [{
        "signal_id": signals["signals"][0]["signal_id"], "ticker": "SAMPLE", "as_of": signals["as_of"],
        "type": "BUY", "grade": "B", "trade_grade": "BUY", "entry_score": 69.4,
        "quant_intent": "pending_buy", "thesis_ko": "예시", "params_version": "params_v001",
        "position": signals["signals"][0]["position"],
        "target_band": signals["signals"][0]["target_band"],
    }]
    return bpd.build(signals, folds, records, _rules(),
                     params=yaml.safe_load((ROOT / "config" / "params_v001.yaml").read_text(encoding="utf-8")),
                     veto=yaml.safe_load((ROOT / "config" / "veto_rules.yaml").read_text(encoding="utf-8")))


def test_required_files_exist(outputs):
    for name in ("meta.json", "signals/today.json", "ledger/summary.json", "ledger/records.json",
                 "ledger/portfolio.json", "radar/13f.json", "config.json"):
        assert name in outputs, f"{name} 가 없다 — 5.7 정적 데이터 계약"
    assert any(name.startswith("tickers/") for name in outputs), "종목 상세 파일이 하나도 없다"


def test_no_forbidden_keys_or_personal_data(outputs):
    deny = set(_rules()["deny_keys"]) | FORBIDDEN_KEYS
    email = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    for name, payload in outputs.items():
        found = sorted(set(_keys(payload)) & deny)
        assert not found, f"{name}: 금지 키 {found}"
        text = json.dumps(payload, ensure_ascii=False)
        assert not email.search(text), f"{name}: 이메일 패턴이 남아 있다"


def test_files_under_200kb(outputs):
    for name, payload in outputs.items():
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        assert size <= MAX_BYTES, f"{name}: {size} bytes — 200KB 제한"


def test_chart_is_normalized_index_not_raw_prices(outputs):
    """차트는 주 1회 정규화 지수여야 한다 — 원시 달러 가격이 그대로 나가면 안 된다(2.2.4)."""
    tickers = {k: v for k, v in outputs.items() if k.startswith("tickers/")}
    assert tickers
    for name, payload in tickers.items():
        chart = payload.get("chart") or {}
        if not chart:
            continue
        assert chart.get("basis") == "split_adj_index_base100", name
        assert chart["index"][0] == pytest.approx(100.0, abs=1e-6), f"{name}: 기준일이 100 이 아니다"
        assert all(0 < v < 10_000 for v in chart["index"]), f"{name}: 값이 지수가 아니다"
        for banned in ("raw_close", "sadj_close", "volume", "values", "close"):
            assert banned not in chart, f"{name}: 차트에 {banned}"


def test_meta_status_is_honest(outputs):
    status = outputs["meta.json"]["status"]
    assert status in ("normal", "degraded", "stale", "sample", "awaiting_first_run")
    if status == "awaiting_first_run":
        assert outputs["signals/today.json"]["signals"] == []
        assert outputs["meta.json"]["note_ko"]


def test_build_writes_files_to_disk(tmp_path):
    out = tmp_path / "public"
    code = bpd.main(["--signals", str(SAMPLE), "--folds", str(FOLDS), "--out", str(out),
                     "--allowlist", str(ALLOWLIST)])
    assert code == 0
    assert (out / "meta.json").exists()
    assert (out / "signals" / "today.json").exists()
    assert any((out / "tickers").glob("*.json"))


def test_identity_of_signal_is_kept(outputs):
    today = outputs["signals/today.json"]
    assert today["signals"], "예시 신호가 사라졌다"
    first = today["signals"][0]
    assert first["signal_id"] and first["ticker"] == "SAMPLE"
    assert "confidence_label_ko" in first, "신뢰도 라벨(A 데이터·근거 충족)이 있어야 한다(5.2)"
