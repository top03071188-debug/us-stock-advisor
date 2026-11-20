"""test_audit_5331ab7_final_review — T1~T5 통합 뒤 **전수 재검토**에서 새로 찾은 결함의 회귀.

이 파일이 고정하는 것 (전부 이번 재검토에서 실측으로 확인한 것들이다)

1. **누적 200KiB 예산이 실제로 지켜진다 — 빌드가 죽지 않는다**
   · `signals/today.json`: 신호 7건이 이미 66KB, 20건 185KB, 43건 396KB 였다. 넘치면 `build()` 가
     `SystemExit` 로 **모든 파일을 쓰지 않고** 끝나 배포가 통째로 멈춘다. 지금은 상한을 넘는 행을
     **요약 행**(`compact: true`, `detail_path`)으로 내리고 몇 건인지 `notes` 에 적는다.
   · `ledger/records.json`: 행 수 상한(200)만 있어 실제 레코드 200건이면 214,705B(200KiB 초과)였다.
     지금은 바이트 예산으로 오래된 행을 빼고 `omitted`·`note_ko` 로 밝힌다.
   · 180 리포트(실제 GOOGL 크기)·43 신호·300 원장 레코드·300 알림을 넣어도 **어떤 파일도 200KiB 를
     넘지 않고** 빌드가 0 으로 끝난다.
2. **공개 계약(가드)이 새 알림 장부를 받아 준다** — `notify.py` 가 쓰는 `n_retry_suppressed`·
   `retry_pending_inactive` 가 계약에 없어 **다음 배치의 `check_commit_scope --staged` 가 실패**할
   상태였다(커밋·배포 중단). 지금은 허용 목록과 계약이 모두 받는다.
3. **실행기 배선** — `run_daily` 가 ① 차단을 구조화 맵(`reason`·`signal_id`·`target_session`·
   `observed_at`)으로, ② 매도 의도를 `observed_at`(발행 시각)과 함께, ③ 같은 실행의 S5 를
   `settle(stop_exits=...)` 로 넘긴다. 관측이 다음 세션 시가보다 늦으면 **그 다음 세션**으로 미룬다.
4. **토론 검증 워크플로의 삭제·빈 파일 edge case** — 리포트를 지우거나 비우는 PR 은 통과하지 않는다.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import date
from pathlib import Path

import pytest

from engine.ledger import live
from scripts import build_public_data as bpd
from scripts import check_commit_scope as guard
from scripts import run_daily

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 200 * 1024
TICKERS = [f"T{i:02d}" for i in range(43)]


def _rules() -> dict:
    return bpd.load_allowlist()


def _signals() -> dict:
    return json.loads((ROOT / "data" / "quant_signals.json").read_text(encoding="utf-8"))


def _real_report() -> dict:
    return json.loads((ROOT / "data" / "debate_reports.json").read_text(encoding="utf-8"))["reports"][0]


def _real_records() -> list[dict]:
    text = (ROOT / "data" / "ledger" / "records.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ══════════════════════════════════════════════════════════════════════════════
# 1. 200KiB 예산 — 신호가 많은 날
# ══════════════════════════════════════════════════════════════════════════════
def _signals_with(n: int) -> dict:
    """실제 신호 한 건의 모양을 n 종목으로 복제한다(뉴스·공시·밸류에이션 포함 — 가장 무거운 행)."""
    doc = copy.deepcopy(_signals())
    base = doc["signals"][0]
    doc["signals"] = [{**copy.deepcopy(base), "ticker": f"T{i:02d}", "signal_id": f"2026-11-20-T{i:02d}-BUY-001"}
                      for i in range(n)]
    return doc


@pytest.mark.parametrize("n", [7, 20, 43])
def test_today_stays_under_budget_and_reports_trimmed_rows(n):
    doc = bpd.build_today(_signals_with(n))
    size = bpd._json_size(doc)
    assert size <= MAX_BYTES, f"signals/today.json 이 {size / 1024:.0f}KB — 빌드가 죽는다"
    assert doc["counts"] == {"buy": 0, "sell": 0, "watch": n}, "집계는 전체를 말해야 한다"
    assert len(doc["signals"]) == n, "요약이라도 모든 종목이 목록에 남아야 한다"
    if size > bpd.TODAY_MAX:
        compact = [row for row in doc["signals"] if row.get("compact")]
        assert compact, "상한을 넘겼는데 요약 행이 없다"
        assert all(row.get("detail_path") == f"data/tickers/{row['ticker']}.json" for row in compact)
        assert "요약만 실었습니다" in str(doc.get("notes") or "")


def test_full_rows_are_kept_while_they_fit():
    """작은 배포(7건)는 **아무 행도 줄이지 않는다** — 화면이 예전과 같은 상세를 보여 준다."""
    doc = bpd.build_today(_signals())
    assert not any(row.get("compact") for row in doc["signals"])
    assert doc["signals"][0].get("valuation"), "상세 행에서 밸류에이션이 빠졌다"


# ══════════════════════════════════════════════════════════════════════════════
# 1-b. 200KiB 예산 — 원장 레코드 누적
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("n", [28, 200, 300])
def test_records_stay_under_budget_when_the_ledger_grows(n):
    rows = _real_records()
    records = []
    for i in range(n):
        record = copy.deepcopy(rows[i % len(rows)])
        record["signal_id"] = f"{record['signal_id']}-{i:03d}"
        records.append(record)
    doc = bpd.build_records(_signals(), records)
    size = bpd._json_size(doc)
    assert size <= MAX_BYTES, f"ledger/records.json 이 {size / 1024:.0f}KB — 빌드가 죽는다"
    assert doc["count"] == len(doc["records"])
    if n == 28:
        assert "omitted" not in doc
    else:
        assert doc["records"], "최근 행이 하나도 없다"
        assert doc.get("omitted", 0) > 0 and "빠졌습니다" in str(doc.get("note_ko") or "")


def test_whole_build_survives_a_realistic_worst_case():
    """180 리포트(실 리포트 크기) · 43 신호 · 300 원장 레코드 — 어떤 파일도 200KiB 를 넘지 않는다."""
    template = _real_report()
    reports = []
    for day in range(1, 31):
        for ticker in ("GOOGL", "MSFT", "NVDA", "ADBE", "AMZN", "META"):
            row = copy.deepcopy(template)
            row.update({"report_id": f"2026-10-{day:02d}-{ticker}-debate-01", "ticker": ticker,
                        "signal_id": f"2026-10-{day:02d}-{ticker}-WATCH-001", "as_of": f"2026-10-{day:02d}"})
            reports.append(row)
    rows = _real_records()
    records = [{**copy.deepcopy(rows[i % len(rows)]), "signal_id": f"{rows[i % len(rows)]['signal_id']}-{i:03d}"}
               for i in range(300)]
    notifications = {
        "schema_version": 1, "generated_at": "2026-11-20T22:00:00Z", "as_of": "2026-11-20",
        "data_status": "ok", "n_events": 300, "n_new": 300, "n_suppressed": 0,
        "n_retried": 0, "n_retry_suppressed": 0, "retry_pending": [],
        "new": [{"event_key": f"event-{i}", "type": "buy", "label_ko": "매수", "severity": "info",
                 "message_ko": "알림 " * 8, "ticker": "GOOGL", "signal_id": f"s{i}",
                 "sent_at": "2026-11-20T22:00:00Z"} for i in range(300)],
        "channels": {"console": {"attempts": 300, "sent": 300}}, "note_ko": "합성",
    }
    out = bpd.build(_signals_with(43), None, records, _rules(), params=None,
                    debate_reports={"schema_version": "1.0", "reports": reports},
                    notifications=notifications, debate_events={})
    sizes = {name: bpd._json_size(payload) for name, payload in out.items()}
    over = {name: size for name, size in sizes.items() if size > MAX_BYTES}
    assert not over, f"200KiB 초과: {over}"
    assert sizes["debate.json"] <= MAX_BYTES
    assert sizes["ledger/records.json"] <= MAX_BYTES
    assert sizes["signals/today.json"] <= MAX_BYTES
    for name, payload in out.items():
        bpd.assert_no_forbidden(payload, _rules(), name)


# ══════════════════════════════════════════════════════════════════════════════
# 2. 공개 계약 — 알림 재시도 장부가 커밋 가드를 통과한다
# ══════════════════════════════════════════════════════════════════════════════
def test_notify_latest_contract_accepts_retry_ledger_fields():
    """`notify.py` 가 쓰는 요약 필드가 계약에 없으면 다음 배치의 커밋 가드가 실패한다(배포 중단)."""
    doc = {
        "schema_version": 1, "generated_at": "2026-11-20T22:00:00Z", "as_of": "2026-11-20",
        "data_status": "ok", "n_events": 3, "n_new": 1, "n_suppressed": 0, "n_retried": 2,
        "n_retry_suppressed": 1, "retry_pending_inactive": [],
        "retry_pending": [{"event_key": "k1", "channel": "webhook", "type": "buy", "attempts": 1}],
        "new": [{"event_key": "k2", "type": "buy", "label_ko": "매수", "severity": "info",
                 "message_ko": "알림", "ticker": "AAA", "signal_id": "s", "sent_at": "2026-11-20T22:00:00Z",
                 "status": "sent", "attempts": 1,
                 "channels": {"console": {"status": "sent", "attempts": 1}}}],
        "channels": {"console": {"attempts": 3, "sent": 3}},
        "note_ko": "합성",
    }
    assert guard._contract_problems("notify_latest", doc) == []


def test_public_notifications_keeps_the_retry_summary():
    doc = {"schema_version": 1, "generated_at": "2026-11-20T22:00:00Z", "as_of": "2026-11-20",
           "data_status": "ok", "n_new": 1, "n_retried": 2, "n_retry_suppressed": 1,
           "retry_pending": [{"event_key": "k1", "channel": "webhook", "type": "buy", "attempts": 1,
                              "detail": "HTTP 500"}],
           "new": [], "channels": {}, "note_ko": "합성"}
    out = bpd.build_notifications(doc)
    assert out["n_retried"] == 2 and out["n_retry_suppressed"] == 1
    assert out["retry_pending"] == [{"event_key": "k1", "channel": "webhook", "type": "buy", "attempts": 1}]
    allow = set(_rules().get("allow", []))
    filtered = bpd.filter_public(out, allow)
    for key in ("n_retried", "n_retry_suppressed", "retry_pending"):
        assert key in filtered, f"{key} 가 공개 허용 목록에서 빠져 화면에 안 온다"


# ══════════════════════════════════════════════════════════════════════════════
# 3. 실행기 배선 — 차단·매도 의도·당일 S5
# ══════════════════════════════════════════════════════════════════════════════
def test_blocked_rows_are_passed_as_the_structured_contract():
    rows = [{"ticker": "AAA", "reason": "V06", "codes": ["V06"]},
            {"ticker": "BBB", "reason": "SECTOR_FORMULA_PENDING"}]
    plans = run_daily._blocked_plans(rows, target_session="2026-11-24",
                                     observed_at="2026-11-23T22:02:11Z")
    assert plans["AAA"] == {"reason": "V06", "signal_id": None, "target_session": "2026-11-24",
                            "observed_at": "2026-11-23T22:02:11Z"}
    # 구조화 맵이 실제로 장후 관측을 구분한다 — 이미 체결된 시가 주문을 뒤집지 않는다.
    panel = {"AAA": __import__("tests.test_phase4_ledger_and_ui", fromlist=["panel_of"]).panel_of(
            [100.0] * 5, start="2026-11-20")}
    record = __import__("tests.test_audit_5331ab7_track2", fromlist=["_record"])._record(
        published="2026-11-23T10:00:00Z", order="2026-11-23T14:25:00Z", session="2026-11-23")
    frozen = live.settle([record], {}, panel, date(2026, 11, 23), now="2026-11-23T22:00:00Z",
                         blocked=run_daily._blocked_plans(
                             [{"ticker": "AAA", "reason": "V06"}],
                             target_session="2026-11-23", observed_at="2026-11-23T22:00:00Z"))
    assert [e for e in frozen["new_events"]
            if (e.get("quant_track") or {}).get("execution_event") == "buy_filled"], \
        "장후에 관측된 차단이 이미 동결된 시가 주문을 뒤집었다"
    assert any(row.get("reason") == "BLOCK_OBSERVED_AFTER_OPEN" for row in frozen["settled"])


def test_forced_exits_carry_the_publication_time():
    checks = [{"ticker": "AAA", "verdict": "EXIT", "triggers": {"S3": True}, "cancel_pending_tranches": True},
              {"ticker": "BBB", "verdict": "TRIM", "triggers": {}}]
    assert run_daily._forced_exits(checks)["AAA"] == "S3", "구 계약(문자열) 호환이 깨졌다"
    plans = run_daily._forced_exits(checks, observed_at="2026-11-23T22:02:11Z")
    assert plans["AAA"] == {"reason": "S3", "observed_at": "2026-11-23T22:02:11Z"}
    assert plans["BBB"]["reason"] in ("TRIM", "S3", "S5")


def test_same_run_stop_plans_reach_settle():
    checks = [{"ticker": "AAA", "verdict": "EXIT", "triggers": {"S5": True}, "cancel_pending_tranches": True},
              {"ticker": "BBB", "verdict": "EXIT", "triggers": {"S3": True}}]
    plans = run_daily._stop_exit_plans(checks, observed_at="2026-11-23T22:02:11Z",
                                       target_session="2026-11-24")
    assert plans == [{"ticker": "AAA", "reason": "S5", "observed_at": "2026-11-23T22:02:11Z",
                      "target_session": "2026-11-24"}], "S5 만 잠금 입력이다(S3 는 재진입 잠금이 아니다)"


@pytest.mark.parametrize("observed,expected", [
    ("2026-05-12T22:00:00Z", date(2026, 5, 13)),   # 장후(정상) → 다음 세션
    ("2026-05-13T13:29:00Z", date(2026, 5, 13)),   # 다음 세션 시가 전 → 그 세션
    ("2026-05-13T15:00:00Z", date(2026, 5, 14)),   # 다음 세션 시가 **이후** → 그 다음 세션
])
def test_deferred_session_is_the_first_open_after_observation(observed, expected):
    plan = live._forced_exit_plan({"AAA": {"reason": "S3", "observed_at": observed}}, date(2026, 5, 12))
    assert plan["AAA"]["when"] == "deferred"
    assert plan["AAA"]["since"] == expected


# ══════════════════════════════════════════════════════════════════════════════
# 4. 토론 검증 워크플로 — 삭제·빈 파일 edge case
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("rel", [".github/workflows/debate-validate.yml",
                                 "codex_debate/workflows/debate-validate.yml"])
def test_workflow_rejects_deleted_or_emptied_reports(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert "삭제 PR 금지" in text, "리포트를 지우는 PR 을 막는 문장이 없다"
    assert "리포트 파일을 비웠다" in text, "0바이트 리포트 PR 을 막는 문장이 없다"
    assert re.search(r"git cat-file -e \"\$?\{?PR_SHA", text), "PR head blob 존재를 확인해야 한다"


# ══════════════════════════════════════════════════════════════════════════════
# 1-c. 상세 파일 하나가 너무 커도 빌드는 죽지 않는다
# ══════════════════════════════════════════════════════════════════════════════
def test_oversized_detail_file_degrades_instead_of_killing_the_build():
    """라운드 원문이 200KB 를 넘는 리포트 1건이 **빌드 전체를 죽이던** 경로(감사 09 잔여).

    지금은 그 리포트만 요약으로 내리고(`detail_path` 는 실제 파일을 가리킨다) 사유를 적는다.
    """
    report = copy.deepcopy(_real_report())
    # 공개 변환은 스킬당 사유를 3개로 자르지만 **스킬 수**는 자르지 않는다 — 라운드가 비정상적으로
    # 길어진 리포트 하나가 빌드를 죽이던 경로를 재현한다.
    report["round1"] = [{"skill": f"s{i}", "score": 1.0, "stance": "hold", "confidence": 0.5,
                         "reasons": ["사유 " * 100] * 3} for i in range(1000)]
    details: dict[str, dict] = {}
    comparison = None
    doc = bpd.build_debate({"schema_version": "1.0", "reports": [report]}, [], comparison, None,
                           "2026-11-20", debate_events={}, details=details)
    assert bpd._json_size(doc) <= MAX_BYTES
    name = bpd.debate_detail_name(report["report_id"])
    payload = details[f"debate_details/{name}.json"]
    assert not any(key in payload for key in bpd.ROUND_FIELDS), "상한을 넘는 원문이 그대로 실렸다"
    assert bpd._json_size(payload) <= MAX_BYTES
    assert "상한" in str(payload.get("note_ko") or "")
    assert "원문을 빼고" in str(doc.get("note_ko") or ""), doc.get("note_ko")
