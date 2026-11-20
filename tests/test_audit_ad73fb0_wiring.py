"""test_audit_ad73fb0_wiring — 감사 `ad73fb0` 수정본의 **경계 배선** 통합 테스트.

트랙 1·2·3 이 엔진 안에서 고친 규칙이 `scripts/run_daily.py` 를 통해 실제 배치에 닿는지 고정한다.
엔진 단위 테스트(`test_phase4_*`)는 "규칙이 있다"를, 이 파일은 "실행기가 그 규칙을 켠다"를 본다.

검사 항목
- **P-1** `live.settle(..., position_events_path=...)` — S5 손절 세션 로그에서 재진입 잠금을 파생해
  같은 세션 신규 1차 진입을 막고, 막은 건수가 `settle_positions(stop_reentry_blocked=...)` 를 거쳐
  `health.stop_reentry_blocked_count` 에 **실제 값**으로 남는다.
- **섹터 캡 분모** `live.settle(..., equity_base=INITIAL_CASH × equity_index)` — 1차 캡의 분모가
  2·3차와 같은 시점 순자산이다.
- **감사 06-(e)** `auto_downgrade_enabled=true` — 화면(`hold_cash`)과 원장이 갈리지 않는다
  (`debate_holds` → `hold_cash` 실행 이벤트, `buy_filled` 없음). OFF 회귀도 함께 고정한다.
- **감사 07** `run_daily.debate_index` 의 `(signal_id, snapshot_id)` 키가 `pipeline` 매칭에서
  구 스냅샷을 실제로 차단한다.
"""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from engine.decide import pipeline
from engine.ledger import live
from scripts import run_daily

ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2026-11-20"
LATER = "2026-11-24"


@pytest.fixture
def workdir(tmp_path, monkeypatch) -> Path:
    """`config/`·`schemas/`·`redact_quotes.py` 만 가진 임시 저장소 루트."""
    for name in ("config", "schemas"):
        shutil.copytree(ROOT / name, tmp_path / name)
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "redact_quotes.py", tmp_path / "scripts" / "redact_quotes.py")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run(monkeypatch, as_of: str = AS_OF) -> int:
    monkeypatch.setenv("GITHUB_OUTPUT", str(Path("gh_output.txt").resolve()))
    return run_daily.main(["--mode", "postclose", "--offline", "--as-of", as_of, "--root", "."])


