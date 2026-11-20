#!/usr/bin/env python3
"""scripts/assemble_cloud.py — Codex Cloud '에이전트 대행' 조립기 (사용 가이드 부록 A 정식판).

Codex Cloud 에는 `codex` CLI·`auth.json` 이 없어 `codex_debate/run_debate.py` 를 그대로 돌릴 수 없다.
그래서 에이전트가 라운드 JSON(`round1_*.json`·`round2_*.json`·`round3_guard.json`·`round3_chair.json`)을
직접 만들고, 이 스크립트가 **저장소 코드의 검증 함수**로 걸러 `data/debate_reports.json` 한 건을 조립한다.
호출(API) 0회 — 검증·조립은 전부 로컬 코드다.

세 가지 모드(짧은 명령 한 줄로 끝난다):

    # 1) 컨텍스트 준비 — 에이전트가 읽을 `data/debate/context/{TICKER}.json` + `manifest.json` 을 만든다
    python scripts/assemble_cloud.py --prepare --tickers MSFT --no-fetch-body
    python scripts/assemble_cloud.py --prepare --top 3 --no-fetch-body

    # 2) 조립 — 저장된 컨텍스트가 있으면 **그대로 재사용**하고(네트워크 재호출 없음) 리포트를 만든다
    CODEX_VERSION=cloud-agent python scripts/assemble_cloud.py --assemble \\
        --tickers MSFT --rounds /tmp/rounds/MSFT --drop-incomplete
    CODEX_VERSION=cloud-agent python scripts/assemble_cloud.py --assemble \\
        --top 6 --rounds /tmp/rounds --drop-incomplete          # 다종목: {rounds}/{TICKER}/ 필수
    CODEX_VERSION=cloud-agent python scripts/assemble_cloud.py --assemble \\
        --rounds /tmp/rounds --drop-incomplete                  # 대상은 --prepare 매니페스트가 정한다

    # 3) 조립 + PR — 검증을 통과하면 GitHub REST(HTTPS)로 단일 파일 PR 까지 연다(git push·gh 불필요)
    CODEX_VERSION=cloud-agent python scripts/assemble_cloud.py --assemble \\
        --tickers MSFT --rounds /tmp/rounds/MSFT --drop-incomplete --create-pr

    # 4) PR 전용 — 조립은 이미 끝났고 PR 만 실패했을 때. 원격 `main` 대비 **미반영 신규분만** 올린다
    CODEX_VERSION=cloud-agent python scripts/assemble_cloud.py --pr-only

`--create-pr`·`--pr-only` 는 `data/debate_reports.json` **한 파일**만 담은 브랜치를 만들어 PR 을 연다.
Cloud 에서 `git push`·`gh` 가 403 이어도 HTTP 시크릿(Network secret)이 `Authorization: Bearer …`
자리의 placeholder 를 실제 토큰으로 바꿔 주므로 api.github.com 만으로 끝난다. 토큰은
`GH_PAT`·`GH_TOKEN`·`GITHUB_TOKEN` 순서로 찾고 **출력·로그·에러 메시지에 남기지 않는다**.

주의:
- `data/debate/context/`·`data/debate_reports.json` 은 실행 산출물이다. 커밋은 범위 가드가 막는다.
- `provider` 는 스키마 허용값(`codex|openai`)이라 `codex` 로 두고, 실행 방식은 `codex.version`(환경변수
  `CODEX_VERSION=cloud-agent`)으로 구분한다.
- 자동 하향(`auto_downgrade_enabled`)은 켜지 않는다 — 동결 전에 규칙을 바꾸는 일이 된다.
- V11 무결성 훼손(`integrity_compromised`) 종목은 **읽지도 저장하지도 않고** 건너뛴다. 지시문이 침투한
  입력을 에이전트가 읽으면 그 순간 판단 경로가 오염된다. 옛 컨텍스트 파일이 남아 있으면 **즉시 지우고**
  매니페스트에 `blocked_v11` 만 남긴다 — 다음 `--assemble` 이 그 파일을 재사용할 수 없게 하기 위해서다.
- `--drop-incomplete` 는 **이번 실행에서 새로 조립한** 미완료 리포트만 뺀다. 파일에 남아 있는 과거
  미완료 이력(`11-19 partial` 등)은 건드리지 않는다.
- 새로 완성된 리포트가 0건이면 **빈 문서로 PR 을 열지 않는다** — 빈 문서 PR 은 원격 리포트 전량 삭제 PR 이다.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from codex_debate.context import (  # noqa: E402 — 저장소 루트를 먼저 sys.path 에 넣는다
    DEFAULT_CONTEXT_DIR,
    INVESTOR_SKILLS,
    build_context,
    save_context,
    select_targets,
)
from codex_debate.run_debate import (  # noqa: E402
    DEFAULT_REPORTS,
    DEFAULT_SIGNALS,
    RunConfig,
    _report_seq,
    load_reports_doc,
    upsert_report,
    write_reports_doc,
)
from codex_debate.validate import (  # noqa: E402
    Validated,
    assemble_validated,
    integrity_compromised,
    report_receipt_sha256,
    validate_chair,
    validate_guard,
    validate_reports_file,
    validate_round1,
    validate_round2,
)

SKILLS: tuple[str, ...] = INVESTOR_SKILLS

# ── 실행 매니페스트 (--prepare ↔ --assemble 공유) ─────────────────────────
#: `--prepare` 가 `--context-dir` 아래에 남기는 실행 매니페스트.
#: 컨텍스트 폴더 **바로 아래**에는 `{TICKER}.json` 만 둔다(운영·테스트가 그 폴더를 `*.json` 으로
#: 훑어 종목 목록을 읽는다). 그래서 매니페스트는 그 아래 `manifest/` 하위 폴더에 둔다.
MANIFEST_DIR_NAME = "manifest"
MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = "1"

# ── GitHub REST (--create-pr) ────────────────────────────────────────────
#: `--repo` 를 주지 않고 `origin` 원격도 못 읽었을 때 쓰는 기본값.
DEFAULT_REPO = "top03071188-debug/us-stock-advisor"
API_ROOT = "https://api.github.com"
USER_AGENT = "us-stock-advisor-cloud"
#: 토큰 탐색 순서 — 비어 있지 않은 첫 값.
TOKEN_ENV_VARS: tuple[str, ...] = ("GH_PAT", "GH_TOKEN", "GITHUB_TOKEN")
HTTP_TIMEOUT_S = 30
#: PR 에 올리는 파일의 저장소 경로(리포트 파일이 저장소 밖에 있으면 이 경로를 쓴다).
REPORTS_REPO_PATH = "data/debate_reports.json"
#: PUT 409/422(동시 갱신) 뒤 최신 `main` 을 다시 읽어 재병합하는 재시도 횟수.
PR_RETRY_LIMIT = 2
#: 브랜치 이름이 이미 있을 때(같은 초 연속 실행) 접미사를 붙여 다시 만드는 횟수.
BRANCH_RETRY_LIMIT = 2
#: 충돌로 보는 HTTP 상태 — 409(sha 불일치·동시 갱신), 422(참조·PR 중복).
CONFLICT_STATUS: tuple[int, ...] = (409, 422)


# ── 해시·시각 ────────────────────────────────────────────────────────────
def _now_iso() -> str:
    """UTC ISO 문자열(Z, 초 단위) — 매니페스트·로그용."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    """파일 **바이트** 해시 — 없으면 빈 문자열(`quant_signals_sha256` 의 근거)."""
    try:
        return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def _canonical_sha256(node: Any) -> str:
    """정규 JSON(키 정렬·공백 없음)의 sha256 — 같은 입력이면 언제나 같은 값."""
    blob = json.dumps(node, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return _sha256_text(blob)


def _short_hash(*parts: str) -> str:
    """브랜치 접미사용 짧은 해시(6자) — 같은 초에 두 번 돌아도 이름이 겹치지 않게."""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:6]


# ── 입출력 ────────────────────────────────────────────────────────────────
def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _doc_bytes(doc: dict) -> bytes:
    """리포트 문서의 **실제 게시 바이트**(들여쓰기 2 + 끝 개행)."""
    return (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_doc(path: Path, doc: dict) -> None:
    """리포트 문서를 파일로 쓴다(들여쓰기 2 + 끝 개행 — 저장소의 다른 산출물과 같은 모양)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_doc_bytes(doc))


