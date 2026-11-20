"""test_public_scope — Pages 산출물에 원시 시세·보유 정보가 **0건**인지 검사 (Phase 3 DoD).

`build_public_data.py` 의 출력은 공개 Pages 에 그대로 올라간다. 그래서 이 테스트는
① 금지 키 0건 ② 파일당 200KB 이하 ③ 필요한 파일이 모두 있는지 ④ 차트에 원시 가격이 없는지를 본다.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml

from engine.ledger import debate as debate_mod
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


# ── 감사 12: 발행 세트 매니페스트 ───────────────────────────────────────
def test_deploy_manifest_hashes_every_public_file(outputs):
    manifest = outputs["deploy_manifest.json"]
    assert manifest["schema_version"] == 1
    assert manifest["bundle_version"].startswith("sha256:")
    assert manifest["files"], "파일 해시 목록이 비었다"
    for name, payload in outputs.items():
        if name == "deploy_manifest.json":
            continue
        assert name in manifest["files"], f"{name} 이 매니페스트에 없다"
        digest = hashlib.sha256(bpd.dumps(payload).encode("utf-8")).hexdigest()
        assert manifest["files"][name]["sha256"] == digest, f"{name} 해시가 실제 바이트와 다르다"
        assert manifest["files"][name]["size"] > 0
    # meta·health 도 **같은** 세트 버전을 담는다(서비스워커가 먼저 읽는 파일)
    assert outputs["meta.json"]["bundle_version"] == manifest["bundle_version"]
    assert outputs["health.json"]["bundle_version"] == manifest["bundle_version"]
    for name in bpd.CORE_PUBLIC_FILES:
        assert name in manifest["files"], f"핵심 공개 파일 {name} 이 매니페스트에 없다"


def test_bundle_version_covers_all_files_not_only_signals():
    """감사 12: health·토론·알림만 바뀐 배포도 **고유 버전**을 갖는다."""
    base = {"meta.json": b"{}", "health.json": b"{}", "debate.json": b"{}",
            "notifications.json": b"{}", "signals/today.json": b"{}"}
    seen = {bpd.files_version(base)}
    for name, payload in (("health.json", b'{"data_status":"outage"}'),
                          ("debate.json", b'{"reports":[1]}'),
                          ("notifications.json", b'{"n_new":3}'),
                          ("signals/today.json", b'{"signals":[1]}')):
        seen.add(bpd.files_version({**base, name: payload}))
    assert len(seen) == 5, seen
    # 같은 내용이면 같은 버전(추가 파일만 달라져도 값이 바뀐다)
    assert bpd.files_version(base) == bpd.files_version({**base})


# ── 02: Pages 는 원장이 확정한 debate_events 를 **그대로** 쓴다 ──────────────
def _report(signal_id: str = "S1", ticker: str = "AAA") -> dict:
    return {"report_id": "R1", "signal_id": signal_id, "ticker": ticker, "status": "complete",
            "completed_at": "2026-11-20T05:10:00Z", "signal_published_at": "2026-11-19T23:00:00Z",
            "order_cutoff_at": "2026-11-20T14:25:00Z", "market_snapshot_date": "2026-11-19",
            "intent": "pending_buy", "round1": [], "round2": [], "round3": {}}


def _event(**overrides) -> dict:
    event = {"event": "debate_attached", "report_id": "R1", "track": "codex_debated",
             "completed_at": "2026-11-20T05:10:00Z", "recorded_at": "2026-11-20T05:12:00Z",
             "eligible": False, "late": True, "invalid": False,
             "reasons": ["REPORT_VALIDATED_AFTER_ORDER_CUTOFF"], "quant_grade": "BUY",
             "recommendation": "WATCH", "quant_action": "buy_filled", "debate_action": "hold_cash",
             "divergent": True, "auto_downgrade_applied": False, "veto_codes": [], "regime": "risk_on"}
    event.update(overrides)
    return event


def test_build_debate_consumes_ledger_events(monkeypatch):
    """편입을 빈 체결 목록으로 다시 계산하지 않는다(감사 02 · 02 계약)."""

    def boom(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("reconcile_debates 로 편입을 재계산하면 안 된다")

    monkeypatch.setattr(debate_mod, "reconcile_debates", boom)
    doc = {"reports": [_report()]}
    events = {"S1": {"signal_id": "S1", "snapshot_id": "sha256:snap",
                     "events": [_event()]}}
    out = bpd.build_debate(doc, [], {}, {}, "2026-11-20", debate_events=events)
    row = out["reports"][0]
    assert row["late"] is True and row["eligible"] is False
    assert row["reasons"] == ["REPORT_VALIDATED_AFTER_ORDER_CUTOFF"]
    assert row["divergent"] is True and row["debate_action"] == "hold_cash"
    assert out["counts"] == {"reports": 1, "eligible": 0, "late": 1, "invalid": 0, "ledger_confirmed": 1}
    assert out["by_ticker"]["AAA"]["signal_id"] == "S1"


def test_build_debate_does_not_invent_eligibility_without_events():
    out = bpd.build_debate({"reports": [_report()]}, [], None, {}, "2026-11-20", debate_events={})
    row = out["reports"][0]
    assert row["eligible"] is None and row["badge"] is None
    assert "LEDGER_EVENT_MISSING" in row["reasons"]
    assert "다시 계산하지 않고" in (out["note_ko"] or "")


def test_build_debate_prefers_the_matching_report_event():
    events = {"S1": {"signal_id": "S1", "events": [
        _event(report_id="R0", recorded_at="2026-11-20T05:00:00Z", late=False, eligible=True,
               reasons=[]),
        _event(report_id="R1", recorded_at="2026-11-20T05:30:00Z"),
    ]}}
    out = bpd.build_debate({"reports": [_report()]}, [], None, {}, "2026-11-20", debate_events=events)
    assert out["reports"][0]["late"] is True
    assert len(events["S1"]["events"]) == 2, "이벤트 목록을 변형하면 안 된다"


def test_filter_public_keeps_data_map_keys_but_deny_keys_still_block():
    """감사 12: `by_ticker`·`sector_weights`·`forced_exits`·`channels` 의 키는 **이름**이다.

    예전에는 허용 목록 필터가 사전 키를 전부 지워 `by_ticker`·섹터 비중이 빈 객체로 나갔다.
    값 안의 금지 키(`quote`)는 그대로 막힌다.
    """
    rules = _rules()
    out = bpd.filter_public({"by_ticker": {"AAA": {"badge": "토론 완료", "quote_sha256_count": 2}},
                             "sector_weights": {"Information Technology": 0.2},
                             "forced_exits": {"AAA": "TRIM"},
                             "channels": {"console": {"attempts": 1}},
                             "unknown_field": {"ticker": "AAA"}}, set(rules["allow"]))
    assert out["by_ticker"]["AAA"]["badge"] == "토론 완료"
    assert out["sector_weights"] == {"Information Technology": 0.2}
    assert out["forced_exits"] == {"AAA": "TRIM"}
    assert out["channels"] == {"console": {"attempts": 1}}
    assert "unknown_field" not in out, "허용 목록에 없는 필드는 계속 지운다"

    import pytest as _pytest

    with _pytest.raises(SystemExit):
        bpd.assert_no_forbidden({"by_ticker": {"AAA": {"quote": "원문 인용"}}}, rules)