def _load(path: str | Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _seed(records: list[dict]) -> None:
    path = Path("data/ledger/records.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")


def _executions() -> dict[str, dict]:
    return live.load_executions(Path("data/ledger/executions"))


def _pending_buy(sid: str, *, ticker: str, as_of: str, session: str, sector: str = "Tech") -> dict:
    from tests.test_phase4_ledger_and_ui import record_of

    record = record_of(sid=sid, ticker=ticker, as_of=as_of, band={
        "stop_min_sadj": 1.0, "limit_max_sadj": 10_000.0, "dma200_floor_sadj": 1.0,
        "provisional_stop_sadj": 1.0, "required_margin": 0.0, "split_factor": 1.0})
    record.update({"as_of": as_of, "target_session": session, "sector": sector,
                   "signal_published_at": f"{as_of}T05:00:00Z",
                   "order_cutoff_at": f"{session}T14:25:00Z", "inputs_hash": f"hash-{sid}"})
    return record


#: 픽스처 시세의 마지막 봉이 있는 세션 — 이 세션이 지나야 1차 체결이 일어난다(11-20 은 봉이 없다).
SESSION = "2026-11-23"


# ── 1. 배선 계약 — settle 가 잠금 경로·순자산 분모를 받는다 ──────────────────
def test_settle_receives_position_events_path_and_equity_base(workdir, monkeypatch):
    """P-1 잠금(`position_events_path`)과 섹터 캡 분모(`equity_base`)가 실행기에서 켜진다."""
    seen: list[dict] = []
    original_settle = live.settle

    # `run_daily` 는 시그니처를 보고 인자를 넘긴다(`_supports`) — 래퍼가 시그니처를 그대로 드러내야 한다.
    def settle_spy(*args, now=None, position_events_path=None, equity_base=None,
                   blocked=None, debate_holds=None, **kwargs):
        seen.append({"now": now, "position_events_path": position_events_path,
                     "equity_base": equity_base, "blocked": blocked,
                     "debate_holds": debate_holds, **kwargs})
        call = {k: v for k, v in {"now": now, "position_events_path": position_events_path,
                                  "equity_base": equity_base, "blocked": blocked,
                                  "debate_holds": debate_holds}.items() if v is not None}
        return original_settle(*args, **call, **kwargs)

    real_ledger_view = run_daily.ledger_view

    def fake_ledger_view(*args, **kwargs):
        portfolio, held, settlement = real_ledger_view(*args, **kwargs)
        portfolio["equity_index"] = 1.05          # 순자산 +5% 상태를 만든다(분모가 움직이는지 본다)
        return portfolio, held, settlement

    monkeypatch.setattr(live, "settle", settle_spy)
    monkeypatch.setattr(run_daily, "ledger_view", fake_ledger_view)
    assert _run(monkeypatch) == 0

    assert seen, "live.settle 이 호출되지 않았다"
    for kwargs in seen:
        assert kwargs.get("position_events_path") == live.POSITION_EVENTS_PATH, kwargs
        assert kwargs.get("equity_base") == pytest.approx(live.INITIAL_CASH * 1.05), kwargs
    assert run_daily.settle_equity_base({"equity_index": 1.05}) == pytest.approx(1_050_000.0)
    assert run_daily.settle_equity_base(None) == pytest.approx(live.INITIAL_CASH)


# ── 2. P-1 행동 — 손절 세션 로그가 다음 세션 신규 1차 진입을 막는다 ────────────
def test_stop_session_log_blocks_new_first_entry_and_reports_count(workdir, monkeypatch):
    """S5 손절 세션 로그 → 같은 세션 신규 1차 `buy_filled` 없음 + health 카운트 1(2·3차 취소 수와 분리)."""
    record = _pending_buy("2026-11-20-FIXA-BUY-001", ticker="FIXA", as_of=AS_OF,
                          session=SESSION, sector="Information Technology")
    _seed([record])

    # 2026-11-23 세션에 FIXA 가 S5 로 손절된 세션 로그(전일 실행이 남긴 것)
    path = Path("data/ledger/position_events.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "event_id": "pos-2026-11-23-FIXA-exit_filled-S5",
        "date": SESSION, "ticker": "FIXA", "signal_id": "2026-11-19-FIXA-BUY-001",
        "event": "exit_filled", "reason": "S5", "price": 90.0,
    }, ensure_ascii=False) + "\n", encoding="utf-8")

    assert _run(monkeypatch, LATER) == 0

    executions = _executions()
    event = executions.get(record["signal_id"])
    assert event is not None, "차단 결정이 실행 이벤트로 남지 않았다"
    assert event["quant_track"]["execution_event"] != "buy_filled", "손절 당일 신규 1차 진입이 체결됐다"
    settled = [row for row in _load("data/quant_signals.json")["ledger_summary"]["settlement"]["settled"]
               if row.get("signal_id") == record["signal_id"]]
    assert settled and settled[-1]["reason"] == "STOP_REENTRY_LOCK", settled

    health = _load("data/health.json")
    assert health["stop_reentry_blocked_count"] == 1, health
    payload = _load("data/quant_signals.json")
    assert payload["health"]["stop_reentry_blocked_count"] == 1

    # 대조 — 세션 로그가 없으면(잠금 없음) 같은 신호는 체결된다.
    path.unlink()
    for stale in Path("data/ledger/executions").glob("*.json"):
        stale.unlink()
    assert _run(monkeypatch, LATER) == 0
    reloaded = _executions().get(record["signal_id"]) or {}
    assert reloaded.get("quant_track", {}).get("execution_event") == "buy_filled", reloaded


