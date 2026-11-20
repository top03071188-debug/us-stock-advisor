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
    ledger/records.json · ledger/portfolio.json · radar/13f.json · health.json ·
    debate.json · notifications.json · config.json · **deploy_manifest.json**
    (+ 하위 호환: folds.json, watchlist.json)
    (+ **토론 상세**: debate_details/{report_id}.json — 1R·2R·3R 원문. `debate.json` 의 행은
      `detail_path` 만 갖고, 화면이 그 경로를 읽어 예전과 같은 1R→2R→3R 을 그린다(감사 09·10).
    `data/debate_reports.json` 이 없으면 `debate/*` 를 만들지 않는다(화면은 "토론 없음").
    `deploy_manifest.json` 은 모든 공개 파일의 sha256 과 통합 `bundle_version` 을 담는다 —
    서비스워커가 이 세트 전체를 받아 검증한 뒤 **원자적으로** 전환한다(감사 12).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.ledger import debate as debate_mod  # noqa: E402

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
    # [감사 09·10] 토론 행의 **구조 키** — 상세 파일 위치와 스냅샷 대조 결과(값 아님).
    "detail_path", "snapshot_matched", "one_line_ko",
    # [통합 재검토] 신호 카드가 "요약 행"임을 말하는 표식(크기 상한에서만 쓰인다).
    "compact",
}


def load_allowlist(path: Path = DEFAULT_ALLOWLIST) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _allowed(key: str, allow: set[str]) -> bool:
    return key in allow or key in STRUCTURAL_KEYS


#: 키가 **데이터 이름**(티커·섹터·채널)인 사전 — 그 키는 공개 허용 목록의 필드명이 아니다.
#  감싸는 키만 허용 목록에 있으면 안쪽 키는 값으로 통과시킨다. 금지 키 검사(`assert_no_forbidden`)는
#  이 안쪽 키까지 계속 보므로 `{"by_ticker": {"quote": …}}` 같은 누출은 그대로 막힌다.
STRUCTURAL_MAPS = {"by_ticker", "sector_weights", "forced_exits", "sector_counts", "channels",
                  "by_regime", "counts_by_sector"}


def filter_public(obj, allow: set[str], parent: str | None = None):
    if isinstance(obj, dict):
        keep_keys = parent in STRUCTURAL_MAPS
        return {k: filter_public(v, allow, k) for k, v in obj.items()
                if keep_keys or _allowed(k, allow)}
    if isinstance(obj, list):
        return [filter_public(v, allow, parent) for v in obj]
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
    """파일에 **실제로 쓰이는** 바이트 수(5.6).

    [감사 09] 예전에는 `ensure_ascii=False` 의 compact 결과를 쟀는데, 게시되는 파일은 `indent=2`
    였다 — 24건 케이스가 compact 189,851B(통과)·실파일 243,123B(200KiB 초과)로 갈렸다.
    검사는 반드시 **커밋·배포되는 그 바이트**로 한다.
    """
    return len(_serialize(obj))


# ────────────────────────────── 각 파일 ──────────────────────────────
def bundle_version(signals: dict | None) -> str | None:
    """입력 지문 버전 — `inputs_hash` + `snapshot_id`(P2-1).

    **잠정값**이다. 발행 세트 버전은 `attach_bundle()` 이 **모든 공개 파일 해시**로 다시 만든다 —
    health·토론·알림만 바뀐 배포도 고유 버전을 가져야 하기 때문이다(감사 12).
    """
    signals = signals or {}
    inputs_hash = str(signals.get("inputs_hash") or "")
    snapshot = str(signals.get("snapshot_id") or "")
    if not inputs_hash and not snapshot:
        return None
    return f"{inputs_hash or 'no-inputs'}|{snapshot or 'no-snapshot'}"


def data_status_of(value, default: str = "ok") -> str:
    """실행 상태를 공개 계약(`ok` | `degraded` | `outage`)으로 정규화한다(P2-1)."""
    text = str(value or "").strip().lower()
    if text in ("ok", "normal", "complete", "sample"):
        return "ok"
    if text in ("degraded", "stale", "partial"):
        return "degraded"
    if text in ("outage", "failed", "down"):
        return "outage"
    return default if text else default


def build_meta(signals: dict | None, debate: dict | None, debate_comparison: dict | None = None,
               health: dict | None = None) -> dict:
    signals = signals or {}
    health = health or {}
    regime = signals.get("regime") or {}
    source = signals.get("source")
    diagnostics = signals.get("diagnostics") or {}
    return {
        # P2-1: `generated_at` 은 **신호 확정 시각**이다. 웹 빌드 시각으로 덮어쓰면 장애일에도
        # 전일 정상 신호가 "방금 갱신된 데이터"처럼 보인다. 빌드 시각은 `build_at` 에 따로 둔다.
        "generated_at": signals.get("signal_published_at") or signals.get("generated_at"),
        "build_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "bundle_version": bundle_version(signals),
        "data_status": data_status_of(health.get("data_status") or signals.get("status")),
        "last_attempt_at": health.get("last_attempt_at") or health.get("last_run_at"),
        "last_success_at": health.get("last_success_at"),
        "outage_id": health.get("outage_id"),
        "params_version": signals.get("params_version"),
        "snapshot_id": signals.get("snapshot_id"),
        "status": signals.get("status", "awaiting_first_run"),
        "mode": signals.get("mode"),
        "source": None if source in (None, "none") else source,
        "regime": regime.get("regime"),
        "regime_verified": regime.get("verified"),
        "regime_multiplier": regime.get("multiplier"),
        "data_cutoff_at": signals.get("data_cutoff_at"),
        "ingest": signals.get("ingest"),
        "signal_published_at": signals.get("signal_published_at"),
        "signal_published_planned_at": signals.get("signal_published_planned_at"),
        "order_cutoff_at": signals.get("order_cutoff_at"),
        "target_session": signals.get("target_session"),
        # v1.3.3 01-b: 배치 **시작** 시각과 발행 시각은 다른 값이다 — 화면이 둘을 같이 보여 준다.
        "run_started_at": signals.get("run_started_at"),
        "publish_lag_seconds": signals.get("publish_lag_seconds"),
        "published_after_order_cutoff": signals.get("published_after_order_cutoff"),
        "inputs_hash": signals.get("inputs_hash"),
        # 01: WACC 무위험금리가 FRED 실값인지 기본값 대체인지
        "wacc_risk_free": diagnostics.get("wacc_risk_free"),
        "as_of": signals.get("as_of"),
        "prices_as_of": signals.get("prices_as_of"),
        "debate_generated_at": (debate or {}).get("generated_at"),
        "debate_status": ("none" if not debate else
                          "stale" if (debate_comparison or {}).get("as_of") not in (None, signals.get("as_of"))
                          else "ok"),
        "debate_effect_verdict": (debate_comparison or {}).get("verdict"),
        "note_ko": {
            "awaiting_first_run": "아직 실전 배치가 한 번도 돌지 않았습니다 — 아래 신호는 비어 있는 것이 정상입니다.",
            "sample": "샘플(합성) 데이터입니다 — 실전 신호가 아닙니다.",
            "degraded": "일부 데이터 소스가 실패했습니다 — 신선도를 확인하세요.",
            "stale": "데이터가 지연되고 있습니다.",
        }.get(signals.get("status", ""), None),
    }


