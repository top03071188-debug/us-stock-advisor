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

    # 3) 조립 + PR — 검증을 통과하면 GitHub REST(HTTPS)로 단일 파일 PR 까지 연다(git push·gh 불필요)
    CODEX_VERSION=cloud-agent python scripts/assemble_cloud.py --assemble \
        --tickers MSFT --rounds /tmp/rounds/MSFT --drop-incomplete --create-pr

`--create-pr` 는 `data/debate_reports.json` **한 파일**만 담은 브랜치를 만들어 PR 을 연다.
Cloud 에서 `git push`·`gh` 가 403 이어도 HTTP 시크릿(Network secret)이 `Authorization: Bearer …`
자리의 placeholder 를 실제 토큰으로 바꿔 주므로 api.github.com 만으로 끝난다. 토큰은
`GH_PAT`·`GH_TOKEN`·`GITHUB_TOKEN` 순서로 찾고 **출력·로그·에러 메시지에 남기지 않는다**.

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
import base64
import json
import os
import re
import subprocess
import sys
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
    validate_chair,
    validate_guard,
    validate_reports_file,
    validate_round1,
    validate_round2,
)

SKILLS: tuple[str, ...] = INVESTOR_SKILLS

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
            # [수정 A] 검증 시각은 `completed_at`(조립 시각)과 같은 순간이다. 초 단위로 절사한
            # 별도 값을 넘기면 완료 시각의 마이크로초가 잘려 `validated < completed`(0.53초 역전)가
            # 되고 트랙 1 이 `VALIDATED_BEFORE_COMPLETED` 로 편입을 거부한다. `None` 이면
            # `assemble_validated` 가 `completed` 와 **같은 타임스탬프**를 쓴다.
            validated_at=None,
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
    if args.create_pr:
        as_of = str(signals.get("as_of") or (doc.get("inputs") or {}).get("as_of") or "")
        return _create_pr(out, doc, done, as_of, args)
    return 0


# ── GitHub PR (--create-pr) ──────────────────────────────────────────────
class GitHubError(RuntimeError):
    """GitHub REST 호출 실패. 메시지에 **토큰·요청 헤더를 담지 않는다**(로그로 새지 않게)."""


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


def _branch_name(as_of: str, tickers: Sequence[str]) -> str:
    """`debate/{as_of}-{tickers_slug}-{utc_hhmmss}` — 같은 날 재실행해도 브랜치가 겹치지 않는다."""
    def _slug(text: str) -> str:
        return re.sub(r"[^A-Za-z0-9-]+", "-", text).strip("-").lower()

    stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    return f"debate/{_slug(as_of) or 'unknown'}-{_slug('-'.join(tickers)) or 'reports'}-{stamp}"


def _pr_body(doc: dict, tickers: Sequence[str], as_of: str, path: str) -> str:
    """조립된 리포트 목록을 PR 본문 마크다운으로 — 판정·상태·건수뿐(원문·계좌 정보 없음)."""
    wanted = set(tickers)
    rows = [row for row in doc.get("reports") or [] if str(row.get("ticker") or "") in wanted]
    lines = [
        f"Codex Cloud 대행 조립 — `{path}` 갱신 ({as_of})",
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
         allow_404: bool = False) -> dict | None:
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
        raise GitHubError(f"{method} {_endpoint(path)} — HTTP {response.status_code}{_error_detail(response)}")
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _create_pr(out: Path, doc: dict, tickers: Sequence[str], as_of: str, args: argparse.Namespace) -> int:
    """검증을 통과한 리포트 파일 하나로 브랜치를 만들어 PR 을 연다(성공 시 `PR_CREATED` 출력)."""
    token = _resolve_token()
    if not token:
        print("GitHub 토큰이 없다 — " + "·".join(TOKEN_ENV_VARS) + " 중 하나를 환경변수로 넣어라"
              "(값은 출력·로그에 남기지 않는다). 조립 결과는 저장·검증된 상태다: " + str(out), file=sys.stderr)
        return 2

    repo = _resolve_repo(args.repo)
    base = (args.base or "main").strip() or "main"
    path = _reports_repo_path(out)
    title = (args.pr_title or "").strip() or f"debate: {', '.join(tickers)} @ {as_of}"
    branch = _branch_name(as_of, tickers)
    print(f"PR 준비 — repo {repo}, base {base}, branch {branch}, file {path}")

    try:
        ref = _api("GET", f"/repos/{repo}/git/ref/heads/{base}", token) or {}
        base_sha = str((ref.get("object") or {}).get("sha") or "")
        if not base_sha:
            raise GitHubError(f"기준 브랜치 SHA 를 찾지 못했다 — {repo}@{base}")

        existing = _api("GET", f"/repos/{repo}/contents/{path}?ref={quote(base, safe='')}",
                        token, allow_404=True)
        existing_sha = str((existing or {}).get("sha") or "") or None

        _api("POST", f"/repos/{repo}/git/refs", token,
             payload={"ref": f"refs/heads/{branch}", "sha": base_sha})

        commit: dict[str, Any] = {"message": title, "branch": branch,
                                  "content": base64.b64encode(out.read_bytes()).decode("ascii")}
        if existing_sha:
            commit["sha"] = existing_sha
        _api("PUT", f"/repos/{repo}/contents/{path}", token, payload=commit)

        pr = _api("POST", f"/repos/{repo}/pulls", token,
                  payload={"title": title, "head": branch, "base": base,
                           "body": _pr_body(doc, tickers, as_of, path)}) or {}
    except GitHubError as exc:
        print(f"PR 생성 실패 — {_redact(str(exc), token)}", file=sys.stderr)
        print(f"조립 결과는 그대로 남아 있다(로컬 검증 통과): {out}", file=sys.stderr)
        return 1

    url = str(pr.get("html_url") or "")
    number = pr.get("number")
    if not url:
        print(f"PR 응답에 html_url 이 없다 — 브랜치 {branch} 의 PR 생성 여부를 확인하라", file=sys.stderr)
        return 1
    print(f"PR_CREATED: #{number} {url}" if number else f"PR_CREATED: {url}")
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
    if not signals_path.exists():
        print(f"신호 파일이 없다: {signals_path}", file=sys.stderr)
        return 2
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

    if not args.tickers or not args.rounds:
        print("--assemble 에는 --tickers 와 --rounds 가 필요하다", file=sys.stderr)
        return 2
    return _assemble(args, signals)


if __name__ == "__main__":
    raise SystemExit(main())