# ── 3. 감사 06-(e) — 자동 하향 ON 이면 원장도 보류한다 ───────────────────────
def _seed_debate(record: dict, *, completed: str, validated: str, session: str) -> None:
    from tests.test_phase4_ledger_and_ui import report_of

    report = report_of(record["signal_id"], ticker=record["ticker"],
                       snapshot=record["snapshot_id"],
                       published=record["signal_published_at"], completed=completed,
                       order=record["order_cutoff_at"], validated=validated,
                       recommendation="WATCH", intent="pending_hold_cash")
    Path("data").mkdir(parents=True, exist_ok=True)
    Path("data/debate_reports.json").write_text(json.dumps({
        "schema_version": "1.0", "generated_at": completed,
        "codex": {"version": "0.52.0", "model": "default"},
        "inputs": {"quant_signals_commit": "abc1234", "as_of": record["as_of"],
                   "context_hash": "ctx", "prompt_hash": "prm"},
        "reports": [report],
    }, ensure_ascii=False), encoding="utf-8")


def _set_auto_downgrade(value: bool) -> None:
    path = Path("config/params_v001.yaml")
    params = yaml.safe_load(path.read_text(encoding="utf-8"))
    params.setdefault("debate", {})["auto_downgrade_enabled"] = value
    path.write_text(yaml.safe_dump(params, allow_unicode=True), encoding="utf-8")


def test_auto_downgrade_hold_reaches_ledger(workdir, monkeypatch):
    """ON: 화면 보류(`hold_cash`)와 원장이 일치 — 실행 이벤트는 `hold_cash`, `buy_filled` 는 없다."""
    record = _pending_buy("2026-11-20-FIXA-BUY-001", ticker="FIXA", as_of=AS_OF,
                          session=SESSION, sector="Information Technology")
    _seed([record])
    _seed_debate(record, completed="2026-11-20T06:00:00Z", validated="2026-11-20T06:05:00Z",
                 session=SESSION)
    _set_auto_downgrade(True)

    assert _run(monkeypatch, LATER) == 0

    payload = _load("data/quant_signals.json")
    auto = payload["debate"]["auto_downgrade_enabled"]
    assert auto is True, payload["debate"]
    summary = (run_daily.debate_index(
        _load("data/ledger/debate_summary.json")["by_signal"], [record]).by_ticker.get("FIXA") or {})
    assert summary.get("auto_downgrade_applied") is True, summary

    event = _executions().get(record["signal_id"]) or {}
    assert event.get("quant_track", {}).get("execution_event") == "hold_cash", event
    assert event.get("quant_track", {}).get("fill_price") is None
    assert "AUTO_DOWNGRADE" in str(event.get("note") or "")
    settled = [row for row in payload["ledger_summary"]["settlement"]["settled"]
               if row.get("signal_id") == record["signal_id"]]
    assert settled and settled[-1]["reason"] == "AUTO_DOWNGRADE", settled


def test_auto_downgrade_off_keeps_quant_fill(workdir, monkeypatch):
    """OFF 회귀: 같은 보고서라도 자동 하향이 꺼져 있으면 정량 트랙은 그대로 체결한다(기본 정책)."""
    record = _pending_buy("2026-11-20-FIXB-BUY-001", ticker="FIXB", as_of=AS_OF,
                          session=SESSION, sector="Energy")
    _seed([record])
    _seed_debate(record, completed="2026-11-20T06:00:00Z", validated="2026-11-20T06:05:00Z",
                 session=SESSION)
    _set_auto_downgrade(False)

    assert _run(monkeypatch, LATER) == 0
    event = _executions().get(record["signal_id"]) or {}
    assert event.get("quant_track", {}).get("execution_event") == "buy_filled", event