SIGNAL_PUBLIC_FIELDS = (
    "signal_id", "ticker", "type", "trade_grade", "grade", "entry_score", "decision",
    "as_of", "data_cutoff_at", "signal_published_at", "signal_published_planned_at",
    "order_cutoff_at", "target_session", "generated_price",
    # v1.3.3 02: 같은 날 재발행이면 회차와 "무엇을 대체했는지"를 화면에 함께 보여 준다.
    "revision", "supersedes",
    "quant_intent", "thesis_ko", "valuation", "indicators", "investor_scores", "components",
    "conflict", "news_state", "position", "target_band", "expected_mdd", "invalidation",
    "gate_summary", "sizing_note_ko", "params_version", "snapshot_id", "track", "veto",
    "news", "filings", "risk", "evidence", "entry_plan",
    # P1-1: 토론 리스크는 게이트와 **분리해** 보여 준다(자동 하향 off 에서는 배지·기록만).
    "debate_structural_risk", "debate_risk_gate_applied", "confidence_cap",
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
    noted = ("오늘 신호 없음 — 기준을 넘은 종목이 없습니다(정상)." if not rows else None)
    doc = {
        "as_of": signals.get("as_of"),
        "status": signals.get("status", "awaiting_first_run"),
        "regime": signals.get("regime"),
        "signals": rows,
        "blocked": blocked,
        "counts": counts,
        "watchlist_count": signals.get("watchlist_count", len(signals.get("watchlist") or [])),
        "params_version": signals.get("params_version"),
        "published_at": signals.get("signal_published_at"),
        "notes": noted,
        "blocked_note_ko": (None if not blocked else f"차단 {len(blocked)}건 — 안 산 이유도 정보입니다."),
    }
    return _fit_today_size(doc)


def _fit_today_size(doc: dict) -> dict:
    """`signals/today.json` 크기 상한(5.6) — 자세한 행을 **점수 낮은 순으로** 요약 행으로 줄인다.

    200KiB 를 넣어 돌려 보면 신호 7건이 이미 66KB, 20건이 185KB, 43건이 396KB 였다.
    한 종목당 상세(뉴스·공시·밸류에이션)가 9KB 라서, \"기준을 넘은 종목이 많은 날\"에는
    **빌드 전체가 SystemExit 로 죽고** 다른 파일까지 하나도 안 쓰였다(감사 09 의 같은 뿌리).
    상세는 이미 `tickers/{T}.json` 에 종목별로 있으므로, 상한을 넘긴 나머지는 **요약 행**으로
    내리고 몇 건을 그렇게 했는지 `notes` 에 적는다(조용히 깎지 않는다).
    """
    rows = doc.get("signals") or []
    trimmed = 0
    idx = len(rows) - 1
    while rows and _json_size(doc) > TODAY_MAX and idx >= 0:
        if rows[idx].get("compact") is not True:
            rows[idx] = _compact_signal_row(rows[idx])
            trimmed += 1
        idx -= 1
    if trimmed:
        extra = (f"화면 크기 상한(200KB) 때문에 신호 {trimmed}건은 요약만 실었습니다 "
                 f"— 전체 내용은 종목 상세(`tickers/<티커>.json`)에 있습니다.")
        doc["notes"] = " ".join(x for x in (doc.get("notes"), extra) if x)
    return doc


#: `signals/today.json` 이 이 바이트를 넘으면 자세한 행을 요약으로 바꿔다(5.6 200KiB 의 안전 여유).
TODAY_MAX = 190 * 1024
#: 요약 행에 남기는 필드 — 카드가 "무엇을·얼마에·왜"를 말하는 데 필요한 최소한이다.
COMPACT_SIGNAL_FIELDS = ("signal_id", "ticker", "as_of", "type", "grade", "trade_grade",
                         "entry_score", "quant_intent", "decision", "generated_price",
                         "thesis_ko", "sector", "target_session", "revision", "supersedes",
                         "params_version", "snapshot_id")


def _compact_signal_row(row: dict) -> dict:
    """상세 행 → 요약 행(같은 종목 상세 파일을 가리키는 `detail_path` 포함).

    `compact: true` 표식은 화면이 "이 카드는 요약"이라고 말할 수 있게 남긴다(값이 아니라 상태).
    """
    out = {key: row.get(key) for key in COMPACT_SIGNAL_FIELDS if key in row}
    ticker = str(row.get("ticker") or "")
    if ticker:
        out["detail_path"] = f"data/tickers/{ticker}.json"
    out["compact"] = True
    return out


def _with_snapshot_flag(debate_row: dict | None, record: dict | None,
                        signals: dict | None) -> dict | None:
    """토론 행에 `snapshot_matched` 를 붙인다(부수 ③).

    종목 상세는 `by_ticker` 의 **티커별 최신 행**을 그대로 붙여 왔기 때문에, 오늘 신호가 새 스냅샷인데
    토론이 지난 스냅샷의 것일 수 있었다. `signal_id`(없으면 `snapshot_id`)를 대조해 명시한다.
      - 비교할 식별자가 아예 없으면 `False` — "오늘 신호의 확정 토론"으로 단정하지 않는다.
    """
    if not debate_row:
        return None
    row = dict(debate_row)
    sig = (record or {}).get("signal_id")
    row_sig = debate_row.get("signal_id")
    snap = (signals or {}).get("snapshot_id")
    row_snap = debate_row.get("snapshot_id")
    if sig and row_sig:
        row["snapshot_matched"] = str(sig) == str(row_sig)
    elif snap and row_snap:
        row["snapshot_matched"] = str(snap) == str(row_snap)
    else:
        row["snapshot_matched"] = False
    return row


def build_ticker(signals: dict | None, ticker: str, watch_row: dict | None = None,
                 debate_row: dict | None = None, position: dict | None = None) -> dict:
    signals = signals or {}
    rows = [s for s in signals.get("signals") or [] if s.get("ticker") == ticker]
    record = public_signal(rows[0]) if rows else None
    chart = (signals.get("charts") or {}).get(ticker)
    checks = next((c for c in signals.get("position_checks") or [] if c.get("ticker") == ticker), None)
    debate = _with_snapshot_flag(debate_row, record, signals)
    return {
        "ticker": ticker,
        "as_of": signals.get("as_of"),
        "status": signals.get("status"),
        "watchlist": {k: (watch_row or {}).get(k) for k in ("ticker", "sector", "q", "v", "g", "rank_score")},
        # v1.3.3 04·06: 신호가 없는 종목에서도 밸류에이션 근거(감점 전 V·표본 품질)를 볼 수 있게 한다.
        "valuation_quality": {k: (watch_row or {}).get(k) for k in WATCH_ROW_FIELDS
                              if k not in ("ticker", "sector", "q", "g", "rank_score",
                                           "valuation_suspended")},
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
        "debate": debate,
        "debate_note_ko": (None if debate else DEBATE_TICKER_NOTE.format(ticker=ticker)),
        "debate_display": None if not debate else {
            "badge": debate.get("badge"), "quant_grade": debate.get("quant_grade"),
            "display_grade": debate.get("display_grade"),
            "recommendation": debate.get("recommendation"),
            # 행은 **요약**이고 의장 한 줄은 행 꼭대기로 옮겼다(라운드 원문은 상세 파일로 빠짐).
            "one_line_ko": debate.get("one_line_ko") or _one_line_of(debate),
            "snapshot_matched": debate.get("snapshot_matched"),
        },
        "position": ({k: position.get(k) for k in POSITION_FIELDS if position.get(k) is not None}
                     if position else None),
        "position_check": None if not checks else {
            "verdict": checks.get("verdict"), "exit_score": checks.get("exit_score"),
            "triggers": checks.get("triggers"), "reason_ko": checks.get("reason_ko"),
        },
        "params_version": signals.get("params_version"),
    }


def _next_quarter_open(today: str | None) -> str | None:
    """다음 분기 개봉일(1·4·7·10월 1일) — 화면의 '다음 개봉' 문구용."""
    if not today:
        return None
    try:
        y, m = int(str(today)[:4]), int(str(today)[5:7])
    except (TypeError, ValueError):
        return None
    for month in (1, 4, 7, 10):
        if m < month:
            return f"{y:04d}-{month:02d}-01"
    return f"{y + 1:04d}-01-01"


def holdout_view(summary: dict, folds: dict) -> dict:
    """홀드아웃 잠금 배너 값 — 신호 파일에 없으면 폴드 요약에서 만든다."""
    holdout = (summary.get("holdout") or {})
    if holdout.get("next_open"):
        return holdout
    start = folds.get("holdout_start") or holdout.get("holdout_start")
    end = folds.get("holdout_end") or holdout.get("holdout_end")
    if not start and not holdout:
        return {}
    next_open = _next_quarter_open(folds.get("as_of") or summary.get("as_of"))
    return {
        "holdout_start": start,
        "holdout_end": end,
        "locked": True,
        "evaluations": holdout.get("evaluations", 0),
        "next_open": next_open,
        "note_ko": f"홀드아웃은 분기 1회만 열립니다 — 다음 개봉 {next_open or '—'}",
    }


def build_ledger(signals: dict | None, records: list[dict] | None, portfolio: dict | None,
                 folds: dict | None = None, params_history: list[dict] | None = None,
                 pilot_eval: dict | None = None) -> dict:
    signals = signals or {}
    summary = signals.get("ledger_summary") or {}
    fold_summary = summary.get("folds") or folds or {}
    pilot = summary.get("pilot") or {}
    if not pilot.get("metrics") and pilot_eval:
        pilot = {**pilot_eval, **(pilot if pilot.get("verdict") else {})}
    history = summary.get("params_history") or params_history or []
    return {
        "schema_version": summary.get("schema_version", 1),
        "as_of": summary.get("as_of") or signals.get("as_of"),
        "n_signals": summary.get("n_signals"),
        "n_graded_3m": summary.get("n_graded_3m"),
        "accuracy": summary.get("accuracy") or {"label_ko": "신호 정확도"},
        "strategy": summary.get("strategy") or {"label_ko": "전략(모의 계좌)"},
        "horizons": summary.get("horizons") or {},
        "folds": fold_summary,
        "pilot": pilot or {"verdict": "보류", "reason_ko": "파일럿 지표 미산출"},
        "params_history": history,
        "signal_history": (summary.get("signal_history") or [])[:200],
        "graded_sample": (summary.get("graded_sample") or [])[:20],
        "settlement": summary.get("settlement") or {},
        "holdout": holdout_view(summary, fold_summary),
        "debate_effect": summary.get("debate_effect") or {
            "pairs": 0, "net_pp": None, "verdict": "표본 0건 — 판정 보류"},
        "portfolio": portfolio or (signals.get("portfolio") or {}),
        # [감사 09] 예전에는 원장 **전체 레코드**를 여기에도 복사해 넣었다(28건에서 251KB).
        # 정식 목록은 `ledger/records.json`(최근 200건)이고, 여기 중복이 바로 5.6 의 200KiB 를
        # 넘기게 만든 원인이었다 — 화면도 `ledger/records.json` 만 읽는다.
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
            # v1.3.3 02: 발행 회차·대체 대상 — 원장 탭에서 "같은 날 몇 번째 발행인지"를 보여 준다.
            "revision": record.get("revision"), "supersedes": record.get("supersedes"),
        })
    # [통합 재검토] 행 수 상한(200)만으로는 부족하다 — 실제 레코드는 행당 1.07KB 라 200건이면
    #   214,705B 로 200KiB 를 넘고 **빌드 전체가 SystemExit 로 죽었다**(다른 파일도 미기록).
    #   오래된 행부터 바이트 예산에 맞춰 빼고 몇 건을 뺐는지 적는다(화면은 최근 50건만 그린다).
    dropped = 0
    doc = {"as_of": signals.get("as_of"), "count": len(rows), "records": rows}
    while rows and _json_size(doc) > RECORDS_MAX:
        rows.pop(0)
        dropped += 1
        doc["count"] = len(rows)
    if dropped:
        doc["omitted"] = dropped
        doc["note_ko"] = (f"최근 {len(rows)}건만 실었습니다 — 오래된 {dropped}건은 크기 상한(200KB)으로 "
                          f"공개 파일에서 빠졌습니다(원장에는 그대로 있음).")
    return doc


