"""test_phase4_ui_and_pipeline — Phase 4 감사 트랙 B (P0-2 · P0-4 · P1-2 · P2-1) 고정 테스트.

검사하는 계약
- **P0-2**: `run_daily` 가 토론 편입을 **정산보다 먼저** 하고, 편입 검증보다 먼저 체결된 주문의 페어는
  비교에서 뺀다(`FILLED_BEFORE_VALIDATION`).
- **P0-4**: Paper 채점은 `holdout_start=None` 이다 — 만기 봉이 관측된 신호를 홀드아웃으로 자르지 않는다.
  보정 홀드아웃 창은 `ledger_summary.calibration_window` 에 따로 남는다.
- **P2-1**: 시세 전체 실패(장애) 실행도 `data/health.json` 에 `data_status=outage`·`last_attempt_at`·
  `last_success_at`·`data_cutoff_at`·`target_session`·`outage_id` 를 남기고, 전일 정상
  `data/quant_signals.json` 은 **덮어쓰지 않는다**. 같은 장애는 같은 `outage_id`(알림 1회).
- **P2-1**: 공개 `meta.json`·`health.json` 의 `generated_at` 은 **신호 확정 시각**이고 빌드 시각은 `build_at`.
  `bundle_version`(=`inputs_hash`+`snapshot_id`)으로 발행 세트를 식별한다.
- **P2-1**: 알림 로그는 채널 단위 `status(sent|failed|pending_retry)`·`attempt_at`·`latency_seconds` 를 남기고,
  실패는 `sent` 로 기록하지 않고 재시도 큐에 둔다. 장애 알림은 `outage_id` 로 묶인다.
- **P2-1**: PWA 는 서비스워커가 발행 세트 단위로 캐시하고, 화면은 실행 상태·세트 불일치를 배지로 보여 준다.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from engine.ledger import debate as debate_mod
from engine.ledger import live
from scripts import build_public_data, notify, run_daily

ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2026-11-20"
LATER = "2026-11-24"


def future_cutoff(hours: int = 3) -> str:
    """동결 마감이 **아직 지나지 않은** 주문 마감 — 실행 시각 기준으로 만든다.

    고정 날짜(`2026-11-20T14:25:00Z` + 버퍼 5분)를 쓰면 그 시각이 지난 뒤부터 교체가
    `FROZEN_ORDER_STANDS` 로 뒤집혀 "취소가 그 실행에서 기록된다" 테스트가 실패한다(시한폭탄).
    """
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")


@pytest.fixture
def workdir(tmp_path, monkeypatch) -> Path:
    """config/·schemas/·scripts/redact_quotes.py 만 가진 임시 저장소 루트."""
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


# ── 1. 실행 순서 · Paper 채점 (P0-2 · P0-4) ──────────────────────────────────
def test_run_daily_reconciles_before_settling(workdir, monkeypatch):
    """P0-2: 편입 검증이 정산보다 먼저 — 같은 배치의 체결로 소급 페어를 만들지 않는다.

    P0-3 배선: `settle` **앞**의 `settle_positions` 1회는 섹터 캡 분모(정산 전 보유)를 읽는
    **조회**다(`position_events_path=None` — 이벤트 로그 미기록). 계좌를 확정하는 정산은
    `settle` 뒤에 오고, 그 호출만 `position_events.jsonl` 에 기록한다.
    """
    order: list[tuple[str, dict]] = []
    originals = {
        "reconcile_debates": debate_mod.reconcile_debates,
        "settle": live.settle,
        "settle_positions": live.settle_positions,
        "grade_all": live.grade_all,
    }

    def spy(name):
        original = originals[name]

        def wrapper(*args, **kwargs):
            order.append((name, kwargs))
            return original(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(debate_mod, "reconcile_debates", spy("reconcile_debates"))
    monkeypatch.setattr(live, "settle", spy("settle"))
    monkeypatch.setattr(live, "settle_positions", spy("settle_positions"))
    monkeypatch.setattr(live, "grade_all", spy("grade_all"))

    assert _run(monkeypatch) == 0
    names = [name for name, _ in order]
    assert "reconcile_debates" in names and "settle" in names, names
    assert names.index("reconcile_debates") < names.index("settle"), names
    probes = [i for i, (name, kw) in enumerate(order)
              if name == "settle_positions" and kw.get("position_events_path") is None]
    persisting = [i for i, (name, kw) in enumerate(order)
                  if name == "settle_positions" and kw.get("position_events_path") is not None]
    assert probes and probes[0] < names.index("settle"), order
    assert persisting and names.index("settle") < persisting[0], order
    # 채점·비교는 정산 뒤에 온다
    assert persisting[0] < names.index("grade_all"), names


def test_reconcile_uses_pre_settlement_executions(workdir, monkeypatch):
    """편입 판정에 **정산 전 체결 스냅샷**을 넘긴다 — 이번 배치 체결이 페어 근거가 되지 않게."""
    captured: list[dict] = []
    original = debate_mod.reconcile_debates

    def spy(reports_doc, records, executions=None, **kwargs):
        captured.append(dict(executions or {}))
        return original(reports_doc, records, executions, **kwargs)

    monkeypatch.setattr(debate_mod, "reconcile_debates", spy)
    assert _run(monkeypatch) == 0
    assert captured and captured[0] == {}      # 첫 배치는 체결이 없다


def test_paper_grading_does_not_use_holdout(workdir, monkeypatch):
    """P0-4: Paper 성적표는 홀드아웃을 보지 않는다. 보정 창은 따로 남는다."""
    captured: dict = {}
    original = live.grade_all

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(live, "grade_all", spy)
    assert _run(monkeypatch) == 0
    assert captured.get("holdout_start") is None
    window = _load("data/quant_signals.json")["ledger_summary"]["calibration_window"]
    assert window["holdout_start"] and window["holdout_end"]
    assert "Paper" in window["note_ko"]


def test_retroactive_pairs_are_dropped_from_comparison():
    """편입 검증 시각보다 먼저 체결된 주문의 페어는 A/B 비교에서 뺀다(P0-2)."""
    by_signal = {"S1": {"ticker": "AAA", "eligible": True, "divergent": True},
                 "S2": {"ticker": "BBB", "eligible": True, "divergent": True},
                 "S3": {"ticker": "CCC", "eligible": True, "divergent": True}}
    executions = {"S1": {"at": "2026-11-20T13:30:00Z"},
                  "S2": {"at": "2026-11-20T13:30:00Z"}}      # S3 은 미체결
    reports = {"reports": [
        {"signal_id": "S1", "validation": {"report_validated_at": "2026-11-20T13:45:00Z"}},  # 검증이 체결 뒤
        {"signal_id": "S2", "validation": {"report_validated_at": "2026-11-20T13:10:00Z"}},  # 체결 전 검증
    ]}
    kept, dropped = run_daily.drop_retroactive_pairs(by_signal, executions, reports)
    assert set(kept) == {"S2", "S3"}
    assert dropped == [{"signal_id": "S1", "ticker": "AAA", "execution_at": "2026-11-20T13:30:00+00:00",
                        "report_validated_at": "2026-11-20T13:45:00+00:00",
                        "reason": "FILLED_BEFORE_VALIDATION"}]


# ── 2. 장애 상태 보존 (P2-1) ─────────────────────────────────────────────────
def _degraded_dataset(as_of):
    return {"as_of": as_of, "universe": [], "panels": {}, "macro": {}, "macro_previous": {},
            "filings": {}, "news": {}, "fundamentals": {}, "valuation_inputs": {},
            "source": "live",
            "diagnostics": {"mode": "live", "degraded": ["prices:timeout", "finnhub:503"],
                            "sources": {"prices": {"status": "failed", "detail": "timeout"}}}}


def test_outage_run_writes_health_and_keeps_previous_signals(workdir, monkeypatch):
    assert _run(monkeypatch) == 0                       # 먼저 정상 배치 → last_success_at 확보
    previous = Path("data/quant_signals.json").read_bytes()
    good_health = _load("data/health.json")
    assert good_health["data_status"] == "ok" and good_health["last_success_at"]

    monkeypatch.setattr(run_daily, "load_dataset", lambda *a, **k: _degraded_dataset(LATER))
    assert _run(monkeypatch, LATER) == 0

    health = _load("data/health.json")
    assert health["data_status"] == "outage"
    assert health["last_success_at"] == good_health["last_success_at"]   # 직전 성공 시각 보존
    assert health["last_attempt_at"] and health["last_attempt_at"] >= health["last_success_at"]
    assert health["data_cutoff_at"] and health["target_session"]
    assert health["reason"] == "price_panels_empty"
    assert health["outage_id"].startswith("outage-")
    assert health["note_ko"].startswith("이번 세션 판단 보류")
    # 규칙 건강 지표(마지막 성공 값)를 지우지 않는다
    assert "stop_reentry_blocked_count" in health

    # 전일 정상 신호는 **그대로**(덮어쓰지 않는다)
    assert Path("data/quant_signals.json").read_bytes() == previous
    degraded = _load("data/quant_signals.degraded.json")
    assert degraded["data_status"] == "outage" and degraded["outage_id"] == health["outage_id"]

    # 장애 알림은 한 번 — 같은 outage_id 로 다시 돌아도 중복되지 않는다
    first = _load("data/notifications_latest.json")
    assert any(e["type"] == "outage" for e in first["new"])
    assert _run(monkeypatch, LATER) == 0
    again = _load("data/notifications_latest.json")
    assert again["n_new"] == 0
    assert _load("data/health.json")["outage_id"] == health["outage_id"]


# ── 3. 공개 데이터 시각 고정·세트 식별자 (P2-1) ──────────────────────────────
def _signals_for_build() -> dict:
    return {"as_of": AS_OF, "status": "normal", "signal_published_at": "2026-11-20T05:57:49Z",
            "generated_at": "2026-11-20T06:40:00Z", "data_cutoff_at": "2026-11-19T22:00:00Z",
            "target_session": AS_OF, "inputs_hash": "sha256:abc123", "snapshot_id": "sha256:def456",
            "params_version": "params_v001", "regime": {"regime": "risk_on"}, "signals": [],
            "blocked": [], "watchlist": [], "ledger_summary": {}, "portfolio": {}}


def test_meta_generated_at_is_signal_time_not_build_time():
    signals = _signals_for_build()
    health = {"data_status": "outage", "last_attempt_at": "2026-11-21T05:00:00Z",
              "last_success_at": "2026-11-20T05:57:49Z", "outage_id": "outage-2026-11-21-abc"}
    meta = build_public_data.build_meta(signals, None, None, health)
    assert meta["generated_at"] == "2026-11-20T05:57:49Z"     # 신호 확정 시각 그대로
    assert meta["build_at"] and meta["build_at"] != meta["generated_at"]
    assert meta["bundle_version"] == "sha256:abc123|sha256:def456"
    assert meta["data_status"] == "outage"
    assert meta["last_attempt_at"] == "2026-11-21T05:00:00Z"
    assert meta["last_success_at"] == "2026-11-20T05:57:49Z"
    assert meta["outage_id"] == "outage-2026-11-21-abc"


def test_health_json_carries_same_time_and_bundle_contract():
    signals = _signals_for_build()
    health = {"data_status": "ok", "status": "normal", "last_attempt_at": "2026-11-20T06:40:00Z",
              "last_success_at": "2026-11-20T06:40:00Z", "data_cutoff_at": "2026-11-19T22:00:00Z",
              "target_session": AS_OF, "outage_id": None, "stop_reentry_blocked_count": 2}
    out = build_public_data.build_health(health, signals)
    assert out["generated_at"] == "2026-11-20T05:57:49Z"
    assert out["build_at"] and out["build_at"] != out["generated_at"]
    assert out["bundle_version"] == "sha256:abc123|sha256:def456"
    assert out["last_attempt_at"] == "2026-11-20T06:40:00Z"
    assert out["data_cutoff_at"] == "2026-11-19T22:00:00Z"
    assert out["target_session"] == AS_OF
    assert out["stop_reentry_blocked_count"] == 2


def test_debate_comparison_separates_policy_effect_from_pair_pnl():
    comparison = {"as_of": AS_OF, "policy_diff_pp": 0.2, "n_graded_pairs": 31, "n_eff": 16,
                  "avoided_loss_pp": 0.4, "missed_gain_pp": -0.1, "net_after_cost_pp": 0.2,
                  "verdict": "토론 유효"}
    debate = build_public_data.build_debate({"reports": []}, [], comparison, {}, AS_OF)
    assert debate["comparison"]["policy_diff_pp"] == 0.2
    pair = debate["comparison"]["pair_summary"]
    assert pair["pairs"] == 31 and pair["n_eff"] == 16
    # 페어 손익만 있고 정책 효과가 없으면 `None` 으로 남는다(대신 채우지 않는다)
    none_policy = build_public_data.build_debate({"reports": []}, [], {"divergent_pairs": 2}, {}, AS_OF)
    assert none_policy["comparison"]["policy_diff_pp"] is None
    assert none_policy["comparison"]["pair_summary"]["pairs"] == 2


def test_allowlist_carries_new_public_fields_and_build_passes():
    rules = yaml.safe_load((ROOT / "config" / "public_allowlist.yaml").read_text(encoding="utf-8"))
    for key in ("build_at", "bundle_version", "last_attempt_at", "last_success_at", "outage_id",
                "data_status", "policy_diff_pp", "pair_summary", "debate_structural_risk",
                "debate_risk_gate_applied", "confidence_cap", "content_hash", "normalizer_version",
                "files", "sha256", "size"):
        assert key in rules["allow"], f"공개 허용 목록에 '{key}' 가 없다"
    out = build_public_data.build(_signals_for_build(), {}, [], rules,
                                  health={"data_status": "ok", "last_success_at": "2026-11-20T05:57:49Z"})
    # 감사 12: 버전은 **모든 공개 파일 해시**에서 나온다(입력 지문 단독이 아니다).
    manifest = out["deploy_manifest.json"]
    assert out["meta.json"]["bundle_version"] == manifest["bundle_version"]
    assert manifest["bundle_version"] != "sha256:abc123|sha256:def456"
    assert out["meta.json"]["inputs_hash"] == "sha256:abc123"
    assert out["meta.json"]["snapshot_id"] == "sha256:def456"
    assert out["meta.json"]["generated_at"] == "2026-11-20T05:57:49Z"
    assert out["health.json"]["build_at"]
    assert manifest["files"]["health.json"]["sha256"]


# ── 4. 알림 로그·재시도 (P2-1) ───────────────────────────────────────────────
class FailingChannel:
    name = "webhook"

    def send(self, event):  # noqa: ARG002 — 실패 응답만 돌려준다
        return {"ok": False, "detail": "HTTP 500"}


def _notify_payload() -> dict:
    return {"as_of": AS_OF, "status": "normal",
            "signals": [{"signal_id": f"{AS_OF}-AAA-BUY-001", "ticker": "AAA", "decision": "BUY",
                         "trade_grade": "STRONG_BUY", "entry_score": 88.0,
                         "quant_intent": "pending_buy"}],
            "position_checks": [], "blocked": [], "diagnostics": {}}


def test_failed_channels_are_not_recorded_as_sent(tmp_path):
    log = tmp_path / "log.json"
    summary = notify.evaluate_notifications(_notify_payload(), log_path=log, out_path=None,
                                            channels=[FailingChannel()])
    doc = json.loads(log.read_text(encoding="utf-8"))
    assert summary["n_new"] == 0
    assert summary["retry_pending"] and summary["n_retried"] == 0
    assert all(entry["status"] == "pending_retry" for entry in doc["keys"].values())
    rows = doc["deliveries"]
    assert rows and {"channel", "event_key", "status", "attempt_at", "latency_seconds"} <= set(rows[0])
    assert {row["status"] for row in rows} == {"pending_retry"}
    assert all(row["channel"] == "webhook" for row in rows)


def test_pending_retry_is_resent_and_marked_sent(tmp_path):
    log = tmp_path / "log.json"
    notify.evaluate_notifications(_notify_payload(), log_path=log, out_path=None,
                                  channels=[FailingChannel()])
    summary = notify.evaluate_notifications(_notify_payload(), log_path=log, out_path=None,
                                            channels=[notify.ConsoleChannel()])
    doc = json.loads(log.read_text(encoding="utf-8"))
    assert summary["n_retried"] >= 1
    assert all(entry["status"] == "sent" for entry in doc["keys"].values())
    assert {row["status"] for row in doc["deliveries"]} == {"pending_retry", "sent"}
    assert summary["retry_pending"] == []


def test_retry_gives_up_after_max_attempts(tmp_path):
    log = tmp_path / "log.json"
    for _ in range(notify.MAX_ATTEMPTS):
        notify.evaluate_notifications(_notify_payload(), log_path=log, out_path=None,
                                      channels=[FailingChannel()])
    doc = json.loads(log.read_text(encoding="utf-8"))
    assert {entry["status"] for entry in doc["keys"].values()} == {"failed"}
    summary = notify.evaluate_notifications(_notify_payload(), log_path=log, out_path=None,
                                            channels=[notify.ConsoleChannel()])
    assert summary["n_retried"] == 0          # 상한을 넘긴 건은 다시 보내지 않는다


def test_outage_notification_is_keyed_by_outage_id(tmp_path):
    log = tmp_path / "log.json"
    health = {"data_status": "outage", "outage_id": "outage-2026-11-21-abc",
              "last_success_at": "2026-11-20T05:57:49Z"}
    first = notify.evaluate_notifications({"as_of": AS_OF, "signals": [], "blocked": [],
                                           "position_checks": [], "diagnostics": {}},
                                          log_path=log, out_path=None, health=health,
                                          channels=[notify.ConsoleChannel()],
                                          outage_id=health["outage_id"])
    events = [e for e in first["new"] if e["type"] == "outage"]
    assert events and events[0]["event_key"] == "outage:outage-2026-11-21-abc"
    assert "마지막 성공" in events[0]["message_ko"]
    second = notify.evaluate_notifications({"as_of": AS_OF, "signals": [], "blocked": [],
                                           "position_checks": [], "diagnostics": {}},
                                           log_path=log, out_path=None, health=health,
                                           channels=[notify.ConsoleChannel()],
                                           outage_id=health["outage_id"])
    assert [e for e in second["new"] if e["type"] == "outage"] == []


# ── 5. PWA 서비스워커·화면 배지 (P2-1 · 감사 12) ──────────────────────────────
def test_service_worker_caches_current_bundle_set():
    sw = (ROOT / "web" / "sw.js").read_text(encoding="utf-8")
    for needle in ("SW_VERSION", "SET_PREFIX", "bundle_version", "prepare-set", "set-activated",
                   "/data/", "offline", "deploy_manifest", "sha256"):
        assert needle in sw, f"sw.js 에 '{needle}' 가 없다"
    # 셸·자산 버전은 **고정 문자열이 아니라 app.js 를 고칠 때마다 올리는 계약**이다.
    # index.html 의 `?v=` 와 같아야 새 셸이 옛 app.js 를 계속 물고 있지 않는다.
    assert "ASSET_VERSION" in sw and "SW_VERSION" in sw
    # meta.json 만 보고 세트를 바꾸던 경로는 **없어야** 한다(감사 12)
    assert "set-bundle-version" not in sw
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "sw.js" in js and "serviceWorker" in js
    assert "bundle_version" in js and "deploy_manifest" in sw
    assert "CORE_FILES" in js


def test_ui_surfaces_data_status_and_bundle_mismatch():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    for element in ("data-status-banner", "bundle-banner", "policy-effect"):
        assert f'id="{element}"' in html, f"index.html 에 '{element}' 자리가 없다"
        assert f'$("{element}")' in js, f"app.js 가 '{element}' 를 그리지 않는다"
    for needle in ("이번 세션 판단 보류", "last_attempt_at", "last_success_at", "data_status",
                   "이전/혼합 세트", "policy_diff_pp", "pair_summary", "계좌 정책 순효과"):
        assert needle in js, f"app.js 에 '{needle}' 표시가 없다"
    asset = __import__("re").search(r'ASSET_VERSION = "([^"]+)"',
                                    (ROOT / "web" / "sw.js").read_text(encoding="utf-8")).group(1)
    assert f"app.js?v={asset}" in html and f"styles.css?v={asset}" in html, \
        "index.html 의 ?v= 와 sw.js 의 ASSET_VERSION 이 어긋났다"
    manifest = json.loads((ROOT / "web" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["start_url"] and manifest["display"] == "standalone"


def test_ui_shows_trim_reentry_and_first_outage_states():
    """TRIM 1/3 익절 · 손절 후 재진입 카운터 · 첫 장애(`last_success_at: null`) 표기."""
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "1/3 익절" in js
    assert "TRIM" in js and "forced_exits" in js
    assert "이전 정상 배치 없음" in js, "`last_success_at` 이 null 인 첫 장애를 숨기면 안 된다"
    assert "손절 후 재진입 차단" in js
    assert "종목별" in js, "회차는 (as_of, ticker) 단위 — 배치 회차처럼 보이면 안 된다"


def test_debate_pr_workflow_uses_base_branch_verifier():
    """감사 10: PR 자신의 검사기를 믿지 않고 **기본 브랜치 검증기**로 검사한다.

    등록본(`.github/workflows/debate-validate.yml`)은 CLI 토큰(workflows 권한 없음)으로 push 할 수 없어
    GitHub 웹에서 등록한다 — 그래서 이 테스트는 **템플릿과 준비본의 일치**를 고정하고, 등록본은
    존재와 핵심 표지만 확인한다(등록본 교체는 통합 단계의 사람 작업이다).
    """
    text = (ROOT / "codex_debate" / "workflows" / "debate-validate.yml").read_text(encoding="utf-8")
    assert "git diff --name-only" in text
    assert "check_commit_scope.py" in text and "--paths" in text
    assert "data/debate_reports.json" in text and "data/debate/manifest.json" in text
    assert ".github/workflows/" in text and "config/params" in text
    assert "fetch-depth: 0" in text
    # 기본 브랜치 검증기 — PR 코드의 검사기·허용 목록을 쓰지 않는다
    assert "/tmp/trusted" in text and "git archive" in text
    assert "--allowlist /tmp/trusted/config/public_allowlist.yaml" in text
    assert '--root "$GITHUB_WORKSPACE"' in text
    # 운영용 주석 — "이 파일은 템플릿입니다" 라는 혼동 문구는 없어야 한다(§14#6)
    assert "템플릿**" not in text and "이 파일은 저장소 루트" not in text
    assert "실제로 도는 워크플로" in text
    prepared = ROOT.parents[1] / "scratch" / "workflows_upload" / "debate-validate.yml"
    if prepared.exists():
        assert prepared.read_text(encoding="utf-8") == text, "준비본과 템플릿이 어긋났다"


# ── 6. Phase 4 잔여 배선(P0-3 · 2·3차/섹터 캡/취소 이벤트) ─────────────────────
def test_ledger_view_persists_position_events_under_its_root(workdir, monkeypatch):
    """`ledger_view` 가 2·3차·손절·익절 세션 이벤트를 **실제 파일**에 남긴다(경로 바인딩 확인).

    기본값은 `live.POSITION_EVENTS_PATH`(상대 경로)이고 실행 루트 기준으로 열려야 한다 —
    테스트가 `tmp_path` 로 돌 때 저장소의 진짜 `data/ledger/position_events.jsonl` 을 오염시키지 않는다.
    """
    assert _run(monkeypatch) == 0
    path = Path("data/ledger/position_events.jsonl")
    assert path.exists(), "ledger_view 가 position_events.jsonl 을 남기지 않았다"
    assert path.resolve().is_relative_to(Path.cwd().resolve())
    assert not (ROOT / "data" / "ledger" / "position_events.jsonl").exists() or \
        (ROOT / "data" / "ledger" / "position_events.jsonl").resolve() != path.resolve()

    # 두 번째 실행(같은 as_of)에서도 파일이 중복 행을 만들지 않는다(event_id 멱등)
    before = path.read_text(encoding="utf-8")
    assert _run(monkeypatch) == 0
    assert path.read_text(encoding="utf-8") == before


def test_ledger_view_passes_sector_cap_from_params(workdir, monkeypatch):
    """2·3차 체결도 params `risk.max_sector` 를 본다 — 엔진 기본값 하드코딩이 아니다."""
    captured: list[dict] = []
    original = live.settle_positions

    def spy(*args, **kwargs):
        captured.append(dict(kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(live, "settle_positions", spy)
    params_path = Path("config/params_v001.yaml")
    params = yaml.safe_load(params_path.read_text(encoding="utf-8"))
    params["risk"]["max_sector"] = 0.18
    params_path.write_text(yaml.safe_dump(params, allow_unicode=True), encoding="utf-8")
    assert _run(monkeypatch) == 0
    caps = [kw.get("max_sector") for kw in captured if kw.get("position_events_path") is not None]
    assert caps and all(cap == pytest.approx(0.18) for cap in caps), captured


def test_run_daily_feeds_existing_sector_exposure_to_settle(workdir, monkeypatch):
    """1차 섹터 캡이 **정산 전 보유**까지 합산된다 — 배선 전에는 이번 배치 체결만 셌다."""
    captured: list[dict] = []
    original = live.settle

    def spy(*args, **kwargs):
        captured.append(dict(kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(live, "settle", spy)
    assert _run(monkeypatch) == 0
    assert captured, "live.settle 이 호출되지 않았다"
    assert isinstance(captured[0].get("sector_exposure"), dict), captured[0]
    assert captured[0].get("max_sector") == pytest.approx(0.25)


def test_sector_exposure_dollars_roundtrips_public_weights():
    """공개 비중(sector_weights) → 달러 평가액 — `settle` 의 `max_sector × equity_base` 와 같은 단위."""
    portfolio = {"equity_index": 1.1, "sector_weights": {"Tech": 0.20, "Energy": 0.05, "": 0.0}}
    out = run_daily.sector_exposure_dollars(portfolio)
    assert out["Tech"] == pytest.approx(0.20 * 1_100_000.0)
    assert out["Energy"] == pytest.approx(0.05 * 1_100_000.0)
    assert "" not in out, "섹터 미상(빈 문자열)은 합산 대상이 아니다"
    assert run_daily.sector_exposure_dollars(None) == {}


def test_existing_holdings_count_toward_first_fill_sector_cap(workdir):
    """기존 보유 Tech 25% + 신규 Tech 1차 2% → 27% 초과로 **체결하지 않는다**(배선 전: 체결)."""
    from tests.test_phase4_ledger_and_ui import fill_of, panel_of, record_of

    holdings = []
    executions = {}
    panels = {}
    for i in range(5):                                    # 5% × 5 = 25% (이미 캡)
        ticker = f"OLD{i}"
        record = record_of(sid=f"2026-05-07-{ticker}-BUY-001", ticker=ticker, as_of="2026-05-07",
                           plan_weight=0.05)
        record["target_session"] = "2026-05-08"
        holdings.append(record)
        executions[record["signal_id"]] = fill_of(record, price=100.0, weight=0.05)
        panels[ticker] = panel_of([100.0] * 6, start="2026-05-07", ticker=ticker)
    new = record_of(sid="2026-05-11-NEW-BUY-001", ticker="NEW", plan_weight=0.05)
    new["target_session"] = "2026-05-11"
    panels["NEW"] = panel_of([100.0] * 3, start="2026-05-11", ticker="NEW")
    records = holdings + [new]
    as_of = date.fromisoformat("2026-05-11")
    params = {"risk": {"max_sector": 0.25}}

    probe = run_daily.ledger_view(records, executions, panels, as_of, {}, params,
                                  position_events_path=None)[0]
    exposure = run_daily.sector_exposure_dollars(probe)
    assert exposure["Tech"] == pytest.approx(0.25 * 1_000_000.0, rel=1e-3)

    blocked = live.settle(records, executions, panels, as_of, max_sector=0.25,
                          sector_exposure=exposure)
    assert any(row.get("reason") == "SECTOR_CAP" for row in blocked["settled"]), blocked["settled"]
    assert not [e for e in blocked["new_events"]
                if e["quant_track"]["execution_event"] == "buy_filled"
                and e["signal_id"] == new["signal_id"]], "기존 보유를 빼고 세어 28% 를 체결했다"

    # 배선이 없을 때(기존 동작)는 같은 시나리오가 체결된다 — 회귀 방지용 대조
    naive = live.settle(records, executions, panels, as_of, max_sector=0.25)
    assert any(e["signal_id"] == new["signal_id"]
               for e in naive["new_events"]
               if e["quant_track"]["execution_event"] == "buy_filled")


def test_order_cancellations_are_logged_to_position_events_and_health(workdir, monkeypatch):
    """동결 전 WATCH 로 취소된 1차 주문이 `position_events.jsonl`·`health.json` 에 남는다(P0-3 배선)."""
    from engine.ledger.record import superseding_release
    from tests.test_phase4_ledger_and_ui import record_of

    buy = record_of(sid="2026-11-19-FIXA-BUY-001", ticker="FIXA", as_of="2026-11-19")
    buy.update({"as_of": "2026-11-19", "target_session": "2026-11-20",
                "signal_published_at": "2026-11-19T23:40:00Z",
                "order_cutoff_at": "2026-11-20T14:25:00Z", "sector": "Information Technology"})
    records = [buy]
    watch = superseding_release(records, ticker="FIXA", as_of="2026-11-19", type="WATCH",
                                data_cutoff_at="2026-11-19T22:00:00Z",
                                signal_published_at="2026-11-19T23:50:00Z",
                                order_cutoff_at="2026-11-20T14:25:00Z",
                                target_session="2026-11-20", inputs_hash="hash-watch", grade="WATCH")
    records.append(watch)
    path = Path("data/ledger/records.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")

    assert _run(monkeypatch) == 0
    rows = live.read_position_events(Path("data/ledger/position_events.jsonl"))
    cancels = [row for row in rows if row.get("event") == "cancel_pending"]
    assert cancels and cancels[0]["signal_id"] == buy["signal_id"], rows[:5]
    assert cancels[0]["cancelled_by"] == "WATCH_OR_BLOCKED"
    health = _load("data/health.json")
    assert health["order_cancellations_count"] >= 1
    assert health["order_cancellations"][0]["signal_id"] == buy["signal_id"]

    # 재실행해도 취소 이벤트가 중복되지 않는다(event_id 멱등)
    events_path = Path("data/ledger/position_events.jsonl")
    before = events_path.read_text(encoding="utf-8")
    assert _run(monkeypatch) == 0
    twice = live.read_position_events(events_path)
    assert len([r for r in twice if r.get("event") == "cancel_pending"]) == len(cancels), twice
    assert events_path.read_text(encoding="utf-8").count("cancel_pending") == before.count("cancel_pending")


# ── 7. 감사 10·11·12 · 02(Pages) · §10⑥ 배선 회귀 ─────────────────────────────
def test_sw_activates_a_whole_set_atomically(tmp_path):
    """서비스워커의 세트 전환을 **실제로 돌린다**(node 필요). 하네스는 sw.js 를 그대로 로드한다."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node 가 없다 — sw.js 논리 검증은 node 가 있는 환경에서만 돌린다")
    script = tmp_path / "sw_harness.js"
    script.write_text(_SW_HARNESS, encoding="utf-8")
    proc = subprocess.run([node, str(script), str(ROOT / "web" / "sw.js")],
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"


_SW_HARNESS = r'''
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(process.argv[2], "utf8");

function makeCaches() {
  const store = new Map();
  const entry = (name) => { if (!store.has(name)) store.set(name, new Map()); return store.get(name); };
  const keyOf = (req) => String(req && req.url ? req.url : req);
  return {
    store,
    async open(name) {
      const m = entry(name);
      return {
        async put(req, res) { m.set(keyOf(req), await res.text()); },
        async match(req) {
          const text = m.get(keyOf(req));
          return text === undefined ? undefined : new Response(text, { headers: { "Content-Type": "application/json" } });
        },
        async addAll(urls) { urls.forEach((u) => m.set(new URL(u, "https://user.github.io/app/").href, "shell")); },
      };
    },
    async delete(name) { return store.delete(name); },
    async keys() { return Array.from(store.keys()); },
    async match(req) {
      for (const m of store.values()) { if (m.has(keyOf(req))) return new Response(m.get(keyOf(req))); }
      return undefined;
    },
  };
}

function makeServer() {
  const files = new Map();
  return {
    files,
    set(name, value) { files.set(name, JSON.stringify(value)); },
    fetch: async (input) => {
      const url = new URL(String(input && input.url ? input.url : input), "https://user.github.io/app/");
      const name = url.pathname.split("/data/").pop();
      if (!files.has(name)) return new Response("missing", { status: 404 });
      return new Response(files.get(name), { status: 200 });
    },
  };
}

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, "0")).join("");
}

const CORE = ["meta.json", "signals/today.json", "folds.json", "watchlist.json",
  "ledger/summary.json", "ledger/records.json", "ledger/portfolio.json", "radar/13f.json",
  "config.json", "debate.json", "health.json", "notifications.json"];

async function boot() {
  const listeners = {};
  const caches = makeCaches();
  const server = makeServer();
  const self = {
    location: { origin: "https://user.github.io", href: "https://user.github.io/app/sw.js" },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    async skipWaiting() {},
    clients: { async claim() {} },
  };
  const ctx = {
    self, caches, fetch: server.fetch, Response, Request, Headers, URL, TextEncoder, crypto,
    console, setTimeout, clearTimeout, JSON, Math, Date, Promise, Map, Set, Array, Object, String,
    Number, Boolean, Error, encodeURIComponent, Uint8Array,
  };
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  const post = (data) => new Promise((resolve) => {
    const port = { postMessage: (payload) => resolve(payload) };
    listeners.message[0]({ data, ports: [port], source: null });
  });
  const fetchListener = listeners.fetch[0];
  const fetchData = async (url) => {
    let responded = null;
    fetchListener({ request: new Request(url), respondWith: (p) => { responded = p; } });
    return responded;
  };
  return { caches, server, post, fetchData };
}

async function publish(server, version) {
  const files = {};
  const bodies = {};
  CORE.forEach((name) => {
    const body = { name, bundle_version: (name === "meta.json" || name === "health.json") ? version : undefined };
    const text = JSON.stringify(body);
    bodies[name] = text;
    files[name] = { sha256: "pending", size: text.length };
  });
  server.set("deploy_manifest.json", { schema_version: 1, bundle_version: version, files });
  for (const name of CORE) {
    server.files.set(name, bodies[name]);
    files[name].sha256 = await sha256Hex(bodies[name]);
  }
  server.set("deploy_manifest.json", { schema_version: 1, bundle_version: version, files });
}

(async () => {
  const results = [];
  const check = (name, ok, detail) => results.push({ name, ok, detail });
  const { caches, server, post, fetchData } = await boot();

  await publish(server, "v1");
  const first = await post({ type: "prepare-set" });
  check("v1 activation", first.ok === true && first.bundle_version === "v1", first.error);
  check("v1 core 12 files", first.files && first.files.length === 12, String(first.files && first.files.length));
  check("status v1", (await post({ type: "status" })).bundle_version === "v1");

  // (1) 새 세트의 health.json 이 구 버전 본문으로 오면 → 전환 거부, 직전 세트 유지
  await publish(server, "v2");
  server.set("health.json", { name: "health.json", bundle_version: "v1", stale: true });
  const second = await post({ type: "prepare-set" });
  const st2 = await post({ type: "status" });
  check("stale member blocks activation", second.ok === false && st2.bundle_version === "v1",
        JSON.stringify({ ok: second.ok, active: st2.bundle_version, err: second.error }));
  check("failed set has no cache", (await caches.keys()).indexOf("set-v2") === -1, JSON.stringify(await caches.keys()));
  const still = await (await caches.open("set-v1")).match(new Request("https://user.github.io/app/data/health.json"));
  check("previous set intact", Boolean(still) && (await still.text()).includes("v1"));

  // (2) 정상 v3 → 원자적 전환, 전환 뒤에만 구 세트 삭제
  await publish(server, "v3");
  const third = await post({ type: "prepare-set" });
  const st3 = await post({ type: "status" });
  const names = await caches.keys();
  check("v3 activation", third.ok === true && st3.bundle_version === "v3", third.error);
  check("trim after activation", names.indexOf("set-v1") === -1 && names.indexOf("set-v3") !== -1, JSON.stringify(names));

  // (3) 뒤늦은 구 버전 응답은 새 세트를 덮지 않고, 캐시로 답한다
  server.set("health.json", { name: "health.json", bundle_version: "v1", late: true });
  const resp = await fetchData("https://user.github.io/app/data/health.json");
  const servedText = resp ? await resp.text() : "";
  const cachedNow = await (await caches.open("set-v3")).match(new Request("https://user.github.io/app/data/health.json"));
  const cachedText = cachedNow ? await cachedNow.text() : "";
  check("late old response not stored", cachedText.includes("v3"), cachedText.slice(0, 60));
  check("late old response not served", servedText.includes("v3"), servedText.slice(0, 60));

  // (4) 오프라인 폴백 — 활성 세트 전체
  const read = await post({ type: "read-set" });
  check("read-set fallback", read.ok === true && read.files.length === 12 && read.bundle_version === "v3");

  const failed = results.filter((r) => !r.ok);
  results.forEach((r) => console.log(`${r.ok ? "ok  " : "FAIL"} ${r.name}${r.ok ? "" : " :: " + r.detail}`));
  console.log(`SUMMARY ${results.length - failed.length}/${results.length}`);
  process.exit(failed.length ? 1 : 0);
})().catch((err) => { console.error("HARNESS ERROR", err); process.exit(2); });
'''


def test_run_daily_passes_run_time_and_receipt_requirement(workdir, monkeypatch):
    """감사 02: 편입·정산에 **실제 배치 시각**을 넘긴다(보고서 자기신고만 믿지 않는다)."""
    seen: dict = {}
    original_reconcile = debate_mod.reconcile_debates
    original_settle = live.settle

    # 래퍼는 대상 함수의 시그니처를 **그대로 드러내야** 한다 — run_daily 는 시그니처를 보고 인자를 넘긴다.
    def reconcile_spy(*args, now=None, require_receipt=True, **kwargs):
        seen["reconcile"] = {"now": now, "require_receipt": require_receipt}
        return original_reconcile(*args, now=now, require_receipt=require_receipt, **kwargs)

    def settle_spy(*args, now=None, **kwargs):
        seen.setdefault("settle", []).append({"now": now})
        return original_settle(*args, now=now, **kwargs)

    monkeypatch.setattr(debate_mod, "reconcile_debates", reconcile_spy)
    monkeypatch.setattr(live, "settle", settle_spy)
    assert _run(monkeypatch) == 0
    assert isinstance(seen["reconcile"].get("now"), datetime), seen["reconcile"]
    assert seen["reconcile"].get("require_receipt") is True
    assert all(isinstance(kw.get("now"), datetime) for kw in seen["settle"]), seen["settle"]


def test_debate_index_is_keyed_by_signal_and_snapshot():
    """감사 07: 같은 종목의 **다른 스냅샷** 토론이 오늘 판단에 붙지 않는다."""
    records = [{"signal_id": "S1", "snapshot_id": "sha256:old"},
               {"signal_id": "S2", "snapshot_id": "sha256:new"}]
    by_signal = {"S1": {"eligible": True, "ticker": "AAA", "snapshot_id": "sha256:old"},
                 "S2": {"eligible": True, "ticker": "AAA", "snapshot_id": "sha256:new"},
                 "S3": {"eligible": False, "ticker": "BBB"}}
    index = run_daily.debate_index(by_signal, records)
    assert index[("S1", "sha256:old")]["snapshot_id"] == "sha256:old"
    assert index[("S2", "sha256:new")]["snapshot_id"] == "sha256:new"
    assert ("S1", "sha256:new") not in index, "스냅샷이 다르면 같은 신호가 아니다"
    assert ("S3", None) not in index, "자격 미달 토론은 판단에 붙지 않는다"
    # 통합 전 호출부(ticker 키)도 깨지지 않는다
    assert index.get("AAA") is not None


def test_forced_exits_includes_trim_only_when_ledger_supports_it(monkeypatch):
    checks = [{"ticker": "AAA", "verdict": "TRIM", "triggers": {"S4": True}},
              {"ticker": "BBB", "verdict": "EXIT", "triggers": {"S3": True},
               "cancel_pending_tranches": True},
              {"ticker": "CCC", "verdict": "HOLD", "triggers": {}}]
    assert run_daily._forced_exits(checks)["AAA"] == "TRIM"
    assert run_daily._forced_exits(checks)["BBB"] == "S3"
    assert "CCC" not in run_daily._forced_exits(checks)
    monkeypatch.setattr(live, "FORCED_EXIT_REASONS", ("S3", "S5", "EXIT"), raising=False)
    assert "AAA" not in run_daily._forced_exits(checks)


def _seed(records: list[dict]) -> None:
    path = Path("data/ledger/records.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")


def _records() -> list[dict]:
    return [json.loads(line) for line in
            Path("data/ledger/records.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def test_blocked_signal_supersedes_the_live_buy_in_the_same_run(workdir, monkeypatch):
    """감사 01-d: 오전 BUY 가 같은 날 차단되면 원장에서 **대체**되고 주문이 취소된다.

    시나리오(2026-11-20 postclose, 대상 세션 2026-11-23)가 달력 날짜로 고정돼 있으므로,
    실행기 시계를 그 배치 시각으로 묶는다 — 실제 날짜가 주문 마감(2026-11-23T14:20Z)을 지나면
    차단 재발행이 "동결 뒤 도착"으로 판정돼 취소 대신 `FROZEN_ORDER_STANDS` 만 남는다.
    """
    from tests.helpers import freeze_runner_clock
    from tests.test_phase4_ledger_and_ui import record_of

    freeze_runner_clock(monkeypatch, "2026-11-20T22:10:00Z")

    buy = record_of(sid="2026-11-20-FIXC-BUY-001", ticker="FIXC", as_of=AS_OF)
    buy.update({"as_of": AS_OF, "target_session": "2026-11-23",
                "signal_published_at": "2026-11-20T05:00:00Z",
                "order_cutoff_at": "2026-11-23T14:25:00Z", "sector": "Energy"})
    _seed([buy])

    assert _run(monkeypatch) == 0
    blocked = [r for r in _records() if r.get("ticker") == "FIXC" and r.get("blocked")]
    assert blocked, "차단이 원장 레코드를 남기지 않았다"
    assert blocked[0]["supersedes"] == "2026-11-20-FIXC-BUY-001"
    assert blocked[0]["quant_intent"] == "no_action"
    health = _load("data/health.json")
    cancels = health.get("order_cancellations") or []
    assert any(c["signal_id"] == "2026-11-20-FIXC-BUY-001" for c in cancels), cancels


def test_revision_chain_cancellations_are_recorded_in_the_same_run(workdir, monkeypatch):
    """감사 §10②: `-001→-002→-003` 대체의 취소가 **그 실행에서** 기록된다(다음 날 back-date 없음)."""
    from tests.test_phase4_ledger_and_ui import record_of

    first = record_of(sid="2026-11-20-FIXA-BUY-001", ticker="FIXA", as_of=AS_OF)
    first.update({"as_of": AS_OF, "target_session": AS_OF, "inputs_hash": "hash-a",
                  "signal_published_at": "2026-11-20T04:00:00Z",
                  "order_cutoff_at": future_cutoff(), "sector": "Information Technology"})
    second = copy.deepcopy(first)
    second.update({"signal_id": "2026-11-20-FIXA-BUY-002", "revision": 2, "inputs_hash": "hash-b",
                   "signal_published_at": "2026-11-20T04:30:00Z",
                   "supersedes": "2026-11-20-FIXA-BUY-001"})
    _seed([first, second])

    assert _run(monkeypatch) == 0
    records = _records()
    newest = [r for r in records if str(r.get("signal_id", "")).startswith("2026-11-20-FIXA-BUY")]
    assert newest[-1]["revision"] == 3 and newest[-1]["supersedes"] == "2026-11-20-FIXA-BUY-002"
    cancels = _load("data/health.json").get("order_cancellations") or []
    assert any(c["signal_id"] == "2026-11-20-FIXA-BUY-002"
               and c["superseded_by"] == "2026-11-20-FIXA-BUY-003" for c in cancels), cancels
    events = live.read_position_events(Path("data/ledger/position_events.jsonl"))
    assert any(row.get("signal_id") == "2026-11-20-FIXA-BUY-002"
               and row.get("event") == "cancel_pending" for row in events), events


def test_position_event_conflict_does_not_kill_the_run(workdir, monkeypatch):
    """감사 14#4: append-only 충돌(같은 event_id·다른 내용)이 배치를 끝내면 안 된다."""
    from tests.test_phase4_ledger_and_ui import record_of
    from engine.ledger.record import superseding_release

    buy = record_of(sid="2026-11-19-FIXA-BUY-001", ticker="FIXA", as_of="2026-11-19")
    buy.update({"as_of": "2026-11-19", "target_session": "2026-11-20",
                "signal_published_at": "2026-11-19T23:40:00Z",
                "order_cutoff_at": "2026-11-20T14:25:00Z", "sector": "Information Technology"})
    watch = superseding_release([buy], ticker="FIXA", as_of="2026-11-19", type="WATCH",
                                data_cutoff_at="2026-11-19T22:00:00Z",
                                signal_published_at="2026-11-19T23:50:00Z",
                                order_cutoff_at="2026-11-20T14:25:00Z",
                                target_session="2026-11-20", inputs_hash="hash-watch", grade="WATCH")
    _seed([buy, watch])

    def boom(*args, **kwargs):  # noqa: ARG001
        raise ValueError("event_id 충돌")

    monkeypatch.setattr(live, "append_position_events", boom)
    assert _run(monkeypatch) == 0
    payload = _load("data/quant_signals.json")
    assert payload["diagnostics"].get("position_events_error")