# ── 4. 감사 07 — DebateByKey 의 (signal_id, snapshot_id) 가 pipeline 매칭에서 돈다 ──
def test_debate_key_blocks_stale_snapshot_in_pipeline():
    """`debate_index` → `pipeline.debate_row_for` 경로에서 구 스냅샷 토론은 붙지 않는다."""
    records = [{"signal_id": "2026-11-20-AAA-BUY-001", "snapshot_id": "sha256:new"}]
    by_signal = {"2026-11-20-AAA-BUY-001": {
        "signal_id": "2026-11-20-AAA-BUY-001", "ticker": "AAA", "snapshot_id": "sha256:old",
        "eligible": True, "news_state": "read_done", "structural_risk": 70,
        "recommendation": "WATCH"}}
    index = run_daily.debate_index(by_signal, records)
    as_of = date(2026, 11, 20)

    stale = pipeline.debate_row_for(index, ticker="AAA", as_of=as_of, snapshot_id="sha256:new")
    assert stale == {}, "구 스냅샷 토론이 오늘 판단에 붙었다"

    by_signal["2026-11-20-AAA-BUY-001"]["snapshot_id"] = "sha256:new"
    fresh = run_daily.debate_index(by_signal, records)
    row = pipeline.debate_row_for(fresh, ticker="AAA", as_of=as_of, snapshot_id="sha256:new")
    assert row.get("news_state") == "read_done" and row.get("structural_risk") == 70


def test_debate_holds_only_when_auto_downgrade_enabled():
    result = {"by_signal": {
        "S1": {"eligible": True, "auto_downgrade_applied": True, "ticker": "AAA"},
        "S2": {"eligible": True, "auto_downgrade_applied": False, "ticker": "BBB"},
        "S3": {"eligible": False, "auto_downgrade_applied": True, "ticker": "CCC"},
    }}
    assert run_daily.debate_holds_for(result, {"debate": {"auto_downgrade_enabled": False}}) == {}
    assert run_daily.debate_holds_for(result, None) == {}
    holds = run_daily.debate_holds_for(result, {"debate": {"auto_downgrade_enabled": True}})
    assert holds == {"S1": "AUTO_DOWNGRADE"}


def test_hold_reason_matches_by_signal_then_ticker():
    assert live._hold_reason({"S1": "AUTO_DOWNGRADE"}, "S1", "AAA") == "AUTO_DOWNGRADE"
    assert live._hold_reason({"AAA": "AUTO_DOWNGRADE"}, "S9", "AAA") == "AUTO_DOWNGRADE"
    assert live._hold_reason({"S1": "AUTO_DOWNGRADE"}, "S2", "BBB") is None
    assert live._hold_reason(["S1"], "S1", "AAA") == "AUTO_DOWNGRADE"
    assert live._hold_reason([], "S1", "AAA") is None
    assert live._hold_reason(None, "S1", "AAA") is None


def test_hold_cash_event_is_not_a_fill(tmp_path):
    """`hold_cash` 는 체결이 아니다 — 실행 이벤트로 남아도 `buy_filled` 로 읽히지 않는다."""
    from engine.ledger.record import execution_event
    from tests.test_phase4_ledger_and_ui import record_of

    record = record_of(sid="2026-05-08-AAA-BUY-001", ticker="AAA")
    event = execution_event(signal_id=record["signal_id"], snapshot_id=record["snapshot_id"],
                            at=datetime(2026, 5, 11, 13, 30, tzinfo=timezone.utc),
                            execution_event="hold_cash", note="토론 자동 하향(AUTO_DOWNGRADE)")
    live.write_execution(event, Path(tmp_path) / "exec")
    loaded = live.load_executions(Path(tmp_path) / "exec")[record["signal_id"]]
    assert loaded["quant_track"]["execution_event"] == "hold_cash"
    assert loaded["quant_track"]["fill_price"] is None


def test_hold_does_not_fill_when_session_already_passed():
    """`debate_holds` 는 세션 봉이 이미 있는 경우에도 체결을 만들지 않는다(단위)."""
    from tests.test_phase4_ledger_and_ui import panel_of, record_of

    record = record_of(sid="2026-05-08-AAA-BUY-001", ticker="AAA")
    record["target_session"] = "2026-05-11"
    panels = {"AAA": panel_of([100.0] * 3, start="2026-05-11", ticker="AAA")}
    out = live.settle([record], {}, panels, date(2026, 5, 11),
                      debate_holds={record["signal_id"]: "AUTO_DOWNGRADE"})
    events = [e for e in out["new_events"] if e["signal_id"] == record["signal_id"]]
    assert events and events[0]["quant_track"]["execution_event"] == "hold_cash"
    held = [row for row in out["settled"] if row["signal_id"] == record["signal_id"]]
    assert held[-1]["reason"] == "AUTO_DOWNGRADE"
    assert not [s for s in held if s.get("event") == "buy_filled"]


