#!/usr/bin/env python3
"""scripts/check_commit_scope.py — 커밋·배포 범위 가드 (2.2.3, 2.2.4).

`--staged`: `git diff --cached --name-only` 로 스테이징된 파일만 검사한다.
`--paths`: 지정한 파일을 검사한다(테스트·수동 점검용).

검사 항목:
1) 금지 경로(`data/raw/`, `data/debate/`, `.env`, `local/` …)
2) 금지 키(`quote`, `body_excerpt_1500`, `avg_cost`, `holdings` …) — JSON 키 기준
3) 금지 패턴(이메일·GitHub 토큰) — 값 안에서 탐지
4) `.env` 추적 여부
하나라도 걸리면 exit 1.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ALLOWLIST = Path("config/public_allowlist.yaml")
TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".yaml", ".yml", ".py", ".ts", ".tsx", ".js", ".txt", ".html", ".css"}


def load_rules(path: Path = ALLOWLIST) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def staged_paths() -> list[str]:
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
                         capture_output=True, text=True, check=True).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def tracked_env() -> list[str]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=False).stdout
    return [line for line in out.splitlines() if Path(line).name in (".env", "auth.json")]


def _iter_keys(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _iter_keys(v, f"{prefix}.{k}")
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_keys(v, prefix)


def check_text(path: Path, text: str, rules: dict) -> list[str]:
    problems: list[str] = []
    deny_keys = set(rules.get("deny_keys", []))
    patterns = [re.compile(p) for p in rules.get("deny_patterns", [])]

    data = None
    if path.suffix in (".json", ".jsonl"):
        try:
            if path.suffix == ".jsonl":
                data = [json.loads(line) for line in text.splitlines() if line.strip()]
            else:
                data = json.loads(text)
        except json.JSONDecodeError:
            problems.append(f"{path}: JSON 파싱 실패")
    if data is not None:
        keys = set(_iter_keys(data))
        bad = sorted(keys & deny_keys)
        if bad:
            problems.append(f"{path}: forbidden keys {bad}")

    for pat in patterns:
        for m in pat.finditer(text):
            token = m.group(0)
            if token.endswith(("@example.com", "@example.org")):
                continue  # .env.example 의 플레이스홀더는 개인정보가 아니다
            problems.append(f"{path}: forbidden pattern {pat.pattern[:32]}…({token[:6]}…)")
            break
    return problems


def check_paths(paths: list[str], rules: dict | None = None) -> list[str]:
    rules = rules or load_rules()
    deny_paths = rules.get("deny_paths", [])
    problems: list[str] = []
    for p in paths:
        path = Path(p)
        norm = p.replace("\\", "/")
        for dp in deny_paths:
            if dp.endswith("/"):
                if norm.startswith(dp):
                    problems.append(f"{p}: forbidden path ({dp})")
            else:
                d = dp.rstrip("/")
                if norm == d or norm.endswith("/" + d):
                    problems.append(f"{p}: forbidden path ({dp})")
        if path.name == ".env" or path.name == "auth.json":
            problems.append(f"{p}: secret file must never be committed")
        if path.exists() and path.is_file() and path.suffix in TEXT_SUFFIXES:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            problems.extend(check_text(path, text, rules))
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--paths", nargs="*", default=None)
    ap.add_argument("--allowlist", default=str(ALLOWLIST))
    args = ap.parse_args(argv)

    rules = load_rules(Path(args.allowlist))
    paths = args.paths if args.paths is not None else (staged_paths() if args.staged else [])
    problems = check_paths(paths, rules)
    problems += [f"{p}: .env/auth.json is tracked" for p in tracked_env()]
    if problems:
        for p in problems:
            print(f"ERROR {p}", file=sys.stderr)
        return 1
    print(f"scope ok ({len(paths)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
