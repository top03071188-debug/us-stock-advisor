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
