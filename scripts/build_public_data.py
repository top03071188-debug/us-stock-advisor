#!/usr/bin/env python3
"""scripts/build_public_data.py — 데이터 계약 → Pages 용 공개 JSON(허용 목록만).

[고정] 2.2.4 / 5.7
- 점수·등급·요약·지표·메타·url·quote_sha256 만 내보낸다.
- 원시 OHLC 시계열·뉴스 본문 발췌·보유 수량·평단·계좌는 **한 키도** 내보내지 않는다.
- 금지 키가 산출물에 남으면 **빌드 실패**(조용히 지우지 않는다 — 지우면 원본에 있다는 사실을 놓친다).

    python scripts/build_public_data.py --signals data/quant_signals.json --folds data/walkforward/folds/summary.json --out web/data
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

DEFAULT_ALLOWLIST = Path("config/public_allowlist.yaml")

#: 항상 허용하는 구조 키(컨테이너·식별자). 값이 아니라 구조라서 통과시킨다.
STRUCTURAL_KEYS = {
    "signals", "blocked", "watchlist", "reports", "items", "folds", "horizons",
    "ticker", "signal_id", "report_id", "event", "events", "code", "id", "name",
    "reason", "reasons", "gates", "components", "notes", "note", "list",
    "regime_votes", "inputs_missing", "eligible_folds", "blocked_horizons",
    "watchlist_count", "count", "as_of_date", "horizon",
}


def load_allowlist(path: Path = DEFAULT_ALLOWLIST) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _allowed(key: str, allow: set[str]) -> bool:
    return key in allow or key in STRUCTURAL_KEYS


def filter_public(obj, allow: set[str]):
    if isinstance(obj, dict):
        return {k: filter_public(v, allow) for k, v in obj.items() if _allowed(k, allow)}
    if isinstance(obj, list):
        return [filter_public(v, allow) for v in obj]
    return obj


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def assert_no_forbidden(obj, rules: dict) -> None:
    deny = set(rules.get("deny_keys", []))
    bad = sorted(set(_keys(obj)) & deny)
    if bad:
        raise SystemExit(f"빌드 실패: 공개 산출물에 금지 키가 남아 있다: {bad}")
    text = json.dumps(obj, ensure_ascii=False)
    for pat in rules.get("deny_patterns", []):
        if re.search(pat, text):
            raise SystemExit(f"빌드 실패: 공개 산출물에서 금지 패턴 탐지({pat[:24]}…)")


def build(signals_payload: dict | None, folds_payload: dict | None, records: list[dict] | None,
          rules: dict) -> dict:
    allow = set(rules.get("allow", []))
    out: dict[str, dict] = {}

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params_version": (signals_payload or {}).get("params_version"),
        "snapshot_id": (signals_payload or {}).get("snapshot_id"),
        "status": (signals_payload or {}).get("status", "normal"),
        "regime": (signals_payload or {}).get("regime"),
        "data_cutoff_at": (signals_payload or {}).get("data_cutoff_at"),
        "signal_published_at": (signals_payload or {}).get("signal_published_at"),
        "order_cutoff_at": (signals_payload or {}).get("order_cutoff_at"),
        "debate_status": "ok" if (signals_payload or {}).get("debate_generated_at") else "none",
    }
    out["meta.json"] = filter_public(meta, allow)

    if signals_payload:
        today = {
            "as_of": signals_payload.get("as_of"),
            "signals": signals_payload.get("signals", []),
            "blocked": signals_payload.get("blocked", []),
            "watchlist": signals_payload.get("watchlist_count", len(signals_payload.get("watchlist", []))),
            "regime": signals_payload.get("regime"),
            "notes": "오늘 신호 없음도 정상 결과입니다." if not signals_payload.get("signals") else None,
        }
        out["signals/today.json"] = filter_public(today, allow)
        out["watchlist.json"] = filter_public(
            {"as_of": signals_payload.get("as_of"), "watchlist": signals_payload.get("watchlist", [])}, allow
        )
        if signals_payload.get("ledger_summary"):
            out["ledger/summary.json"] = filter_public(signals_payload["ledger_summary"], allow)

    if folds_payload:
        out["folds.json"] = filter_public(folds_payload, allow)

    if records is not None:
        out["ledger/records.json"] = filter_public({"records": records[-200:]}, allow)

    for name, payload in out.items():
        assert_no_forbidden(payload, rules)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", default="data/quant_signals.json")
    ap.add_argument("--folds", default="data/walkforward/folds/summary.json")
    ap.add_argument("--records", default="data/ledger/records.jsonl")
    ap.add_argument("--out", default="web/data")
    ap.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST))
    args = ap.parse_args(argv)

    rules = load_allowlist(Path(args.allowlist))
    signals = json.loads(Path(args.signals).read_text(encoding="utf-8")) if Path(args.signals).exists() else None
    folds = json.loads(Path(args.folds).read_text(encoding="utf-8")) if Path(args.folds).exists() else None
    records = None
    rp = Path(args.records)
    if rp.exists():
        records = [json.loads(line) for line in rp.read_text(encoding="utf-8").splitlines() if line.strip()]

    out = build(signals, folds, records, rules)
    root = Path(args.out)
    for name, payload in out.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"written": [str(root / n) for n in out]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
