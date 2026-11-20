"""codex_debate.context — 토론 입력 패킷(`context.json`) 생성 (기획서 §3.9.2·§3.11.2).

[고정] 스킬은 **이 파일에 있는 숫자만** 말할 수 있다. 그래서 숫자를 만드는 곳은 여기 한 곳뿐이고,
`context_hash` 를 리포트에 박아 "어떤 입력으로 토론했는지"를 나중에 대조한다.

- `context.json` 은 `data/debate/context/{TICKER}.json` 에 저장한다(**로컬 전용**, .gitignore).
- 뉴스 본문은 앞 1,500자만 넣고, 본문 안의 프롬프트 인젝션 패턴을 탐지하면 그 기사를 **뺀다**
  (`injection_detected: true` → 코드가 `NEWS_INJECTION` Veto 를 세운다).
- 13F 는 **45일 지연**을 필드로 명시한다(방향 판단 금지).
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
SKILLS_DIR = PACKAGE_DIR / "skills"
PROMPTS_DIR = PACKAGE_DIR / "prompts"
SCHEMAS_DIR = PACKAGE_DIR / "schemas"
DEFAULT_CONTEXT_DIR = Path("data/debate/context")

INVESTOR_SKILLS: tuple[str, ...] = ("buffett", "lynch", "druckenmiller", "gates_13f")
ALL_SKILLS: tuple[str, ...] = INVESTOR_SKILLS + ("risk_guard", "committee_chair")

GRADES: tuple[str, ...] = ("STRONG_BUY", "BUY", "WATCH", "NONE")
GRADE_RANK: dict[str, int] = {"NONE": 0, "WATCH": 1, "BUY": 2, "STRONG_BUY": 3}
INTENTS: tuple[str, ...] = ("pending_buy", "pending_hold_cash", "no_action")

#: Veto 12종 — `config/veto_rules.yaml` 의 `name` 과 같은 이름을 쓴다(코드가 아니라 이름으로 소통).
VETO_CODES: tuple[str, ...] = (
    "EARNINGS_WINDOW", "DATA_STALE", "STRUCTURAL_8K", "GOING_CONCERN",
    "LIQUIDITY", "MACRO_EXTREME", "SECTOR_CAP", "POSITION_CAP",
    "THESIS_BREAK", "SPLIT_ANOMALY", "NEWS_INJECTION", "EVIDENCE_MISSING",
)
#: blocking 기본값이 true 인 규칙(`config/veto_rules.yaml`). V11 은 코드가 세운다.
BLOCKING_VETO_CODES: tuple[str, ...] = (
    "EARNINGS_WINDOW", "DATA_STALE", "STRUCTURAL_8K", "GOING_CONCERN", "LIQUIDITY",
    "MACRO_EXTREME", "SECTOR_CAP", "POSITION_CAP", "THESIS_BREAK", "SPLIT_ANOMALY",
    "NEWS_INJECTION",
)

MAX_QUOTE_CHARS = 200
BODY_EXCERPT_CHARS = 1500
NEWS_WINDOW_HOURS = 48
FILING_13F_LAG_DAYS = 45
WATERMARK_13F_KO = "45일 전 분기말 기준"
MAX_8K_ITEMS = 5
#: 뉴스 본문 **정규화 버전** — 본문 해시(`content_hash`)가 무엇을 기준으로 만든 값인지 밝힌다(감사 P1-1).
#: 정규화 규칙을 바꾸면 올린다(해시가 달라져 재판독 대상이 된다).
NORMALIZER_VERSION = "news-norm-v1"
#: `context_hash` 에서 빼는 휘발성 필드 — 벽시게라서 해시에 넣으면 재개 키가 매번 달라진다.
VOLATILE_KEYS: tuple[str, ...] = ("retrieved_at", "news_retrieved_at")
#: 격리 사유 — 본문·헤드라인 모두 "기사 하나"만 빼면 되는 경우(2단계 격리).
QUARANTINE_BODY = "prompt_injection_body"
QUARANTINE_HEADLINE = "prompt_injection_headline"
#: 판단 경로 무결성 훼손 — 뉴스 본문 밖 필드(공시·자유 텍스트)에 인젝션이 침투한 경우.
INTEGRITY_NON_NEWS = "non_news_field"

#: 프롬프트 인젝션 패턴 — 탐지되면 그 기사는 **판독 대상에서 제외**한다(§3.9.4 6).
INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ignore_previous",
     re.compile(r"ignore\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above|earlier)\s+"
                r"(?:instructions?|prompts?|rules?|directions?)", re.IGNORECASE)),
    ("disregard_previous",
     re.compile(r"disregard\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above)\s+"
                r"(?:instructions?|prompts?|rules?)", re.IGNORECASE)),
    ("system_prompt",
     re.compile(r"(?:system|developer)\s+prompt|reveal\s+your\s+(?:instructions?|prompt)", re.IGNORECASE)),
    ("role_override",
     re.compile(r"you\s+are\s+now\s+(?:a|an|the)?\s*\w+|act\s+as\s+(?:an?\s+)?(?:admin|unrestricted)", re.IGNORECASE)),
    ("korean_ignore",
     re.compile(r"(?:이전|앞|위|앞선)\s*(?:의\s*)?(?:지시|명령|지침|프롬프트)(?:를|을|는|은)?\s*"
                r"(?:무시|잊|따르지\s*마)")),
    ("korean_follow",
     re.compile(r"(?:다음|아래|이)\s*(?:의\s*)?(?:지시|명령|지침|프롬프트)(?:를|을|는|은)?\s*"
                r"(?:따르|따른|따라|수행|실행)")),
    ("korean_output_json",
     re.compile(r"(?:출력|응답|답변)(?:을|를|은|는)?\s*(?:다음|아래)\s*(?:의\s*)?(?:형식|JSON|json)")),
    ("veto_override",
     re.compile(r"veto[_\s]?override|veto\s*(?:를|을)?\s*해제|override\s+(?:the\s+)?veto", re.IGNORECASE)),
)

_WS_RE = re.compile(r"\s+")


# ── 텍스트 정규화·인용 대조 ──────────────────────────────────────────────
def normalize_text(text: str) -> str:
    """유니코드 NFC 정규화 + 공백 축약. 인용 대조는 항상 이 함수를 거친다."""
    if not text:
        return ""
    return _WS_RE.sub(" ", unicodedata.normalize("NFC", str(text))).strip()


def excerpt_body(text: str | None, limit: int = BODY_EXCERPT_CHARS) -> str:
    """본문 앞 `limit`자. 줄바꿈은 공백으로 접어 인용 대조를 단순하게 만든다."""
    if not text:
        return ""
    return normalize_text(text)[:limit]


def quote_in_body(quote: str, body: str) -> bool:
    """인용이 본문에 **글자 그대로** 있는지(공백·NFC 정규화 후 부분 문자열)."""
    q = normalize_text(quote)
    if not q:
        return False
    return q in normalize_text(body)


def quote_sha256(quote: str) -> str:
    """저장용 인용 해시 — 원문 문자열은 리포트에 남기지 않는다(부록 G)."""
    return hashlib.sha256(normalize_text(quote).encode("utf-8")).hexdigest()


# ── 프롬프트 인젝션 ────────────────────────────────────────────────────────
def detect_prompt_injection(text: str | None) -> list[str]:
    """본문에서 인젝션 패턴 이름 목록을 돌려준다(없으면 빈 리스트)."""
    if not text:
        return []
    hits = [name for name, pattern in INJECTION_PATTERNS if pattern.search(text)]
    return hits


def content_hash(text: str | None) -> str:
    """기사 버전 해시 — 정규화된 본문 발췌의 sha256(`sha256:…`).

    같은 기사라도 본문이 바뀌면 해시가 달라진다. 재판독·재개 판단의 근거다(감사 P1-1).
    """
    return "sha256:" + hashlib.sha256(normalize_text(text or "").encode("utf-8")).hexdigest()


def _strip_volatile(node: Any) -> Any:
    """해시에서 빼는 휘발성 필드(`retrieved_at` 류)를 재귀적으로 제거한다."""
    if isinstance(node, dict):
        return {k: _strip_volatile(v) for k, v in node.items() if k not in VOLATILE_KEYS}
    if isinstance(node, list):
        return [_strip_volatile(v) for v in node]
    return node


def scan_fields(node: Any, *, skip_keys: Iterable[str] = ("url", "accession"),
                path: str = "") -> list[dict]:
    """뉴스 본문 **밖** 필드에서 인젝션 패턴을 찾는다 — 격리로 제거할 수 없는 자리다.

    공시 항목 설명·자유 텍스트처럼 기사를 빠도 남는 자리에 지시문이 있으면,
    "문제 기사만 빼고 계속한다"로는 판단 경로의 무결성을 보장할 수 없다(2단계 격리의 두 번째 단계).
    """
    skip = set(skip_keys)
    found: list[dict] = []
    if isinstance(node, str):
        hits = detect_prompt_injection(node)
        if hits:
            found.append({"field": path or "$", "patterns": hits})
        return found
    if isinstance(node, dict):
        for key, value in node.items():
            if key in skip:
                continue
            found.extend(scan_fields(value, skip_keys=skip, path=f"{path}.{key}" if path else str(key)))
        return found
    if isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            found.extend(scan_fields(value, skip_keys=skip, path=f"{path}[{index}]"))
    return found


# ── 스킬·프롬프트 로딩 ────────────────────────────────────────────────────
def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """아주 단순한 YAML front matter 파서(`key: value` 한 줄짜리만)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict[str, Any] = {}
    for line in parts[1].splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if value.lower() in ("true", "false"):
            meta[key.strip()] = value.lower() == "true"
            continue
        try:
            number = float(value)
        except ValueError:
            meta[key.strip()] = value.strip('"').strip("'")
        else:
            meta[key.strip()] = int(number) if number.is_integer() else number
    return meta, parts[2].lstrip("\n")


