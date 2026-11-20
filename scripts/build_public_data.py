#!/usr/bin/env python3
"""scripts/build_public_data.py — 데이터 계약 → Pages 용 공개 JSON(허용 목록만).

[고정] 2.2.4 / 5.7
- 점수·등급·요약·지표·메타·url·quote_sha256 만 내보낸다.
- 원시 OHLC 시계열·뉴스 본문 발췌·보유 수량·평단·계좌는 **한 키도** 내보내지 않는다.
- 금지 키가 산출물에 남으면 **빌드 실패**(조용히 지우지 않는다 — 지우면 원본에 있다는 사실을 놓친다).
- 파일 하나가 200KB 를 넘으면 실패한다(5.6 로딩 규칙).

호출 계약
    daily-postclose.yml : python scripts/build_public_data.py            (인자 없음 → 기본값)
    deploy.yml          : python scripts/build_public_data.py --signals data/quant_signals.json \\
                            --folds data/walkforward/folds/summary.json --out web/data

산출물(5.7)
    meta.json · signals/today.json · tickers/{TICKER}.json · ledger/summary.json ·
    ledger/records.json · ledger/portfolio.json · radar/13f.json · config.json
    (+ 하위 호환: folds.json, watchlist.json)
    `data/debate_reports.json` 이 없으면 `debate/*` 를 만들지 않는다(화면은 "토론 없음").
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
DEFAULT_PARAMS = Path("config/params_v001.yaml")
DEFAULT_VETO = Path("config/veto_rules.yaml")
MAX_BYTES = 200 * 1024

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


def assert_no_forbidden(obj, rules: dict, name: str = "") -> None:
    deny = set(rules.get("deny_keys", []))
    bad = sorted(set(_keys(obj)) & deny)
    if bad:
        raise SystemExit(f"빌드 실패: {name} 에 금지 키가 남아 있다: {bad}")
    text = json.dumps(obj, ensure_ascii=False)
    for pat in rules.get("deny_patterns", []):
        if re.search(pat, text):
            raise SystemExit(f"빌드 실패: {name} 에서 금지 패턴 탐지({pat[:24]}…)")


def _json_size(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


# ────────────────────────────── 각 파일 ──────────────────────────────
def build_meta(signals: dict | None, debate: dict | None) -> dict:
    signals = signals or {}
    regime = signals.get("regime") or {}
    source = signals.get("source")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "params_version": signals.get("params_version"),
        "snapshot_id": signals.get("snapshot_id"),
        "status": signals.get("status", "awaiting_first_run"),
        "mode": signals.get("mode"),
        "source": None if source in (None, "none") else source,
        "regime": regime.get("regime"),
        "regime_verified": regime.get("verified"),
        "regime_multiplier": regime.get("multiplier"),
        "data_cutoff_at": signals.get("data_cutoff_at"),
        "signal_published_at": signals.get("signal_published_at"),
        "order_cutoff_at": signals.get("order_cutoff_at"),
        "as_of": signals.get("as_of"),
        "prices_as_of": signals.get("prices_as_of"),
        "debate_generated_at": (debate or {}).get("generated_at"),
        "debate_status": "ok" if debate else "none",
        "note_ko": {
            "awaiting_first_run": "아직 실전 배치가 한 번도 돌지 않았습니다 — 아래 신호는 비어 있는 것이 정상입니다.",
            "sample": "샘플(합성) 데이터입니다 — 실전 신호가 아닙니다.",
            "degraded": "일부 데이터 소스가 실패했습니다 — 신선도를 확인하세요.",
            "stale": "데이터가 지연되고 있습니다.",
        }.get(signals.get("status", ""), None),
    }


SIGNAL_PUBLIC_FIELDS = (
    "signal_id", "ticker", "type", "trade_grade", "grade", "entry_score", "decision",
    "as_of", "data_cutoff_at", "signal_published_at", "order_cutoff_at", "generated_price",
    "quant_intent", "thesis_ko", "valuation", "indicators", "investor_scores", "components",
    "conflict", "news_state", "position", "target_band", "expected_mdd", "invalidation",
    "gate_summary", "sizing_note_ko", "params_version", "snapshot_id", "track", "veto",
    "news", "filings", "risk", "evidence", "entry_plan",
)


def public_signal(record: dict) -> dict:
    out = {k: record[k] for k in SIGNAL_PUBLIC_FIELDS if k in record}
    out["confidence_label_ko"] = {"A": "A 데이터·근거 충족", "B": "B 참고", "C": "C 근거 약함"}.get(
        str(record.get("grade") or ""), "—")
    out["news_badge_ko"] = {"not_read": "뉴스 미판독", "insufficient_evidence": "뉴스 근거 부족",
                            "read_done": None}.get(str(record.get("news_state") or ""), None)
    return out


def build_today(signals: dict | None) -> dict:
    signals = signals or {}
    rows = [public_signal(s) for s in signals.get("signals") or []]
    blocked = [{"ticker": b.get("ticker"), "reason": b.get("reason"), "codes": b.get("codes"),
                "message_ko": b.get("message_ko"), "grade": b.get("grade")}
               for b in signals.get("blocked") or []]
    counts = {"buy": 0, "sell": 0, "watch": 0}
    for row in rows:
        key = "sell" if str(row.get("type", "")).upper().startswith("SELL") else (
            "buy" if str(row.get("decision") or row.get("type")) == "BUY" else "watch")
        counts[key] += 1
    return {
        "as_of": signals.get("as_of"),
        "status": signals.get("status", "awaiting_first_run"),
        "regime": signals.get("regime"),
        "signals": rows,
        "blocked": blocked,
        "counts": counts,
        "watchlist_count": signals.get("watchlist_count", len(signals.get("watchlist") or [])),
        "params_version": signals.get("params_version"),
        "published_at": signals.get("signal_published_at"),
        "notes": ("오늘 신호 없음 — 기준을 넘은 종목이 없습니다(정상)." if not rows else None),
        "blocked_note_ko": (None if not blocked else f"차단 {len(blocked)}건 — 안 산 이유도 정보입니다."),
    }


def build_ticker(signals: dict | None, ticker: str, watch_row: dict | None = None) -> dict:
    signals = signals or {}
    rows = [s for s in signals.get("signals") or [] if s.get("ticker") == ticker]
    record = public_signal(rows[0]) if rows else None
    chart = (signals.get("charts") or {}).get(ticker)
    checks = next((c for c in signals.get("position_checks") or [] if c.get("ticker") == ticker), None)
    return {
        "ticker": ticker,
        "as_of": signals.get("as_of"),
        "status": signals.get("status"),
        "watchlist": {k: (watch_row or {}).get(k) for k in ("ticker", "sector", "q", "v", "g", "rank_score")},
        "signal": record,
        "last_signal": None if record else "이 종목에 오늘 새 신호 없음 — 아래 지표·차트는 유지됩니다.",
        "chart": chart,
        "indicators": (record or {}).get("indicators"),
        "investor_scores": (record or {}).get("investor_scores") or {},
        "valuation": (record or {}).get("valuation"),
        "news": (record or {}).get("news") or [],
        "filings": (record or {}).get("filings") or [],
        "risk": (record or {}).get("risk") or {"veto": []},
        "invalidation": (record or {}).get("invalidation"),
        "gate_summary": (record or {}).get("gate_summary"),
        "debate": None,
        "debate_note_ko": "토론 없음 — PC에서 `python codex_debate/run_debate.py --tickers "
                          f"{ticker}` 로 돌릴 수 있습니다.",
        "position_check": None if not checks else {
            "verdict": checks.get("verdict"), "exit_score": checks.get("exit_score"),
            "triggers": checks.get("triggers"), "reason_ko": checks.get("reason_ko"),
        },
        "params_version": signals.get("params_version"),
    }


def build_ledger(signals: dict | None, records: list[dict] | None, portfolio: dict | None) -> dict:
    signals = signals or {}
    summary = signals.get("ledger_summary") or {}
    return {
        "schema_version": summary.get("schema_version", 1),
        "as_of": summary.get("as_of") or signals.get("as_of"),
        "n_signals": summary.get("n_signals"),
        "n_graded_3m": summary.get("n_graded_3m"),
        "accuracy": summary.get("accuracy") or {"label_ko": "신호 정확도"},
        "strategy": summary.get("strategy") or {"label_ko": "전략(모의 계좌)"},
        "horizons": summary.get("horizons") or {},
        "folds": summary.get("folds") or {},
        "pilot": summary.get("pilot") or {"verdict": "보류", "reason_ko": "파일럿 지표 미산출"},
        "params_history": summary.get("params_history") or [],
        "signal_history": (summary.get("signal_history") or [])[:200],
        "graded_sample": (summary.get("graded_sample") or [])[:20],
        "settlement": summary.get("settlement") or {},
        "holdout": summary.get("holdout") or {},
        "debate_effect": summary.get("debate_effect") or {
            "pairs": 0, "net_pp": None, "verdict": "표본 0건 — 판정 보류"},
        "portfolio": portfolio or (signals.get("portfolio") or {}),
        "records": records or [],
        "note_ko": "신호 정확도와 계좌 성과는 다른 질문이다 — 따로 읽는다(4.12).",
    }


def build_records(signals: dict | None, records: list[dict] | None) -> dict:
    signals = signals or {}
    actions = {}
    for row in (signals.get("ledger_summary") or {}).get("graded_sample") or []:
        actions[row.get("signal_id")] = row.get("action")
    rows = []
    for record in (records or [])[-200:]:
        rows.append({
            "signal_id": record.get("signal_id"), "ticker": record.get("ticker"),
            "as_of": record.get("as_of"), "type": record.get("type"), "grade": record.get("grade"),
            "trade_grade": record.get("trade_grade"), "entry_score": record.get("entry_score"),
            "quant_intent": record.get("quant_intent"), "action": actions.get(record.get("signal_id")),
            "decision": record.get("decision"), "thesis_ko": record.get("thesis_ko"),
            "target_band": record.get("target_band"), "position": record.get("position"),
            "params_version": record.get("params_version"),
            "signal_published_at": record.get("signal_published_at"),
        })
    return {"as_of": signals.get("as_of"), "count": len(rows), "records": rows}


def build_portfolio(signals: dict | None, state: dict | None) -> dict:
    signals = signals or {}
    portfolio = state or signals.get("portfolio") or {}
    return {
        "as_of": portfolio.get("as_of") or signals.get("as_of"),
        "cash_pct": portfolio.get("cash_pct"),
        "invested_pct": portfolio.get("invested_pct"),
        "equity_index": portfolio.get("equity_index"),
        "n_positions": portfolio.get("n_positions", len(portfolio.get("positions") or [])),
        "positions": [{"ticker": p.get("ticker"), "weight": p.get("weight"), "sector": p.get("sector"),
                       "opened_at": p.get("opened_at")} for p in portfolio.get("positions") or []],
        "sector_weights": portfolio.get("sector_weights") or {},
        "note_ko": portfolio.get("note_ko") or "모의 계좌 — 비중만 공개한다.",
    }


def _pct(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return str(value)


def _num(value) -> str:
    return "—" if value is None else f"{value}"


def _rule(key: str, label: str, value: str) -> dict:
    return {"key": key, "label_ko": label, "value": value}


def build_config(params: dict | None, veto: dict | None) -> dict:
    """화면의 '현재 규칙' — 값은 **문자열로 렌더**해서 공개 필드를 좁게 유지한다.

    파라미터 dict 를 그대로 내보내면 수십 개의 내부 키가 공개 목록에 필요해진다.
    화면이 보여 줄 것만 `{key, label_ko, value}` 행으로 만들어 허용 목록을 작게 유지한다.
    """
    params = params or {}
    veto = veto or {}
    weights = params.get("entry_weights") or {}
    gates = params.get("entry_gates") or {}
    margins = gates.get("margin_of_safety") or {}
    grades = params.get("grades") or {}
    exit_cfg = params.get("exit") or {}
    risk = params.get("risk") or {}
    scaling = params.get("scaling") or {}
    universe = params.get("universe") or {}

    tranches = scaling.get("entry") or [0.40, 0.30, 0.30]
    drops = scaling.get("entry_drops") or [0.0, -0.07, -0.14]
    groups = [
        {"name": "매수 가중치", "items": [
            _rule("quality", "품질 Q", _pct(weights.get("quality"))),
            _rule("valuation", "가치 V", _pct(weights.get("valuation"))),
            _rule("timing", "타이밍 T", _pct(weights.get("timing"))),
            _rule("macro", "거시 M", _pct(weights.get("macro"))),
            _rule("growth", "성장 G", _pct(weights.get("growth"))),
        ]},
        {"name": "진입 임계", "items": [
            _rule("margin_risk_on", "안전마진 · Risk-On", _pct(margins.get("risk_on"))),
            _rule("margin_neutral", "안전마진 · Neutral", _pct(margins.get("neutral"))),
            _rule("margin_risk_off", "안전마진 · Risk-Off", _pct(margins.get("risk_off"))),
            _rule("timing_min", "타이밍 최소 T", _num(gates.get("timing_min"))),
            _rule("dip_conditions_required", "눌림 조건 개수", _num(gates.get("dip_conditions_required"))),
            _rule("rsi_buy", "RSI 매수 기준", _num(gates.get("rsi_buy"))),
            _rule("dma50_deviation", "50일선 이격", _pct(gates.get("dma50_deviation"))),
            _rule("dma200_mult", "200일선 하한 배수", _num(gates.get("dma200_mult"))),
            _rule("structural_risk_block", "구조적 리스크 차단", _num(gates.get("structural_risk_block"))),
        ]},
        {"name": "등급 컷", "items": [
            _rule("strong_buy", "STRONG_BUY", _num(grades.get("strong_buy"))),
            _rule("buy", "BUY", _num(grades.get("buy"))),
            _rule("watch", "WATCH", _num(grades.get("watch"))),
        ]},
        {"name": "청산", "items": [
            _rule("margin_overheat", "안전마진 과열", _pct(exit_cfg.get("margin_overheat"))),
            _rule("rsi_overheat", "RSI 과열", _num(exit_cfg.get("rsi_overheat"))),
            _rule("pe_percentile", "밸류 백분위 상한", _pct(exit_cfg.get("pe_percentile"))),
            _rule("exit_score", "exit_score 청산", _num(exit_cfg.get("exit_score"))),
            _rule("trim_score", "exit_score 축소", _num(exit_cfg.get("trim_score"))),
        ]},
        {"name": "리스크", "items": [
            _rule("risk_per_trade", "1회 계획 손실", _pct(risk.get("risk_per_trade"))),
            _rule("max_position", "종목 상한", _pct(risk.get("max_position"))),
            _rule("max_sector", "섹터 상한", _pct(risk.get("max_sector"))),
            _rule("cash_min", "최소 현금", _pct(risk.get("cash_min"))),
            _rule("cash_min_risk_off", "최소 현금 · Risk-Off", _pct(risk.get("cash_min_risk_off"))),
            _rule("stop_atr_mult", "ATR 배수(손절)", _num(risk.get("stop_atr_mult"))),
            _rule("stop_max", "손절 최대", _pct(risk.get("stop_max"))),
        ]},
        {"name": "분할", "items": [
            _rule("tranches", "1·2·3차 비중", " / ".join(_pct(t) for t in tranches)),
            _rule("tranche_drops", "2·3차 지정가 하락", " / ".join(_pct(d) for d in drops)),
        ]},
        {"name": "유니버스", "items": [
            _rule("index", "지수", str(universe.get("index") or "—")),
            _rule("min_market_cap_usd", "최소 시총", f"${float(universe.get('min_market_cap_usd') or 0):,.0f}"),
            _rule("min_adv20_usd", "최소 20일 거래대금", f"${float(universe.get('min_adv20_usd') or 0):,.0f}"),
            _rule("watchlist_max", "워치리스트 상한", _num(universe.get("watchlist_max"))),
        ]},
    ]
    rules = [{k: r.get(k) for k in ("code", "name", "group", "message_ko")} for r in veto.get("rules") or []]
    return {
        "params_version": params.get("version"),
        "created_at": params.get("created_at"),
        "groups": groups,
        "veto_rules": rules,
        "note_ko": "화면의 '현재 규칙' — 보정은 분기 PR 로만 반영한다.",
    }


def build_folds(folds: dict | None) -> dict:
    return folds or {}


def build_radar(radar: dict | None) -> dict:
    radar = radar or {}
    return {
        "as_of": radar.get("as_of"),
        "watermark_ko": radar.get("watermark_ko", "45일 전 분기말 기준"),
        "period_end": radar.get("period_end"),
        "lag_days": radar.get("lag_days", 45),
        "stale": radar.get("stale", True),
        "stale_days": radar.get("stale_days"),
        "filers": radar.get("filers") or [],
        "n_filers": radar.get("n_filers", len(radar.get("filers") or [])),
        "counts": radar.get("counts") or {"new": 0, "increase": 0, "reduce": 0, "exit": 0},
        "watchlist_overlap": radar.get("watchlist_overlap") or [],
        "note_ko": radar.get("note_ko"),
    }


def build_watchlist(watch: dict | None) -> dict:
    watch = watch or {}
    return {
        "as_of": watch.get("as_of"),
        "count": watch.get("count", len(watch.get("watchlist") or [])),
        "watchlist": [{k: w.get(k) for k in ("ticker", "sector", "q", "v", "g", "rank_score",
                                             "valuation_suspended")}
                      for w in watch.get("watchlist") or []],
        "entered": [{k: e.get(k) for k in ("ticker", "sector", "rank_score", "reason_ko")}
                    for e in watch.get("entered") or []],
        "exited": [{k: e.get(k) for k in ("ticker", "sector", "reason_ko")}
                   for e in watch.get("exited") or []],
        "valuation_suspended": [{"ticker": s.get("ticker"), "sector": s.get("sector"),
                                 "reason": s.get("reason")} for s in watch.get("valuation_suspended") or []],
        "excluded": [{"ticker": e.get("ticker"), "reasons": e.get("reasons")} for e in watch.get("excluded") or []],
    }


def build(signals: dict | None, folds: dict | None, records: list[dict] | None, rules: dict,
          *, radar: dict | None = None, portfolio_state: dict | None = None,
          watch: dict | None = None, params: dict | None = None, veto: dict | None = None) -> dict:
    out: dict[str, dict] = {}
    out["meta.json"] = build_meta(signals, None)
    out["signals/today.json"] = build_today(signals)
    out["ledger/summary.json"] = build_ledger(signals, records, portfolio_state)
    out["ledger/records.json"] = build_records(signals, records)
    out["ledger/portfolio.json"] = build_portfolio(signals, portfolio_state)
    out["radar/13f.json"] = build_radar(radar)
    out["config.json"] = build_config(params, veto)
    out["folds.json"] = build_folds(folds)
    out["watchlist.json"] = build_watchlist(watch)

    watch_rows = {w.get("ticker"): w for w in (watch or {}).get("watchlist") or []}
    tickers = sorted({s.get("ticker") for s in (signals or {}).get("signals") or []} | set(watch_rows))
    for ticker in tickers:
        out[f"tickers/{ticker}.json"] = build_ticker(signals, ticker, watch_rows.get(ticker))

    allow = set(rules.get("allow", []))
    public = {name: filter_public(payload, allow) for name, payload in out.items()}
    for name, payload in public.items():
        assert_no_forbidden(payload, rules, name)
        size = _json_size(payload)
        if size > MAX_BYTES:
            raise SystemExit(f"빌드 실패: {name} 이 {size / 1024:.0f}KB — 200KB 제한(5.6)")
    return public


def _load(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _load_yaml(path: Path):
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except yaml.YAMLError:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", default="data/quant_signals.json")
    ap.add_argument("--folds", default="data/walkforward/folds/summary.json")
    ap.add_argument("--records", default="data/ledger/records.jsonl")
    ap.add_argument("--radar", default="data/radar/13f.json")
    ap.add_argument("--portfolio", default="data/ledger/portfolio/state.json")
    ap.add_argument("--watchlist", default="data/watchlist/current.json")
    ap.add_argument("--params", default=str(DEFAULT_PARAMS))
    ap.add_argument("--veto", default=str(DEFAULT_VETO))
    ap.add_argument("--out", default="web/data")
    ap.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST))
    args = ap.parse_args(argv)

    rules = load_allowlist(Path(args.allowlist))
    signals = _load(Path(args.signals))
    folds = _load(Path(args.folds))
    records = None
    rp = Path(args.records)
    if rp.exists():
        records = [json.loads(line) for line in rp.read_text(encoding="utf-8").splitlines() if line.strip()]
    out = build(signals, folds, records, rules,
                radar=_load(Path(args.radar)),
                portfolio_state=_load(Path(args.portfolio)),
                watch=_load(Path(args.watchlist)),
                params=_load_yaml(Path(args.params)),
                veto=_load_yaml(Path(args.veto)))
    root = Path(args.out)
    for name, payload in out.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"written": [str(root / n) for n in out],
                      "signals_status": (signals or {}).get("status", "missing")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