def _signal_id_for(ticker: str, signals: dict) -> str:
    """`build_context` 가 붙이는 `quant_signal_id` 와 같은 규칙 — 재사용 컨텍스트의 대조 기준."""
    for signal in signals.get("signals") or []:
        if str(signal.get("ticker", "")).upper() == ticker and signal.get("signal_id"):
            return str(signal["signal_id"])
    return f"{signals.get('as_of') or ''}-{ticker}-NONE"


def _snapshot_id_for(ticker: str, signals: dict) -> str:
    """그 종목 컨텍스트가 쓰는 `snapshot_id` — 신호 행 우선, 없으면 파일 전역값."""
    for signal in signals.get("signals") or []:
        if str(signal.get("ticker", "")).upper() == ticker and signal.get("snapshot_id"):
            return str(signal["snapshot_id"])
    return str(signals.get("snapshot_id") or "")


def _signal_payload_for(ticker: str, signals: dict) -> dict:
    """재사용 판정에 쓰는 **그 종목 입력의 전부**(신호 행 + 워치리스트 행 + 전역 식별자).

    신호 ID 가 그대로여도 수치 한 칸이 바뀌면 다른 입력이다 — 그 경우 재사용하면 저장 당시의
    숫자로 토론을 이어 가게 된다(감사 07).
    """
    signal = next((s for s in signals.get("signals") or []
                   if str(s.get("ticker", "")).upper() == ticker), None)
    watch = next((w for w in signals.get("watchlist") or []
                  if str(w.get("ticker", "")).upper() == ticker), None)
    return {"ticker": ticker, "signal": signal, "watch": watch,
            "as_of": signals.get("as_of"), "snapshot_id": _snapshot_id_for(ticker, signals)}


def _signal_digest(ticker: str, signals: dict) -> str:
    """현재 `quant_signals.json` 안 **그 종목 수치의 해시** — 저장 당시 값과 대조한다."""
    return _canonical_sha256(_signal_payload_for(ticker, signals))


# ── 실행 매니페스트 ──────────────────────────────────────────────────────
def _manifest_path(context_dir: str | Path) -> Path:
    return Path(context_dir) / MANIFEST_DIR_NAME / MANIFEST_NAME


def _load_manifest(context_dir: str | Path) -> dict:
    """저장된 매니페스트(없거나 깨졌으면 빈 골격). 조립을 막지 않는다 — 대조에만 쓴다."""
    path = _manifest_path(context_dir)
    if not path.exists():
        return {"schema_version": MANIFEST_VERSION, "quant_signals_sha256": "", "tickers": {}}
    try:
        doc = _read_json(path)
    except (json.JSONDecodeError, OSError):
        return {"schema_version": MANIFEST_VERSION, "quant_signals_sha256": "", "tickers": {}}
    if not isinstance(doc, dict):
        return {"schema_version": MANIFEST_VERSION, "quant_signals_sha256": "", "tickers": {}}
    doc.setdefault("tickers", {})
    return doc


def _manifest_for(context_dir: str | Path, signals_sha: str) -> dict:
    """이번 실행에 쓸 매니페스트 — 이전 항목을 **이어 쓴다**.

    신호 파일이 바뀌었다고 항목을 통째로 버리면 "무엇이 바뀌었는지"를 비교할 기준이 사라진다.
    재사용·차단 판정은 항목별 `signal_sha256` 대조로 하므로, 신호 파일 전체가 아니라 **그 종목**의
    수치가 바뀐 것만 다시 조립하게 된다.
    """
    manifest = _load_manifest(context_dir)
    manifest["schema_version"] = MANIFEST_VERSION
    manifest["quant_signals_sha256"] = signals_sha
    manifest["tickers"] = dict(manifest.get("tickers") or {})
    return manifest


def _manifest_entry(manifest: dict, ticker: str) -> dict:
    entry = (manifest.get("tickers") or {}).get(ticker)
    return dict(entry) if isinstance(entry, dict) else {}


def _record_manifest(manifest: dict, ticker: str, *, status: str, ctx: dict | None = None,
                     digest: str = "", scope: str | None = None) -> None:
    """매니페스트의 종목 항목을 갱신한다 — `status` 는 `prepared` 아니면 `blocked_v11`."""
    entry = _manifest_entry(manifest, ticker)
    entry["status"] = status
    entry["recorded_at"] = _now_iso()
    if ctx:
        entry["signal_id"] = str(ctx.get("quant_signal_id") or "")
        entry["snapshot_id"] = str(ctx.get("snapshot_id") or "")
        entry["context_sha256"] = str(ctx.get("context_hash") or "")
        entry["as_of"] = str(ctx.get("as_of") or "")
    if digest:
        entry["signal_sha256"] = digest
    if scope:
        entry["injection_scope"] = str(scope)
    manifest.setdefault("tickers", {})[ticker] = entry