def load_skill(name: str) -> str:
    """스킬 본문(front matter 제외). 파일이 없으면 예외 — 조용히 빈 프롬프트로 돌지 않는다."""
    path = SKILLS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"스킬 파일이 없다: {path}")
    _, body = parse_front_matter(path.read_text(encoding="utf-8"))
    return body


def skill_meta(name: str) -> dict[str, Any]:
    path = SKILLS_DIR / f"{name}.md"
    meta, _ = parse_front_matter(path.read_text(encoding="utf-8"))
    return meta


def load_prompt(round_name: str, **subs: Any) -> str:
    """`prompts/{round_name}.md` 를 읽어 `{{KEY}}` 를 치환한다."""
    path = PROMPTS_DIR / f"{round_name}.md"
    if not path.exists():
        raise FileNotFoundError(f"프롬프트 파일이 없다: {path}")
    text = path.read_text(encoding="utf-8")
    for key, value in subs.items():
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False, indent=2)
        text = text.replace("{{" + key + "}}", value)
    return text


def prompt_hash(skill: str | None = None, round_name: str | None = None) -> str:
    """프롬프트·스킬 본문 해시. 인자 없으면 6스킬 + 4프롬프트 전체."""
    h = hashlib.sha256()
    skills = [skill] if skill else list(ALL_SKILLS)
    for name in skills:
        path = SKILLS_DIR / f"{name}.md"
        h.update(path.read_bytes() if path.exists() else name.encode("utf-8"))
    rounds = [round_name] if round_name else ["round1", "round2", "round3_guard", "round3_chair"]
    for name in rounds:
        path = PROMPTS_DIR / f"{name}.md"
        h.update(path.read_bytes() if path.exists() else name.encode("utf-8"))
    return "sha256:" + h.hexdigest()