#: `ledger/records.json` 이 이 바이트를 넘으면 오래된 행부터 뺀다(5.6 200KiB 의 안전 여유).
RECORDS_MAX = 170 * 1024


def build_portfolio(signals: dict | None, state: dict | None) -> dict:
    signals = signals or {}
    portfolio = state or signals.get("portfolio") or {}
    return {
        "as_of": portfolio.get("as_of") or signals.get("as_of"),
        "cash_pct": portfolio.get("cash_pct"),
        "invested_pct": portfolio.get("invested_pct"),
        "equity_index": portfolio.get("equity_index"),
        "n_positions": portfolio.get("n_positions", len(portfolio.get("positions") or [])),
        "positions": [{k: p.get(k) for k in POSITION_FIELDS if p.get(k) is not None}
                      for p in portfolio.get("positions") or []],
        "sector_weights": portfolio.get("sector_weights") or {},
        "note_ko": portfolio.get("note_ko") or "모의 계좌 — 비중만 공개한다.",
    }


def _pct(value) -> str:
    if value is None:
        return "—"
    try:
        num = float(value) * 100
    except (TypeError, ValueError):
        return str(value)
    return f"{num:.2f}%".replace(".00%", "%") if abs(num) < 1 else f"{num:.0f}%"


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


#: 워치리스트 행에서 공개하는 필드 — v1.3.3 04: 감점 전 V·원시 안전마진·괴리 부호·EV/EBIT 표본 품질까지.
WATCH_ROW_FIELDS = (
    "ticker", "sector", "q", "v", "v_raw", "margin", "g", "rank_score",
    "v_penalties", "hist_divergence_signed",
    "ev_ebit_percentile", "ev_ebit_current", "ev_ebit_median_10y", "ev_ebit_samples",
    "ev_ebit_years", "ev_ebit_window", "ev_ebit_reason",
    "growth", "growth_raw_cagr", "growth_clipped_to_zero", "effective_fcff_years",
    "normalization_method", "dcf_sensitivity", "dcf_dropped", "wacc_sensitivity",
    "terminal_g_sensitivity", "valuation_suspended",
)


