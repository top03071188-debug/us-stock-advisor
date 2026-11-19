#!/usr/bin/env python3
"""scripts/redact_quotes.py — 커밋 직전에 기사 인용 원문을 `quote_sha256` 으로 바꾼다.

[고정] 2.2.3: 공개 저장소·Pages 에는 `quote`/`body`/`body_excerpt_1500` **원문 문자열**을 넣지 않는다.
검증 결과(해시·통과 여부·글자 수)만 남긴다. 원문은 `data/debate/`(gitignore)에만 있다.

    python scripts/redact_quotes.py --mode sha256 data/quant_signals.json data/ledger/records.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import unicodedata
from pathlib import Path

QUOTE_KEYS = ("quote", "evidence_quote")
BODY_KEYS = ("body", "body_excerpt_1500", "body_excerpt")


def normalize(text: str) -> str:
    """공백·유니코드 정규화 — 같은 문장이면 같은 해시가 나오게."""
    return " ".join(unicodedata.normalize("NFKC", str(text)).split())


def quote_digest(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def redact(obj):
    """dict/list 를 재귀 순회하며 금지 키를 해시·요약으로 치환한다."""
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    if not isinstance(obj, dict):
        return obj
    out: dict = {}
    for k, v in obj.items():
        if k in QUOTE_KEYS and isinstance(v, str):
            out["quote_sha256"] = quote_digest(v)
            out.setdefault("quote_verified", True)
            out["quote_chars"] = len(v)
            continue
        if k in BODY_KEYS and isinstance(v, str):
            out["body_present"] = True
            out["body_sha256"] = quote_digest(v)
            out["body_chars"] = len(v)
            continue
        out[k] = redact(v)
    return out


def redact_file(path: Path) -> dict:
    if path.suffix == ".jsonl":
        lines = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            lines.append(json.dumps(redact(json.loads(line)), ensure_ascii=False))
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return {"file": str(path), "records": len(lines)}
    payload = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(redact(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    return {"file": str(path), "records": 1}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="sha256", choices=["sha256"])
    ap.add_argument("--check", action="store_true", help="쓰지 않고 금지 키가 남아 있는지만 검사")
    ap.add_argument("files", nargs="+")
    args = ap.parse_args(argv)
    exit_code = 0
    for f in map(Path, args.files):
        if not f.exists():
            print(f"skip(missing): {f}", file=sys.stderr)
            continue
        if args.check:
            text = f.read_text(encoding="utf-8")
            bad = [k for k in QUOTE_KEYS + BODY_KEYS if f'"{k}"' in text]
            if bad:
                print(f"{f}: forbidden keys {bad}", file=sys.stderr)
                exit_code = 1
            continue
        print(json.dumps(redact_file(f), ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
