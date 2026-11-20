"""test_run_daily_offline — 오프라인 일일 파이프라인 계약 (Phase 3 DoD).

`daily-postclose.yml` 이 부르는 경로를 그대로 재현한다:
`run_daily.py --mode postclose` → `validate_outputs.py` → `archive_snapshot.py`.
네트워크는 쓰지 않는다(픽스처). 검사 항목:
- `GITHUB_OUTPUT` 에 `snapshot_id`, `snapshot_id_short` 기록
- 발행 의도(`quant_intent`)와 체결 이벤트의 **분리** (발행 레코드에 체결가 없음)
- `shares`(실수량) 가 커밋 파일에 없음 — 모의 계좌는 비중만 공개
- 신호 0건도 정상 / 산출물 전체가 `validate_outputs.py` 를 통과
- `archive_snapshot.py check-asset` 종료 코드 0/1/10
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from engine.ledger import live
from engine.ledger.record import check_target_session
from engine.models import FORBIDDEN_KEYS, validate_quant_signals
from scripts import archive_snapshot, run_daily, validate_outputs

ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2026-11-20"
LATER = "2026-11-24"


@pytest.fixture
def workdir(tmp_path, monkeypatch) -> Path:
    """config/·schemas/·scripts/redact_quotes.py 만 가진 임시 저장소 루트."""
    for name in ("config", "schemas"):
        shutil.copytree(ROOT / name, tmp_path / name)
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "redact_quotes.py", tmp_path / "scripts" / "redact_quotes.py")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def _run_daily(monkeypatch, as_of: str = AS_OF, mode: str = "postclose") -> int:
    out = Path("gh_output.txt")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out.resolve()))
    return run_daily.main(["--mode", mode, "--offline", "--as-of", as_of, "--root", "."])


def test_postclose_writes_contract_and_github_output(workdir, monkeypatch):
    assert _run_daily(monkeypatch) == 0
    payload = json.loads(Path("data/quant_signals.json").read_text(encoding="utf-8"))
    summary = validate_quant_signals(payload)
    assert summary["n_signals"] >= 1
    assert payload["status"] == "sample"                      # 픽스처는 항상 sample 로 표시된다
    assert payload["charts"], "차트(주 1회 정규화 지수)가 없다"

    gh = Path("gh_output.txt").read_text(encoding="utf-8")
    assert "snapshot_id=sha256:" in gh
    short = [line for line in gh.splitlines() if line.startswith("snapshot_id_short=")][0]
    assert len(short.split("=", 1)[1]) == 12

    assert (Path("data/snapshots") / AS_OF / "manifest.json").exists(), "아카이브 스냅샷이 없다"


def test_publish_record_has_no_fill_and_no_share_count(workdir, monkeypatch):
    assert _run_daily(monkeypatch) == 0
    lines = [json.loads(line) for line in
             Path("data/ledger/records.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines
    for record in lines:
        assert record["execution_event"] is None
        assert record["position"]["stop_price"] is None
        assert record["quant_intent"] in ("pending_buy", "pending_hold_cash", "no_action", "pending_exit")
        for row in record["entry_plan"]:
            assert row["provisional"] is True
        assert "shares" not in set(_keys(record)), "발행 레코드에 실수량이 들어갔다"
        assert not (set(_keys(record)) & FORBIDDEN_KEYS)
        # 대상 거래일이 실제 발행 시각 기준으로 붙고, 그 세션의 주문 마감이 아직 오지 않았다(R08)
        assert record.get("target_session"), "target_session 이 없다"
        check_target_session(record)


def test_blocked_list_keeps_reason(workdir, monkeypatch):
    assert _run_daily(monkeypatch) == 0
    payload = json.loads(Path("data/quant_signals.json").read_text(encoding="utf-8"))
    blocked = {b["ticker"]: b for b in payload["blocked"]}
    assert "FIXC" in blocked, "실적 임박(V01) 종목이 차단 목록에 남아야 한다"
    assert "V01" in blocked["FIXC"]["codes"]
    assert blocked["FIXC"]["message_ko"]


def test_settlement_is_a_separate_event_next_day(workdir, monkeypatch):
    assert _run_daily(monkeypatch) == 0
    signals = json.loads(Path("data/quant_signals.json").read_text(encoding="utf-8"))["signals"]
    buy = [s for s in signals if s["quant_intent"] == "pending_buy"]
    assert buy, "체결 대기(pending_buy) 신호가 있어야 정산을 검증할 수 있다"
    assert not list(Path("data/ledger/executions").glob("*.json"))

    assert _run_daily(monkeypatch, LATER) == 0
    events = {}
    for path in Path("data/ledger/executions").glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if "quant_track" in payload:          # `.reanchor.json` 은 체결 이벤트가 아니다
            events[path.stem] = payload
    assert events, "시가가 확정된 신호의 체결 이벤트가 없다"
    # 로더가 `.reanchor.json` 을 체결로 착각하지 않는다(R01) — 리로드 결과가 위 필터와 같아야 한다
    loaded = live.load_executions(Path("data/ledger/executions"))
    assert set(loaded) == set(events)
    assert all(v["quant_track"]["execution_event"] in ("buy_filled", "no_action", "hold_cash", "exit_filled")
               for v in loaded.values())
    for signal_id, event in events.items():
        track = event["quant_track"]
        assert track["execution_event"] in ("buy_filled", "no_action")
        assert "shares" not in track, "체결 이벤트(커밋본)에 실수량이 들어갔다"
        if track["execution_event"] == "buy_filled":
            assert track["fill_price"] > 0 and track["stop_price"] > 0
        # 발행 레코드는 여전히 무수정
        record = next(json.loads(line) for line in
                      Path("data/ledger/records.jsonl").read_text(encoding="utf-8").splitlines()
                      if signal_id in line)
        assert record["execution_event"] is None


def test_portfolio_state_is_percent_only(workdir, monkeypatch):
    assert _run_daily(monkeypatch) == 0
    state = json.loads(Path("data/ledger/portfolio/state.json").read_text(encoding="utf-8"))
    assert set(_keys(state)) & FORBIDDEN_KEYS == set()
    assert state["cash_pct"] + state["invested_pct"] == pytest.approx(1.0, abs=0.01)


def test_validate_outputs_passes_and_detects_breakage(workdir, monkeypatch):
    assert _run_daily(monkeypatch) == 0
    assert validate_outputs.main([]) == 0

    path = Path("data/quant_signals.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["signals"][0]["position"]["stop_price"] = 123.4      # 발행 시점에 체결가가 있으면 안 된다
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert validate_outputs.main([]) == 1


def test_validate_outputs_requires_four_pilot_metrics(workdir, monkeypatch):
    assert _run_daily(monkeypatch) == 0
    path = Path("data/ledger/pilot_eval.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["metrics"] = payload["metrics"][:3]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert validate_outputs.main([]) == 1


def test_premarket_mode_times(workdir, monkeypatch):
    assert _run_daily(monkeypatch, AS_OF, mode="premarket") == 0
    payload = json.loads(Path("data/quant_signals.json").read_text(encoding="utf-8"))
    assert payload["mode"] == "premarket"
    assert payload["data_cutoff_at"] < payload["signal_published_at"] < payload["order_cutoff_at"]
    assert payload["as_of"] == AS_OF
    assert validate_outputs.main([]) == 0


def test_archive_snapshot_contract(workdir, monkeypatch, tmp_path):
    assert _run_daily(monkeypatch) == 0
    out = tmp_path / "dist_archive"
    assert archive_snapshot.main(["package", "--source", "data/snapshots", "--out", str(out),
                                  "--name", "snapshot_test_run"]) == 0
    tgz = next(out.glob("snapshot_test_run.tgz"))
    digest = archive_snapshot.sha256_file(tgz)

    assets = tmp_path / "assets.json"
    assets.write_text(json.dumps({"assets": []}), encoding="utf-8")
    assert archive_snapshot.main(["check-asset", "--assets", str(assets), "--file", str(tgz)]) == 10

    assets.write_text(json.dumps({"assets": [{"name": tgz.name, "digest": f"sha256:{digest}"}]}),
                      encoding="utf-8")
    assert archive_snapshot.main(["check-asset", "--assets", str(assets), "--file", str(tgz)]) == 0

    assets.write_text(json.dumps({"assets": [{"name": tgz.name, "digest": "sha256:" + "0" * 64}]}),
                      encoding="utf-8")
    assert archive_snapshot.main(["check-asset", "--assets", str(assets), "--file", str(tgz)]) == 1


def test_packaging_is_deterministic(workdir, monkeypatch, tmp_path):
    assert _run_daily(monkeypatch) == 0
    a = tmp_path / "a"
    b = tmp_path / "b"
    archive_snapshot.main(["package", "--source", "data/snapshots", "--out", str(a), "--name", "snap"])
    archive_snapshot.main(["package", "--source", "data/snapshots", "--out", str(b), "--name", "snap"])
    assert archive_snapshot.sha256_file(a / "snap.tgz") == archive_snapshot.sha256_file(b / "snap.tgz")


def test_redaction_runs_inside_pipeline(workdir, monkeypatch):
    """파이프라인이 커밋 직전에 인용 원문을 해시로 바꾼다(2.2.3)."""
    assert _run_daily(monkeypatch) == 0
    text = Path("data/quant_signals.json").read_text(encoding="utf-8")
    assert '"quote"' not in text
    assert validate_outputs.main([]) == 0
    proc = subprocess.run([sys.executable, "scripts/redact_quotes.py", "--check",
                           "data/quant_signals.json"], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
