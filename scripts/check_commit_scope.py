#!/usr/bin/env python3
"""scripts/check_commit_scope.py — 커밋·배포 범위 가드 (2.2.3, 2.2.4).

검사 대상은 **커밋될 바로 그 바이트**다(감사 10).

    --staged        Git 인덱스 blob(`git show :{path}`) — 워킹트리를 읽지 않는다.
    --commit REV    그 커밋이 담은 blob(`git show REV:{path}`) — PR·푸시 검사용.
    --paths ...     지정한 파일(테스트·수동 점검용, 워킹트리 — `--root` 기준).

검사 항목
1) 금지 경로(`data/raw/`, `data/debate/`(매니페스트 제외), `.env`, `local/` …)
2) 금지 키(`quote`, `body_excerpt_1500`, `avg_cost`, `holdings` …) — JSON 키 기준
   - `*.schema.json` 예외는 **승인된 스키마 경로**(`schemas/`, `codex_debate/schemas/`)의
     **유효한 JSON Schema** 에서 `properties`/`required`/`$defs` 아래 **이름**에만 준다.
     `examples`·`default`·`description` 등 값은 일반 데이터처럼 검사한다.
   - `units` 예외는 XBRL companyfacts(`facts/<taxonomy>/<concept>/units`)·메트릭 단위
     계약(`metrics|measures/<name>/units`) 구조에만 준다. 임의 JSON 의 `units` 는 예외가 아니다.
3) 금지 패턴(이메일·GitHub 토큰) — 값 안에서 탐지
4) **UTF-8 로 읽을 수 없는 파일**은 조용히 건너뛰지 않는다 — 허용된 이미지·폰트 확장자
   (`.png`, `.svg`, `.ico` …)가 아니면 `ERROR non-utf8` 로 실패한다.
5) `.env` 추적 여부
6) **새 산출물 필드 계약**(`position_events.jsonl`, `debate_events/*.json`, `health.json`,
   `notifications_log.json`, `notifications_latest.json`, `web/data/**`) — 필수 필드·허용 키·
   `deploy_manifest.json` 해시 일치.

하나라도 걸리면 exit 1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ALLOWLIST = Path("config/public_allowlist.yaml")
TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".yaml", ".yml", ".py", ".ts", ".tsx", ".js", ".txt", ".html", ".css"}
#: 내용을 텍스트로 검사하지 않아도 되는 **허용된 바이너리**. 목록 밖의 바이너리는 실패다.
ALLOWED_BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".svg",
                           ".woff", ".woff2", ".ttf", ".otf", ".pdf", ".mp3", ".mp4"}
#: JSON Schema 예외를 인정하는 경로 접두사(감사 10 — 파일 이름만으로는 예외가 아니다).
SCHEMA_PREFIXES = ("schemas/", "codex_debate/schemas/")
#: 스키마 안에서 자식 키가 **필드 선언 이름**인 컨테이너.
SCHEMA_CONTAINERS = {"properties", "required", "$defs", "definitions", "patternProperties",
                     "dependentSchemas", "dependentRequired"}
#: 스키마 안에서도 **값이 데이터**인 키 — 예외를 주지 않는다.
SCHEMA_DATA_KEYWORDS = {"examples", "example", "default", "const", "enum"}
#: XBRL companyfacts 의 단위 컨테이너가 놓이는 루트 키.
XBRL_UNIT_ROOTS = {"facts"}
#: 메트릭 단위 계약의 루트 키(예: `metrics.<name>.units`).
METRIC_UNIT_ROOTS = {"metrics", "measures"}
#: 단위 이름으로 허용하는 모양(XBRL unit id: `shares`, `USD`, `USD/shares`, `pure` …).
UNIT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]*(?:/[A-Za-z][A-Za-z0-9_.\-]*)?$")


def load_rules(path: Path = ALLOWLIST) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def staged_paths() -> list[str]:
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
                         capture_output=True, text=True, check=True).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def commit_paths(rev: str) -> list[str]:
    """커밋이 바꾼 경로 — 병합 커밋도 첫 부모와 비교해 목록을 얻는다."""
    out = subprocess.run(["git", "show", "--pretty=format:", "--name-only", "--diff-filter=ACMR", rev],
                         capture_output=True, text=True, check=True).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def tracked_env() -> list[str]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=False).stdout
    return [line for line in out.splitlines() if Path(line).name in (".env", "auth.json")]


def _git_blob(ref: str) -> bytes | None:
    """`git show <ref>` 의 **바이트**. 없는 경로(삭제 등)면 None."""
    proc = subprocess.run(["git", "show", ref], capture_output=True, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout


def _norm(path: str | Path, root: str | Path | None = None) -> str:
    """저장소 상대 경로로 정규화 — deny_paths 매칭·스키마 경로 판정의 기준."""
    text = str(path).replace("\\", "/")
    if root is not None:
        try:
            text = str(Path(text).resolve().relative_to(Path(root).resolve())).replace("\\", "/")
        except (ValueError, OSError):
            pass
    while text.startswith("./"):
        text = text[2:]
    return text


def _read_worktree(path: str | Path, root: str | Path | None = None) -> bytes | None:
    target = Path(path)
    if root is not None and not target.is_absolute():
        target = Path(root) / target
    if not target.exists() or not target.is_file():
        return None
    return target.read_bytes()


# ────────────────────────────── 키 판정 ──────────────────────────────
def _iter_keys(obj, chain: tuple[str, ...] = ()):
    """JSON 의 모든 키와 **조상 키 사슬**을 돌려준다.

    부모가 필요한 이유: SEC companyfacts 의 `units: {"shares": [...]}` 는 XBRL **단위 이름**이지
    보유 주식수가 아니다. 반면 `position: {"shares": 3.2}` 는 계좌 정보다 — 둘을 같은 키 하나로
    판정하면 공개 가능한 픽스처를 커밋할 수 없고, 그렇다고 `shares` 를 통째로 빼면 계좌가 새어 나간다.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, chain
            yield from _iter_keys(v, chain + (k,))
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_keys(v, chain)