def _save_manifest(context_dir: str | Path, manifest: dict) -> Path:
    path = _manifest_path(context_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _manifest_targets(context_dir: str | Path, signals: dict) -> list[str]:
    """`--prepare` 가 남긴 **준비 완료** 종목 — `--assemble` 에 대상 목록을 주지 않았을 때 쓴다.

    같은 `as_of` 의 항목만 쓴다 — 지난 실행의 대상 목록이 오늘 조립을 좌우하면 안 된다.
    """
    manifest = _load_manifest(context_dir)
    as_of = str(signals.get("as_of") or "")
    return [ticker for ticker, entry in (manifest.get("tickers") or {}).items()
            if isinstance(entry, dict) and entry.get("status") == "prepared"
            and (not as_of or str(entry.get("as_of") or as_of) == as_of)]


# ── 대상 선별 ────────────────────────────────────────────────────────────
def _resolve_targets(args: argparse.Namespace, signals: dict) -> tuple[list[str], int]:
    """`--tickers`(순서 유지·중복 제거) 또는 `--top N`. 워치리스트·신호 밖 종목은 받지 않는다."""
    if args.tickers:
        picked: list[str] = []
        for raw in args.tickers.split(","):
            ticker = raw.strip().upper()
            if ticker and ticker not in picked:
                picked.append(ticker)
        if not picked:
            print("--tickers 가 비었다", file=sys.stderr)
            return [], 2
        allowed = {row["ticker"] for row in select_targets(signals, tickers=picked, max_tickers=len(picked))}
        unknown = [ticker for ticker in picked if ticker not in allowed]
        if unknown:
            print(f"신호·워치리스트 밖 종목은 조립 대상이 아니다: {', '.join(unknown)}", file=sys.stderr)
            return [], 2
        return picked, 0
    if args.top is not None:
        return [row["ticker"] for row in
                select_targets(signals, top=args.top, max_tickers=args.max_tickers)], 0
    return [], 0


# ── 라운드 파일 ──────────────────────────────────────────────────────────
def _rounds_dir(rounds_root: Path, ticker: str, *, multi: bool = False) -> Path | None:
    """라운드 폴더 — 다종목이면 `{root}/{TICKER}` **만** 인정한다(공용 루트 폴백 금지).

    한 종목만 돌 때는 `--rounds` 가 이미 그 종목 폴더일 수 있으므로 그대로 쓴다. 다종목에서
    공용 루트로 폴백하면 두 번째 종목이 첫 종목의 라운드를 읽어 다른 종목의 판단을 조립한다(감사 07).
    """
    per_ticker = rounds_root / ticker
    if per_ticker.is_dir():
        return per_ticker
    return None if multi else rounds_root


def _failed(kind: str, reason: str) -> Validated:
    return Validated(kind, None, False, [reason], {}, {})


def _validate_file(path: Path, kind: str, ctx: dict, expected_skill: str | None = None) -> Validated:
    """라운드 JSON 한 건을 검증한다. 파일이 없거나 깨졌으면 **탈락 사유**로 남긴다(예외로 죽지 않는다)."""
    if not path.exists():
        return _failed(kind, f"missing_file:{path.name}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _failed(kind, f"json_error:{path.name}:{exc.msg}")
    if kind == "round3_guard":
        return validate_guard(payload, ctx)
    if kind == "round3_chair":
        return validate_chair(payload, ctx)
    if kind == "round2":
        return validate_round2(payload, ctx, expected_skill=expected_skill)
    return validate_round1(payload, ctx, expected_skill=expected_skill)


#: 이번 실행의 라운드 파일 이름 — 순서 고정(출처 해시 목록도 같은 순서).
def _round_file_names() -> list[str]:
    names = [f"round1_{skill}.json" for skill in SKILLS]
    names += [f"round2_{skill}.json" for skill in SKILLS]
    return names + ["round3_guard.json", "round3_chair.json"]


def _round_hashes(rdir: Path | None) -> dict[str, str]:
    """조립에 **실제로 쓴** 라운드 파일의 바이트 해시 — 없으면 빈 문자열(파일이 없었다는 뜻)."""
    if rdir is None:
        return {}
    return {name: _file_sha256(rdir / name) for name in _round_file_names()}


# ── 컨텍스트 ─────────────────────────────────────────────────────────────
def _load_context(args: argparse.Namespace, ticker: str, signals: dict,
                  manifest: dict) -> tuple[dict, str]:
    """저장된 컨텍스트를 재사용하거나 새로 만든다. 반환: (ctx, '재사용'|'신규').

    재사용 조건은 세 가지다 — `quant_signal_id`·`snapshot_id` 가 같고, **그 종목 수치 해시**가
    저장 당시와 같을 것. 하나라도 다르면 저장된 컨텍스트는 다른 입력의 것이므로 새로 만든다.
    """
    path = Path(args.context_dir) / f"{ticker}.json"
    digest = _signal_digest(ticker, signals)
    expected_id = _signal_id_for(ticker, signals)
    expected_snapshot = _snapshot_id_for(ticker, signals)
    if path.exists() and not args.refresh_context:
        try:
            stored = _read_json(path)
        except (json.JSONDecodeError, OSError):
            print(f"{ticker}: 저장된 컨텍스트를 읽을 수 없다({path.name}) — 다시 만든다")
        else:
            stale: list[str] = []
            if not isinstance(stored, dict):
                stale.append("저장된 파일이 객체가 아니다")
                stored = {}
            if str(stored.get("quant_signal_id") or "") != expected_id:
                stale.append(f"signal_id {stored.get('quant_signal_id')} → {expected_id}")
            if str(stored.get("snapshot_id") or "") != expected_snapshot:
                stale.append(f"snapshot_id {stored.get('snapshot_id') or '-'} → {expected_snapshot or '-'}")
            recorded = str(_manifest_entry(manifest, ticker).get("signal_sha256") or "")
            if recorded != digest:
                stale.append("quant_signals 수치 해시가 저장 당시와 다르다" if recorded
                             else "저장 당시 수치 해시 기록이 없다")
            if not stale:
                return stored, "재사용"
            print(f"{ticker}: 저장된 컨텍스트가 현재 신호와 다르다({' / '.join(stale)}) — 다시 만든다")
    return build_context(ticker, signals, fetch_body=not args.no_fetch_body), "신규"


def _drop_stale_context(context_dir: str | Path, ticker: str) -> Path | None:
    """V11 차단 종목의 옛 컨텍스트 파일을 지운다 — 다음 실행이 그 파일을 재사용하지 못하게."""
    path = Path(context_dir) / f"{ticker}.json"
    if not path.exists():
        return None
    try:
        path.unlink()
    except OSError:
        return None
    return path


# ── 모드 1: --prepare ────────────────────────────────────────────────────
def _prepare(args: argparse.Namespace, signals: dict) -> int:
    targets, code = _resolve_targets(args, signals)
    if code:
        return code
    if not targets:
        print("준비할 대상 종목이 없다", file=sys.stderr)
        return 2

    context_dir = Path(args.context_dir)
    signals_sha = _file_sha256(Path(args.signals))
    manifest = _manifest_for(context_dir, signals_sha)
    manifest["prepared_at"] = _now_iso()
    manifest["signals_path"] = str(Path(args.signals))

    prepared: list[str] = []
    blocked: list[str] = []
    for ticker in targets:
        digest = _signal_digest(ticker, signals)
        ctx = build_context(ticker, signals, fetch_body=not args.no_fetch_body)
        if integrity_compromised(ctx):
            blocked.append(ticker)
            removed = _drop_stale_context(context_dir, ticker)
            _record_manifest(manifest, ticker, status="blocked_v11", digest=digest,
                             scope=str(ctx.get("injection_scope") or ""))
            print(f"{ticker}: V11 무결성 훼손(scope={ctx.get('injection_scope')}) — "
                  "컨텍스트를 저장하지 않고 건너뛴다")
            if removed is not None:
                print(f"{ticker}: 옛 컨텍스트 파일을 지웠다 — {removed}")
            continue
        path = save_context(ctx, args.context_dir)
        _record_manifest(manifest, ticker, status="prepared", ctx=ctx, digest=digest)
        news = ctx.get("news") or []
        with_body = sum(1 for item in news if item.get("body_available"))
        prepared.append(ticker)
        print(f"{ticker}: 컨텍스트 저장 {path} — 뉴스 {len(news)}건(본문 {with_body}건), "
              f"격리 {len(ctx.get('excluded_news') or [])}건, 8-K {len(ctx.get('filings_8k') or [])}건, "
              f"as_of {ctx.get('as_of')}")

    manifest_path = _save_manifest(context_dir, manifest)
    print(f"매니페스트: {manifest_path} — quant_signals_sha256 {signals_sha or '-'}")
    print(f"준비 {len(prepared)}건: {', '.join(prepared) if prepared else '-'} / "
          f"V11 차단 {len(blocked)}건: {', '.join(blocked) if blocked else '-'}")
    if blocked:
        return 1
    return 0 if prepared else 1


# ── 모드 2: --assemble ───────────────────────────────────────────────────
def _assemble(args: argparse.Namespace, signals: dict) -> int:
    targets, code = _resolve_targets(args, signals)
    if code:
        return code
    if not targets:
        targets = _manifest_targets(args.context_dir, signals)
        if targets:
            print(f"--prepare 매니페스트 기준 대상: {', '.join(targets)}")
    if not targets:
        print("조립할 대상 종목이 없다", file=sys.stderr)
        return 2

    context_dir = Path(args.context_dir)
    signals_sha = _file_sha256(Path(args.signals))
    manifest = _manifest_for(context_dir, signals_sha)
    multi = len(targets) > 1

    out = Path(args.reports_path)
    rounds_root = Path(args.rounds)
    cfg = RunConfig(signals_path=Path(args.signals), reports_path=out,
                    context_dir=context_dir, min_valid_skills=args.min_valid_skills,
                    provider="codex", model=args.model, fetch_body=not args.no_fetch_body)
    doc = load_reports_doc(out, signals)
    done: list[str] = []
    blocked: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    #: 이번 실행이 만든 리포트 — (ticker, report_id, status). `--drop-incomplete` 와 PR 대상의 범위다.
    produced: list[tuple[str, str, str]] = []

    for ticker in targets:
        entry = _manifest_entry(manifest, ticker)
        digest = _signal_digest(ticker, signals)
        # 차단 표식은 **같은 입력**일 때만 유효하다 — 신호가 바뀌면 다시 판정해야 한다.
        if entry.get("status") == "blocked_v11" and str(entry.get("signal_sha256") or "") == digest:
            blocked.append(ticker)
            removed = _drop_stale_context(context_dir, ticker)
            print(f"{ticker}: V11 무결성 훼손으로 --prepare 에서 차단됐다"
                  f"(scope={entry.get('injection_scope') or '-'}) — 조립하지 않는다")
            if removed is not None:
                print(f"{ticker}: 옛 컨텍스트 파일을 지웠다 — {removed}")
            continue

        ctx, origin = _load_context(args, ticker, signals, manifest)
        as_of = str(ctx.get("as_of") or "")
        if integrity_compromised(ctx):
            blocked.append(ticker)
            removed = _drop_stale_context(context_dir, ticker)
            _record_manifest(manifest, ticker, status="blocked_v11", digest=digest,
                             scope=str(ctx.get("injection_scope") or ""))
            print(f"{ticker}: V11 무결성 훼손(scope={ctx.get('injection_scope')}) — 조립하지 않는다")
            if removed is not None:
                print(f"{ticker}: 옛 컨텍스트 파일을 지웠다 — {removed}")
            continue
        already = any(
            report.get("ticker") == ticker and report.get("as_of") == as_of
            and report.get("status") == "complete"
            and report.get("signal_id") == ctx.get("quant_signal_id")
            for report in doc.get("reports") or []
        )
        if already:
            skipped.append(ticker)
            print(f"{ticker}: 같은 signal_id 의 complete 리포트가 이미 있다 — 건너뛴다")
            continue

        rdir = _rounds_dir(rounds_root, ticker, multi=multi)
        if rdir is None or not rdir.is_dir():
            failed.append(ticker)
            where = rounds_root / ticker if multi else rounds_root
            print(f"{ticker}: 라운드 폴더가 없다 — {where}" +
                  (" (다종목 조립은 종목별 폴더가 있어야 한다)" if multi else ""), file=sys.stderr)
            continue

        round1 = [_validate_file(rdir / f"round1_{skill}.json", "round1", ctx, skill) for skill in SKILLS]
        round2 = [_validate_file(rdir / f"round2_{skill}.json", "round2", ctx, skill) for skill in SKILLS]
        guard = _validate_file(rdir / "round3_guard.json", "round3_guard", ctx)
        chair = _validate_file(rdir / "round3_chair.json", "round3_chair", ctx)
        for item in round1 + round2 + [guard, chair]:
            if not item.ok:
                print(f"{ticker}: {item.kind} 탈락 — {item.reasons[:4]}")

        if origin == "신규":
            save_context(ctx, args.context_dir)  # 로컬 전용(.gitignore) — 커밋되지 않는다
        _record_manifest(manifest, ticker, status="prepared", ctx=ctx, digest=digest)

        report = assemble_validated(
            ctx, round1=round1, round2=round2, guard=guard, chair=chair,
            min_valid_skills=cfg.min_valid_skills,
            auto_downgrade_enabled=False,
            provider="codex",                         # 스키마 허용값은 codex|openai 뿐이다
            report_seq=_report_seq(doc, ticker, as_of),
            # [수정 A] 검증 시각은 `completed_at`(조립 시각)과 같은 순간이다. 초 단위로 절사한
            # 별도 값을 넘기면 완료 시각의 마이크로초가 잘려 `validated < completed`(0.53초 역전)가
            # 되고 트랙 1 이 `VALIDATED_BEFORE_COMPLETED` 로 편입을 거부한다. `None` 이면
            # `assemble_validated` 가 `completed` 와 **같은 타임스탬프**를 쓴다.
            validated_at=None,
            model_version=cfg.model,
        )
        # ── [감사 07] 리포트별 출처(provenance) — 뒤 종목이 앞 종목의 출처를 덮지 않게 **건별**로 남긴다.
        # 리포트 객체에는 스키마가 이미 허용하는 `context_sha256` 만 넣고(그 컨텍스트의 해시),
        # 나머지(라운드 파일 해시·코덱스 버전)는 `inputs.reports_provenance[report_id]` 에 건별 저장한다.
        provenance = {
            "report_id": report["report_id"],
            "ticker": ticker,
            "as_of": as_of,
            "context_sha256": str(ctx.get("context_hash") or ""),
            "quant_signal_id": str(ctx.get("quant_signal_id") or ""),
            "snapshot_id": str(ctx.get("snapshot_id") or ""),
            "round_hashes": _round_hashes(rdir),
            "codex_version": os.environ.get("CODEX_VERSION") or "unknown",
            "context_origin": origin,
            "assembled_at": str(report.get("completed_at") or ""),
        }
        report["context_sha256"] = provenance["context_sha256"]
        # 리포트 바이트 영수증은 출처 필드를 붙인 **뒤의 내용**으로 다시 찍는다(붙이기 전 값이면
        # `validate_reports_file` 이 "검증 뒤 편집됨"으로 본다).
        report["validation"]["report_sha256"] = report_receipt_sha256(report)

        upsert_report(doc, report)
        write_reports_doc(out, doc, signals, ctx, cfg,
                          quant_signals_sha256=signals_sha, provenance=provenance)
        produced.append((ticker, str(report["report_id"]), str(report["status"])))
        done.append(ticker)
        print(json.dumps({"ticker": ticker, "report_id": report["report_id"], "status": report["status"],
                          "skills_kept": report["validation"]["skills_kept"], "news_state": report["news_state"],
                          "late": report["late"], "invalid": report["invalid"],
                          "context": origin}, ensure_ascii=False))

    _save_manifest(context_dir, manifest)

    if args.drop_incomplete:
        # 이번 실행이 만든 미완료만 뺀다 — 파일에 남아 있는 **과거 미완료 이력**은 그대로 둔다(감사 06).
        produced_ids = {report_id for _, report_id, _ in produced}
        reports = doc.get("reports") or []
        dropped = [report for report in reports
                   if str(report.get("report_id") or "") in produced_ids and report.get("status") != "complete"]
        if dropped:
            keep = {id(report) for report in dropped}
            doc["reports"] = [report for report in reports if id(report) not in keep]
            _write_doc(out, doc)
            print(f"완주하지 못한 리포트 {len(dropped)}건을 뺐다: "
                  f"{', '.join(str(report.get('report_id')) for report in dropped)}")
            produced = [(ticker, report_id, status) for ticker, report_id, status in produced
                        if status == "complete"]
        else:
            print("이번 실행에서 뺄 미완료 리포트가 없다")

    print(f"처리 {len(done)}건: {', '.join(done) if done else '-'} / 이미 있음 {len(skipped)}건 / "
          f"V11 차단 {len(blocked)}건 / 실패 {len(failed)}건")
    if not done:
        print("새로 만든 리포트가 없다")
        return 2 if failed else 1

    errors = validate_reports_file(out, auto_downgrade_enabled=False)
    if errors:
        print("파일 검증 실패 — 커밋하지 말 것:", file=sys.stderr)
        for error in errors[:10]:
            print("  -", error, file=sys.stderr)
        return 2
    print(f"OK — {len(done)}건 처리, 파일 검증 통과: {out}")
    if args.create_pr:
        present_ids = {str(report.get("report_id") or "") for report in doc.get("reports") or []}
        publish = [(ticker, report_id) for ticker, report_id, status in produced
                   if status == "complete" and report_id in present_ids]
        if not publish:
            print("이번 실행에서 완성된 리포트가 0건 — PR 을 만들지 않는다"
                  "(빈 문서로 원격 파일을 덮으면 기존 리포트가 사라진다)", file=sys.stderr)
            return 1
        as_of = str(signals.get("as_of") or (doc.get("inputs") or {}).get("as_of") or "")
        return _create_pr(out, doc, [ticker for ticker, _ in publish], as_of, args,
                          new_ids=[report_id for _, report_id in publish])
    return 0


# ── GitHub PR ────────────────────────────────────────────────────────────
class GitHubError(RuntimeError):
    """GitHub REST 호출 실패. 메시지에 **토큰·요청 헤더를 담지 않는다**(로그로 새지 않게)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _redact(text: str, token: str | None) -> str:
    """메시지에 토큰 모양 문자열이 섞였으면 가린다 — 마지막 안전망."""
    if token and token in text:
        return text.replace(token, "***")
    return text


def _resolve_token() -> str | None:
    """`GH_PAT` → `GH_TOKEN` → `GITHUB_TOKEN` 중 비어 있지 않은 첫 값."""
    for name in TOKEN_ENV_VARS:
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def _repo_slug(remote_url: str) -> str | None:
    """원격 URL → `owner/repo`. https·ssh(scp) 형식 둘 다, 모양이 다르면 None."""
    text = remote_url.strip()
    if not text:
        return None
    if "://" in text:
        rest = text.split("://", 1)[1]
        text = rest.split("/", 1)[1] if "/" in rest else ""
    elif ":" in text:
        text = text.split(":", 1)[1]
    text = text.strip("/")
    if text.endswith(".git"):
        text = text[:-4]
    parts = [part for part in text.split("/") if part]
    if len(parts) < 2:
        return None
    return f"{parts[-2]}/{parts[-1]}"


def _repo_from_remote() -> str | None:
    """`git remote get-url origin` 에서 저장소를 읽는다 — 실패하면 None(호출자가 기본값을 쓴다)."""
    try:
        proc = subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(REPO_ROOT),
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return _repo_slug(proc.stdout)


def _resolve_repo(specified: str | None) -> str:
    """`--repo` → `origin` 원격 → 기본값 순서로 저장소를 정한다."""
    value = str(specified or "").strip().strip("/")
    if value:
        return value
    return _repo_from_remote() or DEFAULT_REPO


def _reports_repo_path(reports_path: Path) -> str:
    """PR 에 올릴 저장소 경로 — 저장소 밖(임시 파일)이면 기본 경로를 쓴다."""
    try:
        return reports_path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except (ValueError, OSError):
        return REPORTS_REPO_PATH


#: 마지막으로 만든 브랜치 시각(6자리) — 같은 초에 두 번 불리면 다음 초로 넘겨 이름이 겹치지 않게 한다.
_LAST_BRANCH_STAMP = ""


def _branch_name(as_of: str, tickers: Sequence[str], unique: str | None = None) -> str:
    """`debate/{as_of}-{tickers_slug}-{utc_hhmmss}` (+ 선택 접미사).

    기본형의 6자리는 **초** 단위다(기존 운영·테스트가 읽는 형식). 같은 초에 두 번 만들어지면
    시계를 다음 초로 넘겨 이름을 분리하고, 그래도 원격에 이미 있는 이름이면 `unique` 접미사
    (짧은 해시)를 붙여 다시 만든다 — 같은 초 연속 실행이 브랜치에서 충돌하지 않는다.
    """
    def _slug(text: str) -> str:
        return re.sub(r"[^A-Za-z0-9-]+", "-", text).strip("-").lower()

    global _LAST_BRANCH_STAMP
    stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    if stamp == _LAST_BRANCH_STAMP:
        # 같은 초 안의 두 번째 호출 — 초가 넘어간 뒤의 값을 쓴다(최대 1초).
        for _ in range(25):
            time.sleep(0.04)
            stamp = datetime.now(timezone.utc).strftime("%H%M%S")
            if stamp != _LAST_BRANCH_STAMP:
                break
    _LAST_BRANCH_STAMP = stamp
    base = f"debate/{_slug(as_of) or 'unknown'}-{_slug('-'.join(tickers)) or 'reports'}-{stamp}"
    return f"{base}-{_slug(unique)}" if unique else base


def _pr_body(doc: dict, tickers: Sequence[str], as_of: str, path: str) -> str:
    """조립된 리포트 목록을 PR 본문 마크다운으로 — 판정·상태·건수뿐(원문·계좌 정보 없음)."""
    wanted = set(tickers)
    rows = [row for row in doc.get("reports") or [] if str(row.get("ticker") or "") in wanted]
    lines = [
        f"Codex Cloud 대행 조립 — `{path}` 갱신 ({as_of})",
        "",
        f"원격 기존 리포트를 보존한 병합 결과 — 파일 전체 {len(doc.get('reports') or [])}건.",
        "",
        "| report_id | ticker | status | news_state | skills_kept | late |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        validation = row.get("validation") or {}
        lines.append("| {rid} | {ticker} | {status} | {news} | {skills} | {late} |".format(
            rid=row.get("report_id") or "-", ticker=row.get("ticker") or "-",
            status=row.get("status") or "-", news=row.get("news_state") or "-",
            skills=validation.get("skills_kept", "-"), late=row.get("late")))
    lines += ["", "생성: `scripts/assemble_cloud.py --assemble … --create-pr` — 저장소 검증 함수를 통과한 파일만 올린다."]
    return "\n".join(lines)


def _endpoint(path: str) -> str:
    """로그에 남길 경로 — 쿼리(브랜치 이름 등)는 그대로, 토큰은 원래 들어가지 않는다."""
    return path if path.startswith("https://") else f"{API_ROOT}{path}"


def _error_detail(response) -> str:
    """HTTP 에러 본문에서 `message` 만 뽑는다(헤더·토큰은 애초에 여기에 없다)."""
    detail = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        detail = str(body.get("message") or "")
        errors = body.get("errors")
        if errors:
            detail += f" ({str(errors)[:200]})"
    elif body is None:
        try:
            detail = str(response.text or "")[:200]
        except Exception:  # noqa: BLE001 — 본문을 못 읽어도 실패 메시지는 남긴다
            detail = ""
    detail = " ".join(detail.split())
    return f" {detail[:300]}" if detail else ""


def _api(method: str, path: str, token: str, payload: dict | None = None,
         allow_404: bool = False) -> Any:
    """GitHub REST 한 번 — `Bearer` 인증(Cloud HTTP 시크릿 placeholder 치환 경로)만 쓴다."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    try:
        response = requests.request(method, _endpoint(path), headers=headers, json=payload,
                                    timeout=HTTP_TIMEOUT_S)
    except requests.RequestException as exc:
        # 원인 예외에는 요청 헤더(토큰)가 달려 있다 — 체인을 끊고 이름만 남긴다.
        raise GitHubError(f"{method} {_endpoint(path)} — 요청 실패({type(exc).__name__})") from None
    if allow_404 and response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise GitHubError(f"{method} {_endpoint(path)} — HTTP {response.status_code}{_error_detail(response)}",
                          status=int(response.status_code))
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, (dict, list)) else None


def _is_conflict(exc: GitHubError) -> bool:
    return exc.status in CONFLICT_STATUS


def _find_open_pull(repo: str, branch: str, token: str) -> dict | None:
    """이미 열려 있는 같은 브랜치의 PR — 충돌 진단용(없거나 조회 실패면 None, 조용히 넘어간다)."""
    owner = repo.split("/", 1)[0]
    head = quote(f"{owner}:{branch}", safe="")
    try:
        rows = _api("GET", f"/repos/{repo}/pulls?state=open&head={head}", token)
    except GitHubError:
        return None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict):
            return row
    return None