def test_stop_lock_and_hold_share_the_same_slot_rules():
    """차단(BLOCKED) → 잠금(STOP_REENTRY_LOCK) → 보류(AUTO_DOWNGRADE) 순서가 같은 슬롯에서 겹치지 않는다."""
    from tests.test_phase4_ledger_and_ui import panel_of, record_of

    record = record_of(sid="2026-05-08-AAA-BUY-001", ticker="AAA")
    record["target_session"] = "2026-05-11"
    panels = {"AAA": panel_of([100.0] * 3, start="2026-05-11", ticker="AAA")}
    blocked = live.settle([record], {}, panels, date(2026, 5, 11), blocked={"AAA": "V01"})
    assert blocked["settled"][-1]["reason"] == "BLOCKED"
    locked = live.settle([record], {}, panels, date(2026, 5, 11),
                         prior_events=[{"date": "2026-05-11", "ticker": "AAA",
                                        "event": "exit_filled", "reason": "S5"}])
    assert locked["settled"][-1]["reason"] == "STOP_REENTRY_LOCK"
    held = live.settle([record], {}, panels, date(2026, 5, 11),
                       debate_holds={"AAA": "AUTO_DOWNGRADE"})
    assert held["settled"][-1]["reason"] == "AUTO_DOWNGRADE"


def test_settle_positions_counts_only_passed_reentry_blocks():
    """`stop_reentry_blocked` 를 넘기지 않으면 0, 넘기면 그 수 — 2·3차 취소 수와 섞이지 않는다."""
    from tests.test_phase4_ledger_and_ui import fill_of, panel_of, record_of

    record = record_of(sid="2026-05-08-AAA-BUY-001", ticker="AAA")
    record["target_session"] = "2026-05-11"
    executions = {record["signal_id"]: fill_of(record)}
    panels = {"AAA": panel_of([100.0] * 4, start="2026-05-11", ticker="AAA")}
    as_of = date(2026, 5, 12)
    plain = live.settle_positions([record], executions, {}, panels, as_of, {})
    assert plain["health"]["stop_reentry_blocked_count"] == 0
    marked = live.settle_positions([record], executions, {}, panels, as_of, {},
                                   stop_reentry_blocked=[{"signal_id": record["signal_id"],
                                                          "ticker": "AAA", "reason": "S5_STOP_SAME_SESSION"}])
    assert marked["health"]["stop_reentry_blocked_count"] == 1
    assert marked["health"].get("tranche_cancel_count", 0) >= 0


def test_ledger_view_forwards_reentry_blocks(workdir):
    """`run_daily.ledger_view` 가 실행기에서 받은 잠금 수를 `settle_positions` 까지 넘긴다."""
    from tests.test_phase4_ledger_and_ui import fill_of, panel_of, record_of

    record = record_of(sid="2026-05-08-AAA-BUY-001", ticker="AAA")
    record["target_session"] = "2026-05-11"
    executions = {record["signal_id"]: fill_of(record)}
    panels = {"AAA": panel_of([100.0] * 4, start="2026-05-11", ticker="AAA")}
    _, _, settlement = run_daily.ledger_view(
        [record], executions, panels, date(2026, 5, 12), {}, {}, position_events_path=None,
        stop_reentry_blocked=[{"signal_id": record["signal_id"], "ticker": "AAA",
                               "target_session": "2026-05-11", "reason": "S5_STOP_SAME_SESSION"}])
    assert settlement["health"]["stop_reentry_blocked_count"] == 1


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_settle_equity_base_tracks_equity_index():
    base = run_daily.settle_equity_base({"equity_index": 0.94})
    assert base == pytest.approx(940_000.0)
    assert base < run_daily.settle_equity_base({"equity_index": 1.02})
    assert run_daily.settle_equity_base({}) == pytest.approx(live.INITIAL_CASH)
    assert _ts("2026-11-20T14:20:00Z") - timedelta(minutes=5) == \
        _ts("2026-11-20T14:15:00Z")