def build_watchlist(watch: dict | None) -> dict:
    watch = watch or {}
    return {
        "as_of": watch.get("as_of"),
        "count": watch.get("count", len(watch.get("watchlist") or [])),
        "watchlist": [{k: w.get(k) for k in WATCH_ROW_FIELDS}
                      for w in watch.get("watchlist") or []],
        "entered": [{k: e.get(k) for k in ("ticker", "sector", "rank_score", "reason_ko")}
                    for e in watch.get("entered") or []],
        "exited": [{k: e.get(k) for k in ("ticker", "sector", "reason_ko")}
                   for e in watch.get("exited") or []],
        "valuation_suspended": [{"ticker": s.get("ticker"), "sector": s.get("sector"),
                                 "reason": s.get("reason")} for s in watch.get("valuation_suspended") or []],
        "excluded": [{"ticker": e.get("ticker"), "reasons": e.get("reasons")} for e in watch.get("excluded") or []],
    }


# ────────────────────────────── Phase 4: 토론·건강·알림 ──────────────────────────────
DEBATE_TICKER_NOTE = ("토론 없음 — PC에서 `python codex_debate/run_debate.py --tickers {ticker}` "
                      "로 돌릴 수 있습니다.")

COMPARISON_FIELDS = (
    "schema_version", "as_of", "generated_at", "horizon_td", "auto_downgrade_enabled",
    "divergent_pairs", "same_action_pairs", "n_graded_pairs", "n_eff",
    "avoided_loss", "missed_gain", "net", "cost", "net_after_cost",
    "avoided_loss_pp", "missed_gain_pp", "net_pp", "net_after_cost_pp",
    "avoidance_accuracy", "drawdown", "pairs", "by_regime", "quarters",
    "consecutive_quarters", "verdict", "verdict_ko", "note_ko", "counts",
    # P1-3 화면 분리: 계좌 두 개의 **순자산 차이**(정책 효과)와 **페어 손익**(보류·회피)은 다른 값이다.
    "policy_diff_pp", "pair_summary", "retroactive_dropped", "retroactive_dropped_pairs",
    # 두 계좌의 기간 수익률·MDD — "정책 순효과"가 어느 두 값의 차이인지 화면이 바로 보여 준다.
    "R_quant", "R_debated", "quant_mdd", "debate_mdd",
)
#: `pair_summary` 가 없을 때(구 비교 파일) 채우는 하위 호환 기본값 — 페어 손익만 담는다.
EMPTY_PAIR_SUMMARY = {"pairs": 0, "n_graded": 0, "n_eff": 0, "avoided_loss": 0.0,
                      "missed_gain": 0.0, "pair_net": 0.0, "pair_net_pp": 0.0,
                      "avoidance_accuracy": None}


def pair_summary_of(comparison: dict) -> dict:
    """페어 손익 요약 — 회피 손실·놓친 수익·비용 후 손익만(계좌 성과와 분리한 표).

    비교 모듈(`engine.ledger.debate`)이 자체 `pair_summary` 를 내면 **그것을 그대로 쓴다**.
    구 파일처럼 없는 경우에만 같은 키 모양으로 최소한을 재구성한다.
    """
    source = comparison or {}
    existing = source.get("pair_summary")
    if isinstance(existing, dict) and existing:
        return {**EMPTY_PAIR_SUMMARY, **existing}
    summary = dict(EMPTY_PAIR_SUMMARY)
    for key in ("n_graded_pairs", "n_eff", "avoided_loss", "missed_gain", "avoidance_accuracy"):
        if source.get(key) is not None:
            summary[{"n_graded_pairs": "n_graded"}.get(key, key)] = source[key]
    graded = source.get("n_graded_pairs") or 0
    summary["pairs"] = int(source.get("divergent_pairs") or graded or 0)
    summary["pair_net"] = source.get("net") or 0.0
    summary["pair_net_pp"] = source.get("net_pp") or 0.0
    return summary


NO_DEBATE_COMPARISON = {
    "schema_version": 1, "as_of": None, "horizon_td": 63, "auto_downgrade_enabled": False,
    "divergent_pairs": 0, "same_action_pairs": 0, "n_graded_pairs": 0, "n_eff": 0,
    "avoided_loss": 0.0, "missed_gain": 0.0, "net": 0.0, "cost": 0.0, "net_after_cost": 0.0,
    "avoided_loss_pp": 0.0, "missed_gain_pp": 0.0, "net_pp": 0.0, "net_after_cost_pp": 0.0,
    "avoidance_accuracy": None, "pairs": [], "by_regime": {}, "quarters": [],
    "consecutive_quarters": 0, "verdict": debate_mod.VERDICT_INSUFFICIENT,
    "note_ko": "토론 비교 표본 없음 — 행동이 갈린 페어가 아직 없다(토론 0건이면 정상).",
    # 계좌 정책 효과는 **두 모의 계좌의 순자산 차이**다 — 페어 손익과 섞지 않는다(4.8 · P1-3).
    "policy_diff_pp": None,
    "pair_summary": EMPTY_PAIR_SUMMARY,
}

#: `debate.json` 목록에 싣는 최대 리포트 수(오래된 것부터 목록에서 뺄 뿐, 상세 파일은 남긴다).
MAX_DEBATE_REPORTS = 40
#: 라운드 원문(1R·2R·3R)을 `debate.json` 에 **인라인**으로 넣어도 되는 상한(실파일 바이트).
#  감사 09: 원문은 보고서마다 5KB 안팎이고, 43종목 × 수십 건이 쌓이면 500KB 를 넘겼다.
#  그래서 ① 원문은 **항상** `debate_details/{report_id}.json` 으로 분리하고 ② 목록은 전부
#  인라인일 때만(작은 배포·구 샘플과의 호환) 이 예산 안에서 인라인을 유지한다.
DEBATE_INLINE_BUDGET = 150 * 1024
#: 상세 파일을 빼고도 남은 `debate.json` 이 이 값을 넘으면 오래된 리포트 행부터 목록에서 뺀다.
DEBATE_LIST_MAX = 190 * 1024
DETAIL_PATH_PREFIX = "data/debate_details/"
#: 채택되지 않은 회차의 배지 — "토톤 완료"로 읽히면 안 된다(02 · T1 채택 계약).
BADGE_NOT_ADOPTED = "채택 아님"
REASON_NOT_ADOPTED = "NOT_ADOPTED"
REASON_ADOPTED_MISMATCH = "ADOPTED_REPORT_MISMATCH"
#: 상세 파일로 빠지는 필드 — 이 셋이 본문 크기의 대부분이다.
ROUND_FIELDS = ("round1", "round2", "round3")

POSITION_FIELDS = ("ticker", "weight", "sector", "opened_at", "tranches_filled", "tranches",
                   "stop_distance", "stop_price", "stop_headroom_pct", "trailing", "trim_stage",
                   "trim_done", "last_event_ko")


def _as_date(value):
    from datetime import date as _date
    try:
        return _date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def load_debate_events(event_dir: str | Path | None = "data/ledger/debate_events") -> dict[str, dict]:
    """원장이 확정한 편입 이벤트(`debate_events/{signal_id}.json`)를 읽는다(02)."""
    if event_dir is None:
        return {}
    return debate_mod.load_events(event_dir)


