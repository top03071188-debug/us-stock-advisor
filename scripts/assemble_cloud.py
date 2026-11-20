#!/usr/bin/env python3
"""scripts/assemble_cloud.py — Codex Cloud '에이전트 대행' 조립기 (사용 가이드 부록 A 정식판).

Codex Cloud 에는 `codex` CLI·`auth.json` 이 없어 `codex_debate/run_debate.py` 를 그대로 돌릴 수 없다.
그래서 에이전트가 라운드 JSON(`round1_*.json`·`round2_*.json`·`round3_guard.json`·`round3_chair.json`)을
직접 만들고, 이 스크립트가 **저장소 코드의 검증 함수**로 걸러 `data/debate_reports.json` 한 건을 조립한다.
호출(API) 0회 — 검증·조립은 전부 로컬 코드다.

두 가지 모드(짧은 명령 한 줄로 끝난다):

    # 1) 컨텍스트 준비 — 에이전트가 읽을 `data/debate/context/{TICKER}.json` 을 만든다(기본 본문 수집)
    python scripts/assemble_cloud.py --prepare --tickers MSFT --no-fetch-body
    python scripts/assemble_cloud.py --prepare --top 3 --no-fetch-body

    # 2) 조립 — 저장된 컨텍스트가 있으면 **그대로 재사용**하고(네트워크 재호출 없음) 리포트를 만든다
    CODEX_VERSION=cloud-agent python scripts/assemble_cloud.py --assemble \
        --tickers MSFT --rounds /tmp/rounds/MSFT --drop-incomplete

주의:
- `data/debate/context/`·`data/debate_reports.json` 은 실행 산출물이다. 커밋은 범위 가드가 막는다.
- `provider` 는 스키마 허용값(`codex|openai`)이라 `codex` 로 두고, 실행 방식은 `codex.version`(환경변수
  `CODEX_VERSION=cloud-agent`)으로 구분한다.
- 자동 하향(`auto_downgrade_enabled`)은 켜지 않는다 — 동결 전에 규칙을 바꾸는 일이 된다.
- V11 무결성 훼손(`integrity_compromised`) 종목은 **읽지도 저장하지도 않고** 건너뛴다. 지시문이 침투한
  입력을 에이전트가 읽으면 그 순간 판단 경로가 오염된다.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

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
    validate_chair,
    validate_guard,
    validate_reports_file,
    validate_round1,
    validate_round2,
)

SKILLS: tuple[str, ...] = INVESTOR_SKILLS


# ── 입출력 ────────────────────────────────────────────────────────────────
def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_doc(path: Path, doc: dict) -> None:
    """리포트 문서를 파일로 쓴다(들여쓰기 2 + 끝 개행 — 저장소의 다른 산출물과 같은 모양)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _signal_id_for(ticker: str, signals: dict) -> str:
    """`build_context` 가 붙이는 `quant_signal_id` 와 같은 규칙 — 재사용 컨텍스트의 대조 기준."""
    for signal in signals.get("signals") or []:
        if str(signal.get("ticker", "")).upper() == ticker and signal.get("signal_id"):
            return str(signal["signal_id"])
    return f"{signals.get('as_of') or ''}-{ticker}-NONE"


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
    return [], 2


# ── 라운드 파일 ──────────────────────────────────────────────────────────
def _rounds_dir(rounds_root: Path, ticker: str) -> Path:
    """`--rounds` 가 공용 루트면 `{root}/{TICKER}`, 이미 그 종목 폴더면 그대로."""
    per_ticker = rounds_root / ticker
    return per_ticker if per_ticker.is_dir() else rounds_root


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


# ── 컨텍스트 ─────────────────────────────────────────────────────────────
def _load_context(args: argparse.Namespace, ticker: str, signals: dict) -> tuple[dict, str]:
    """저장된 컨텍스트를 재사용하거나 새로 만든다. 반환: (ctx, '재사용'|'신규')."""
    path = Path(args.context_dir) / f"{ticker}.json"
    if path.exists() and not args.refresh_context:
        try:
            stored = _read_json(path)
        except json.JSONDecodeError:
            print(f"{ticker}: 저장된 컨텍스트를 읽을 수 없다({path.name}) — 다시 만든다")
        else:
            expected = _signal_id_for(ticker, signals)
            if str(stored.get("quant_signal_id") or "") == expected:
                return stored, "재사용"
            print(f"{ticker}: 저장된 컨텍스트가 현재 신호와 다르다"
                  f"({stored.get('quant_signal_id')} → {expected}) — 다시 만든다")
    return build_context(ticker, signals, fetch_body=not args.no_fetch_body), "신규"