def _looks_like_schema(data) -> bool:
    """유효한 JSON Schema 구조인가 — 이름만 `*.schema.json` 인 데이터 파일과 구분한다."""
    if not isinstance(data, dict):
        return False
    if "$schema" in data:
        return True
    has_body = any(key in data for key in ("properties", "$defs", "definitions", "required", "items"))
    return has_body and any(key in data for key in ("type", "$defs", "definitions", "properties"))


def _is_schema_scope(norm: str, data) -> bool:
    """승인된 경로의 **유효한** 스키마일 때만 True."""
    if not norm.endswith(".schema.json"):
        return False
    if not any(f"/{prefix}" in f"/{norm}" for prefix in SCHEMA_PREFIXES):
        return False
    return _looks_like_schema(data)


def _units_exempt(chain: tuple[str, ...]) -> bool:
    """XBRL·메트릭 **단위 계약 구조**의 자식 키만 예외 — 임의 JSON 의 `units` 는 아니다."""
    if not chain or chain[-1] != "units":
        return False
    body = chain[:-1]
    if body and body[0] in XBRL_UNIT_ROOTS:
        # facts.<taxonomy>.<concept>.units.<unit>
        return len(body) >= 3
    if body and body[0] in METRIC_UNIT_ROOTS:
        return len(body) >= 2
    return False


def _key_exempt(key: str, chain: tuple[str, ...], *, schema: bool) -> bool:
    if _units_exempt(chain):
        return bool(UNIT_NAME_RE.match(str(key)))
    if schema and chain and chain[-1] in SCHEMA_CONTAINERS and chain[-1] not in SCHEMA_DATA_KEYWORDS:
        return True
    return False


def check_text(path: Path, text: str, rules: dict, norm: str | None = None) -> list[str]:
    problems: list[str] = []
    deny_keys = set(rules.get("deny_keys", []))
    patterns = [re.compile(p) for p in rules.get("deny_patterns", [])]
    label = norm or str(path)

    data = None
    if path.suffix in (".json", ".jsonl"):
        try:
            if path.suffix == ".jsonl":
                data = [json.loads(line) for line in text.splitlines() if line.strip()]
            else:
                data = json.loads(text)
        except json.JSONDecodeError:
            problems.append(f"{label}: JSON 파싱 실패")
    if data is not None:
        schema = _is_schema_scope(norm or str(path), data)
        bad = sorted({k for k, chain in _iter_keys(data)
                      if k in deny_keys and not _key_exempt(k, chain, schema=schema)})
        if bad:
            problems.append(f"{label}: forbidden keys {bad}")

    for pat in patterns:
        for m in pat.finditer(text):
            token = m.group(0)
            if token.endswith(("@example.com", "@example.org")):
                continue  # .env.example 의 플레이스홀더는 개인정보가 아니다
            problems.append(f"{label}: forbidden pattern {pat.pattern[:32]}…({token[:6]}…)")
            break
    return problems