def _events_of(doc_row: dict) -> list[dict]:
    return [e for e in (doc_row.get("events") or []) if isinstance(e, dict)]


def _adopted_block(doc_row: dict) -> dict:
    """신호 단위로 확정된 **채택 정체성**(`report_id`·`report_sha256`·`snapshot_id`)."""
    adopted = doc_row.get("adopted")
    return adopted if isinstance(adopted, dict) else {}


def _event_for_report(doc_row: dict, report_id) -> dict | None:
    """이 리포트 **자신**의 이벤트만 돌려준다 — 다른 회차의 이벤트로 내려가지 않는다(02 · 채택 계약).

    [T1 · 02] 예전에는 `report_id` 가 맞는 이벤트가 없으면 **신호의 마지막 이벤트**로 폴백했다.
    그래서 지연 도착 `-02` 행이 적격 `-01` 의 이벤트를 그대로 받아 "토톤 완료"로 보였다.
    """
    rid = str(report_id or "")
    if not rid:
        return None
    matched = [e for e in _events_of(doc_row) if str(e.get("report_id") or "") == rid]
    if not matched:
        return None
    return max(matched, key=lambda e: str(e.get("recorded_at") or e.get("completed_at") or ""))


def _superseded_reason(doc_row: dict, report_id) -> str | None:
    rid = str(report_id or "")
    for row in doc_row.get("superseded") or []:
        if isinstance(row, dict) and str(row.get("report_id") or "") == rid:
            return str(row.get("reason") or "") or None
    return None


def _adoption_of(doc_row: dict, report: dict) -> tuple[bool, list[str]]:
    """(이 리포트가 **채택본**인가, 어긋난 경우의 사유 코드).

    [02 · T1] 채택은 `adopted` 블록의 **정체성**(`report_id`·`report_sha256`·`snapshot_id`)으로 판정한다.
    같은 `report_id` 라도 내용이 바뀌면(영수증 불일치) 그 결정에 붙은 보고서가 아니다.
    """
    adopted = _adopted_block(doc_row)
    if not adopted:
        # 채택 블록 이전 문서 — 자기 `report_id` 의 이벤트가 있으면 그 리포트에 대한 판정으로 쓴다.
        return (_event_for_report(doc_row, report.get("report_id")) is not None), []
    if str(adopted.get("report_id") or "") != str(report.get("report_id") or ""):
        return False, [REASON_NOT_ADOPTED]
    snapshot = str(adopted.get("snapshot_id") or "")
    report_snapshot = str(report.get("snapshot_id") or "")
    if snapshot and report_snapshot and snapshot != report_snapshot:
        return False, [REASON_ADOPTED_MISMATCH]
    claimed = str(adopted.get("report_sha256") or "")
    if claimed:
        try:
            actual = debate_mod.compute_report_sha256(report)
        except (TypeError, ValueError):
            actual = ""
        if actual and actual != claimed:
            return False, [REASON_ADOPTED_MISMATCH]
    return True, []


def _event_el(event: dict) -> dict:
    """이벤트 → `public_report` 의 자격 인자(`el`). 자격을 **다시 계산하지 않는다**."""
    return {"eligible": bool(event.get("eligible")), "late": bool(event.get("late")),
            "invalid": bool(event.get("invalid")), "reasons": list(event.get("reasons") or []),
            "report_validated_at": event.get("report_validated_at"),
            "frozen_at": event.get("frozen_at"), "order_cutoff_at": event.get("order_cutoff_at"),
            "signal_observed_at": event.get("signal_observed_at")}


def debate_detail_name(report_id) -> str | None:
    """`report_id` → 파일 이름(경로 조작 문자를 지운다). 비었으면 None."""
    rid = str(report_id or "").strip()
    if not rid:
        return None
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", rid)[:120].strip(".")
    return safe or None


def debate_detail_path(report_id) -> str | None:
    name = debate_detail_name(report_id)
    return f"{DETAIL_PATH_PREFIX}{name}.json" if name else None


def _one_line_of(row: dict):
    return ((row.get("round3") or {}).get("chair") or {}).get("one_line_ko")


def slim_report_row(row: dict, *, with_detail: bool = True) -> dict:
    """공개 목록·`by_ticker` 용 요약 행 — 라운드 원문만 빼고 식별·판정은 그대로 둔다.

    `one_line_ko`(의장 한 줄)는 요약이므로 남기고, 상세 위치를 `detail_path` 로 가리킨다.
    """
    slim = {k: v for k, v in row.items() if k not in ROUND_FIELDS}
    slim["one_line_ko"] = _one_line_of(row)
    path = debate_detail_path(row.get("report_id")) if with_detail else None
    if path:
        slim["detail_path"] = path
    return slim


def _inline_row(row: dict) -> dict:
    """상세(원문 포함) 행 + `detail_path` — 작은 배포에서 구 화면과 똑같이 보이게 한다."""
    inline = dict(row)
    inline["one_line_ko"] = _one_line_of(row)
    path = debate_detail_path(row.get("report_id"))
    if path:
        inline["detail_path"] = path
    return inline


def _event_actions(event: dict) -> dict:
    """이벤트 → `public_report` 의 행동 인자(`actions`)."""
    return {"quant_grade": event.get("quant_grade"), "recommendation": event.get("recommendation"),
            "quant_action": event.get("quant_action"), "debate_action": event.get("debate_action"),
            "divergent": bool(event.get("divergent")),
            "auto_downgrade_applied": bool(event.get("auto_downgrade_applied")),
            "veto_codes": list(event.get("veto_codes") or [])}


def _by_ticker_of(rows: list[dict], detail_names: set[str] | None = None) -> dict[str, dict]:
    """종목별 **최신 요약 1행** — 라운드 원문은 담지 않는다(43종목이 쌓여도 크기가 상수를 유지).

    행은 이미 최신순으로 정렬되어 있으므로 종목당 첫 행이 최신이다. `detail_names` 를 주면
    상세 파일이 **실제로 있는** 행만 `detail_path` 를 갖는다(없는 경로를 가리키지 않게).
    """
    by_ticker: dict[str, dict] = {}
    for row in rows:
        ticker = row.get("ticker")
        if ticker and ticker not in by_ticker:
            name = debate_detail_name(row.get("report_id"))
            has_detail = detail_names is None or (name is not None and name in detail_names)
            by_ticker[ticker] = slim_report_row(row, with_detail=has_detail)
    return by_ticker


def _with_inline_rounds(doc: dict, rows: list[dict]) -> dict:
    """작은 배포에서는 라운드 원문을 **인라인으로도** 남긴다(구 화면·구 샘플 호환).

    [감사 09] 큰 배포에서 크기를 먹은 것은 원문의 **반복**이다. 그래서
      - 상세 파일(`debate_details/*.json`)은 항상 만들고,
      - 목록은 **전부 인라인해도 `DEBATE_INLINE_BUDGET` 안에 들 때만** 인라인한다(전부 아니면 전무).
    이러면 작은 배포는 예전과 똑같은 바이트를 보이고, 종목·리포트가 쌓여도 상한이 지켜진다.
    """
    if not rows:
        return doc
    if not any(row.get("round1") or row.get("round2") or row.get("round3") for row in rows):
        return doc
    candidate = {**doc, "reports": [_inline_row(row) for row in rows]}
    if _json_size(candidate) <= DEBATE_INLINE_BUDGET:
        return candidate
    return doc