# ── 모드 1: --prepare ────────────────────────────────────────────────────
def _prepare(args: argparse.Namespace, signals: dict) -> int:
    targets, code = _resolve_targets(args, signals)
    if code:
        return code
    if not targets:
        print("준비할 대상 종목이 없다", file=sys.stderr)
        return 2

    prepared: list[str] = []
    blocked: list[str] = []
    for ticker in targets:
        ctx = build_context(ticker, signals, fetch_body=not args.no_fetch_body)
        if integrity_compromised(ctx):
            blocked.append(ticker)
            print(f"{ticker}: V11 무결성 훼손(scope={ctx.get('injection_scope')}) — "
                  "컨텍스트를 저장하지 않고 건너뛴다")
            continue
        path = save_context(ctx, args.context_dir)
        news = ctx.get("news") or []
        with_body = sum(1 for item in news if item.get("body_available"))
        prepared.append(ticker)
        print(f"{ticker}: 컨텍스트 저장 {path} — 뉴스 {len(news)}건(본문 {with_body}건), "
              f"격리 {len(ctx.get('excluded_news') or [])}건, 8-K {len(ctx.get('filings_8k') or [])}건, "
              f"as_of {ctx.get('as_of')}")

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
        print("조립할 대상 종목이 없다", file=sys.stderr)
        return 2

    out = Path(args.reports_path)
    rounds_root = Path(args.rounds)
    cfg = RunConfig(signals_path=Path(args.signals), reports_path=out,
                    context_dir=Path(args.context_dir), min_valid_skills=args.min_valid_skills,
                    provider="codex", model=args.model, fetch_body=not args.no_fetch_body)
    doc = load_reports_doc(out, signals)
    done: list[str] = []
    blocked: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []

    for ticker in targets:
        ctx, origin = _load_context(args, ticker, signals)
        as_of = str(ctx.get("as_of") or "")
        if integrity_compromised(ctx):
            blocked.append(ticker)
            print(f"{ticker}: V11 무결성 훼손(scope={ctx.get('injection_scope')}) — 조립하지 않는다")
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

        rdir = _rounds_dir(rounds_root, ticker)
        if not rdir.is_dir():
            failed.append(ticker)
            print(f"{ticker}: 라운드 폴더가 없다 — {rdir}", file=sys.stderr)
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

        report = assemble_validated(
            ctx, round1=round1, round2=round2, guard=guard, chair=chair,
            min_valid_skills=cfg.min_valid_skills,
            auto_downgrade_enabled=False,
            provider="codex",                         # 스키마 허용값은 codex|openai 뿐이다
            report_seq=_report_seq(doc, ticker, as_of),
            validated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            model_version=cfg.model,
        )
        upsert_report(doc, report)
        write_reports_doc(out, doc, signals, ctx, cfg)  # codex.version ← CODEX_VERSION 환경변수
        done.append(ticker)
        print(json.dumps({"ticker": ticker, "report_id": report["report_id"], "status": report["status"],
                          "skills_kept": report["validation"]["skills_kept"], "news_state": report["news_state"],
                          "late": report["late"], "invalid": report["invalid"],
                          "context": origin}, ensure_ascii=False))

    if args.drop_incomplete:
        reports = doc.get("reports") or []
        dropped = [report for report in reports if report.get("status") != "complete"]
        if dropped:
            doc["reports"] = [report for report in reports if report.get("status") == "complete"]
            _write_doc(out, doc)
            print(f"완주하지 못한 리포트 {len(dropped)}건을 뺐다: "
                  f"{', '.join(str(report.get('report_id')) for report in dropped)}")

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
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Codex Cloud 대행 조립기 — 에이전트가 만든 라운드 JSON 을 검증해 리포트를 조립한다")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true", help="컨텍스트만 만든다(에이전트가 읽을 입력 패킷)")
    mode.add_argument("--assemble", action="store_true", help="라운드 JSON 을 조립한다(기본 모드)")
    parser.add_argument("--tickers", default=None, help="쉼표 구분 종목(신호·워치리스트 안에서만)")
    parser.add_argument("--top", type=int, default=None, help="--prepare 에서 신호 점수 상위 N종목")
    parser.add_argument("--rounds", default=None, help="라운드 JSON 폴더(`{root}` 또는 `{root}/{TICKER}`)")
    parser.add_argument("--reports", "--out", dest="reports_path", default=str(DEFAULT_REPORTS),
                        help="리포트 파일(기본 data/debate_reports.json)")
    parser.add_argument("--signals", default=str(DEFAULT_SIGNALS), help="정량 신호 파일")
    parser.add_argument("--context-dir", default=str(DEFAULT_CONTEXT_DIR), help="컨텍스트 폴더(로컬 전용)")
    parser.add_argument("--no-fetch-body", action="store_true", help="뉴스 본문 수집(네트워크)을 하지 않는다")
    parser.add_argument("--refresh-context", action="store_true",
                        help="저장된 컨텍스트가 있어도 다시 만든다(기본은 재사용)")
    parser.add_argument("--drop-incomplete", action="store_true",
                        help="status != complete 인 리포트를 파일에서 뺀다(PR 에 남기지 않는다)")
    parser.add_argument("--min-valid-skills", type=int, default=3, help="1R 정족수(기본 3)")
    parser.add_argument("--max-tickers", type=int, default=6, help="--top 의 상한(기본 6)")
    parser.add_argument("--model", default=None, help="모델 이름(리포트의 model_version)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    signals_path = Path(args.signals)
    if not signals_path.exists():
        print(f"신호 파일이 없다: {signals_path}", file=sys.stderr)
        return 2
    try:
        signals = _read_json(signals_path)
    except json.JSONDecodeError as exc:
        print(f"신호 파일을 읽을 수 없다: {signals_path} ({exc.msg})", file=sys.stderr)
        return 2

    if args.prepare:
        if not args.tickers and args.top is None:
            print("--prepare 에는 --tickers 또는 --top 이 필요하다", file=sys.stderr)
            return 2
        return _prepare(args, signals)

    if not args.tickers or not args.rounds:
        print("--assemble 에는 --tickers 와 --rounds 가 필요하다", file=sys.stderr)
        return 2
    return _assemble(args, signals)


if __name__ == "__main__":
    raise SystemExit(main())