# ────────────────────────────── 새 산출물 필드 계약 ──────────────────────────────
#: 이 파일들은 기계가 만드는 새 산출물이라 **필드 계약**을 고정한다(감사 10).
#: `required`: 있어야 하는 키 · `allowed`: 있을 수 있는 키(허용 목록·구조 키와 합집합).
#: 목록 밖의 키는 계약 위반이다 — 공개 허용 목록에 필드를 **의도적으로** 추가하게 만든다.
ARTIFACT_CONTRACTS: dict[str, dict] = {
    "health": {
        "required": {"schema_version", "as_of", "data_status"},
        "allowed": {"status", "generated_at", "last_run_at", "last_attempt_at", "last_success_at",
                    "data_cutoff_at", "target_session", "outage_id", "reason", "degraded",
                    "note_ko", "kept_previous_signals", "outage_previous_as_of",
                    "outage_previous_status", "positions_open", "tranches_filled", "tranches_pending",
                    "stop_count", "trim_count", "stop_reentry_blocked_count", "blocked_reentries",
                    "order_cancellations", "order_cancellations_count", "forced_exits", "cash_pct",
                    "failed_sources", "mode", "tranche_cancel_count", "pending_exit_count",
                    "cancelled_tranches", "trim_orders", "trim_pending", "stop_locks",
                    "stop_reentry_blocked", "tranche_cancel_count", "pending_exit_count"},
    },
    "position_event": {
        "required": {"event", "signal_id", "date", "event_id"},
        "allowed": {"ticker", "recorded_for", "tranche", "reason", "price", "qty_pct", "fraction",
                    "ratio", "fill_basis", "cancelled_by", "superseded_by", "frozen_at", "note_ko",
                    "trim_stage", "at", "snapshot_id"},
    },
    "debate_event": {
        "required": {"signal_id", "events"},
        "allowed": {"snapshot_id", "ticker", "event", "report_id", "track", "intent", "completed_at",
                    "recorded_at", "signal_observed_at", "report_validated_at", "frozen_at",
                    "order_cutoff_at", "eligible", "late", "invalid", "reasons", "auto_downgrade_applied",
                    "quant_grade", "recommendation", "display_grade", "quant_action", "debate_action",
                    "divergent", "veto_codes", "regime", "news_state", "structural_risk",
                    "quote_verified", "receipt", "report_sha256"},
    },
    "notify_log": {
        "required": {"schema_version", "keys", "deliveries"},
        "allowed": {"event", "sent_at", "type", "status", "attempts", "attempt_at", "channel",
                    "latency_seconds", "detail", "event_key", "keys", "deliveries", "schema_version",
                    "label_ko", "severity", "message_ko", "ticker", "signal_id", "outage_id"},
    },
    "notify_latest": {
        "required": {"schema_version", "generated_at", "as_of", "data_status"},
        "allowed": {"outage_id", "n_events", "n_new", "n_suppressed", "n_retried", "retry_pending",
                    "new", "suppressed", "channels", "note_ko", "label_ko", "severity", "message_ko",
                    "ticker", "signal_id", "event_key", "type", "sent_at", "attempts", "last_attempt_at",
                    "last_success_at"},
    },
    "deploy_manifest": {
        "required": {"schema_version", "bundle_version", "files"},
        "allowed": {"generated_at", "snapshot_id", "as_of", "inputs_hash", "note_ko"},
    },
}

#: 필드 계약 검사에서 허용 목록과 무관하게 통과시키는 **구조·식별자** 키.
CONTRACT_SAFE_KEYS = {
    "signals", "blocked", "watchlist", "reports", "items", "folds", "horizons", "events", "keys",
    "deliveries", "files", "channels", "ticker", "signal_id", "report_id", "snapshot_id", "event",
    "code", "id", "name", "reason", "reasons", "note_ko", "count", "counts", "as_of",
    "generated_at", "build_at", "schema_version", "status", "type", "value", "list", "label_ko",
    "severity", "message_ko", "event_key", "attempts", "sent_at", "attempt_at", "channel",
    "latency_seconds", "detail", "retry_pending", "suppressed", "n_events", "n_new", "n_suppressed",
    "n_retried", "outage_id", "last_attempt_at", "last_success_at", "data_status", "at", "date",
    "event_id", "frozen_at", "cancelled_by", "superseded_by", "tranche", "recorded_for",
    "ok", "skipped", "retried", "n_retried", "sha256", "size", "cancel_kind",
    "freeze_deadline_at", "session", "lock", "expected_fill_day", "note",
}