def schema_path(round_name: str) -> Path:
    mapping = {
        "round1": "round1.schema.json",
        "round2": "round2.schema.json",
        "round3_guard": "round3_guard.schema.json",
        "round3_chair": "round3_chair.schema.json",
        "debate_reports": "debate_reports.schema.json",
    }
    if round_name not in mapping:
        raise KeyError(f"알 수 없는 라운드: {round_name}")
    return SCHEMAS_DIR / mapping[round_name]


def load_schema(round_name: str) -> dict:
    return json.loads(schema_path(round_name).read_text(encoding="utf-8"))


#: `codex exec --output-schema`·OpenAI `json_schema(strict)` 가 받지 않는 검증 키워드.
#: 그런 제약은 **코드가** 강제한다(점수 클램프·인용 200자 절단·개수 제한).
STRICT_UNSUPPORTED_KEYWORDS: frozenset[str] = frozenset({
    "minLength", "maxLength", "pattern", "format", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minItems", "maxItems", "uniqueItems", "contains", "minContains", "maxContains",
    "minProperties", "maxProperties", "patternProperties", "propertyNames",
})


def to_strict_schema(node: Any) -> Any:
    """스키마 사본에서 strict 모드가 거부하는 키워드를 걷어낸다(구조·enum·required 는 유지)."""
    if isinstance(node, dict):
        return {key: to_strict_schema(value) for key, value in node.items()
                if key not in STRICT_UNSUPPORTED_KEYWORDS}
    if isinstance(node, list):
        return [to_strict_schema(value) for value in node]
    return node


