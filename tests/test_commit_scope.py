"""test_commit_scope — 커밋 범위 가드 (기획서 2.2.3 Phase 3 DoD).

`redact_quotes.py` 가 인용 원문을 `quote_sha256` 으로 바꾸고, `check_commit_scope.py` 가
원문·`.env`·원시 시계열·개인정보가 **커밋 전에** 걸러 내는지 확인한다.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts import check_commit_scope, redact_quotes

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / "config" / "public_allowlist.yaml"


def _rules() -> dict:
    return check_commit_scope.load_rules(ALLOWLIST)


def test_quote_original_blocks_commit(tmp_path):
    path = tmp_path / "quant_signals.json"
    path.write_text(json.dumps({"signals": [{"ticker": "MSFT",
                                             "quote": "실적이 예상을 크게 웃돌았다는 CEO 발언"}]},
                               ensure_ascii=False), encoding="utf-8")
    problems = check_commit_scope.check_paths([str(path)], _rules())
    assert any("forbidden keys" in p and "quote" in p for p in problems), problems


def test_redacted_file_passes(tmp_path):
    path = tmp_path / "quant_signals.json"
    path.write_text(json.dumps({"signals": [{"ticker": "MSFT",
                                             "quote": "실적이 예상을 크게 웃돌았다는 CEO 발언"}]},
                               ensure_ascii=False), encoding="utf-8")
    redact_quotes.redact_file(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    evidence = payload["signals"][0]
    assert "quote" not in evidence
    assert evidence["quote_sha256"] == redact_quotes.quote_digest("실적이 예상을 크게 웃돌았다는 CEO 발언")
    assert evidence["quote_verified"] is True
    assert check_commit_scope.check_paths([str(path)], _rules()) == []


def test_body_excerpt_is_hashed_not_committed(tmp_path):
    path = tmp_path / "records.jsonl"
    path.write_text(json.dumps({"signal_id": "s1", "body_excerpt_1500": "본문 발췌 " * 40},
                               ensure_ascii=False) + "\n", encoding="utf-8")
    assert check_commit_scope.check_paths([str(path)], _rules())
    redact_quotes.redact_file(path)
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert "body_excerpt_1500" not in row and row["body_present"] is True
    assert check_commit_scope.check_paths([str(path)], _rules()) == []


def test_raw_price_series_blocks_commit(tmp_path):
    path = tmp_path / "records.json"
    path.write_text(json.dumps({"records": [{"ticker": "MSFT", "raw_close": 412.35,
                                             "raw_open": 410.0}]}, ensure_ascii=False), encoding="utf-8")
    problems = check_commit_scope.check_paths([str(path)], _rules())
    assert any("raw_close" in p for p in problems)


def test_xbrl_unit_named_shares_is_not_a_position(tmp_path):
    """SEC companyfacts 의 `facts.<tax>.<concept>.units.<unit>` 는 **단위 이름**이라 막지 않고,
    같은 `shares` 라도 계좌 안에 있거나 임의 JSON 의 `units` 에 있으면 계속 막는다(감사 10)."""
    fixture = tmp_path / "companyfacts.json"
    fixture.write_text(json.dumps({"facts": {"us-gaap": {
        "WeightedAverageNumberOfDilutedSharesOutstanding": {
            "units": {"shares": [{"end": "2025-12-31", "val": 12_230_000_000,
                                   "form": "10-K", "fp": "FY", "filed": "2026-02-05",
                                   "accn": "0001652044-26-000018"}]}}}}}
        , ensure_ascii=False), encoding="utf-8")
    assert check_commit_scope.check_paths([str(fixture)], _rules()) == []

    account = tmp_path / "portfolio2.json"
    account.write_text(json.dumps({"units": {"USD": 1}, "position": {"ticker": "MSFT", "shares": 3.2}}
                                  , ensure_ascii=False), encoding="utf-8")
    problems = check_commit_scope.check_paths([str(account)], _rules())
    assert any("shares" in p for p in problems), problems


def test_units_key_outside_xbrl_contract_is_not_exempt(tmp_path):
    """임의 JSON 의 `units` 는 예외가 아니다 — XBRL·메트릭 단위 계약 구조에만 준다(감사 10)."""
    arbitrary = tmp_path / "payload.json"
    arbitrary.write_text(json.dumps({"units": {"shares": [100, 200]}, "note": "임의 데이터"}),
                         encoding="utf-8")
    problems = check_commit_scope.check_paths([str(arbitrary)], _rules())
    assert any("shares" in p for p in problems), problems

    nested = tmp_path / "nested.json"
    nested.write_text(json.dumps({"wrapper": {"units": {"shares": [1]}}}, ensure_ascii=False),
                      encoding="utf-8")
    assert any("shares" in p for p in check_commit_scope.check_paths([str(nested)], _rules()))

    metric = tmp_path / "metrics.json"
    metric.write_text(json.dumps({"metrics": {"price": {"units": {"USD": [1]}}}}, ensure_ascii=False),
                      encoding="utf-8")
    assert check_commit_scope.check_paths([str(metric)], _rules()) == []


def test_holdings_fields_block_commit(tmp_path):
    path = tmp_path / "portfolio.json"
    path.write_text(json.dumps({"positions": [{"ticker": "MSFT", "shares": 3.2,
                                               "avg_cost": 388.4, "cash_balance": 1200.0}]},
                               ensure_ascii=False), encoding="utf-8")
    problems = check_commit_scope.check_paths([str(path)], _rules())
    assert any("shares" in p for p in problems)
    assert any("avg_cost" in p for p in problems)


def test_env_and_auth_json_are_rejected(tmp_path):
    env = tmp_path / ".env"
    env.write_text("FRED_API_KEY=abcd1234\n", encoding="utf-8")
    auth = tmp_path / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    problems = check_commit_scope.check_paths([str(env), str(auth)], _rules())
    assert any(".env" in p for p in problems)
    assert any("auth.json" in p for p in problems)


# ── 감사 10: 커밋될 **바이트**(인덱스/커밋 blob)를 본다 ──────────────────────
def _git_repo(tmp_path, monkeypatch) -> Path:
    """가드가 읽는 `config/public_allowlist.yaml` 만 가진 임시 git 저장소."""
    import shutil

    (tmp_path / "config").mkdir()
    shutil.copy(ROOT / "config" / "public_allowlist.yaml", tmp_path / "config" / "public_allowlist.yaml")

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_staged_mode_reads_the_index_blob_not_the_worktree(tmp_path, monkeypatch):
    """인덱스에 원문 인용이 있는데 워킹트리에서 지우면 **예전에는 통과**했다(감사 10)."""
    repo = _git_repo(tmp_path, monkeypatch)
    (repo / "data.json").write_text(json.dumps({"quote": "원문 인용"}, ensure_ascii=False), encoding="utf-8")
    subprocess.run(["git", "add", "data.json"], cwd=repo, check=True, capture_output=True)
    (repo / "data.json").write_text(json.dumps({"clean": True}), encoding="utf-8")   # 커밋 전에 고쳐 씀
    assert check_commit_scope.main(["--staged"]) == 1

    # 인덱스도 고치면 통과한다(같은 바이트를 검사한다는 증거)
    subprocess.run(["git", "add", "data.json"], cwd=repo, check=True, capture_output=True)
    assert check_commit_scope.main(["--staged"]) == 0


def test_commit_mode_reads_the_commit_blob(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, monkeypatch)
    (repo / "data.json").write_text(json.dumps({"quote": "원문 인용"}, ensure_ascii=False), encoding="utf-8")
    subprocess.run(["git", "add", "data.json"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "leak"], cwd=repo, check=True, capture_output=True)
    (repo / "data.json").write_text(json.dumps({"clean": True}), encoding="utf-8")   # 커밋 뒤 작업트리 수정
    assert check_commit_scope.main(["--commit", "HEAD"]) == 1


def test_non_utf8_files_are_not_skipped(tmp_path):
    """감사 10: UTF-8 실패를 `continue` 로 건너뛰면 토큰이 든 바이너리가 통과했다."""
    token = "ghp_" + "A" * 24
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"\xff\xfe{\"token\": \"" + token.encode() + b"\"}\n")
    problems = check_commit_scope.check_paths([str(bad)], _rules())
    assert any("non-utf8" in p for p in problems), problems

    # 허용된 바이너리(아이콘)는 내용 검사 대상이 아니다
    icon = tmp_path / "icon.svg"
    icon.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01\x02")
    assert check_commit_scope.check_paths([str(icon)], _rules()) == []


def test_debate_manifest_is_allowed_but_other_debate_files_are_blocked():
    """감사 10: `public_allowlist.yaml` 의 `data/debate/` 차단 과 `manifest.json` 허용의 모순 정리."""
    rules = _rules()
    assert "data/debate/manifest.json" in rules.get("allow_paths", [])
    assert check_commit_scope.check_paths(["data/debate/manifest.json"], rules) == []
    for blocked in ("data/debate/context.json", "data/debate/context/AAA.md", "data/debate/raw.json"):
        problems = check_commit_scope.check_paths([blocked], rules)
        assert any("forbidden path" in p for p in problems), (blocked, problems)


# ── 감사 10: 새 산출물 필드 계약 ────────────────────────────────────────
def test_new_artifact_field_contract_is_enforced(tmp_path):
    (tmp_path / "data").mkdir()
    health = tmp_path / "data" / "health.json"
    health.write_text(json.dumps({"schema_version": 1, "as_of": "2026-11-20", "data_status": "ok"}),
                      encoding="utf-8")
    assert check_commit_scope.check_paths([str(health)], _rules(), root=tmp_path) == []

    # 필요한 필드가 없으면 실패(실행 상태부가 빠진 health 는 화면이 읽을 수 없다)
    health.write_text(json.dumps({"schema_version": 1, "as_of": "2026-11-20"}), encoding="utf-8")
    assert any("필수 필드" in p for p in check_commit_scope.check_paths([str(health)], _rules(), root=tmp_path))

    # 계약에 없는 필드도 실패(허용 목록을 의도적으로 갱신하게 만든다)
    health.write_text(json.dumps({"schema_version": 1, "as_of": "2026-11-20",
                                  "data_status": "ok", "unexpected_series": [1, 2]}, ensure_ascii=False),
                      encoding="utf-8")
    assert any("계약에 없는 필드" in p for p in check_commit_scope.check_paths([str(health)], _rules(), root=tmp_path))


def test_new_artifacts_escape_hatches_are_closed(tmp_path):
    events = tmp_path / "data" / "ledger"
    events.mkdir(parents=True)
    path = events / "position_events.jsonl"
    path.write_text(json.dumps({"event": "cancel_pending", "signal_id": "s-1", "date": "2026-11-20",
                                "event_id": "e1", "ticker": "AAA", "cancelled_by": "WATCH_OR_BLOCKED"},
                               ensure_ascii=False) + "\n", encoding="utf-8")
    assert check_commit_scope.check_paths([str(path)], _rules(), root=tmp_path) == []

    # 원문 인용·수량은 새 산출물에서도 막힌다
    path.write_text(json.dumps({"event": "cancel_pending", "signal_id": "s-1", "date": "2026-11-20",
                                "event_id": "e1", "quote": "원문", "shares": 3.2}, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    problems = check_commit_scope.check_paths([str(path)], _rules(), root=tmp_path)
    assert any("forbidden keys" in p for p in problems), problems
    assert any("계약에 없는 필드" in p for p in problems), problems


def test_deploy_manifest_hashes_must_match_files(tmp_path):
    import hashlib

    data = tmp_path / "web" / "data"
    (data / "signals").mkdir(parents=True)
    (data / "ledger").mkdir(parents=True)
    (data / "radar").mkdir(parents=True)
    names = {"meta.json": {"bundle_version": "v1"},
             "signals/today.json": {"signals": []},
             "health.json": {"data_status": "ok"},
             "debate.json": {"reports": []},
             "notifications.json": {"new": []},
             "ledger/summary.json": {}, "ledger/records.json": {}, "ledger/portfolio.json": {},
             "radar/13f.json": {}, "config.json": {}}
    files = {}
    for name, payload in names.items():
        path = data / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        files[name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size}
    manifest = {"schema_version": 1, "bundle_version": "v1", "files": files}
    (data / "deploy_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert check_commit_scope.check_paths(["web/data/deploy_manifest.json"], _rules(), root=tmp_path) == []

    (data / "health.json").write_text(json.dumps({"data_status": "outage"}), encoding="utf-8")
    problems = check_commit_scope.check_paths(["web/data/deploy_manifest.json"], _rules(), root=tmp_path)
    assert any("sha256 불일치" in p for p in problems), problems


def test_repository_artifacts_pass_the_guard():
    """저장소에 실제로 커밋되는 새 산출물이 가드를 통과한다."""
    paths = ["data/health.json", "data/notifications_latest.json", "data/notifications_log.json",
             "data/ledger/position_events.jsonl", "web/data/deploy_manifest.json"]
    existing = [p for p in paths if (ROOT / p).exists()]
    if not existing:
        return
    problems = check_commit_scope.check_paths(existing, _rules(), root=ROOT)
    assert problems == [], problems


def test_forbidden_paths_rejected():
    problems = check_commit_scope.check_paths(
        ["data/raw/prices.parquet", "data/debate/context.json", "data/cache/yfinance/x.csv"], _rules())
    assert len(problems) == 3


def test_email_pattern_is_rejected(tmp_path):
    # 실제 주소를 파일에 박지 않는다 — 가드가 이 테스트 파일 자체를 막기 때문이다.
    address = "someone" + "@" + "gmail" + ".com"
    path = tmp_path / "config.yaml"
    path.write_text(f"contact: {address}\n", encoding="utf-8")
    problems = check_commit_scope.check_paths([str(path)], _rules())
    assert any("forbidden pattern" in p for p in problems)


def test_env_example_placeholder_is_allowed(tmp_path):
    path = tmp_path / "env_example.yaml"
    path.write_text("SEC_USER_AGENT: app/0.1 (contact: you@example.com)\n", encoding="utf-8")
    assert check_commit_scope.check_paths([str(path)], _rules()) == []


def test_repository_has_no_tracked_secret_file():
    """이 저장소에 `.env`·`auth.json` 이 추적된 적이 없어야 한다(2.2.3)."""
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return
    tracked = out.stdout.splitlines()
    assert not [p for p in tracked if Path(p).name in (".env", "auth.json")]


def test_committed_contract_files_pass_the_guard():
    """실제로 커밋되는 계약 파일(있으면)이 가드를 통과한다."""
    paths = ["data/quant_signals.json", "data/ledger/records.jsonl", "data/params_history.json",
             "data/ledger/pilot_eval.json", "data/ledger/portfolio/state.json",
             "data/radar/13f.json", "data/watchlist/current.json",
             "data/walkforward/folds/summary.json", "data/samples/quant_signals.example.json"]
    existing = [p for p in paths if (ROOT / p).exists()]
    assert existing, "계약 파일이 하나도 없다"
    assert check_commit_scope.check_paths(existing, _rules()) == []


def test_quote_digest_is_normalized_and_stable():
    a = redact_quotes.quote_digest("  같은   문장입니다  ")
    b = redact_quotes.quote_digest("같은 문장입니다")
    assert a == b
    assert len(a) == 64


def test_schema_file_declaration_is_structure_not_data(tmp_path):
    """`*.schema.json` 의 "`properties.quote`" 는 **필드 선언**이다 — 단, 승인된 스키마 경로에서만.

    감사 10: 이름만 `*.schema.json` 으로 바꿔 데이터를 통과시키는 우회를 막는다 — 예외는
    `schemas/`·`codex_debate/schemas/` 아래의 **유효한 JSON Schema** 에서만 인정한다.
    """
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    schema = schemas / "round1.schema.json"
    schema.write_text(json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema",
                                  "properties": {"news_read": {"items": {
                                      "properties": {"quote": {"type": "string"}}}}}},
                                 ensure_ascii=False), encoding="utf-8")
    assert check_commit_scope.check_paths([str(schema)], _rules()) == []

    # 승인되지 않은 경로의 같은 이름은 **데이터**다 — 파일 이름만으로 예외를 주지 않는다
    outside = tmp_path / "payload.schema.json"
    outside.write_text(json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema",
                                   "properties": {"quote": "실적이 예상을 크게 웃돌았다"}},
                                  ensure_ascii=False), encoding="utf-8")
    assert any("forbidden keys" in p for p in check_commit_scope.check_paths([str(outside)], _rules()))

    # 같은 이름이 아니라 확장자가 .schema.json 이 아닌 데이터 파일은 그대로 차단된다
    data = tmp_path / "round1.json"
    data.write_text(json.dumps({"properties": {"quote": "실적이 예상을 크게 웃돌았다"}},
                               ensure_ascii=False), encoding="utf-8")
    assert check_commit_scope.check_paths([str(data)], _rules())

    # 스키마 파일이라도 값 안의 이메일 패턴은 계속 걸린다
    leaky = schemas / "leaky.schema.json"
    leaky.write_text(json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema",
                                 "examples": ["someone@" + "example.net"]}, ensure_ascii=False),
                     encoding="utf-8")
    assert check_commit_scope.check_paths([str(leaky)], _rules())


def test_schema_examples_and_defaults_are_data_not_declarations(tmp_path):
    """감사 10: `examples`/`default`/`description` 의 **값**은 일반 데이터처럼 검사한다."""
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    schema = schemas / "trap.schema.json"
    schema.write_text(json.dumps({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"signal": {"type": "object",
                                  "default": {"quote": "원문 인용", "shares": 3.2}}},
        "examples": [{"evidence": {"quote": "원문 인용"}}],
    }, ensure_ascii=False), encoding="utf-8")
    problems = check_commit_scope.check_paths([str(schema)], _rules())
    assert any("forbidden keys" in p for p in problems), problems


def test_schema_name_without_schema_structure_is_data(tmp_path):
    """이름만 스키마인 파일(유효한 JSON Schema 구조가 아님)은 예외가 없다."""
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    fake = schemas / "data.schema.json"
    fake.write_text(json.dumps({"items": [{"quote": "원문 인용"}]}, ensure_ascii=False), encoding="utf-8")
    assert any("forbidden keys" in p for p in check_commit_scope.check_paths([str(fake)], _rules()))



def test_health_contract_accepts_the_engine_health_shape(tmp_path):
    """원장이 실제로 쓰는 `data/health.json` 모양이 계약을 통과해야 한다(통합 회귀 방지).

    엔진(`live.write_health`, `settle_positions`)이 건강 지표·취소 상세 필드를 늘릴 때 이 계약이
    그것을 막아 CI 가 거짓 실패하지 않도록, 여기서 한 번에 고정한다.
    """
    (tmp_path / "data").mkdir()
    health = {
        "schema_version": 1, "as_of": "2026-11-20", "status": "normal", "data_status": "ok",
        "generated_at": "2026-11-20T05:57:49Z", "degraded": [],
        "positions_open": 1, "tranches_filled": 1, "tranches_pending": 2, "stop_count": 0,
        "trim_count": 1, "stop_reentry_blocked_count": 1, "tranche_cancel_count": 2,
        "pending_exit_count": 0,
        "blocked_reentries": [{"signal_id": "s", "ticker": "AAA", "target_session": "2026-11-20",
                               "reason": "S5_STOP_SAME_SESSION"}],
        "cancelled_tranches": [{"signal_id": "s", "ticker": "AAA", "date": "2026-11-20", "tranche": 2,
                                "cancelled_by": "S5", "reason": "S5"}],
        "note_ko": "설명", "last_run_at": "2026-11-20T05:57:49Z",
        "last_attempt_at": "2026-11-20T05:57:49Z", "last_success_at": "2026-11-20T05:57:49Z",
        "data_cutoff_at": "2026-11-19T22:00:00Z", "target_session": "2026-11-20",
        "outage_id": None, "reason": None,
        "order_cancellations_count": 1,
        "order_cancellations": [{"signal_id": "s", "ticker": "AAA", "target_session": "2026-11-20",
                                 "cancelled_by": "REVISION_REPLACED", "cancel_kind": "WATCH",
                                 "superseded_by": "s2", "at": "2026-11-20T05:00:00Z",
                                 "frozen_at": "2026-11-20T05:00:00Z",
                                 "freeze_deadline_at": "2026-11-20T14:20:00Z"}],
        "forced_exits": {"AAA": "TRIM"}, "cash_pct": 0.8,
    }
    path = tmp_path / "data" / "health.json"
    path.write_text(json.dumps(health, ensure_ascii=False), encoding="utf-8")
    assert check_commit_scope.check_paths([str(path)], _rules(), root=tmp_path) == []