#: 행 목록을 담는 필드 — 그 안의 dict 는 **필드 계약**을 따른다(그 밖의 dict 값은 데이터 맵).
CONTRACT_ROW_FIELDS = {"order_cancellations", "blocked_reentries", "new", "deliveries", "events",
                       "retry_pending", "suppressed", "keys", "channels", "files"}

CORE_PUBLIC_FILES = ("meta.json", "signals/today.json", "health.json", "debate.json",
                     "notifications.json", "ledger/summary.json", "ledger/records.json",
                     "ledger/portfolio.json", "radar/13f.json", "config.json")


def _contract_of(norm: str) -> str | None:
    if norm == "data/health.json":
        return "health"
    if norm == "data/ledger/position_events.jsonl":
        return "position_event"
    if norm.startswith("data/ledger/debate_events/") and norm.endswith(".json"):
        return "debate_event"
    if norm == "data/notifications_log.json":
        return "notify_log"
    if norm == "data/notifications_latest.json":
        return "notify_latest"
    if norm == "web/data/deploy_manifest.json":
        return "deploy_manifest"
    return None


def _contract_problems(name: str, data) -> list[str]:
    contract = ARTIFACT_CONTRACTS[name]
    required = set(contract["required"])
    # 중첩 행(취소 목록·알림 이벤트·배달 로그)은 **계약 전체 키의 합집합**으로 판정한다 —
    # 파일별 계약은 **최상위 필수 필드**를 고정하고, 중첩 행은 어느 계약에든 선언된 필드만 쓰게 한다.
    allowed = set(CONTRACT_SAFE_KEYS) | set(contract["required"]) | set(contract["allowed"])
    for other in ARTIFACT_CONTRACTS.values():
        allowed |= set(other["required"]) | set(other["allowed"])
    rows = data if isinstance(data, list) else [data]
    problems: list[str] = []
    unknown: set[str] = set()
    missing: set[str] = set()

    def _row_keys(row: dict) -> None:
        unknown.update(k for k in row if k not in allowed)
        for field, value in row.items():
            if field not in CONTRACT_ROW_FIELDS:
                continue
            items = value.values() if isinstance(value, dict) else (value if isinstance(value, list) else [])
            for item in items:
                if isinstance(item, dict):
                    unknown.update(k for k in item if k not in allowed)

    for row in rows:
        if not isinstance(row, dict):
            problems.append(f"{name}: 행이 객체가 아니다")
            continue
        missing |= required - set(row)
        _row_keys(row)
    if missing:
        problems.append(f"{name}: 필수 필드 누락 {sorted(missing)}")
    if unknown:
        problems.append(f"{name}: 계약에 없는 필드 {sorted(unknown)} — 공개 허용 목록에 추가하거나 필드를 고쳐라")
    return problems


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def check_deploy_manifest(norm: str, data, root: str | Path | None) -> list[str]:
    """`web/data/deploy_manifest.json` 이 실제 파일 해시와 일치하는가(공개 세트 무결성)."""
    if not isinstance(data, dict):
        return [f"{norm}: 객체가 아니다"]
    files = data.get("files")
    if not isinstance(files, dict) or not files:
        return [f"{norm}: files 해시 목록이 비었다"]
    problems: list[str] = []
    root_path = Path(root) if root is not None else Path(".")
    base = root_path / "web" / "data"
    for name, entry in sorted(files.items()):
        digest = (entry or {}).get("sha256") if isinstance(entry, dict) else None
        target = base / str(name)
        if not target.exists():
            problems.append(f"{norm}: files[{name}] 이 가리키는 파일이 없다")
            continue
        if not digest:
            problems.append(f"{norm}: files[{name}] 에 sha256 이 없다")
        elif _sha256(target) != str(digest):
            problems.append(f"{norm}: files[{name}] sha256 불일치 — 매니페스트가 낡았다")
    missing_core = [name for name in CORE_PUBLIC_FILES if name not in files]
    if missing_core:
        problems.append(f"{norm}: 핵심 공개 파일 해시 누락 {missing_core}")
    return problems