def _fit_debate_size(doc: dict) -> dict:
    """`debate.json` 의 마지막 안전핀 — 그래도 넘으면 **오래된 리포트부터** 목록에서 뻐낸다.

    `by_ticker`(종목당 1행)는 남긴다. 그걸로도 넘치면 조용히 깎지 않고 **실패**한다(5.6).
    """
    rows = doc.get("reports") or []
    trimmed = 0
    while rows and _json_size(doc) > DEBATE_LIST_MAX:
        rows.pop()
        trimmed += 1
    if trimmed:
        extra = f"파일 크기 상한(5.6) 때문에 오래된 {trimmed}건을 목록에서 더 뻐습니다."
        doc["note_ko"] = " ".join(x for x in (doc.get("note_ko"), extra) if x)
    if _json_size(doc) > MAX_BYTES:
        raise SystemExit(f"빌드 실패: debate.json 이 {_json_size(doc) / 1024:.0f}KB — 200KB 제한(5.6)")
    return doc


def build_debate(reports_doc: dict | None, records: list[dict] | None,
                 comparison: dict | None, params: dict | None, as_of: str | None, *,
                 debate_events: dict | None = None, details: dict | None = None) -> dict:
    """`web/data/debate.json` — 토론 0건이어도 **항상 만든다**(화면은 '토론 없음'을 읽는다, 5.7).

    [02 · 감사 02] 예전에는 `reconcile_debates(doc, records, {}, ...)` 로 **빈 체결 목록**으로 편입을
    다시 계산했다 — 그렇게 하면 `ATTACHED_AFTER_FILL` 이 원리적으로 작동하지 않고, Pages 의 편입
    판정이 원장과 달라진다. 지금은 원장이 `debate_events` 에 남긴 **확정 결과를 그대로** 쓰고,
    리포트 본문(1R·2R·3R)만 `data/debate_reports.json` 에서 가져와 붙인다.
    [02 · T1] 편입 결과는 원장이 `debate_events` 에 남긴 **채택 결과**를 그대로 쓴다:
    채택본(`adopted.report_id`·`report_sha256`·`snapshot_id`)과 일치하는 행만 적격을 신뢰하고,
    채택되지 않은 회차는 자기 이벤트의 지연·기각 사유만 달고 `채택 아님` 으로 남긴다
    (다른 회차의 이벤트를 빌려 “토톤 완료”로 보이지 않게 — 감사 02).
    이벤트가 없는 신호는 자격을 추정하지 않고 `badge: null` 로 **표시만** 한다.

    [감사 09] 본문은 여기서 **분리**한다 — 라운드 원문(`round1`·`round2`·`round3`)은
    `details` 에 `debate_details/{report_id}.json` 으로 나가고, `debate.json` 의 행은
    `detail_path` 로 그 파일을 가리킨다. 그래서 종목·리포트가 쌓여도 목록은 상수 크기로 남는다.
    """
    doc = reports_doc or {}
    reports = debate_mod.reports_of(doc)
    events = debate_events if debate_events is not None else {}
    rows: list[dict] = []
    n_eligible = n_late = n_invalid = 0
    missing_events = 0
    for report in reports:
        sid = str(report.get("signal_id") or "")
        doc_row = events.get(sid) or {}
        has_ledger = bool(_events_of(doc_row) or _adopted_block(doc_row) or doc_row.get("superseded"))
        if not has_ledger:
            # 원장이 이 신호를 **한 번도 판정하지 않았다** — 자격을 지어내지 않고 표시만 한다.
            missing_events += 1
            row = debate_mod.public_report(report, {}, {})
            row.update({"badge": None, "eligible": None, "late": None, "invalid": None,
                        "track": None, "reasons": ["LEDGER_EVENT_MISSING"]})
            rows.append(row)
            continue
        adopted, why = _adoption_of(doc_row, report)
        event = _event_for_report(doc_row, report.get("report_id"))
        if not adopted:
            # [02 · T1] 채택되지 않은 회차(`-02` 등)는 **자기 이벤트의 지연·기각 사유**만 쓰고,
            # 적격·“토톤 완료”로는 절대 올리지 않는다.
            local = _event_el(event) if event is not None else {}
            reasons = [*why, *(str(r) for r in local.get("reasons") or [])]
            superseded = _superseded_reason(doc_row, report.get("report_id"))
            if superseded:
                reasons.append(superseded)
            if not reasons:
                reasons = [REASON_NOT_ADOPTED]
            el = {"eligible": False, "late": bool(local.get("late")), "invalid": bool(local.get("invalid")),
                  "reasons": reasons, "report_validated_at": local.get("report_validated_at"),
                  "frozen_at": local.get("frozen_at"), "order_cutoff_at": local.get("order_cutoff_at"),
                  "signal_observed_at": local.get("signal_observed_at")}
            row = debate_mod.public_report(report, el, {})
            if row.get("badge") == debate_mod.BADGE_DONE:
                row["badge"] = BADGE_NOT_ADOPTED  # `BADGE_DONE` 는 “토톤 완료”다 — 채택본만 말할 수 있다.
            row["reasons"] = reasons
            if el["late"]:
                n_late += 1
            if el["invalid"]:
                n_invalid += 1
            rows.append(row)
            continue
        if event is None:
            # 채택본인데 그 리포트의 이벤트가 없다 — 자격을 추정하지 않고 표시만 한다.
            missing_events += 1
            row = debate_mod.public_report(report, {}, {})
            row.update({"badge": None, "eligible": None, "late": None, "invalid": None,
                        "track": None, "reasons": ["LEDGER_EVENT_MISSING"]})
            rows.append(row)
            continue
        el = _event_el(event)
        actions = _event_actions(event)
        row = debate_mod.public_report(report, el, actions)
        if el["late"]:
            n_late += 1
        if el["invalid"]:
            n_invalid += 1
        if el["eligible"]:
            n_eligible += 1
        rows.append(row)
    rows.sort(key=lambda r: (str(r.get("completed_at") or ""), str(r.get("signal_id") or "")),
              reverse=True)
    # 200KB 제한(5.6): 목록은 최근 것만 싣는다 — 종목별 요약은 전부 남긴다.
    dropped = max(0, len(rows) - MAX_DEBATE_REPORTS)
    listed = rows[:MAX_DEBATE_REPORTS]
    # 상세 파일은 ① 목록에 실린 행 ② 종목별 최신 행 — 종목 화면이 빈 상세를 가리키지 않게.
    detail_rows = list(listed)
    detail_names = {debate_detail_name(row.get("report_id")) for row in listed} - {None}
    listed_tickers = {row.get("ticker") for row in listed}
    for row in rows[MAX_DEBATE_REPORTS:]:
        ticker = row.get("ticker")
        name = debate_detail_name(row.get("report_id"))
        if ticker in listed_tickers or not name or name in detail_names:
            continue
        listed_tickers.add(ticker)
        detail_names.add(name)
        detail_rows.append(row)
    by_ticker = _by_ticker_of(rows, detail_names)
    oversized: list[str] = []
    degraded_detail: list[str] = []
    skipped_detail: set[str] = set()
    if details is not None:
        # 라운드 원문은 **리포트별 파일**로 — 파일 하나가 다시 200KiB 를 넘지 않게 여기서도 잰다.
        # [통합 재검토] 너무 큰 리포트 하나가 빌드 전체를 죽이면 안 된다 — 라운드 원문만 빼고
        # 요약을 남기고(그래도 넘치면 파일을 안 만들고 `detail_path` 를 지운다) 사유를 적는다.
        for row in detail_rows:
            name = debate_detail_name(row.get("report_id"))
            if not name:
                continue
            payload = _inline_row(row)
            if _json_size(payload) > MAX_BYTES:
                payload = slim_report_row(row, with_detail=False)
                payload["note_ko"] = ("이 리포트의 라운드 원문은 파일 크기 상한(200KB)을 넘어"
                                      " 공개 상세에 실리지 않았습니다 — 요약만 제공합니다.")
                degraded_detail.append(name)
            if _json_size(payload) > MAX_BYTES:
                oversized.append(name)
                skipped_detail.add(name)
                continue
            details[f"debate_details/{name}.json"] = payload
    # 상세 파일을 만들지 못한 리포트는 `detail_path` 를 지워 **없는 파일을 가리키지 않게** 한다.
    for row in [*listed, *by_ticker.values()]:
        if debate_detail_name(row.get("report_id")) in skipped_detail:
            row.pop("detail_path", None)
    source = comparison or {}
    summary = {k: source.get(k) for k in COMPARISON_FIELDS if k in source}
    summary.update({k: v for k, v in NO_DEBATE_COMPARISON.items() if k not in summary})
    summary["pair_summary"] = pair_summary_of(source)
    # `policy_diff_pp` 는 **계좌 순자산 차이**다 — 산출 주체는 비교 모듈이고, 없으면 None 으로 둔다
    # (페어 손익으로 대신 채우지 않는다 — 그게 감사 P1-3 이 지적한 혼합이다).
    summary["policy_diff_pp"] = source.get("policy_diff_pp")
    notes = []
    if not rows:
        notes.append("토론을 아직 한 번도 돌리지 않았습니다 — 표시가 비어 있는 것이 정상입니다.")
    if missing_events:
        notes.append(f"원장 편입 이벤트가 없는 리포트 {missing_events}건은 자격을 다시 계산하지 않고"
                     " 표시만 합니다(다음 배치가 확정합니다).")
    if degraded_detail:
        notes.append(f"라운드 원문이 200KB 상한을 넘은 리포트 {len(degraded_detail)}건은 원문을 빼고"
                     " 요약만 상세로 제공합니다.")
    if oversized:
        notes.append(f"요약으로도 200KB 상한을 넘는 리포트 {len(oversized)}건은 상세 파일을 만들지 않습니다.")
    if dropped:
        notes.append(f"최근 {len(listed)}건만 목록에 싣고 오래된 {dropped}건은 상세 파일로 읽을 수 있습니다(5.6).")
    doc = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "as_of": as_of,
        "status": "ok" if rows else "none",
        "counts": {"reports": len(reports), "eligible": n_eligible, "late": n_late,
                   "invalid": n_invalid, "ledger_confirmed": len(reports) - missing_events},
        "auto_downgrade_enabled": bool(((params or {}).get("debate") or {}).get(
            "auto_downgrade_enabled", False)),
        "comparison": summary,
        # `reports` = 최근 요약 행, `by_ticker` = 종목별 최신 요약 1행 — 둘 다 라운드 원문이 없다.
        "reports": [slim_report_row(row) for row in listed],
        "by_ticker": by_ticker,
        "note_ko": " ".join(notes) or None,
    }
    return _fit_debate_size(_with_inline_rounds(doc, listed))