def _log_existing(repo: str, branch: str, token: str) -> None:
    """같은 브랜치/PR 이 이미 있는지 **확인해 로그로 남긴다**(조용한 재생성을 막는다)."""
    pull = _find_open_pull(repo, branch, token)
    if pull:
        print(f"이미 열린 PR 이 있다 — #{pull.get('number')} {pull.get('html_url') or ''} (브랜치 {branch})")
        return
    try:
        ref = _api("GET", f"/repos/{repo}/git/ref/heads/{quote(branch, safe='')}", token, allow_404=True)
    except GitHubError:
        ref = None
    if ref:
        print(f"원격에 같은 브랜치가 이미 있다 — {branch} (열린 PR 은 없다)")
    else:
        print(f"같은 브랜치·PR 을 찾지 못했다 — {branch}")


def _decode_remote_doc(payload: dict | None) -> dict | None:
    """contents API 응답의 `content`(base64) → 문서. 읽을 수 없으면 None."""
    if not payload:
        return None
    content = str(payload.get("content") or "")
    encoding = str(payload.get("encoding") or "base64")
    if not content or encoding != "base64":
        return None
    try:
        raw = base64.b64decode(content, validate=False)
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _fetch_raw_doc(url: str, token: str) -> dict | None:
    """큰 파일(contents API 가 `content: ""` 로 주는 경우)은 raw 미디어 타입으로 읽는다."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw",
               "User-Agent": USER_AGENT}
    try:
        response = requests.request("GET", url, headers=headers, timeout=HTTP_TIMEOUT_S)
    except requests.RequestException as exc:
        raise GitHubError(f"GET {url} — 요청 실패({type(exc).__name__})") from None
    if response.status_code != 200:
        return None
    try:
        doc = json.loads(response.text or "")
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def _read_remote_doc(repo: str, path: str, ref: str, token: str) -> tuple[str | None, dict | None]:
    """원격 파일 → (`blob sha`, 문서). 파일이 없으면 `(None, None)`, 내용을 못 읽으면 `(sha, None)`."""
    existing = _api("GET", f"/repos/{repo}/contents/{path}?ref={quote(ref, safe='')}", token, allow_404=True)
    if not isinstance(existing, dict):
        return None, None
    sha = str(existing.get("sha") or "") or None
    doc = _decode_remote_doc(existing)
    if doc is None:
        raw_url = str(existing.get("download_url") or "")
        if raw_url:
            doc = _fetch_raw_doc(raw_url, token)
    return sha, doc


def _merge_docs(doc: dict, remote_doc: dict | None, new_ids: Sequence[str]) -> dict:
    """원격 보고서 **위에** 이번 실행 신규 리포트만 얹은 문서 — 원격 기존 보고서는 사라지지 않는다.

    감사 08: 예전에는 로컬 파일 전체로 원격을 덮어써, 다른 실행이 올린 종목이 조용히 사라졌다.
    병합 결과가 원격보다 적거나 원격 ID 가 하나라도 빠지면 **올리지 않고 중단**한다.
    """
    local_reports = {str(report.get("report_id") or ""): report for report in doc.get("reports") or []}
    wanted = [str(report_id) for report_id in new_ids]
    if remote_doc is None:
        merged = json.loads(json.dumps(doc, ensure_ascii=False))
        if not merged.get("reports"):
            raise GitHubError("올릴 리포트가 없다 — 빈 문서로 PR 을 만들지 않는다")
        return merged

    merged = json.loads(json.dumps(remote_doc, ensure_ascii=False))
    remote_reports = list(merged.get("reports") or [])
    remote_ids = {str(report.get("report_id") or "") for report in remote_reports}
    for report_id in wanted:
        report = local_reports.get(report_id)
        if report is None:
            continue
        upsert_report(merged, report)
    merged_reports = list(merged.get("reports") or [])
    merged_ids = {str(report.get("report_id") or "") for report in merged_reports}
    if len(merged_reports) < len(remote_reports):
        raise GitHubError(f"병합 뒤 보고서가 {len(remote_reports)}건 → {len(merged_reports)}건으로 줄었다 — 중단")
    missing = remote_ids - merged_ids
    if missing:
        raise GitHubError(f"병합에서 원격 리포트가 사라졌다: {', '.join(sorted(missing))[:200]} — 중단")
    if not merged_reports:
        raise GitHubError("병합 결과가 빈 문서다 — PR 을 만들지 않는다")

    for key in ("schema_version", "generated_at", "codex"):
        if doc.get(key):
            merged[key] = doc[key]
    remote_inputs = dict((remote_doc.get("inputs") or {}))
    local_inputs = dict(doc.get("inputs") or {})
    provenance = {**(remote_inputs.get("reports_provenance") or {}),
                  **(local_inputs.get("reports_provenance") or {})}
    by_ticker = {**(remote_inputs.get("context_hash_by_ticker") or {}),
                 **(local_inputs.get("context_hash_by_ticker") or {})}
    local_inputs.pop("reports_provenance", None)
    local_inputs.pop("context_hash_by_ticker", None)
    inputs = {**remote_inputs, **local_inputs}
    if provenance:
        inputs["reports_provenance"] = provenance
    if by_ticker:
        inputs["context_hash_by_ticker"] = by_ticker
    merged["inputs"] = inputs
    return merged


def _ensure_branch(repo: str, base_sha: str, as_of: str, tickers: Sequence[str],
                   branch: str, token: str) -> str:
    """기준 SHA 로 브랜치를 만든다. 이름이 이미 있으면(같은 초 실행) 확인 로그 + 접미사로 재시도."""
    for attempt in range(BRANCH_RETRY_LIMIT + 1):
        try:
            _api("POST", f"/repos/{repo}/git/refs", token,
                 payload={"ref": f"refs/heads/{branch}", "sha": base_sha})
            return branch
        except GitHubError as exc:
            if not _is_conflict(exc) or attempt >= BRANCH_RETRY_LIMIT:
                raise
            print(f"브랜치 {branch} 이 이미 있다(HTTP {exc.status}) — 열린 PR/브랜치를 확인하고 "
                  f"접미사를 붙여 다시 만든다 ({attempt + 1}/{BRANCH_RETRY_LIMIT})")
            _log_existing(repo, branch, token)
            branch = _branch_name(as_of, tickers,
                                  unique=f"{attempt + 1}-{_short_hash(branch, base_sha, as_of)}")
    raise GitHubError(f"브랜치를 만들지 못했다 — {branch}")


def _create_pr(out: Path, doc: dict, tickers: Sequence[str], as_of: str, args: argparse.Namespace,
               new_ids: Sequence[str] | None = None) -> int:
    """검증을 통과한 리포트만 담아 브랜치를 만들고 PR 을 연다(성공 시 `PR_CREATED` 출력).

    올리는 본문은 **원격 최신 파일 위에 이번 실행 신규분을 병합한 결과**다 — 로컬 전체로 덮지
    않는다(감사 08). PUT 409/422 는 최신 `main` 을 다시 읽어 재병합한 뒤 다시 시도한다(최대 2회).
    """
    token = _resolve_token()
    if not token:
        print("GitHub 토큰이 없다 — " + "·".join(TOKEN_ENV_VARS) + " 중 하나를 환경변수로 넣어라"
              "(값은 출력·로그에 남기지 않는다). 조립 결과는 저장·검증된 상태다: " + str(out), file=sys.stderr)
        return 2

    repo = _resolve_repo(args.repo)
    base = (args.base or "main").strip() or "main"
    path = _reports_repo_path(out)
    title = (args.pr_title or "").strip() or f"debate: {', '.join(tickers)} @ {as_of}"
    if new_ids is None:
        wanted = set(tickers)
        new_ids = [str(report.get("report_id") or "") for report in doc.get("reports") or []
                   if str(report.get("ticker") or "") in wanted]
    new_ids = [report_id for report_id in new_ids if report_id]
    if not new_ids:
        print("새로 올릴 리포트가 없다 — PR 을 만들지 않는다(빈 문서 PR 금지)", file=sys.stderr)
        return 1
    branch = _branch_name(as_of, tickers)
    print(f"PR 준비 — repo {repo}, base {base}, branch {branch}, file {path}, 신규 {len(new_ids)}건")

    try:
        for attempt in range(PR_RETRY_LIMIT + 1):
            ref = _api("GET", f"/repos/{repo}/git/ref/heads/{base}", token) or {}
            base_sha = str((ref.get("object") or {}).get("sha") or "")
            if not base_sha:
                raise GitHubError(f"기준 브랜치 SHA 를 찾지 못했다 — {repo}@{base}")

            existing_sha, remote_doc = _read_remote_doc(repo, path, base, token)
            if existing_sha and remote_doc is None:
                print("원격 리포트 내용을 읽지 못했다 — 병합 없이 올린다"
                      "(원격에만 있는 리포트가 사라질 수 있다). 원인은 contents API 응답 형식이다.",
                      file=sys.stderr)
            merged = _merge_docs(doc, remote_doc, new_ids)
            errors = validate_reports_file(merged, auto_downgrade_enabled=False)
            if errors:
                raise GitHubError("병합 결과가 파일 계약을 통과하지 못했다 — "
                                  + " / ".join(errors[:3]))
            body = _doc_bytes(merged)
            branch = _ensure_branch(repo, base_sha, as_of, tickers, branch, token)

            commit: dict[str, Any] = {"message": title, "branch": branch,
                                      "content": base64.b64encode(body).decode("ascii")}
            if existing_sha:
                commit["sha"] = existing_sha
            try:
                _api("PUT", f"/repos/{repo}/contents/{path}", token, payload=commit)
            except GitHubError as exc:
                if _is_conflict(exc) and attempt < PR_RETRY_LIMIT:
                    print(f"PUT 충돌(HTTP {exc.status}) — 최신 {base} 를 다시 읽어 재병합한 뒤 다시 올린다 "
                          f"({attempt + 1}/{PR_RETRY_LIMIT})")
                    branch = _branch_name(as_of, tickers)
                    continue
                raise

            out.write_bytes(body)  # 로컬 파일도 원격과 같은 내용으로 맞춘다
            pr = _open_pull(repo, branch, base, title, merged, tickers, as_of, path, token)
            url = str(pr.get("html_url") or "")
            number = pr.get("number")
            if not url:
                print(f"PR 응답에 html_url 이 없다 — 브랜치 {branch} 의 PR 생성 여부를 확인하라", file=sys.stderr)
                return 1
            print(f"PR_CREATED: #{number} {url}" if number else f"PR_CREATED: {url}")
            return 0
    except GitHubError as exc:
        print(f"PR 생성 실패 — {_redact(str(exc), token)}", file=sys.stderr)
        print(f"조립 결과는 그대로 남아 있다(로컬 검증 통과): {out}", file=sys.stderr)
        print("조립은 끝났고 PR 만 실패한 상태다 — `--pr-only` 로 다시 올릴 수 있다", file=sys.stderr)
        return 1
    return 1


def _open_pull(repo: str, branch: str, base: str, title: str, doc: dict, tickers: Sequence[str],
               as_of: str, path: str, token: str) -> dict:
    """PR 생성. 이미 같은 브랜치의 PR 이 있으면(422) 그것을 확인해 로그로 남기고 실패로 돌려준다."""
    payload = {"title": title, "head": branch, "base": base,
               "body": _pr_body(doc, tickers, as_of, path)}
    try:
        return _api("POST", f"/repos/{repo}/pulls", token, payload=payload) or {}
    except GitHubError as exc:
        if _is_conflict(exc):
            print(f"PR 생성이 거부됐다(HTTP {exc.status}) — 같은 브랜치의 열린 PR 을 확인한다")
            _log_existing(repo, branch, token)
        raise


# ── 모드 3: --pr-only ────────────────────────────────────────────────────
def _pr_only(args: argparse.Namespace, signals: dict) -> int:
    """조립은 끝났고 PR 만 실패했을 때 — 원격 `main` 대비 **미반영 신규분만** PR 로 올린다."""
    out = Path(args.reports_path)
    if not out.exists():
        print(f"로컬 리포트 파일이 없다: {out}", file=sys.stderr)
        return 2
    try:
        doc = _read_json(out)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"로컬 리포트를 읽을 수 없다: {out} ({exc})", file=sys.stderr)
        return 2
    if not isinstance(doc, dict) or not (doc.get("reports") or []):
        print("로컬 리포트가 비었다 — PR 을 만들지 않는다(빈 문서 PR 금지)", file=sys.stderr)
        return 1

    token = _resolve_token()
    if not token:
        print("GitHub 토큰이 없다 — " + "·".join(TOKEN_ENV_VARS) + " 중 하나를 환경변수로 넣어라"
              "(값은 출력·로그에 남기지 않는다). 조립 결과는 그대로 있다: " + str(out), file=sys.stderr)
        return 2
    repo = _resolve_repo(args.repo)
    base = (args.base or "main").strip() or "main"
    path = _reports_repo_path(out)
    try:
        existing_sha, remote_doc = _read_remote_doc(repo, path, base, token)
    except GitHubError as exc:
        print(f"원격 파일을 읽지 못했다 — {_redact(str(exc), token)}", file=sys.stderr)
        return 1
    if existing_sha and remote_doc is None:
        print("원격 리포트 내용을 읽지 못해 신규분을 가릴 수 없다 — PR 을 만들지 않는다", file=sys.stderr)
        return 1

    local_reports = [report for report in doc.get("reports") or [] if report.get("report_id")]
    remote_ids = {str(report.get("report_id") or "") for report in (remote_doc or {}).get("reports") or []}
    new_reports = [report for report in local_reports if str(report.get("report_id")) not in remote_ids]
    if not new_reports:
        print(f"원격 {base} 에 이미 모두 반영됐다(원격 {len(remote_ids)}건, 로컬 {len(local_reports)}건) "
              "— PR 을 만들지 않는다")
        return 0

    tickers: list[str] = []
    for report in new_reports:
        ticker = str(report.get("ticker") or "")
        if ticker and ticker not in tickers:
            tickers.append(ticker)
    as_of = str((doc.get("inputs") or {}).get("as_of") or signals.get("as_of") or "")
    print(f"PR 전용 — 원격 {base} 에 없는 리포트 {len(new_reports)}건: "
          f"{', '.join(str(report.get('report_id')) for report in new_reports)}")
    return _create_pr(out, doc, tickers, as_of, args,
                      new_ids=[str(report.get("report_id")) for report in new_reports])


# ── CLI ──────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Codex Cloud 대행 조립기 — 에이전트가 만든 라운드 JSON 을 검증해 리포트를 조립한다")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true", help="컨텍스트만 만든다(에이전트가 읽을 입력 패킷)")
    mode.add_argument("--assemble", action="store_true", help="라운드 JSON 을 조립한다(기본 모드)")
    mode.add_argument("--pr-only", action="store_true",
                      help="조립 없이 PR 만 — 로컬 리포트 중 원격 main 에 없는 신규분만 올린다")
    parser.add_argument("--tickers", default=None, help="쉼표 구분 종목(신호·워치리스트 안에서만)")
    parser.add_argument("--top", type=int, default=None,
                        help="신호 점수 상위 N종목(--prepare·--assemble 공통)")
    parser.add_argument("--rounds", default=None,
                        help="라운드 JSON 폴더(`{root}` 또는 `{root}/{TICKER}` — 다종목은 종목별 폴더 필수)")
    parser.add_argument("--reports", "--out", dest="reports_path", default=str(DEFAULT_REPORTS),
                        help="리포트 파일(기본 data/debate_reports.json)")
    parser.add_argument("--signals", default=str(DEFAULT_SIGNALS), help="정량 신호 파일")
    parser.add_argument("--context-dir", default=str(DEFAULT_CONTEXT_DIR),
                        help="컨텍스트·매니페스트 폴더(로컬 전용)")
    parser.add_argument("--no-fetch-body", action="store_true", help="뉴스 본문 수집(네트워크)을 하지 않는다")
    parser.add_argument("--refresh-context", action="store_true",
                        help="저장된 컨텍스트가 있어도 다시 만든다(기본은 입력 해시가 같을 때만 재사용)")
    parser.add_argument("--drop-incomplete", action="store_true",
                        help="**이번 실행에서 만든** status != complete 리포트를 파일에서 뺀다"
                             "(과거 이력은 건드리지 않는다)")
    parser.add_argument("--min-valid-skills", type=int, default=3, help="1R 정족수(기본 3)")
    parser.add_argument("--max-tickers", type=int, default=6, help="--top 의 상한(기본 6)")
    parser.add_argument("--model", default=None, help="모델 이름(리포트의 model_version)")
    parser.add_argument("--create-pr", action="store_true",
                        help="조립·파일 검증 뒤 GitHub REST 로 단일 파일 PR 을 연다(--assemble 전용)")
    parser.add_argument("--repo", default=None,
                        help=f"PR 대상 저장소(기본: origin 원격, 없으면 {DEFAULT_REPO})")
    parser.add_argument("--base", default="main", help="기준 브랜치(기본 main)")
    parser.add_argument("--pr-title", default=None, help="PR 제목(기본 `debate: {TICKERS} @ {as_of}`)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    signals_path = Path(args.signals)
    signals: dict = {}
    if not signals_path.exists():
        if not args.pr_only:
            print(f"신호 파일이 없다: {signals_path}", file=sys.stderr)
            return 2
        # `--pr-only` 는 조립을 다시 하지 않는다 — 신호 파일 없이도 PR 만 다시 올릴 수 있다.
    else:
        try:
            signals = _read_json(signals_path)
        except json.JSONDecodeError as exc:
            print(f"신호 파일을 읽을 수 없다: {signals_path} ({exc.msg})", file=sys.stderr)
            return 2

    if args.prepare:
        if args.create_pr:
            print("--create-pr 는 조립(--assemble) 모드에서만 쓴다", file=sys.stderr)
            return 2
        if not args.tickers and args.top is None:
            print("--prepare 에는 --tickers 또는 --top 이 필요하다", file=sys.stderr)
            return 2
        return _prepare(args, signals)

    if args.pr_only:
        if args.create_pr:
            print("--create-pr 는 --pr-only 와 함께 쓰지 않는다(--pr-only 가 PR 을 연다)", file=sys.stderr)
            return 2
        return _pr_only(args, signals)

    if not args.rounds:
        print("--assemble 에는 --rounds 가 필요하다(--tickers · --top · --prepare 매니페스트로 대상을 정한다)",
              file=sys.stderr)
        return 2
    return _assemble(args, signals)


if __name__ == "__main__":
    raise SystemExit(main())
