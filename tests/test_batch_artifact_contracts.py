"""test_batch_artifact_contracts — 첫 예약 배치(run 44830634315)가 드러낸 산출물 계약 누락.

2026-11-21 07:38 KST `daily-postclose` 의 첫 `schedule` 실행은 파이프라인·스키마 검증·아카이브를
모두 통과했지만, 커밋 단계의 `check_commit_scope.py --staged` 가 **계약에 없는 필드**로 실패했다.

    ERROR debate_event: ['adopted', 'adoption', 'frozen_source', 'received_at',
                         'validation_claimed_at', 'validation_source']
    ERROR notify_latest: ['failed', 'pending_retry', 'unconfigured']

그 필드들은 감사 5331ab7 수정(02 신호당 채택 확정 · 11 채널별 발송 장부)이 **생산 코드에 먼저**
들어가고 계약·공개 허용 목록에는 반영되지 않아 생긴 것이다. 이 파일은 **생산 코드가 실제로 만드는
문서**를 그대로 만들어 계약 검사를 통과시키는지 확인한다(합성 dict 가 아니라 writer 를 부른다).

검사 항목
1. `reconcile_debates` → `debate_events/{sid}.json` (채택 `adopted` 블록 · `superseded` · 이벤트) 통과
2. `evaluate_notifications` → `notifications_latest.json` (failed · pending_retry · unconfigured) 통과
3. `write_health_state` → `health.json` (정상·지연·장애 세 경로) 통과
4. `position_event_rows` → `position_events.jsonl` 통과
5. 계약이 **중첩 키**까지 본다 — `adopted` 안에 낯선 키가 들어오면 실패한다
6. 공개 허용 목록에 새 필드가 있어 `build_public_data` 가 값을 버리지 않는다
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest
import yaml

from engine.ledger import debate as debate_mod
from engine.ledger import live as live_mod
from engine.ledger.record import execution_event, publish_record
from scripts import build_public_data as bpd
from scripts import check_commit_scope as guard
from scripts import notify as notify_mod
from scripts import run_daily

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
SNAP = "sha256:abc123"
PUBLISHED = "2026-05-08T22:02:11Z"
ORDER_CUTOFF = "2026-05-11T14:25:00Z"
FILL_AT = "2026-05-11T13:30:00Z"
SID = "2026-05-08-AAA-BUY-001"


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def record_of() -> dict:
    record = publish_record(
        signal_id=SID, ticker="AAA", type="BUY", grade="B", entry_score=72.0, as_of="2026-05-08",
        data_cutoff_at="2026-05-08T22:00:00Z", signal_published_at=PUBLISHED,
        order_cutoff_at=ORDER_CUTOFF, target_session="2026-05-11", generated_price=100.0,
        target_band={"lo": 110.0, "hi": 130.0}, expected_mdd=-0.12, plan_weight=0.05,
        stop_distance=0.14, quant_intent="pending_buy", snapshot_id=SNAP,
    )
    record["trade_grade"] = "BUY"
    record["veto"] = []
    record["sector"] = "Tech"
    record["entry_band"] = {"stop_min_sadj": 90.0, "limit_max_sadj": 105.0,
                            "dma200_floor_sadj": 70.0, "provisional_stop_sadj": 86.0,
                            "required_margin": 0.05, "split_factor": 1.0}
    return record


def report_of(report_id: str, *, completed: str, validated: str) -> dict:
    report = {
        "report_id": report_id, "ticker": "AAA", "signal_id": SID, "snapshot_id": SNAP,
        "as_of": "2026-05-08", "data_cutoff_at": "2026-05-08T22:00:00Z",
        "signal_published_at": PUBLISHED, "order_cutoff_at": ORDER_CUTOFF,
        "completed_at": completed, "late": False, "status": "complete",
        "intent": "pending_hold_cash",
        "round1": [{"skill": "buffett", "score": 62, "stance": "buy", "confidence": 0.71,
                    "reasons": ["IV 대비 21% 할인"], "news_read": []}],
        "round2": [],
        "round3": {"guard": {"veto_codes": [], "additional_blocks": [], "structural_risk": 58},
                   "chair": {"consensus": ["가격은 매력적"], "dissent": [],
                             "grade_recommendation": "WATCH", "conflict": False,
                             "one_line_ko": "2차 분할가까지 기다린다"}},
        "validation": {"report_validated_at": validated},
        "quant_grade": "BUY",
    }
    return debate_mod.with_report_receipt(report)


def _rules() -> dict:
    return guard.load_rules(ROOT / "config" / "public_allowlist.yaml")


# ── 1. 토론 편입 문서 ─────────────────────────────────────────────────────────
def test_debate_event_document_from_writer_passes_contract(tmp_path):
    """채택 블록·superseded·재시도 필드가 모두 계약을 통과한다(첫 배치 실패의 재현·수정 확인)."""
    record = record_of()
    first = report_of(f"{SID}-debate-01", completed="2026-05-11T11:00:00Z",
                      validated="2026-05-11T11:30:00Z")
    second = report_of(f"{SID}-debate-02", completed="2026-05-11T12:00:00Z",
                       validated="2026-05-11T13:10:00Z")
    debate_mod.reconcile_debates({"reports": [first, second]}, [record], {},
                                 event_dir=tmp_path / "data/ledger/debate_events",
                                 now=ts("2026-05-11T14:24:00Z"), as_of=date(2026, 5, 11))
    rel = f"data/ledger/debate_events/{SID}.json"
    doc = json.loads((tmp_path / rel).read_text(encoding="utf-8"))
    for key in ("adopted", "superseded", "events"):
        assert key in doc, f"생산 코드가 만든 문서에 '{key}' 가 없다 — 테스트 전제가 바뀌었다"
    event = doc["events"][0]
    for key in ("adoption", "frozen_source", "received_at", "validation_claimed_at",
                "validation_source"):
        assert key in event, f"이벤트에 '{key}' 가 없다 — 테스트 전제가 바뀌었다"
    assert guard.check_paths([rel], _rules(), root=tmp_path) == []
    assert guard._contract_problems("debate_event", doc) == []


def test_debate_event_contract_checks_the_adopted_block(tmp_path):
    """`adopted` 안에 낯선 키가 들어오면 통과하지 않는다 — 블록도 필드 계약 대상이다."""
    doc = {"signal_id": SID, "events": [],
           "adopted": {"report_id": f"{SID}-debate-01", "report_sha256": "x", "snapshot_id": SNAP,
                       "signal_id": SID, "report_validated_at": None, "freeze_deadline_at": None,
                       "received_at": None, "adopted_at": None, "avg_cost": 1.0}}
    assert guard._contract_problems("debate_event", doc), "중첩 블록의 금지 키를 놓쳤다"


# ── 2. 알림 요약 ─────────────────────────────────────────────────────────────
class _FailingWebhook:
    name = "webhook"

    def send(self, event):  # noqa: ARG002 — 실패 응답만 돌려준다
        return {"ok": False, "detail": "HTTP 500"}


def _notify_payload() -> dict:
    return {"as_of": "2026-11-20", "status": "degraded", "data_status": "degraded",
            "signals": [{"signal_id": "s1", "ticker": "AAA", "trade_grade": "STRONG_BUY",
                         "entry_score": 88.0},
                        {"signal_id": "s2", "ticker": "BBB", "decision": "WATCH"}],
            "position_checks": [], "blocked": [{"codes": ["V06"]}],
            "diagnostics": {"degraded": ["finnhub"]}}


def test_notifications_latest_with_every_channel_status_passes_contract(tmp_path):
    """실패·재시도 대기·미설정 채널 상태가 계약에 들어와 있다(첫 배치 실패의 재현·수정 확인)."""
    out = tmp_path / "data/notifications_latest.json"
    log = tmp_path / "data/notifications_log.json"
    channels = [notify_mod.ConsoleChannel(), _FailingWebhook(),
                notify_mod.GithubIssueChannel(repo=None)]
    common = {"log_path": log, "out_path": out, "health": {"data_status": "degraded"},
              "channels": channels}
    notify_mod.evaluate_notifications(_notify_payload(), now=ts("2026-11-20T22:30:00Z"), **common)
    summary = notify_mod.evaluate_notifications(_notify_payload(), now=ts("2026-11-20T23:30:00Z"),
                                                **common)
    pending = {row["channel"] for row in summary["retry_pending"]}
    assert "webhook" in pending, "재시도 대기 채널이 요약에 없다 — 테스트 전제가 바뀌었다"
    logged = json.loads(log.read_text(encoding="utf-8"))
    webhook_rows = [entry["channels"]["webhook"] for entry in logged["keys"].values()
                    if "webhook" in (entry.get("channels") or {})]
    assert any(row.get("status") in ("pending_retry", "failed") for row in webhook_rows)
    summary = notify_mod.evaluate_notifications(
        {"as_of": "2026-11-21", "signals": [{"signal_id": "s9", "ticker": "ZZZ",
                                             "trade_grade": "STRONG_BUY", "entry_score": 91.0}]},
        now=ts("2026-11-21T22:30:00Z"), **common)
    buckets = summary["channels"]
    assert "failed" in buckets["webhook"] and "unconfigured" in buckets["github_issue"]
    rel = "data/notifications_latest.json"
    assert guard.check_paths([rel], _rules(), root=tmp_path) == []
    assert guard.check_paths(["data/notifications_log.json"], _rules(), root=tmp_path) == []


def test_notify_contract_rejects_unknown_channel_status():
    doc = {"schema_version": 3, "generated_at": "2026-11-20T22:00:00Z", "as_of": "2026-11-20",
           "data_status": "ok", "channels": {"webhook": {"attempts": 1, "exploded": 1}}}
    assert guard._contract_problems("notify_latest", doc)


# ── 3. 실행 상태(health) ─────────────────────────────────────────────────────
@pytest.mark.parametrize("status", ["ok", "degraded", "outage"])
def test_health_run_state_passes_contract(tmp_path, status):
    payload = run_daily.write_health_state(as_of=date(2026, 11, 20), mode="postclose",
                                           data_status=status, degraded=["finnhub"],
                                           reason="partial_sources", previous={})
    payload.update({"positions_open": 1, "tranches_filled": 1, "tranches_pending": 2,
                    "stop_count": 0, "trim_count": 0, "stop_reentry_blocked_count": 1,
                    "blocked_reentries": [], "cancelled_tranches": [],
                    "order_cancellations_count": 1,
                    "order_cancellations": [{"signal_id": SID, "ticker": "AAA",
                                             "target_session": "2026-05-11",
                                             "cancelled_by": "WATCH_OR_BLOCKED",
                                             "cancel_kind": "watch", "superseded_by": "x",
                                             "at": PUBLISHED, "frozen_at": None,
                                             "freeze_deadline_at": None}]})
    target = tmp_path / "data/health.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    assert guard.check_paths(["data/health.json"], _rules(), root=tmp_path) == []


# ── 4. 포지션 이벤트 ────────────────────────────────────────────────────────
def test_position_events_from_writer_pass_contract(tmp_path):
    events = [execution_event(signal_id=SID, snapshot_id=SNAP, at=FILL_AT,
                              execution_event="buy_filled", tranche=1, fill_price=100.0,
                              weight=0.05, stop_price=90.0)]
    # 정산 이벤트(`live.settle`)는 `date` 를 함께 남긴다 — 실행기가 as_of 세션을 붙이는 자리다.
    events[0]["date"] = FILL_AT[:10]
    rows = live_mod.position_event_rows(events, cancelled=[{"at": FILL_AT, "ticker": "AAA",
                                                            "signal_id": SID, "tranche": 2,
                                                            "cancelled_by": "stop_lock"}])
    target = tmp_path / "data/ledger/position_events.jsonl"
    target.parent.mkdir(parents=True)
    target.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                      encoding="utf-8")
    assert guard.check_paths(["data/ledger/position_events.jsonl"], _rules(), root=tmp_path) == []


# ── 5. 공개 허용 목록·공개 빌드 ─────────────────────────────────────────────
def test_allowlist_carries_adoption_and_channel_fields():
    rules = yaml.safe_load((ROOT / "config" / "public_allowlist.yaml").read_text(encoding="utf-8"))
    for key in ("adopted", "adoption", "adopted_at", "adopted_report_id", "superseded",
                "frozen_source", "received_at", "validation_claimed_at", "validation_source",
                "failed", "pending_retry", "unconfigured"):
        assert key in rules["allow"], f"공개 허용 목록에 '{key}' 가 없다"


def test_public_build_keeps_adoption_fields_and_accepts_debate_events(tmp_path):
    """`build_public_data` 가 새 필드를 버리지 않고, 편입 문서를 읽어도 실패하지 않는다."""
    rules = yaml.safe_load((ROOT / "config" / "public_allowlist.yaml").read_text(encoding="utf-8"))
    filtered = bpd.filter_public({"adoption": "adopted", "validation_source": "argument",
                                  "received_at": "2026-05-11T14:24:00Z"}, set(rules["allow"]))
    assert filtered == {"adoption": "adopted", "validation_source": "argument",
                        "received_at": "2026-05-11T14:24:00Z"}
    record = record_of()
    debate_mod.reconcile_debates({"reports": [report_of(f"{SID}-debate-01",
                                                       completed="2026-05-11T11:00:00Z",
                                                       validated="2026-05-11T11:30:00Z")]},
                                 [record], {}, event_dir=tmp_path / "data/ledger/debate_events",
                                 now=ts("2026-05-11T14:24:00Z"), as_of=date(2026, 5, 11))
    events = debate_mod.load_events(tmp_path / "data/ledger/debate_events")
    report = report_of(f"{SID}-debate-01", completed="2026-05-11T11:00:00Z",
                       validated="2026-05-11T11:30:00Z")
    out = bpd.build(None, None, [record], rules, debate_reports={"reports": [report]},
                    debate_events=events)
    assert "debate.json" in out
    # 원장의 채택 결과를 그대로 읽어 화면 행이 적격으로 올라온다(새 필드가 방해하지 않는다).
    assert out["debate.json"]["counts"]["eligible"] == 1
    assert out["debate.json"]["reports"][0]["eligible"] is True