def check_paths(paths: list[str], rules: dict | None = None, *, root: str | Path | None = None,
                reader=None) -> list[str]:
    """경로 목록 검사. `reader(path) -> bytes | None` 로 **커밋될 바이트**를 주입할 수 있다.

    `reader` 가 없으면 워킹트리(`--root` 기준)를 읽는다 — `--staged`·`--commit` 은 인덱스/커밋 blob 을
    넘겨서 검사한다(감사 10: 워킹트리와 인덱스가 다르면 가드가 무의미해진다).
    """
    rules = rules or load_rules()
    deny_paths = rules.get("deny_paths", [])
    allow_paths = [str(p).replace("\\", "/").strip("/") for p in rules.get("allow_paths", [])]
    problems: list[str] = []
    for p in paths:
        path = Path(p)
        norm = _norm(p, root)
        allowed_exact = any(norm == entry or norm.endswith("/" + entry) for entry in allow_paths)
        if not allowed_exact:
            for dp in deny_paths:
                if dp.endswith("/"):
                    if norm.startswith(dp) or f"/{dp}" in f"/{norm}":
                        problems.append(f"{norm}: forbidden path ({dp})")
                else:
                    d = dp.rstrip("/")
                    if norm == d or norm.endswith("/" + d) or f"/{d}/" in f"/{norm}":
                        problems.append(f"{norm}: forbidden path ({dp})")
        if path.name == ".env" or path.name == "auth.json":
            problems.append(f"{norm}: secret file must never be committed")

        raw = reader(p) if reader is not None else _read_worktree(p, root)
        if raw is None:
            continue  # 삭제·미추적 — 내용 검사 대상이 아니다
        suffix = path.suffix.lower()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is None:
            # 감사 10: UTF-8 실패를 조용히 건너뛰지 않는다 — 허용된 바이너리 확장자만 통과한다.
            if suffix not in ALLOWED_BINARY_SUFFIXES:
                problems.append(f"{norm}: non-utf8 — 허용된 바이너리 확장자가 아니다"
                                f"(허용: {sorted(ALLOWED_BINARY_SUFFIXES)})")
            continue
        problems.extend(check_text(path, text, rules, norm))
        if suffix not in TEXT_SUFFIXES:
            continue
        contract = _contract_of(norm)
        if contract:
            try:
                data = (json.loads(text) if suffix == ".json"
                        else [json.loads(line) for line in text.splitlines() if line.strip()])
            except json.JSONDecodeError:
                data = None
            if data is not None:
                problems.extend(_contract_problems(contract, data))
                if contract == "deploy_manifest":
                    problems.extend(check_deploy_manifest(norm, data, root))
    return problems


def check_staged(rules: dict, root: str | Path | None = None) -> tuple[list[str], int]:
    paths = staged_paths()

    def reader(p: str) -> bytes | None:
        return _git_blob(f":{p}")

    return check_paths(paths, rules, root=root, reader=reader), len(paths)


def check_commit(rev: str, rules: dict, root: str | Path | None = None) -> tuple[list[str], int]:
    paths = commit_paths(rev)

    def reader(p: str) -> bytes | None:
        return _git_blob(f"{rev}:{p}")

    return check_paths(paths, rules, root=root, reader=reader), len(paths)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="커밋·배포 범위 가드 (인덱스/커밋 blob 기준)")
    ap.add_argument("--staged", action="store_true", help="Git 인덱스 blob 을 검사한다")
    ap.add_argument("--commit", default=None, help="이 커밋이 담은 blob 을 검사한다(예: HEAD, 커밋 sha)")
    ap.add_argument("--paths", nargs="*", default=None, help="지정 경로(워킹트리)를 검사한다")
    ap.add_argument("--root", default=None, help="경로·파일 기준 디렉터리(기본: 현재 디렉터리)")
    ap.add_argument("--allowlist", default=str(ALLOWLIST))
    args = ap.parse_args(argv)

    rules = load_rules(Path(args.allowlist))
    if args.paths is not None:
        problems = check_paths(args.paths, rules, root=args.root)
        count = len(args.paths)
    elif args.commit:
        found, count = check_commit(args.commit, rules, root=args.root)
        problems = found
    elif args.staged:
        found, count = check_staged(rules, root=args.root)
        problems = found
    else:
        problems, count = [], 0

    problems += [f"{p}: .env/auth.json is tracked" for p in tracked_env()]
    if problems:
        for p in problems:
            print(f"ERROR {p}", file=sys.stderr)
        return 1
    print(f"scope ok ({count} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