def build_health(health: dict | None, signals: dict | None = None) -> dict:
    """`web/data/health.json` — 규칙이 실제로 작동했는지 보는 지표(3.5) + 실행 상태(P2-1)."""
    health = health or {}
    signals = signals or {}
    return {
        "schema_version": 1,
        "as_of": health.get("as_of"),
        # 생성 시각은 **신호 확정 시각**(빌드 시각이 아니다) — 장애일에도 시장 데이터 시각은 그대로다.
        "generated_at": signals.get("signal_published_at") or health.get("generated_at"),
        "build_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "bundle_version": bundle_version(signals),
        "status": health.get("status"),
        "data_status": data_status_of(health.get("data_status") or health.get("status")
                                      or signals.get("status")),
        "last_attempt_at": health.get("last_attempt_at") or health.get("last_run_at"),
        "last_success_at": health.get("last_success_at"),
        "data_cutoff_at": health.get("data_cutoff_at") or signals.get("data_cutoff_at"),
        "target_session": health.get("target_session") or signals.get("target_session"),
        "outage_id": health.get("outage_id"),
        "reason": health.get("reason"),
        "degraded": health.get("degraded") or [],
        "positions_open": health.get("positions_open", 0),
        "tranches_filled": health.get("tranches_filled", 0),
        "tranches_pending": health.get("tranches_pending", 0),
        "stop_count": health.get("stop_count", 0),
        "trim_count": health.get("trim_count", 0),
        "stop_reentry_blocked_count": health.get("stop_reentry_blocked_count", 0),
        # 트랙 1(v1.3.5): 신규 1차 진입 차단 수와 같은 봉 2·3차 취소 수를 **따로** 센다(3.5).
        "tranche_cancel_count": health.get("tranche_cancel_count", 0),
        "pending_exit_count": health.get("pending_exit_count", 0),
        "blocked_reentries": (health.get("blocked_reentries") or [])[:20],
        "cancelled_tranches": (health.get("cancelled_tranches") or [])[:20],
        # 동결 전 WATCH·차단으로 취소된 1차 주문(P0-3) — 원장 색인·감사 근거를 화면에도 남긴다.
        "order_cancellations_count": health.get("order_cancellations_count", 0),
        "order_cancellations": (health.get("order_cancellations") or [])[:20],
        "forced_exits": health.get("forced_exits") or {},
        "cash_pct": health.get("cash_pct"),
        "last_run_at": health.get("last_run_at"),
        "note_ko": health.get("note_ko") or "손절 직후 재진입 차단 건수는 같은 봉에서 취소된 2·3차 주문 수다.",
    }


def build_notifications(doc: dict | None) -> dict:
    """`web/data/notifications.json` — 최근 알림 요약(중복 0건 계약)."""
    doc = doc or {}
    rows = [{k: e.get(k) for k in ("event_key", "type", "label_ko", "severity", "message_ko",
                                   "ticker", "signal_id", "sent_at")}
            for e in (doc.get("new") or [])[:20]]
    # [5331ab7-11] 채널별 재시도 장부의 **요약** — 화면이 "다음 실행에서 다시 보낼 알림"을 보여 준다.
    retry_rows = [{k: e.get(k) for k in ("event_key", "channel", "type", "attempts")}
                  for e in (doc.get("retry_pending") or [])[:20] if isinstance(e, dict)]
    return {
        "schema_version": 1,
        "generated_at": doc.get("generated_at"),
        "as_of": doc.get("as_of"),
        # §10⑥: 알림 요약도 실행 상태를 함께 보여 준다(장애 시 화면이 판단 보류를 알 수 있게).
        "data_status": data_status_of(doc.get("data_status")),
        "outage_id": doc.get("outage_id"),
        "n_events": doc.get("n_events", 0),
        "n_new": doc.get("n_new", 0),
        "n_suppressed": doc.get("n_suppressed", 0),
        "n_retried": doc.get("n_retried", 0),
        "n_retry_suppressed": doc.get("n_retry_suppressed", 0),
        "retry_pending": retry_rows,
        "new": rows,
        "channels": doc.get("channels") or {},
        "note_ko": doc.get("note_ko") or "같은 signal_id·event_key 는 다시 보내지 않는다.",
    }


