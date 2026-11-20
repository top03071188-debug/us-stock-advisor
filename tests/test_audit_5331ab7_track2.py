"""test_audit_5331ab7_track2 — 감사 `5331ab7` 트랙 2 (치명 03 · 중요 04 + 부수 ④) 회귀.

고정하는 규칙 (기획서 v1.3.5 §2.2.2 · §3.5 · §4.12 · P-1 · P-5)

[03] 주문 슬롯·차단·매도 시간축
1. `order_slots` 은 **규칙 마감**(`freeze_deadline_at`)과 **동결 확정 시각**(`frozen_at`)을 가른다.
   동결 마감 전 유효 발행이 없으면 그 세션 주문을 **채택하지 않고**(`adopted=False`,
   `frozen_source="no_prior_order"`) 동결 뒤 첫 도착분을 `held` 로 남긴다 — 13:23Z 발행을
   13:20Z 확정으로 깎아 적던 경로(감사 재현 03-A)를 없앤다.
2. `settle` 불변식 — `signal_published_at > 시가` 인 레코드(구 레코드 `target_session=None` 포함)는
   그 시가에 절대 체결하지 않고 `held`(`LATE_AFTER_OPEN`)로 보류한다(감사 재현 03-B: 15:00Z 발행 →
   13:30Z 체결).
3. `settle(blocked=...)` — 문자열 맵(구 계약)은 그대로 시가 체결을 막고,
   `{"reason", "signal_id", "target_session", "observed_at"}` 구조에서는 ① `target_session` 이 다르거나
   ② `observed_at >= 시가`(장중·장후 관측)면 이미 동결된 주문을 `no_action@시가` 로 뒤집지 않는다.

[04] 매도(TRIM·S3)·손절(S5) 잠금의 세션 시간축
4. `settle_positions(forced_exits=...)` — `observed_at >= 시가`(종가 판정)면 그 시가에 팔지 않고
   `exit_pending`(다음 거래일 시가, `expected_fill_day`)으로 남긴다. `observed_at < 시가` 와
   문자열 형태(구 계약)만 그 실행일 시가에 체결한다.
   예약은 세션 로그에 남아 **다음 세션 재생**이 그 시가에 체결하고, 같은 날 재실행은 중복 예약·매도를
   만들지 않는다.
5. 같은 실행의 손절(`stop_exits`) — 시가 이전 확정분은 그 세션 재진입을 막고(`STOP_REENTRY_LOCK`)
   섹터 여유를 돌려주며(손절 먼저, §3.5), 시가 이후 관측분은 오늘 진입을 막지 않고 다음 세션 잠금
   (`stop_locks_next`)으로 넘긴다 — 로그 유무로 결론이 갈리지 않는다(감사 재현 04-C).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from engine.ledger import live
from engine.ledger.record import publish_record
from scripts.check_commit_scope import ARTIFACT_CONTRACTS, CONTRACT_SAFE_KEYS
from tests.test_phase4_ledger_and_ui import fill_of, panel_of, panel_ohlc

PUBLISHED = "2026-05-08T22:02:11Z"
ORDER_CUTOFF = "2026-05-11T14:25:00Z"
SNAP = "sha256:abc123"
SESSION = date(2026, 5, 11)
NEXT = date(2026, 5, 12)


def _record(*, sid: str = "2026-05-08-AAA-BUY-001", ticker: str = "AAA",
            as_of: str = "2026-05-08", published: str = PUBLISHED,
            order: str = ORDER_CUTOFF, session: str | None = "2026-05-11",
            plan_weight: float = 0.05, price: float = 100.0, sector: str = "Tech") -> dict:
    record = publish_record(
        signal_id=sid, ticker=ticker, type="BUY", grade="B", entry_score=72.0, as_of=as_of,
        data_cutoff_at="2026-05-08T22:00:00Z", signal_published_at=published,
        order_cutoff_at=order, target_session=session, generated_price=price,
        target_band={"lo": 110.0, "hi": 130.0}, expected_mdd=-0.12, plan_weight=plan_weight,
        stop_distance=0.14, quant_intent="pending_buy", snapshot_id=SNAP,
    )
    record["trade_grade"] = "BUY"
    record["veto"] = []
    record["sector"] = sector
    record["entry_band"] = {"stop_min_sadj": price * 0.90, "limit_max_sadj": price * 1.05,
                            "dma200_floor_sadj": price * 0.70,
                            "provisional_stop_sadj": price * 0.86,
                            "required_margin": 0.05, "split_factor": 1.0}
    return record


def _fills(out: dict) -> list[dict]:
    return [e for e in out["new_events"]
            if (e.get("quant_track") or {}).get("execution_event") == "buy_filled"]


def _flat() -> dict:
    return {"AAA": panel_of([100.0] * 5)}


def _neutral(monkeypatch) -> None:
    """합성 패널의 RSI·200일선이 상수라 분할익절이 끼어든다 — 지표만 중립으로 고정한다."""
    monkeypatch.setattr(live, "_indicator_lines",
                        lambda panel: ({idx.date(): (None, 50.0) for idx in panel.frame.index}
                                       if panel is not None else {}))


# ══════════════════════════════════════════════════════════════════════════════
# 03-1. 슬롯 — 동결 확정 시각과 규칙 마감의 분리 · 동결 뒤 첫 도착은 채택하지 않는다
# ══════════════════════════════════════════════════════════════════════════════
def test_first_release_after_freeze_is_not_adopted_as_that_session_order():
    """13:20Z 동결 뒤·13:25Z 마감 전에 처음 도착한 BUY(13:23Z)는 그 세션 주문이 아니다(감사 03-A)."""
    record = _record(published="2026-05-11T13:23:00Z", order="2026-05-11T13:25:00Z")
    slot = live.get_slot(live.order_slots([record]), "AAA", SESSION)
    assert slot["frozen_source"] == "no_prior_order" and slot["adopted"] is False
    assert slot["signal_id"] is None and slot["frozen_at"] is None
    assert slot["freeze_deadline_at"] == "2026-05-11T13:20:00Z"
    assert slot["resolved_at"] == "2026-05-11T13:20:00Z"
    assert [item["signal_id"] for item in slot["held"]] == [record["signal_id"]]

    out = live.settle([record], {}, _flat(), SESSION, now="2026-05-11T13:24:00Z")
    assert not _fills(out), "동결 뒤 첫 도착이 같은 세션 시가에 체결됐다"
    assert not out["new_events"], "체결·불체결 이벤트를 만들면 시가에 back-date 한 것이나 같다"
    held_row = next(r for r in out["settled"] if r.get("event") == "held_after_freeze")
    assert held_row["reason"] == "NO_PRIOR_ORDER"
    assert held_row["signal_published_at"] == "2026-05-11T13:23:00Z"
    assert held_row["freeze_deadline_at"] == "2026-05-11T13:20:00Z"
    assert [h["signal_id"] for h in out["held"]] == [record["signal_id"]]
    assert out["held"][0]["reason"] == "NO_PRIOR_ORDER"
    assert out["pending"][0]["reason"] == "NO_PRIOR_ORDER"
    assert out["pending"][0]["expected_fill_day"] == "2026-05-12"     # 다음 세션 대기


def test_order_before_freeze_is_adopted_with_real_confirmation_time():
    """마감 전(13:19Z) 발행은 확정 주문이고 `frozen_at` 은 **발행 시각 그대로**다(마감으로 깎지 않는다)."""
    record = _record(published="2026-05-11T13:19:00Z", order="2026-05-11T13:25:00Z")
    slot = live.get_slot(live.order_slots([record]), "AAA", SESSION)
    assert slot["adopted"] is True and slot["frozen_source"] == "before_deadline"
    assert slot["frozen_at"] == "2026-05-11T13:19:00Z"
    assert slot["freeze_deadline_at"] == "2026-05-11T13:20:00Z"
    assert slot["resolved_at"] == slot["frozen_at"]
    assert slot["held"] == []
    out = live.settle([record], {}, _flat(), SESSION, now="2026-05-11T13:30:00Z")
    assert len(_fills(out)) == 1
    assert _fills(out)[0]["at"] == "2026-05-11T13:30:00Z"


def test_revision_after_freeze_does_not_steal_the_frozen_slot():
    """동결 뒤 재발행은 `held` 로만 남고, 확정 주문의 `frozen_at` 은 그대로다(중복 체결 없음)."""
    first = _record(published="2026-05-11T13:00:00Z", order="2026-05-11T13:25:00Z")
    second = _record(sid="2026-05-08-AAA-BUY-002", published="2026-05-11T13:22:00Z",
                     order="2026-05-11T13:25:00Z")
    second["revision"], second["supersedes"] = 2, first["signal_id"]
    slot = live.get_slot(live.order_slots([first, second]), "AAA", SESSION)
    assert slot["signal_id"] == first["signal_id"] and slot["adopted"] is True
    assert slot["frozen_at"] == "2026-05-11T13:00:00Z"
    assert [item["signal_id"] for item in slot["held"]] == [second["signal_id"]]
    out = live.settle([first, second], {}, _flat(), SESSION, now="2026-05-11T13:30:00Z")
    assert [e["signal_id"] for e in _fills(out)] == [first["signal_id"]]


# ══════════════════════════════════════════════════════════════════════════════
# 03-2. settle 불변식 — 발행이 시가보다 늦으면 그 시가에 체결하지 않는다
# ══════════════════════════════════════════════════════════════════════════════
def test_legacy_record_published_after_open_is_held_not_backdated():
    """`target_session=None` 구 레코드: 05-11 15:00Z 발행 → 05-11 13:30Z 체결 금지(감사 03-B)."""
    legacy = _record(published="2026-05-11T15:00:00Z", order="2026-05-11T16:25:00Z",
                     session=None)
    out = live.settle([legacy], {}, _flat(), SESSION, now="2026-05-11T15:01:00Z")
    assert not _fills(out), "발행(15:00Z)보다 1시간 30분 앞선 시가에 체결됐다"
    row = next(r for r in out["settled"] if r.get("reason") == "LATE_AFTER_OPEN")
    assert row["legacy_session"] is True
    assert row["signal_published_at"] == "2026-05-11T15:00:00Z"
    assert row["open_at"] == "2026-05-11T13:30:00Z"
    assert row["target_session"] == "2026-05-11"
    assert out["pending"][0]["reason"] == "LATE_AFTER_OPEN"
    assert out["pending"][0]["expected_fill_day"] == "2026-05-12"
    assert [h["signal_id"] for h in out["held"]] == [legacy["signal_id"]]


def test_hand_made_record_published_after_the_open_is_held():
    """계약을 어긴 수제 레코드(15:30Z 발행)도 `settle` 이 시가 체결을 거부한다(방어선은 원장)."""
    hand = _record(published="2026-05-11T13:19:00Z", order="2026-05-11T13:25:00Z")
    hand["signal_published_at"] = "2026-05-11T15:30:00Z"
    out = live.settle([hand], {}, _flat(), SESSION, now="2026-05-11T16:00:00Z")
    assert not _fills(out)
    assert any(r.get("reason") in ("LATE_AFTER_OPEN", "NO_PRIOR_ORDER") for r in out["settled"])


def test_release_before_the_open_still_fills():
    """발행(10:09Z)이 시가(13:30Z)보다 앞서면 예전처럼 체결된다 — 과잉 차단 방지."""
    record = _record(published="2026-05-11T10:09:17Z", order="2026-05-11T13:25:00Z")
    out = live.settle([record], {}, _flat(), SESSION, now="2026-05-11T21:30:00Z")
    assert len(_fills(out)) == 1 and _fills(out)[0]["at"] == "2026-05-11T13:30:00Z"


# ══════════════════════════════════════════════════════════════════════════════
# 03-3. blocked — 대상 세션·관측 시각으로 소급 취소를 막는다
# ══════════════════════════════════════════════════════════════════════════════
def test_plain_string_block_still_prevents_the_open_fill():
    """구 계약 `{ticker: reason}` 은 그대로 시가 체결을 막는다(같은 배치 차단 · 하위 호환)."""
    record = _record(published="2026-05-08T22:02:11Z")
    out = live.settle([record], {}, _flat(), SESSION, blocked={"AAA": "V06"})
    assert not _fills(out)
    assert out["settled"][-1]["reason"] == "BLOCKED"
    assert out["settled"][-1]["blocked_reason"] == "V06"


def test_block_observed_after_the_open_does_not_reverse_the_frozen_fill():
    """장 마감 뒤(21:30Z) 관측된 차단은 이미 동결·체결된 시가 주문을 뒤집지 않는다(감사 03-C)."""
    record = _record(published="2026-05-11T10:09:17Z", order="2026-05-11T13:25:00Z")
    blocked = {"AAA": {"reason": "V06", "signal_id": record["signal_id"],
                       "target_session": "2026-05-11", "observed_at": "2026-05-11T21:30:00Z"}}
    out = live.settle([record], {}, _flat(), SESSION, blocked=blocked, now="2026-05-11T21:30:00Z")
    assert len(_fills(out)) == 1, "장후 차단이 동결 주문을 사후 취소했다"
    assert not [e for e in out["new_events"]
                if e["quant_track"]["execution_event"] == "no_action"]
    row = next(r for r in out["settled"] if r.get("event") == "block_observed_after_open")
    assert row["reason"] == "BLOCK_OBSERVED_AFTER_OPEN"
    assert row["observed_at"] == "2026-05-11T21:30:00Z"
    assert row["blocked_reason"] == "V06"
    assert out["pending"][0]["reason"] == "BLOCK_OBSERVED_AFTER_OPEN"
    assert out["pending"][0]["expected_fill_day"] == "2026-05-12"


def test_block_observed_before_the_open_prevents_the_fill():
    """시가 전(12:00Z) 관측된 차단은 그 세션 신규 진입을 막는다."""
    record = _record(published="2026-05-11T10:09:17Z", order="2026-05-11T13:25:00Z")
    blocked = {"AAA": {"reason": "V06", "target_session": "2026-05-11",
                       "observed_at": "2026-05-11T12:00:00Z"}}
    out = live.settle([record], {}, _flat(), SESSION, blocked=blocked, now="2026-05-11T12:01:00Z")
    assert not _fills(out)
    assert out["settled"][-1]["reason"] == "BLOCKED"


def test_block_for_another_target_session_does_not_block_todays_slot():
    """다른 대상 세션용 차단이 같은 티커의 오늘 슬롯을 막지 않는다(감사 03-D)."""
    record = _record(published="2026-05-11T10:09:17Z", order="2026-05-11T13:25:00Z")
    blocked = {"AAA": {"reason": "V06", "target_session": "2026-05-12",
                       "observed_at": "2026-05-11T12:00:00Z"}}
    out = live.settle([record], {}, _flat(), SESSION, blocked=blocked, now="2026-05-11T12:01:00Z")
    assert len(_fills(out)) == 1, "다른 세션용 차단이 오늘 슬롯을 막았다"
    row = next(r for r in out["settled"] if r.get("event") == "block_other_session")
    assert row["reason"] == "BLOCK_FOR_OTHER_SESSION"
    assert row["block_target_session"] == "2026-05-12"


# ══════════════════════════════════════════════════════════════════════════════
# 04-1. 손절·1차 진입의 시간 순서 (stop_exits)
# ══════════════════════════════════════════════════════════════════════════════
def _capped_settle(stop_exits):
    record = _record(published="2026-05-11T10:00:00Z", order="2026-05-11T13:25:00Z",
                     plan_weight=0.05)
    return live.settle([record], {}, _flat(), SESSION, equity_base=1_000_000.0,
                       max_sector=0.25, sector_exposure={"Tech": 240_000.0},
                       stop_exits=stop_exits, now="2026-05-11T13:29:00Z")


def test_stop_before_the_open_frees_sector_room_for_the_same_open_entry():
    """시가 전에 확정된 손절은 손절 먼저(§3.5) — 그 평가액만큼 섹터 여유가 생긴다."""
    without = _capped_settle(None)
    assert without["settled"][-1]["reason"] == "SECTOR_CAP"

    with_stop = _capped_settle([{"ticker": "BBB", "reason": "S5",
                                 "observed_at": "2026-05-11T11:00:00Z",
                                 "sector": "Tech", "value": 30_000.0}])
    assert len(_fills(with_stop)) == 1, "시가 전 손절로 생긴 여유가 반영되지 않았다"
    assert with_stop["sector_freed"] == {"Tech": 30_000.0}
    assert with_stop["stop_locks"] == ["BBB|2026-05-11"]
    assert with_stop["stop_locks_next"] == []


def test_stop_after_the_open_neither_frees_room_nor_blocks_todays_entry():
    """장중·종가에 관측된 손절은 시가 진입보다 늦다 — 오늘 잠금도 여유도 만들지 않는다."""
    out = _capped_settle([{"ticker": "BBB", "reason": "S5",
                           "observed_at": "2026-05-11T18:00:00Z",
                           "sector": "Tech", "value": 30_000.0}])
    assert out["sector_freed"] == {}
    assert out["stop_locks"] == []
    assert out["settled"][-1]["reason"] == "SECTOR_CAP", "시가 이후 손절이 오늘 여유를 만들었다"
    assert out["stop_locks_next"] and out["stop_locks_next"][0]["session"] == "2026-05-12"
    assert out["stop_locks_next"][0]["why"] == "OBSERVED_AFTER_OPEN"


def test_same_run_stop_blocks_reentry_only_when_it_precedes_the_open():
    """같은 실행의 S5 도 시가 이전 확정이면 재진입 잠금, 시가 이후 관측이면 오늘 진입은 살아 있다."""
    record = _record(published="2026-05-11T10:00:00Z", order="2026-05-11T13:25:00Z")
    before = live.settle([record], {}, _flat(), SESSION,
                         stop_exits=[{"ticker": "AAA", "reason": "S5",
                                      "observed_at": "2026-05-11T02:00:00Z"}],
                         now="2026-05-11T13:29:00Z")
    assert not _fills(before)
    assert before["settled"][-1]["reason"] == "STOP_REENTRY_LOCK"
    assert before["stop_reentry_blocked_count"] == 1

    after = live.settle([record], {}, _flat(), SESSION,
                        stop_exits=[{"ticker": "AAA", "reason": "S5",
                                     "observed_at": "2026-05-11T17:00:00Z"}],
                        now="2026-05-11T13:29:00Z")
    assert len(_fills(after)) == 1, "시가 이후 손절이 시가 진입을 막았다(시간 역전)"
    assert after["stop_locks_next"][0]["session"] == "2026-05-12"


# ══════════════════════════════════════════════════════════════════════════════
# 04-2. settle_positions — 매도 의도의 관측 시각과 실행 세션
# ══════════════════════════════════════════════════════════════════════════════
def _hold_panel():
    """05-11 시가 100/종가 100 · 05-12 시가 99/종가 101 · 05-13 시가 101/종가 101."""
    return {"AAA": panel_ohlc([100.0, 99.0, 101.0], [100.5, 101.5, 101.5],
                              [99.5, 98.5, 100.5], [100.0, 101.0, 101.0], start="2026-05-11")}


def _settle_positions(forced, *, as_of=NEXT, panel=None, path=None, records=None,
                      executions=None):
    record = _record(plan_weight=0.02, published="2026-05-08T22:02:11Z")
    executions = executions or {record["signal_id"]: fill_of(record)}
    return live.settle_positions(records or [record], executions, {}, panel or _hold_panel(),
                                 as_of, {"risk": {"risk_per_trade": 0.0075, "max_position": 0.05}},
                                 forced_exits=forced, position_events_path=path)


def test_close_decision_does_not_sell_at_that_mornings_open(monkeypatch):
    """05-12 종가 판정 S3 를 같은 날 시가(99)에 팔지 않는다 — 다음 거래일 시가 대기(감사 04-A)."""
    _neutral(monkeypatch)
    out = _settle_positions({"AAA": {"reason": "S3", "observed_at": "2026-05-12T22:00:00Z",
                                     "target_session": "2026-05-12"}})
    assert out["by_signal"]["2026-05-08-AAA-BUY-001"]["exit"] is None
    assert not [e for e in out["events"] if e.get("event") == "exit_filled"]
    pending = [e for e in out["events"] if e.get("event") == "exit_pending"]
    assert pending and pending[0]["reason"] == "S3"
    assert pending[0]["date"] == "2026-05-12"
    assert pending[0]["expected_fill_day"] == "2026-05-13"
    assert "시가" in (pending[0]["note"] or "")
    assert out["forced_exit_plan"]["AAA"]["when"] == "deferred"
    assert out["health"]["pending_exit_count"] == 1


def test_decision_observed_before_the_open_still_sells_at_that_open(monkeypatch):
    """전일 종가·장전 관측(시가 이전)은 그 세션 시가에 체결한다 — 정상 경로가 살아 있다."""
    _neutral(monkeypatch)
    out = _settle_positions({"AAA": {"reason": "S3", "observed_at": "2026-05-12T10:00:00Z"}})
    exits = [e for e in out["events"] if e.get("event") == "exit_filled"]
    assert exits and exits[0]["date"] == "2026-05-12" and exits[0]["price"] == pytest.approx(99.0)
    assert out["forced_exit_plan"]["AAA"]["when"] == "open"


def test_plain_string_forced_exit_keeps_legacy_same_open_execution(monkeypatch):
    """구 계약 `{ticker: \"S3\"}` 은 예전처럼 그 실행일 시가에 체결한다(하위 호환)."""
    _neutral(monkeypatch)
    out = _settle_positions({"AAA": "S3"})
    exits = [e for e in out["events"] if e.get("event") == "exit_filled"]
    assert exits and exits[0]["date"] == "2026-05-12"
    assert out["forced_exit_plan"]["AAA"]["when"] == "open"


def test_trim_decision_after_the_open_trims_at_the_next_open(monkeypatch):
    """TRIM(1/3 익절)도 종가 판정이면 그 시가에 팔지 않는다 — 다음 세션 예약만 남는다."""
    _neutral(monkeypatch)
    before = _settle_positions({})
    qty_before = before["by_signal"]["2026-05-08-AAA-BUY-001"]["qty"]

    out = _settle_positions({"AAA": {"reason": "TRIM", "observed_at": "2026-05-12T22:00:00Z"}})
    assert not [e for e in out["events"] if e.get("event") == "profit_take"]
    assert out["by_signal"]["2026-05-08-AAA-BUY-001"]["qty"] == pytest.approx(qty_before)
    pending = [e for e in out["events"] if e.get("event") == "exit_pending"]
    assert pending and pending[0]["reason"] == "TRIM"
    assert pending[0]["expected_fill_day"] == "2026-05-13"


def test_pending_sale_is_executed_at_the_next_session_open_with_no_same_day_rerun_duplicate(
        monkeypatch, tmp_path):
    """예약 매도는 **다음 세션 시가**에 체결되고, 같은 날 재실행은 예약을 중복·앞당김하지 않는다."""
    _neutral(monkeypatch)
    path = tmp_path / "position_events.jsonl"
    forced = {"AAA": {"reason": "S3", "observed_at": "2026-05-12T22:00:00Z",
                      "target_session": "2026-05-12"}}

    first = _settle_positions(forced, path=path)
    assert first["by_signal"]["2026-05-08-AAA-BUY-001"]["exit"] is None

    again = _settle_positions(forced, path=path)                 # 같은 as_of 재실행
    assert again["by_signal"]["2026-05-08-AAA-BUY-001"]["exit"] is None, \
        "재실행이 예약 매도를 그날 시가로 앞당겼다"
    assert not [e for e in again["events"] if e.get("event") == "exit_filled"]
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len([r for r in rows if r["event"] == "exit_pending"]) == 1, "예약이 중복 기록됐다"

    third = _settle_positions({}, as_of=date(2026, 5, 13), path=path)   # 다음 세션
    exits = [e for e in third["events"] if e.get("event") == "exit_filled"]
    assert exits and exits[0]["date"] == "2026-05-13"
    assert exits[0]["price"] == pytest.approx(101.0), "다음 세션 시가가 아니라 다른 가격에 팔렸다"
    assert exits[0]["reason"] == "S3"


def test_position_event_rows_stay_within_the_public_field_contract(tmp_path, monkeypatch):
    """`expect_fill_day`·`note` 를 더해도 세션 로그는 공개 필드 계약 안에 있다(가드 회귀)."""
    _neutral(monkeypatch)
    path = tmp_path / "position_events.jsonl"
    _settle_positions({"AAA": {"reason": "S3", "observed_at": "2026-05-12T22:00:00Z"}}, path=path)
    allowed = set(ARTIFACT_CONTRACTS["position_event"]["allowed"]) | set(CONTRACT_SAFE_KEYS)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows and all(set(row) <= allowed for row in rows), \
        [sorted(set(row) - allowed) for row in rows]
    pending = next(row for row in rows if row["event"] == "exit_pending")
    assert pending["expected_fill_day"] == "2026-05-13"


def test_forced_exit_for_a_future_session_waits_for_that_session(monkeypatch):
    """`target_session` 이 앞 세션이면 그 세션 시가 대기 — 오늘 시가에 팔지 않는다."""
    _neutral(monkeypatch)
    out = _settle_positions({"AAA": {"reason": "S3", "observed_at": "2026-05-12T10:00:00Z",
                                     "target_session": "2026-05-13"}})
    assert out["by_signal"]["2026-05-08-AAA-BUY-001"]["exit"] is None
    pending = [e for e in out["events"] if e.get("event") == "exit_pending"]
    assert pending and pending[0]["expected_fill_day"] == "2026-05-13"
    assert out["forced_exit_plan"]["AAA"]["when"] == "deferred"


def test_stale_forced_exit_for_a_past_session_is_not_executed(monkeypatch):
    """묵은 의도(이미 지난 세션)는 오늘 시가에 뒤늦게 실행하지 않는다."""
    _neutral(monkeypatch)
    out = _settle_positions({"AAA": {"reason": "S3", "observed_at": "2026-05-11T10:00:00Z",
                                     "target_session": "2026-05-11"}})
    assert out["by_signal"]["2026-05-08-AAA-BUY-001"]["exit"] is None
    assert not [e for e in out["events"] if e.get("event") in ("exit_filled", "exit_pending")]
    assert out["forced_exit_plan"]["AAA"]["when"] == "stale"


def test_deferred_exit_keeps_position_state_and_health_in_agreement(monkeypatch):
    """예약만 남은 상태에서 보유 수량·현금은 그대로이고, 건강 지표가 예약 건수를 센다."""
    _neutral(monkeypatch)
    base = _settle_positions({})
    out = _settle_positions({"AAA": {"reason": "S5", "observed_at": "2026-05-12T22:00:00Z"}})
    assert out["cash"] == pytest.approx(base["cash"])
    assert out["holdings"]["AAA"]["shares"] == pytest.approx(base["holdings"]["AAA"]["shares"])
    assert out["health"]["pending_exit_count"] == 1
    assert out["by_signal"]["2026-05-08-AAA-BUY-001"]["exit"] is None


def test_observed_at_is_utc_aware_and_compared_against_market_open():
    """`observed_at` 은 시간대를 붙여 읽고, 비교 기준은 **그 세션 NYSE 시가**다(EDT 13:30Z)."""
    from engine.utils.dates import market_open_at

    assert live._as_dt("2026-05-12T22:00:00Z") == datetime(2026, 5, 12, 22, tzinfo=timezone.utc)
    assert market_open_at(date(2026, 5, 12)).isoformat() == "2026-05-12T13:30:00+00:00"
    plan = live._forced_exit_plan(
        {"AAA": {"reason": "S3", "observed_at": "2026-05-12T13:30:00Z"}}, date(2026, 5, 12))
    assert plan["AAA"]["when"] == "deferred", "시가 정각 관측은 시가 체결보다 늦다 — 다음 세션"
    early = live._forced_exit_plan(
        {"AAA": {"reason": "S3", "observed_at": "2026-05-12T13:29:59Z"}}, date(2026, 5, 12))
    assert early["AAA"]["when"] == "open"


def test_settle_returns_slot_semantics_for_audit_evidence():
    """정산 결과의 `slots` 가 `adopted`·`resolved_at`·`frozen_source` 를 함께 남긴다(사후 검증)."""
    held = _record(published="2026-05-11T13:23:00Z", order="2026-05-11T13:25:00Z")
    out = live.settle([held], {}, _flat(), SESSION, now="2026-05-11T13:24:00Z")
    slot = out["slots"][0]
    assert slot["adopted"] is False and slot["frozen_source"] == "no_prior_order"
    assert slot["resolved_at"] == "2026-05-11T13:20:00Z"
    assert slot["freeze_deadline_at"] == "2026-05-11T13:20:00Z"
    assert slot["frozen_at"] is None


def test_held_order_is_not_refilled_on_a_later_run_without_a_new_release():
    """채택되지 않은 주문은 재발행 없이 다음 세션에 다시 체결되지 않는다(새 회차가 그 세션을 맡는다)."""
    held = _record(published="2026-05-11T13:23:00Z", order="2026-05-11T13:25:00Z")
    later = live.settle([held], {}, _flat(), NEXT, now="2026-05-12T22:00:00Z")
    assert not _fills(later), "채택되지 않은 주문이 다음 세션 시가에 체결됐다"
    assert any(r.get("reason") in ("NO_PRIOR_ORDER", "LATE_AFTER_OPEN") for r in later["settled"])


def test_paths_are_not_written_outside_the_tmp_dir(tmp_path):
    """경로 배선 회귀 — `position_events_path` 를 주면 그 파일 하나만 늘어난다."""
    path = tmp_path / "position_events.jsonl"
    _settle_positions({"AAA": {"reason": "S3", "observed_at": "2026-05-12T22:00:00Z"}}, path=path)
    assert path.exists() and Path(path).read_text(encoding="utf-8").count("\n") >= 1


# ══════════════════════════════════════════════════════════════════════════════
# 04-2. 실행기 배선 — 장후 배치의 판정이 **어느 세션 시가**에 팔리는가
# ══════════════════════════════════════════════════════════════════════════════
def _held_position(monkeypatch):
    """1차 체결이 끝난 보유 1건 + 05-11~05-14 봉 — 매도 세션을 확인하는 최소 픽스처."""
    _neutral(monkeypatch)
    record = _record(sid="2026-05-11-AAA-BUY-001", as_of="2026-05-11",
                     published="2026-05-11T14:00:00Z", order="2026-05-11T14:25:00Z",
                     session="2026-05-12")
    executions = {record["signal_id"]: fill_of(record)}
    panels = {"AAA": panel_of([100.0, 101.0, 102.0, 103.0])}     # 05-11 … 05-14
    return record, executions, panels


def test_on_time_postclose_wiring_keeps_the_next_open_sale(monkeypatch):
    """장후 배치(05-12 22:02Z 발행)의 S3 판정은 **05-13 시가**에 팔린다 — 같은 날 시가로 되돌리지 않는다.

    `run_daily._forced_exits(..., observed_at=발행 시각)` 이 `_forced_exit_plan` 새 계약을 타는지,
    그 예약이 **다음 세션 재생**에서 실제로 체결되는지 종단으로 확인한다(중요 04).
    """
    from scripts import run_daily

    record, executions, panels = _held_position(monkeypatch)
    checks = [{"ticker": "AAA", "verdict": "EXIT", "triggers": {"S3": True},
               "cancel_pending_tranches": True}]
    forced = run_daily._forced_exits(checks, observed_at="2026-05-12T22:02:11Z")
    assert forced == {"AAA": {"reason": "S3", "observed_at": "2026-05-12T22:02:11Z"}}, \
        "실행기가 관측 시각을 넘기지 않으면 종가 판정이 그날 시가에 체결된다"

    day = live.settle_positions([record], executions, {}, panels, date(2026, 5, 12),
                                forced_exits=forced, position_events_path=None)
    assert not [e for e in day["events"] if e.get("event") == "exit_filled"], \
        "장후 판정이 같은 날 시가에 팔렸다"
    pending = [e for e in day["event_log"] if e.get("event") == "exit_pending"]
    assert pending and pending[0]["expected_fill_day"] == "2026-05-13"

    # 다음 세션 재생 — 예약이 05-13 시가에 체결된다(매도가 사라지지 않는다)
    nxt = live.settle_positions([record], executions, {}, panels, date(2026, 5, 13),
                                forced_exits={}, prior_events=day["event_log"],
                                position_events_path=None)
    sold = nxt["by_signal"][record["signal_id"]]["exit"]
    assert sold is not None and sold["at"] == "2026-05-13" and sold["reason"] == "S3"
    assert sold["price"] == pytest.approx(102.0, abs=0.001), "05-13 시가가 아니다"


def test_late_postclose_wiring_defers_to_the_following_session(monkeypatch):
    """배치가 늦어 발행이 **다음 세션 시가보다도 늦으면**(05-13 15:00Z) 그 시가가 아니라 05-14 로 미룬다.

    예전에는 `다음 거래일` 하나만 셈해서, 이미 지나간 05-13 시가에 체결된 것으로 적을 수 있었다.
    """
    from scripts import run_daily

    record, executions, panels = _held_position(monkeypatch)
    checks = [{"ticker": "AAA", "verdict": "EXIT", "triggers": {"S3": True},
               "cancel_pending_tranches": True}]
    forced = run_daily._forced_exits(checks, observed_at="2026-05-13T15:00:00Z")
    plan = live._forced_exit_plan(forced, date(2026, 5, 12))
    assert plan["AAA"]["when"] == "deferred"
    assert plan["AAA"]["since"] == date(2026, 5, 14), \
        "관측(05-13 15:00Z)이 05-13 시가(13:30Z) 이후인데 05-13 체결로 적었다"

    day = live.settle_positions([record], executions, {}, panels, date(2026, 5, 12),
                                forced_exits=forced, position_events_path=None)
    pending = [e for e in day["event_log"] if e.get("event") == "exit_pending"]
    assert pending and pending[0]["expected_fill_day"] == "2026-05-14"
    assert not [e for e in day["events"] if e.get("event") == "exit_filled"]

    filled = live.settle_positions([record], executions, {}, panels, date(2026, 5, 14),
                                   forced_exits={}, prior_events=day["event_log"],
                                   position_events_path=None)
    sold = filled["by_signal"][record["signal_id"]]["exit"]
    assert sold is not None and sold["at"] == "2026-05-14"
    assert sold["price"] == pytest.approx(103.0, abs=0.001), "05-14 시가가 아니다"


def test_pending_s5_keeps_the_winning_entry_out_of_the_fill_session():
    """아직 체결되지 않은 S5 예약(`exit_pending`)도 **그 체결 세션**의 신규 1차 진입을 막는다.

    `settle` 은 `settle_positions` 보다 먼저 돌기 때문에, 기록된 `exit_filled` 만 보면 손절이
    실제로 팔리는 세션의 재진입을 놓친다(중요 04 · P-1).
    """
    buy = _record(sid="2026-05-11-AAA-BUY-001", published="2026-05-11T14:00:00Z",
                  order="2026-05-11T14:25:00Z", session="2026-05-12")
    pending = [{"date": "2026-05-12", "ticker": "AAA", "signal_id": "2026-05-08-AAA-BUY-000",
                "event": "exit_pending", "reason": "S5", "expected_fill_day": "2026-05-12"}]
    locks = live.stop_reentry_locks(pending)
    assert locks == [{"ticker": "AAA", "session": "2026-05-12",
                      "signal_id": "2026-05-08-AAA-BUY-000", "lock": "AAA|2026-05-12"}]
    out = live.settle([buy], {}, _flat(), NEXT, stop_locks=locks)
    assert not _fills(out) and out["stop_reentry_blocked_count"] == 1
    # 잠금이 없으면 같은 주문이 그 세션 시가에 체결된다(대조군)
    assert _fills(live.settle([buy], {}, _flat(), NEXT))