# ── 종목 선별 ────────────────────────────────────────────────────────────
def _now_iso() -> str:
    """수집(정규화) 시각 — UTC ISO 문자열(Z)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _news_count_48h(signal: dict, cutoff: datetime | None) -> int:
    news = signal.get("news") or []
    if cutoff is None:
        return len(news)
    start = cutoff - timedelta(hours=NEWS_WINDOW_HOURS)
    count = 0
    for item in news:
        ts = _parse_ts(item.get("published_at") or item.get("available_at"))
        if ts is not None and start <= ts <= cutoff:
            count += 1
    return count


def _has_new_8k(signal: dict, as_of: str | None) -> bool:
    for filing in signal.get("filings") or []:
        if str(filing.get("form", "")).upper() != "8-K":
            continue
        filed = str(filing.get("filed_date") or "")
        if not as_of or filed == as_of:
            return True
    return False


def select_targets(
    quant_signals: dict,
    tickers: Iterable[str] | None = None,
    top: int | None = None,
    max_tickers: int = 6,
) -> list[dict]:
    """토론 대상 종목 선별 (§3.9.2).

    우선순위: `--tickers` 지정 → 신호 후보(`entry_score` 높은 순) → 48시간 내 신규 뉴스 3건 이상 →
    오늘 신규 8-K 제출. 워치리스트 밖 종목은 대상이 될 수 없다.
    """
    signals = [s for s in (quant_signals.get("signals") or []) if s.get("ticker")]
    watch = {w.get("ticker"): w for w in (quant_signals.get("watchlist") or []) if w.get("ticker")}
    as_of = quant_signals.get("as_of")
    cutoff = _parse_ts(quant_signals.get("data_cutoff_at"))
    limit = max_tickers if top is None else max(0, min(int(top), max_tickers))
    if limit == 0:
        return []

    out: list[dict] = []
    seen: set[str] = set()

    def add(ticker: str, reason: str, signal: dict | None = None) -> None:
        ticker = str(ticker).upper().strip()
        if not ticker or ticker in seen or len(out) >= limit:
            return
        seen.add(ticker)
        out.append({
            "ticker": ticker,
            "signal_id": (signal or {}).get("signal_id"),
            "reasons": [reason],
            "news_count_48h": _news_count_48h(signal or {}, cutoff),
            "has_new_8k": _has_new_8k(signal or {}, as_of),
        })

    if tickers:
        by_ticker = {str(s.get("ticker", "")).upper(): s for s in signals}
        for raw in tickers:
            ticker = str(raw).strip().upper()
            if not ticker:
                continue
            if ticker in by_ticker:
                add(ticker, "explicit", by_ticker[ticker])
            elif ticker in watch:
                add(ticker, "explicit_watchlist")
            else:
                # 워치리스트·신호 밖 종목은 분석 대상으로 추가할 수 없다(§3.11.1).
                continue
        return out[:limit]

    ranked = sorted(signals, key=lambda s: (-float(s.get("entry_score") or 0.0), str(s.get("ticker"))))
    for sig in ranked:
        add(sig["ticker"], "signal", sig)
    for sig in ranked:
        if _news_count_48h(sig, cutoff) >= 3:
            add(sig["ticker"], "news_3plus", sig)
    for sig in ranked:
        if _has_new_8k(sig, as_of):
            add(sig["ticker"], "filing_8k", sig)
    return out[:limit]


# ── 스냅샷 입력(선택) ─────────────────────────────────────────────────────
def load_snapshot_inputs(as_of: str, root: str | Path = "data/snapshots") -> dict | None:
    """`data/snapshots/{as_of}/inputs.json`(로컬 전용). 없으면 None — 테스트는 넘겨받은 dict 만 쓴다."""
    path = Path(root) / str(as_of) / "inputs.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _snapshot_row(snapshot: dict | None, block: str, ticker: str) -> dict:
    if not isinstance(snapshot, dict):
        return {}
    table = snapshot.get(block)
    if isinstance(table, dict):
        row = table.get(ticker)
        return row if isinstance(row, dict) else {}
    if isinstance(table, list):
        for row in table:
            if isinstance(row, dict) and str(row.get("ticker", "")).upper() == ticker:
                return row
    return {}


# ── 13F ───────────────────────────────────────────────────────────────────
def _normalize_13f(rows: Iterable[dict]) -> list[dict]:
    out: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        out.append({
            "fund": row.get("fund") or row.get("filer_name") or row.get("filer"),
            "period_end": row.get("period_end"),
            "filed_date": row.get("filed_date") or row.get("acceptance_datetime"),
            "delta_shares_pct": row.get("delta_shares_pct", row.get("delta_pct")),
            "lag_days": FILING_13F_LAG_DAYS,
            "watermark_ko": WATERMARK_13F_KO,
        })
    return out


def holdings_for_ticker(holdings_13f: Any, ticker: str, radar: dict | None = None) -> list[dict]:
    """13F 보유 변화 입력을 정규화한다(dict[ticker]·list·radar 어느 형태든)."""
    rows: list[Any] = []
    if isinstance(holdings_13f, dict):
        rows = list(holdings_13f.get(ticker) or [])
    elif isinstance(holdings_13f, list):
        rows = [r for r in holdings_13f if not isinstance(r, dict) or r.get("ticker") in (None, ticker)]
    elif holdings_13f is None and isinstance(radar, dict):
        for filer in radar.get("filers") or []:
            for holding in filer.get("holdings") or []:
                if str(holding.get("ticker", "")).upper() == ticker:
                    rows.append({**holding, "fund": filer.get("filer_name") or filer.get("name")})
    return _normalize_13f(rows)


# ── 본문 수집(선택) ───────────────────────────────────────────────────────
def fetch_article_body(url: str, timeout: int = 10) -> str:
    """기사 원문 URL 에서 본문 텍스트를 긁는다(로컬 전용, 실패하면 빈 문자열).

    기본값은 `fetch_body=False` 다 — 본문 발췌는 저장소에 남기지 않는다(2.2.3).
    """
    if not url:
        return ""
    try:
        import requests  # 지역 import — 오프라인 테스트에서 모듈 로딩 비용을 만들지 않는다

        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "us-stock-advisor/0.1 (debate body fetch)"})
        resp.raise_for_status()
        html = resp.text or ""
    except Exception:  # noqa: BLE001 — 본문 수집 실패는 그 기사만 본문 없이 둔다
        return ""
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    return excerpt_body(html)


# ── context.json ─────────────────────────────────────────────────────────
def news_id_for(item: dict) -> str:
    """기사 ID. 수집기가 준 `id`/`news_id` 가 있으면 그것을, 없으면 URL 해시."""
    for key in ("news_id", "id"):
        if item.get(key):
            return str(item[key])
    url = str(item.get("url") or "")
    if not url:
        return "news-unknown"
    return "news-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def _signal_for(ticker: str, quant_signals: dict) -> tuple[dict, dict]:
    signals = quant_signals.get("signals") or []
    signal = next((s for s in signals if str(s.get("ticker", "")).upper() == ticker), {})
    watch = next((w for w in quant_signals.get("watchlist") or []
                  if str(w.get("ticker", "")).upper() == ticker), {})
    return signal, watch


def _grade_of(signal: dict, watch: dict) -> str:
    for key in ("trade_grade", "decision", "type"):
        value = str(signal.get(key) or "").upper()
        if value in GRADES:
            return value
    return "NONE"


def _intent_of(signal: dict) -> str:
    value = signal.get("quant_intent")
    return value if value in INTENTS else "no_action"


def _held_state(ticker: str, quant_signals: dict) -> str:
    portfolio = quant_signals.get("portfolio") or {}
    for pos in portfolio.get("positions") or []:
        if str(pos.get("ticker", "")).upper() == ticker and float(pos.get("weight") or 0) > 0:
            return "held"
    return "none"


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _first_num(*values: Any) -> float | None:
    for value in values:
        got = _num(value)
        if got is not None:
            return got
    return None


def _veto_name(entry: Any) -> str | None:
    if isinstance(entry, str):
        if entry in VETO_CODES:
            return entry
        return None
    if isinstance(entry, dict):
        name = entry.get("name")
        if name in VETO_CODES:
            return str(name)
        return None
    return None


def _allowed_prices(signal: dict, valuation: dict, extra: Iterable[Any] | None = None) -> list[float]:
    prices: list[float] = []
    for tranche in signal.get("entry_plan") or []:
        prices.append(_num(tranche.get("price_ref")))
    prices.extend([
        _num((signal.get("invalidation") or {}).get("price")),
        _num(valuation.get("price")),
        _num(signal.get("generated_price")),
        _num(valuation.get("iv_low")), _num(valuation.get("iv_median")), _num(valuation.get("iv_high")),
        _num((signal.get("target_band") or {}).get("lo")), _num((signal.get("target_band") or {}).get("hi")),
    ])
    prices.extend(_num(v) for v in (extra or []))
    clean = sorted({round(p, 2) for p in prices if p is not None and p > 0})
    return clean


def _macro_block(quant_signals: dict, snapshot: dict | None) -> dict:
    regime_block = quant_signals.get("regime") or {}
    macro = (snapshot or {}).get("macro") or {}
    previous = (snapshot or {}).get("macro_previous") or {}
    real_rate = None
    dff, cpi_yoy = _num(macro.get("dff")), _num(macro.get("cpi_yoy"))
    if dff is not None and cpi_yoy is not None:
        real_rate = round(dff - cpi_yoy, 2)  # 퍼센트 포인트(코드가 계산, 스킬은 인용만)
    dxy_3m = None
    now_level, prev_level = _num(macro.get("dtwexbg")), _num(previous.get("dtwexbg"))
    if now_level and prev_level:
        dxy_3m = round((now_level / prev_level - 1.0) * 100.0, 2)
    hy_oas_bp = _num(macro.get("hy_oas_bp"))
    return {
        "regime": regime_block.get("regime") or "unknown",
        "regime_score": _num(regime_block.get("score")),
        "real_rate": real_rate,
        "t10y2y": _num(macro.get("t10y2y")),
        "hy_oas": round(hy_oas_bp / 100.0, 2) if hy_oas_bp is not None else None,
        "vix": _num(macro.get("vix")),
        "dxy_3m": dxy_3m,
        "units": {"real_rate": "percent", "t10y2y": "percent", "hy_oas": "percent", "dxy_3m": "percent_3m_change"},
    }


def build_context(
    ticker: str,
    quant_signals: dict,
    news_items: Iterable[dict] | None = None,
    filings_8k: Iterable[dict] | None = None,
    holdings_13f: Any = None,
    fetch_body: bool = False,
    snapshot: dict | None = None,
    body_fetcher: Callable[[str], str] | None = None,
    radar: dict | None = None,
    extra_prices: Iterable[Any] | None = None,
) -> dict:
    """§3.11.2 입력 패킷 1종목분을 만든다(숫자는 여기서만 만들어진다)."""
    ticker = str(ticker).upper().strip()
    signal, watch = _signal_for(ticker, quant_signals)
    valuation_in = signal.get("valuation") or {}
    methods = valuation_in.get("methods") or {}
    indicators = signal.get("indicators") or {}
    watch_metrics = (watch.get("metrics") or {})
    snap_valuation = _snapshot_row(snapshot, "valuation_inputs", ticker)

    price = _first_num(valuation_in.get("price"), signal.get("generated_price"), snap_valuation.get("price"))
    atr_pct = _num(indicators.get("atr_pct"))
    atr14 = _num(indicators.get("atr14"))
    if atr14 is None and price is not None and atr_pct is not None:
        atr14 = round(price * atr_pct, 2)

    news_list: list[dict] = []
    excluded: list[dict] = []
    injection_detected = False
    retrieved_at = _now_iso()
    raw_news = list(news_items) if news_items is not None else list(signal.get("news") or [])
    for item in raw_news:
        if not isinstance(item, dict):
            continue
        nid = news_id_for(item)
        body = item.get("body_excerpt_1500") or item.get("body") or item.get("body_excerpt") or ""
        if not body and fetch_body:
            fetcher = body_fetcher or fetch_article_body
            body = fetcher(str(item.get("url") or "")) or ""
        body = excerpt_body(body)
        headline = str(item.get("headline") or item.get("title") or "")
        # V11 2단계: 문제 **기사 하나만** 격리하고 나머지 유효 기사로 판단을 계속한다.
        # 격리 목록(ID·사유·패턴)은 `excluded_news` 에 남겨 "왜 뺐는지"를 감사할 수 있게 한다.
        hits = detect_prompt_injection(body)
        headline_hits = detect_prompt_injection(headline)
        if hits or headline_hits:
            injection_detected = True
            excluded.append({"id": nid, "reason": QUARANTINE_BODY if hits else QUARANTINE_HEADLINE,
                             "patterns": sorted(set(hits) | set(headline_hits)),
                             "source": item.get("source"), "url": item.get("url"),
                             "content_hash": content_hash(body or headline)})
            continue
        news_list.append({
            "id": nid,
            "headline": item.get("headline") or item.get("title"),
            "url": item.get("url"),
            "published_at": item.get("published_at") or item.get("available_at"),
            "source": item.get("source"),
            "body_excerpt_1500": body,
            "body_available": bool(body),
            # 기사 **버전** — 무엇을 읽었는지 나중에 대조할 수 있게(감사 P1-1).
            "content_hash": content_hash(body) if body else content_hash(headline),
            "retrieved_at": item.get("retrieved_at") or retrieved_at,
            "normalizer_version": item.get("normalizer_version") or NORMALIZER_VERSION,
        })
    news_list = news_list[:10]

    eights: list[dict] = []
    candidates = list(filings_8k) if filings_8k is not None else list(signal.get("filings") or [])
    as_of = str(quant_signals.get("as_of") or signal.get("as_of") or "")
    for filing in candidates:
        if not isinstance(filing, dict):
            continue
        form = str(filing.get("form") or filing.get("form_type") or "").upper()
        if form != "8-K":
            continue
        eights.append({
            "accession": filing.get("accession"),
            "filed_date": filing.get("filed_date"),
            "items": list(filing.get("items") or []),
            "url": filing.get("url"),
        })
    eights = eights[:MAX_8K_ITEMS]

    code_vetoes = sorted({name for name in (_veto_name(v) for v in signal.get("veto") or []) if name})
    # 2단계 격리의 **두 번째 단계**: 기사를 빼도 남는 자리(공시 항목·자유 텍스트)에 지시문이 있으면
    # 격리만으로는 무결성을 보장할 수 없다 → 이 종목은 `NEWS_INJECTION`(V11) 으로 차단한다.
    non_news_hits = []
    if candidates:
        non_news_hits.extend(scan_fields(candidates, skip_keys=("url", "accession", "form", "form_type"),
                                         path="filings"))
    for key in ("thesis_ko",):
        value = signal.get(key)
        if isinstance(value, str):
            hits = detect_prompt_injection(value)
            if hits:
                non_news_hits.append({"field": key, "patterns": hits})
    if non_news_hits:
        injection_detected = True
    injection_scope = (INTEGRITY_NON_NEWS if non_news_hits
                       else QUARANTINE_BODY if any(item.get("reason") == QUARANTINE_BODY for item in excluded)
                       else QUARANTINE_HEADLINE if excluded else None)
    regime = quant_signals.get("regime") or {}
    allowed_prices = _allowed_prices(signal, valuation_in, extra_prices)
    context = {
        # ── 식별 (2.2.2 세 시각 분리)
        "ticker": ticker,
        "as_of": as_of or None,
        "data_cutoff_at": signal.get("data_cutoff_at") or quant_signals.get("data_cutoff_at"),
        "signal_published_at": signal.get("signal_published_at") or quant_signals.get("signal_published_at"),
        "order_cutoff_at": signal.get("order_cutoff_at") or quant_signals.get("order_cutoff_at"),
        "quant_signal_id": signal.get("signal_id") or f"{as_of}-{ticker}-NONE",
        "snapshot_id": signal.get("snapshot_id") or quant_signals.get("snapshot_id"),
        "quant_grade": _grade_of(signal, watch),
        "quant_intent": _intent_of(signal),
        "held_state": _held_state(ticker, quant_signals),
        "params_version": signal.get("params_version") or quant_signals.get("params_version"),
        "sector": signal.get("sector") or watch.get("sector"),
        "code_vetoes": code_vetoes,
        # ── 시세·기술
        "price": price,
        "dma50": _num(indicators.get("dma50")),
        "dma200": _num(indicators.get("dma200")),
        "rsi14": _num(indicators.get("rsi14")),
        "pct_b": _num(indicators.get("pct_b")),
        "atr14": atr14,
        "atr_pct": atr_pct,
        "vol20": _first_num(signal.get("vol20"), indicators.get("vol20")),
        "rel_strength_6m": _first_num(signal.get("rel_strength_6m"), watch.get("rel_strength_6m")),
        # ── 밸류에이션
        "iv_low": _num(valuation_in.get("iv_low")),
        "iv_median": _num(valuation_in.get("iv_median")),
        "iv_high": _num(valuation_in.get("iv_high")),
        "margin": _num(valuation_in.get("margin")),
        "dcf_bear": _num(valuation_in.get("iv_dcf_bear")),
        "dcf_base": _num(methods.get("dcf_base")),
        "dcf_bull": _num(valuation_in.get("iv_dcf_bull")),
        "epv": _num(methods.get("epv")),
        "graham": _num(methods.get("graham")),
        "median_method": valuation_in.get("median_method"),
        # ── 품질
        "q": _first_num(watch.get("q"), (signal.get("components") or {}).get("quality"), snap_valuation.get("q")),
        "roic": _num(watch_metrics.get("roic")),
        "roic_spread": _num(watch_metrics.get("roic_spread")),
        "fcf_margin": _num(watch_metrics.get("fcf_margin")),
        "fcf_to_ni": _num(watch_metrics.get("fcf_to_ni")),
        "net_debt_ebitda": _num(watch_metrics.get("net_debt_ebitda")),
        "interest_cover": _num(watch_metrics.get("interest_cover")),
        "dilution_3y": _num(watch_metrics.get("dilution_3y")),
        "roe_stdev": _num(watch_metrics.get("roe_stdev")),
        # ── 성장
        "peg": _first_num(watch.get("peg"), snap_valuation.get("peg")),
        "pegy": _num(watch.get("pegy") or snap_valuation.get("pegy")),
        "rev_cagr5": _first_num(snap_valuation.get("revenue_cagr"), snap_valuation.get("rev_cagr")),
        "eps_cagr5": _first_num(snap_valuation.get("eps_cagr"), snap_valuation.get("eps_cagr5")),
        "growth": _first_num(watch.get("g"), (signal.get("components") or {}).get("growth")),
        # ── 거시
        **_macro_block(quant_signals, snapshot),
        # ── 13F (45일 시차 명시)
        "holdings_13f": holdings_for_ticker(holdings_13f, ticker, radar),
        "holdings_13f_note_ko": WATERMARK_13F_KO,
        "holdings_13f_lag_days": FILING_13F_LAG_DAYS,
        # ── 뉴스·8-K
        "news": news_list,
        "news_window_hours": NEWS_WINDOW_HOURS,
        "news_retrieved_at": retrieved_at,
        "news_normalizer_version": NORMALIZER_VERSION,
        "filings_8k": eights,
        "injection_detected": injection_detected,
        # 격리 목록·범위 — 리포트에 그대로 옮길 수 있는 형태(ID·사유·패턴·출처).
        "excluded_news": excluded,
        "injection_scope": injection_scope,
        "injection_integrity_compromised": bool(non_news_hits),
        "injection_fields": non_news_hits[:5],
        # ── 허용값·제약
        "allowed": {
            "prices": allowed_prices,
            "grades": list(GRADES),
            "intents": list(INTENTS),
            "veto_codes": list(VETO_CODES),
        },
        "constraints": {"no_new_numbers": True, "max_quote_chars": MAX_QUOTE_CHARS},
        "paper_grade": _num(signal.get("grade")),
        "entry_score": _num(signal.get("entry_score")),
        "thesis_ko": signal.get("thesis_ko"),
        "target_band": signal.get("target_band"),
        "invalidation": signal.get("invalidation"),
    }
    if regime.get("verified") is False:
        context["regime_verified"] = False
    else:
        context["regime_verified"] = bool(regime.get("verified"))
    context["context_notes"] = {
        "news_excluded": excluded,
        "units": "비율은 소수(0.21 = 21%), 거시(hy_oas·real_rate·t10y2y)는 퍼센트",
    }
    context["context_hash"] = context_hash(context)
    context["prompt_hash"] = prompt_hash()
    return context


def context_hash(context: dict) -> str:
    """`context_hash`·`prompt_hash` 자신과 **휘발성 필드**를 뺀 정규화 JSON 의 sha256.

    `retrieved_at` 은 수집 시각(벽시계)이다. 해시에 넣으면 같은 입력으로 다시 돌릴 때마다 해시가
    달라져 재개 키(`signal_id+snapshot_id+context_hash+prompt_version+model_version`)가 깨진다.
    그래서 기록은 하되 해시에서는 뺀다 — 기사 **버전**은 `content_hash`(본문)가 결정한다.
    """
    payload = {k: v for k, v in context.items() if k not in ("context_hash", "prompt_hash")}
    payload = _strip_volatile(payload)
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def save_context(context: dict, directory: str | Path = DEFAULT_CONTEXT_DIR) -> Path:
    """`data/debate/context/{TICKER}.json` 로 저장(로컬 전용, .gitignore)."""
    path = Path(directory) / f"{context['ticker']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def context_numbers(context: dict) -> set[float]:
    """서술 검증에 쓰는 '허용 숫자' 집합 — context 의 모든 수치와 그 표기 변형."""
    found: set[float] = set()

    def walk(node: Any, parent: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("context_notes", "news", "body_excerpt_1500", "headline", "thesis_ko", "reason"):
                    continue
                walk(value, key)
        elif isinstance(node, list):
            for value in node:
                walk(value, parent)
        elif isinstance(node, str):
            return
        else:
            number = _num(node)
            if number is not None:
                found.update(number_forms(number))

    walk(context)
    return found


def number_forms(value: float) -> set[float]:
    """한 수치의 표기 변형 — 반올림 자리·퍼센트 표기(0.2176 → 21.76 → 22)."""
    base = abs(float(value))
    forms: set[float] = {base, -base}
    for digits in range(0, 5):
        forms.add(round(base, digits))
        forms.add(round(-base, digits))
    for digits in range(0, 3):
        forms.add(round(base * 100.0, digits))
        forms.add(round(-base * 100.0, digits))
    return forms


__all__ = [
    "ALL_SKILLS", "BLOCKING_VETO_CODES", "BODY_EXCERPT_CHARS", "DEFAULT_CONTEXT_DIR", "GRADE_RANK",
    "GRADES", "INJECTION_PATTERNS", "INTENTS", "INVESTOR_SKILLS", "MAX_QUOTE_CHARS", "NEWS_WINDOW_HOURS",
    "REPO_ROOT", "VETO_CODES", "build_context", "context_hash", "context_numbers", "detect_prompt_injection",
    "excerpt_body", "fetch_article_body", "holdings_for_ticker", "load_prompt", "load_schema", "load_skill",
    "load_snapshot_inputs", "news_id_for", "normalize_text", "number_forms", "parse_front_matter",
    "prompt_hash", "quote_in_body", "quote_sha256", "save_context", "schema_path", "select_targets", "skill_meta",
    "to_strict_schema",
]