def dumps(payload) -> str:
    """파일로 나가는 **정확한 문자열** — 해시가 이 바이트와 일치해야 한다."""
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _serialize(payload) -> bytes:
    return dumps(payload).encode("utf-8")


#: 매니페스트에 반드시 들어가는 핵심 공개 파일 — 서비스원커가 먼저 받는 세트다.
CORE_PUBLIC_FILES = ("meta.json", "signals/today.json", "health.json", "debate.json",
                     "notifications.json", "ledger/summary.json", "ledger/records.json",
                     "ledger/portfolio.json", "radar/13f.json", "config.json")
MANIFEST_NAME = "deploy_manifest.json"


def files_version(files: dict[str, bytes]) -> str:
    """**모든 공개 파일 해시**로 만든 세트 버전(감사 12).

    `inputs_hash|snapshot_id` 만 쓰면 health·토론·알림만 바뀐 배포가 같은 버전을 가져
    서비스원커가 새 세트로 전환하지 않는다. 파일 목록·순서가 달라지면 다른 버전이 된다.
    """
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(files[name]).digest())
    return "sha256:" + digest.hexdigest()[:40]


def attach_bundle(public: dict, signals: dict | None) -> dict:
    """파일 해시 → 통합 `bundle_version` → `deploy_manifest.json`(감사 12).

    두 단계인 이유: `meta.json`·`health.json` 도 버전을 담으므로, 버전을 심은 뒤의 바이트로 해시를
    다시 계산해야 매니페스트가 **실제 파일**과 맞는다. 버전 계산 시점에는 그 필드를 `None` 으로 둔다.
    """
    if not public:
        return public
    stamped = ["meta.json", "health.json"]
    for name in stamped:
        if isinstance(public.get(name), dict):
            public[name]["bundle_version"] = None
    version = files_version({name: _serialize(payload) for name, payload in public.items()})
    for name in stamped:
        if isinstance(public.get(name), dict):
            public[name]["bundle_version"] = version
    final = {name: _serialize(payload) for name, payload in public.items()}
    manifest = {
        "schema_version": 1,
        "bundle_version": version,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "as_of": (signals or {}).get("as_of"),
        "snapshot_id": (signals or {}).get("snapshot_id"),
        "inputs_hash": (signals or {}).get("inputs_hash"),
        "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                  for name, data in sorted(final.items())},
        "note_ko": "모든 핵심 공개 파일의 해시로 만든 발행 세트 — 서비스원커가 이 세트만 캐시한다.",
    }
    public[MANIFEST_NAME] = manifest
    return public


def build(signals: dict | None, folds: dict | None, records: list[dict] | None, rules: dict,
          *, radar: dict | None = None, portfolio_state: dict | None = None,
          watch: dict | None = None, params: dict | None = None, veto: dict | None = None,
          params_history: list[dict] | None = None, pilot_eval: dict | None = None,
          debate_reports: dict | None = None, debate_comparison: dict | None = None,
          health: dict | None = None, notifications: dict | None = None,
          debate_events: dict | None = None) -> dict:
    out: dict[str, dict] = {}
    debate_details: dict[str, dict] = {}
    debate_doc = build_debate(debate_reports, records, debate_comparison, params,
                              (signals or {}).get("as_of"), debate_events=debate_events,
                              details=debate_details)
    # 상세 파일은 **매니페스트·크기 검사·금지 키 검사**를 같은 규칙으로 통과한다(감사 09).
    out.update(debate_details)
    out["meta.json"] = build_meta(signals, debate_reports, debate_comparison, health)
    out["signals/today.json"] = build_today(signals)
    out["ledger/summary.json"] = build_ledger(signals, records, portfolio_state, folds=folds,
                                              params_history=params_history, pilot_eval=pilot_eval)
    out["ledger/records.json"] = build_records(signals, records)
    out["ledger/portfolio.json"] = build_portfolio(signals, portfolio_state)
    out["radar/13f.json"] = build_radar(radar)
    out["config.json"] = build_config(params, veto)
    out["folds.json"] = build_folds(folds)
    out["watchlist.json"] = build_watchlist(watch)
    out["debate.json"] = debate_doc
    out["health.json"] = build_health(health, signals)
    out["notifications.json"] = build_notifications(notifications)

    watch_rows = {w.get("ticker"): w for w in (watch or {}).get("watchlist") or []}
    debate_rows = debate_doc.get("by_ticker") or {}
    position_rows = {p.get("ticker"): p for p in (portfolio_state or {}).get("positions") or []}
    tickers = sorted({s.get("ticker") for s in (signals or {}).get("signals") or []}
                     | set(watch_rows) | set(position_rows))
    for ticker in tickers:
        out[f"tickers/{ticker}.json"] = build_ticker(signals, ticker, watch_rows.get(ticker),
                                                     debate_rows.get(ticker), position_rows.get(ticker))

    allow = set(rules.get("allow", []))
    public = {name: filter_public(payload, allow) for name, payload in out.items()}
    # 발행 세트 버전·매니페스트는 **필터 뒤**에 만든다 — `files` 사전의 키가 파일 이름이라
    # 공개 허용 목록 필터를 태우면 지워진다.
    public = attach_bundle(public, signals)
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
    ap.add_argument("--params-history", default="data/params_history.json")
    ap.add_argument("--pilot", default="data/ledger/pilot_eval.json")
    ap.add_argument("--veto", default=str(DEFAULT_VETO))
    ap.add_argument("--debate-reports", default="data/debate_reports.json")
    ap.add_argument("--debate-comparison", default="data/ledger/debate_comparison.json")
    ap.add_argument("--debate-events", default="data/ledger/debate_events",
                    help="원장이 확정한 편입 이벤트 폴더(02 — 편입을 다시 계산하지 않는다)")
    ap.add_argument("--health", default="data/health.json")
    ap.add_argument("--notifications", default="data/notifications_latest.json")
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
                veto=_load_yaml(Path(args.veto)),
                params_history=_load(Path(args.params_history)),
                pilot_eval=_load(Path(args.pilot)),
                debate_reports=_load(Path(args.debate_reports)),
                debate_comparison=_load(Path(args.debate_comparison)),
                health=_load(Path(args.health)),
                notifications=_load(Path(args.notifications)),
                debate_events=load_debate_events(Path(args.debate_events)))
    root = Path(args.out)
    for name, payload in out.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(dumps(payload), encoding="utf-8")
    print(json.dumps({"written": [str(root / n) for n in out],
                      "bundle_version": (out.get(MANIFEST_NAME) or {}).get("bundle_version"),
                      "signals_status": (signals or {}).get("status", "missing")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
