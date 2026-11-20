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

import json
import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

from engine.ledger import debate as debate_mod
from engine.ledger import live
from scripts import build_public_data, notify, run_daily

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
                "debate_risk_gate_applied", "confidence_cap", "content_hash", "normalizer_version"):
        assert key in rules["allow"], f"공개 허용 목록에 '{key}' 가 없다"
    out = build_public_data.build(_signals_for_build(), {}, [], rules,
                                  health={"data_status": "ok", "last_success_at": "2026-11-20T05:57:49Z"})
    assert out["meta.json"]["bundle_version"] == "sha256:abc123|sha256:def456"
    assert out["meta.json"]["generated_at"] == "2026-11-20T05:57:49Z"
    assert out["health.json"]["build_at"]


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


# ── 5. PWA 서비스워커·화면 배지 (P2-1) ───────────────────────────────────────
def test_service_worker_caches_current_bundle_set():
    sw = (ROOT / "web" / "sw.js").read_text(encoding="utf-8")
    for needle in ("SW_VERSION", "SET_PREFIX", "bundle_version", "set-bundle-version",
                   "/data/", "offline"):
        assert needle in sw, f"sw.js 에 '{needle}' 가 없다"
    assert "phase4-2" in sw
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "sw.js" in js and "serviceWorker" in js
    assert "bundle_version" in js


def test_ui_surfaces_data_status_and_bundle_mismatch():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    for element in ("data-status-banner", "bundle-banner", "policy-effect"):
        assert f'id="{element}"' in html, f"index.html 에 '{element}' 자리가 없다"
        assert f'$("{element}")' in js, f"app.js 가 '{element}' 를 그리지 않는다"
    for needle in ("이번 세션 판단 보류", "last_attempt_at", "last_success_at", "data_status",
                   "이전/혼합 세트", "policy_diff_pp", "pair_summary", "계좌 정책 순효과"):
        assert needle in js, f"app.js 에 '{needle}' 표시가 없다"
    assert "app.js?v=phase4-2" in html and "styles.css?v=phase4-2" in html
    manifest = json.loads((ROOT / "web" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["start_url"] and manifest["display"] == "standalone"


def test_debate_pr_workflow_template_checks_whole_pr_and_guards_paths():
    """P1-2: PR 전체 경로 검사 + 리포트 PR 경로 가드(템플릿·웹 등록본)."""
    text = (ROOT / "codex_debate" / "workflows" / "debate-validate.yml").read_text(encoding="utf-8")
    assert "git diff --name-only" in text
    assert "check_commit_scope.py --paths" in text
    assert "data/debate_reports.json" in text and "data/debate/manifest.json" in text
    assert ".github/workflows/" in text and "config/params" in text
    assert "fetch-depth: 0" in text
    # 등록본(.github/workflows/debate-validate.yml)은 CLI 토큰으로 push 할 수 없어(workflows 권한 없음)
    # GitHub 웹에서 등록한다(docs/codex_debate_guide.md §10). 등록본은 템플릿과 **같은 내용**이어야 한다 —
    # 한쪽만 고치면 실제로 도는 검사와 템플릿이 어긋난다(드리프트 방지).
    registered = ROOT / ".github" / "workflows" / "debate-validate.yml"
    assert registered.exists(), "웹 등록본 .github/workflows/debate-validate.yml 이 없다"
    assert registered.read_text(encoding="utf-8") == text


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
