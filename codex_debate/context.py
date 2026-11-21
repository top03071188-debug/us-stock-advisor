"""codex_debate.context — 토론 입력 패킷(`context.json`) 생성 (기획서 §3.9.2·§3.11.2).

[고정] 스킬은 **이 파일에 있는 숫자만** 말할 수 있다. 그래서 숫자를 만드는 곳은 여기 한 곳뿐이고,
`context_hash` 를 리포트에 박아 "어떤 입력으로 토론했는지"를 나중에 대조한다.

- `context.json` 은 `data/debate/context/{TICKER}.json` 에 저장한다(**로컬 전용**, .gitignore).
- 뉴스 본문은 앞 1,500자만 넣고, 본문·헤드라인·`source`·`url` 안의 프롬프트 인젝션을 탐지하면
  그 기사를 **뺀다**(격리 메타데이터는 `article_id`·`content_sha256`·`reason_code` 만 — 원문·매체·URL 은
  프롬프트에 되돌려 놓지 않는다). 뉴스 밖 필드(13F·업종·공시·자유 서술)에 침투했으면 격리로 지울 수
  없으므로 `injection_integrity_compromised` 를 세우고, 오케스트레이터가 **Codex 를 부르기 전에** V11 로 끝낸다.
- 숫자 검증용 표(`context_number_table`)는 수치를 **경로·값·단위**로 남긴다 — `PEG 3.5` 를 `350%` 로
  바꿔 말하는 식의 단위 세탁을 막기 위해서다.
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

from engine.data.relevance import rank_news

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
#: 격리 사유 — 기사 하나만 빼면 되는 경우(2단계 격리). 사유 코드는 리포트에 그대로 남는다.
QUARANTINE_BODY = "prompt_injection_body"
QUARANTINE_HEADLINE = "prompt_injection_headline"
QUARANTINE_SOURCE = "prompt_injection_source"
QUARANTINE_URL = "prompt_injection_url"
QUARANTINE_REASONS: tuple[str, ...] = (QUARANTINE_BODY, QUARANTINE_HEADLINE, QUARANTINE_SOURCE, QUARANTINE_URL)
#: 판단 경로 무결성 훼손 — 뉴스 밖 필드(공시·13F·자유 텍스트)에 인젝션이 침투한 경우.
INTEGRITY_NON_NEWS = "non_news_field"
#: 프롬프트 경계 직전 스캔에서 제외하는 **장부 필드** — 모델에게 가지 않는 메타데이터다.
#: (격리 목록·해시·사유 코드 자체를 다시 스캔하면 자기 참조로 오탐이 난다.)
PROMPT_SCAN_SKIP_KEYS: tuple[str, ...] = (
    "news", "excluded_news", "context_notes", "injection_detected", "injection_scope",
    "injection_fields", "injection_integrity_compromised", "context_hash", "prompt_hash",
)

#: 프롬프트 인젝션 패턴 — 탐지되면 그 기사는 **판독 대상에서 제외**한다(§3.9.4 6).
#: [감사 08] 정상 리서치·금융 기사에 흔한 표현("system prompt", "developer prompt",
#: "You are now a shareholder of record")은 **잡지 않는다** — 실제 지시문 탈취·역할 덮어쓰기만 잡는다.
#: 구분자는 공백뿐 아니라 `-`·`_`·`.` 도 허용한다(URL·슬러그로 위장한 지시문 대응).
INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ignore_previous",
     re.compile(r"ignore[\s._\-]+(?:all[\s._\-]+)?(?:the[\s._\-]+)?"
                r"(?:previous|prior|above|earlier)[\s._\-]+"
                r"(?:instructions?|prompts?|rules?|directions?|directives?)", re.IGNORECASE)),
    ("disregard_previous",
     re.compile(r"disregard[\s._\-]+(?:all[\s._\-]+)?(?:the[\s._\-]+)?"
                r"(?:previous|prior|above)[\s._\-]+"
                r"(?:instructions?|prompts?|rules?)", re.IGNORECASE)),
    ("reveal_system_prompt",
     re.compile(r"(?:reveal|show|print|output|repeat|leak|dump|expose|tell\s+me)[\s._\-]+"
                r"(?:me[\s._\-]+)?(?:your|the)[\s._\-]+"
                r"(?:system[\s._\-]+|developer[\s._\-]+|hidden[\s._\-]+|initial[\s._\-]+)?"
                r"(?:prompt|instructions?|system\s+message)", re.IGNORECASE)),
    ("system_prompt_override",
     re.compile(r"(?:new|updated|override|overwrite|forget|replace)[\s._\-]+"
                r"(?:system|developer|original)[\s._\-]+(?:prompt|instructions?)", re.IGNORECASE)),
    ("role_override",
     re.compile(r"you[\s._\-]+are[\s._\-]+now[\s._\-]+(?:an?[\s._\-]+|the[\s._\-]+)?"
                r"(?:ai|a\.i\.|assistant|model|chatbot|bot|system|developer|admin|administrator|"
                r"unrestricted|jailbroken|dan)\b", re.IGNORECASE)),
    ("act_as_admin",
     re.compile(r"act[\s._\-]+as[\s._\-]+(?:an?[\s._\-]+)?"
                r"(?:admin|administrator|root|superuser|unrestricted|developer[\s._\-]+mode)",
                re.IGNORECASE)),
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


def content_sha256(text: str | None) -> str:
    """기사 버전 영수증 — 정규화된 본문 발췌의 sha256(`sha256:…`).

    같은 기사라도 본문이 바뀌면 해시가 달라진다. 재판독·재개 판단과 "그때 읽은 그 기사인가"의
    대조 근거다(감사 P1-1 · 08).
    """
    return "sha256:" + hashlib.sha256(normalize_text(text or "").encode("utf-8")).hexdigest()


def content_hash(text: str | None) -> str:
    """`content_sha256` 의 옛 이름 — 근거 목록(`evidence[]`)이 쓰는 필드명과 호환한다."""
    return content_sha256(text)


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


def _snapshot_news(snapshot: dict | None, ticker: str) -> list[dict]:
    """비공개 스냅샷 입력의 해당 종목 뉴스 행(있으면). 없으면 빈 리스트."""
    if not isinstance(snapshot, dict):
        return []
    table = snapshot.get("news")
    if isinstance(table, dict):
        rows = table.get(ticker)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _news_match_key(row: dict) -> tuple[str, str]:
    """뉴스 행 대조 키 — (URL, 제목 영숫자). 둘 중 하나만 같아도 같은 기사로 본다."""
    url = str(row.get("url") or "").strip()
    headline = "".join(ch for ch in str(row.get("headline") or "").lower() if ch.isalnum())[:80]
    return url, headline


def _merge_snapshot_news(base: list[dict], extra: list[dict]) -> list[dict]:
    """공개 신호의 뉴스 목록에 **비공개 스냅샷의 `summary` 등**을 덧붙인다.

    신호 행이 기준이다(그날 판단에 쓴 기사 목록·순서를 바꾸지 않는다). 스냅샷 행은 같은 기사를
    찾았을 때 빈 필드만 채워 준다 — 없는 기사를 끼워 넣거나 있던 기사를 빼지 않는다.
    신호에 뉴스가 아예 없으면(공개 행에 목록이 비었으면) 스냅샷 목록을 그대로 쓴다.
    """
    if not base:
        return [dict(row) for row in extra]
    merged = [dict(row) for row in base]  # 호출자의 dict 를 건드리지 않는다
    by_url: dict[str, dict] = {}
    by_headline: dict[str, dict] = {}
    for row in extra:
        url, headline = _news_match_key(row)
        if url:
            by_url.setdefault(url, row)
        if headline:
            by_headline.setdefault(headline, row)
    for row in merged:
        url, headline = _news_match_key(row)
        match = by_url.get(url) or by_headline.get(headline)
        if not match:
            continue
        for field in ("summary", "related_tickers", "provider"):
            if not row.get(field) and match.get(field):
                row[field] = match[field]
    return merged


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
# v1.3.7 — 본문 추출기 전면 교체.
# 이전 구현은 `<script>/<style>/<noscript>` 만 지우고 **페이지 전체 텍스트 앞 1,500자**를 잘랐다.
# 그래서 Yahoo Finance·TIKR·Motley Fool 같은 페이지에서는 네비게이션 바·쿠키 배너가 본문으로 잡혔고
# (`"Oops, something went wrong Skip to navigation..."`), 봇 차단 껍데기가 "본문 1,500자"로 통과해
# 인용 대조가 전부 실패했다(`quote_missing`). 지금은 ①브라우저형 헤더 ②리다이렉트 추적(Finnhub 링크)
# ③JSON-LD `articleBody` → `<article>`/`<main>` 문단 → 메타 설명 순으로 **기사 본문만** 뽑고
# ④차단·에러 껍데기와 지나치게 짧은 발췌는 빈 문자열로 버린다.

#: 브라우저형 헤더 — 봇 UA(`us-stock-advisor/0.1`)로는 신문사가 껍데기 페이지를 준다.
BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
#: 본문으로 인정하는 최소 길이 — 이보다 짧으면 기사 본문이 아니라 껍데기·요약이다.
BODY_MIN_CHARS = 200
#: 요약(`summary`)을 본문 대신 쓰는 최소 길이(로컬 컨텍스트 전용 폴백).
SUMMARY_MIN_CHARS = 80
#: 토론 컨텍스트에 넣는 뉴스 최대 건수.
NEWS_MAX_ITEMS = 10
#: 봇 차단·에러 껍데기 표지 — 본문이 짧을 때 이 문구가 있으면 본문이 아니다.
BLOCK_PAGE_MARKERS: tuple[str, ...] = (
    "oops, something went wrong", "enable javascript", "access denied", "just a moment",
    "403 forbidden", "401 unauthorized", "are you a robot", "verify you are human",
    "checking your browser", "please enable cookies", "unusual traffic", "captcha",
    "subscribe to continue reading", "this content is not available in your region",
)
#: 본문에서 걷어내는 껍데기 요소(네비게이션·헤더·푸터·사이드바·광고·쿠키 배너·폼).
JUNK_SELECTORS: tuple[str, ...] = (
    "script", "style", "noscript", "nav", "header", "footer", "aside", "form", "figure",
    "iframe", "svg", "button", "dialog", "template",
    "[role=navigation]", "[role=banner]", "[role=contentinfo]", "[role=complementary]",
    ".nav", ".navbar", ".navigation", ".menu", ".footer", ".sidebar", ".ad", ".ads",
    ".advertisement", ".cookie", ".cookies", ".consent", ".newsletter", ".subscribe",
    ".related", ".related-articles", ".share", ".social", ".breadcrumb", ".comments",
)
#: 본문 후보 컨테이너 — 앞에서부터 우선순위, 문단 밀도로 고른다.
CONTAINER_SELECTORS: tuple[str, ...] = (
    "[itemprop=articleBody]", "div.caas-body", "div.article-body", "div.article__body",
    "div.post-content", "div.entry-content", "div.content-body", "article", "main",
    "[role=main]",
)


def _soup_of(html: str):
    """HTML → BeautifulSoup. lxml 이 없으면 파이썬 표준 파서로 떨어진다(오프라인 환경 보호)."""
    from bs4 import BeautifulSoup  # 지역 import — 오프라인 테스트에서 모듈 로딩 비용을 만들지 않는다

    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001 — lxml 미설치·파서 오류
        return BeautifulSoup(html, "html.parser")


def _walk_json(node: Any):
    """JSON-LD 안의 모든 dict 를 훑는다(`@graph`·중첩 구조 대응)."""
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk_json(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_json(value)


def _jsonld_body(soup) -> str:
    """JSON-LD `articleBody` — 신문사가 구조화 데이터로 **본문 전문**을 주는 경우가 많다.

    `description` 은 여기서 쓰지 않는다: 신문사가 150~200자로 **잘라 놓은** 요약을 넣기 때문에
    그것을 "본문"으로 인정하면 1,500자 짜리 껍데기와 같은 실수를 반복한다(메타 폴백에서만 본다).
    """
    best = ""
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (script.string or script.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        for node in _walk_json(data):
            if not isinstance(node, dict):
                continue
            value = node.get("articleBody")
            if isinstance(value, str) and len(normalize_text(value)) > len(best):
                best = normalize_text(value)
        if len(best) >= BODY_EXCERPT_CHARS:
            break
    return best


def _jsonld_description(soup) -> str:
    """JSON-LD `description` — 메타 폴백용(본문 대체가 아니다)."""
    best = ""
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (script.string or script.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        for node in _walk_json(data):
            if isinstance(node, dict):
                value = node.get("description")
                if isinstance(value, str) and len(normalize_text(value)) > len(best):
                    best = normalize_text(value)
    return best


def _container_body(soup) -> str:
    """`<article>`·`<main>`·본문 컨테이너의 `<p>` 문단 — 껍데기 요소를 지운 뒤 밀도로 고른다."""
    for junk in JUNK_SELECTORS:
        for element in soup.select(junk):
            element.decompose()
    candidates: list[tuple[int, int, str]] = []
    for selector in CONTAINER_SELECTORS:
        try:
            found = soup.select(selector)
        except Exception:  # noqa: BLE001 — 선택자 문법 오류는 그 후보만 건너뛴다
            continue
        for element in found:
            paragraphs = [normalize_text(p.get_text(" ")) for p in element.find_all("p")]
            paragraphs = [p for p in paragraphs if len(p) >= 40]
            total = sum(len(p) for p in paragraphs)
            if len(paragraphs) >= 2 and total >= BODY_MIN_CHARS:
                candidates.append((total, len(paragraphs), " ".join(paragraphs)))
        if candidates:
            # 우선순위가 높은 선택자에서 본문을 찾았으면 더 넓은 선택자로 내려가지 않는다.
            break
    if candidates:
        candidates.sort(key=lambda row: (-row[0], -row[1]))
        return candidates[0][2]
    # 마지막 수단: 페이지 전체의 긴 문단(제목·캡션 제외). 문단이 하나뿐이면 160자 이상일 때만 인정한다.
    paragraphs = [normalize_text(p.get_text(" ")) for p in soup.find_all("p")]
    paragraphs = [p for p in paragraphs if len(p) >= 60]
    if len(paragraphs) >= 2 or (paragraphs and len(paragraphs[0]) >= BODY_MIN_CHARS):
        return " ".join(paragraphs)
    return ""


def _meta_description(soup) -> str:
    """`og:description`·`description`·JSON-LD 설명 — 본문을 못 얻었을 때의 최후 폴백(짧으면 버린다)."""
    candidates: list[str] = []
    for attrs in ({"property": "og:description"}, {"name": "description"},
                  {"name": "twitter:description"}):
        element = soup.find("meta", attrs=attrs)
        if element and element.get("content"):
            candidates.append(normalize_text(element["content"]))
    candidates.append(_jsonld_description(soup))
    best = max(candidates, key=len, default="")
    return best if len(best) >= BODY_MIN_CHARS else ""


def _looks_like_block_page(text: str, title: str = "") -> bool:
    """차단·에러 껍데기인가 — **앞부분**(또는 제목)에 차단 문구가 있으면 본문으로 인정하지 않는다.

    문서 중간에 나오는 `captcha` 같은 단어는 보안 기사에도 나오므로 앞 300자만 본다.
    """
    head = normalize_text(title).lower()
    if any(marker in head for marker in BLOCK_PAGE_MARKERS):
        return True
    lead = normalize_text(text)[:300].lower()
    return any(marker in lead for marker in BLOCK_PAGE_MARKERS)


def fetch_article_body_detail(url: str, timeout: int = 10, *, retry_delay: float = 0.75) -> dict:
    """기사 원문에서 본문·출처를 뽑는다(로컬 전용). 실패·차단은 **빈 본문**이다.

    신문사가 짧은 시간에 몰아 친 요청을 봇으로 보고 껍데기를 주는 경우가 있다(실측). 그래서
    차단으로 판정되면 `retry_delay` 초 후 **한 번만** 다시 시도한다.

    Returns: `{"body": str(정규화·1,500자), "source": jsonld|article|meta|blocked|thin|none,
               "status": int|None, "final_url": str, "title": str}`
    """
    detail: dict = {"body": "", "source": "none", "status": None, "final_url": url or "", "title": ""}
    if not url:
        return detail
    for attempt in range(2):
        detail = _fetch_once(url, timeout)
        if detail["source"] != "blocked" or attempt:
            break
        try:
            import time

            time.sleep(max(0.0, retry_delay))
        except Exception:  # noqa: BLE001 — 대기 실패는 재시도를 포기하는 것으로 본다
            break
    return detail


def _fetch_once(url: str, timeout: int) -> dict:
    detail: dict = {"body": "", "source": "none", "status": None, "final_url": url, "title": ""}
    try:
        import requests  # 지역 import — 오프라인 테스트에서 모듈 로딩 비용을 만들지 않는다

        # Finnhub 링크(`finnhub.io/api/news?id=…`)는 원문으로 302 한다 — 최종 URL 까지 따라간다.
        resp = requests.get(url, timeout=timeout, headers=BROWSER_HEADERS, allow_redirects=True)
        detail["status"] = resp.status_code
        detail["final_url"] = str(resp.url or url)
        if resp.status_code in (401, 403, 429, 451):
            detail["source"] = "blocked"
            return detail
        resp.raise_for_status()
        html = resp.text or ""
    except Exception:  # noqa: BLE001 — 본문 수집 실패는 그 기사만 본문 없이 둔다
        return detail
    if not html:
        return detail
    try:
        soup = _soup_of(html)
    except Exception:  # noqa: BLE001 — 파싱 실패도 그 기사만 본문 없이 둔다
        return detail
    title_el = soup.find("title")
    detail["title"] = normalize_text(title_el.get_text(" ") if title_el else "")[:200]
    body, source = _jsonld_body(soup), "jsonld"
    if len(body) < BODY_MIN_CHARS:
        candidate = _container_body(soup)
        if len(candidate) > len(body):
            body, source = candidate, "article"
    if len(body) < BODY_MIN_CHARS:
        candidate = _meta_description(soup)
        if len(candidate) > len(body):
            body, source = candidate, "meta"
    if len(body) >= BODY_MIN_CHARS and _looks_like_block_page(body, detail["title"]):
        detail["body"], detail["source"] = "", "blocked"
        return detail
    if len(body) < BODY_MIN_CHARS:
        # 껍데기 페이지는 본문다운 `<p>` 가 없다 — 그러면 페이지 앞부분의 차단 문구로 판정한다.
        page_lead = normalize_text(soup.get_text(" "))[:400]
        detail["body"] = ""
        detail["source"] = "blocked" if _looks_like_block_page(page_lead, detail["title"]) else "thin"
        return detail
    detail["body"], detail["source"] = excerpt_body(body), source
    return detail


def fetch_article_body(url: str, timeout: int = 10) -> str:
    """기사 원문 URL 에서 본문 발췌(1,500자)를 얻는다(로컬 전용, 실패하면 빈 문자열).

    기본값은 `fetch_body=False` 다 — 본문 발췌는 저장소에 남기지 않는다(2.2.3).
    """
    return fetch_article_body_detail(url, timeout=timeout)["body"]


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
    if news_items is not None:
        raw_news = list(news_items)
    else:
        raw_news = _merge_snapshot_news(list(signal.get("news") or []), _snapshot_news(snapshot, ticker))
    # 종목 관련 기사를 앞으로(무관 기사는 뒤로, 0건 방지) — 잘라내기 전에 정렬한다.
    raw_news = rank_news([item for item in raw_news if isinstance(item, dict)], ticker)
    cutoff = _parse_ts(quant_signals.get("data_cutoff_at") or signal.get("data_cutoff_at"))
    future_dropped = 0
    for item in raw_news:
        nid = news_id_for(item)
        headline = str(item.get("headline") or item.get("title") or "")
        # 시점 고정: 마감(`data_cutoff_at`) 이후 기사는 모델에게 보내지 않는다(미래 뉴스 차단).
        published = _parse_ts(item.get("published_at") or item.get("available_at"))
        if cutoff is not None and published is not None and published > cutoff:
            future_dropped += 1
            continue
        # 본문: 저장된 발췌 → 원문 추출 → Finnhub `summary` 폴백 순서. 껍데기(차단·에러 페이지)는 버린다.
        body = str(item.get("body_excerpt_1500") or item.get("body") or item.get("body_excerpt") or "")
        body_source = "stored" if body else "none"
        if body and _looks_like_block_page(body, headline):
            body, body_source = "", "none"
        if not body and fetch_body:
            fetcher = body_fetcher or fetch_article_body
            body = fetcher(str(item.get("url") or "")) or ""
            body_source = "article" if body else "none"
            if body and _looks_like_block_page(body, headline):
                body, body_source = "", "none"
        summary = normalize_text(item.get("summary") or "")
        if len(normalize_text(body)) < SUMMARY_MIN_CHARS and len(summary) >= SUMMARY_MIN_CHARS \
                and len(summary) > len(normalize_text(body)):
            # 원문이 막혔을 때의 **로컬 컨텍스트 전용** 폴백 — 수집기가 받은 기사 요약(실제 문장)이다.
            # 공개 파일·보고서에는 나가지 않는다(§2.2.3·§2.2.4).
            body, body_source = excerpt_body(summary), "summary"
        body = excerpt_body(body)
        source = str(item.get("source") or "")
        url = str(item.get("url") or "")
        # V11 2단계: 문제 **기사 하나만** 격리하고 나머지 유효 기사로 판단을 계속한다.
        # [감사 08] 본문·헤드라인만 보면 부족하다 — `source`·`url` 도 프롬프트에 들어가는 자유
        # 문자열이므로 같은 경계에서 스캔한다(슬러그로 위장한 지시문 대응).
        reason_code = None
        for scope, text in ((QUARANTINE_BODY, body), (QUARANTINE_HEADLINE, headline),
                            (QUARANTINE_SOURCE, source), (QUARANTINE_URL, url)):
            if detect_prompt_injection(text):
                reason_code = scope
                break
        if reason_code is not None:
            injection_detected = True
            # 격리 메타데이터에는 **원문·source·url 을 남기지 않는다** — 기사 ID·본문 해시·사유 코드만.
            excluded.append({"article_id": nid,
                             "content_sha256": content_sha256(body or headline or url),
                             "reason_code": reason_code})
            continue
        news_list.append({
            "id": nid,
            "headline": item.get("headline") or item.get("title"),
            "url": item.get("url"),
            "published_at": item.get("published_at") or item.get("available_at"),
            "source": item.get("source"),
            "body_excerpt_1500": body,
            "body_available": bool(body),
            # 본문을 어디서 얻었나(stored/article/summary/none) — 판독 분모 판정의 근거다(v1.3.7).
            "body_source": body_source,
            # 기사 **버전 영수증** — 무엇을 읽었는지 나중에 대조할 수 있게(감사 P1-1 · 08).
            "content_sha256": content_sha256(body if body else headline),
            "content_hash": content_sha256(body if body else headline),
            "retrieved_at": item.get("retrieved_at") or retrieved_at,
            "normalizer_version": item.get("normalizer_version") or NORMALIZER_VERSION,
        })
    news_list = news_list[:NEWS_MAX_ITEMS]

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
    # 2단계 격리의 **두 번째 단계**: 기사를 빼도 남는 자리(공시 항목·13F·업종·자유 텍스트)에
    # 지시문이 있으면 격리만으로는 무결성을 보장할 수 없다 → 이 종목은 `NEWS_INJECTION`(V11) 으로 차단한다.
    # 실제 스캔은 컨텍스트를 다 만든 뒤 **프롬프트 경계 직전**에 한 번만 한다(아래).
    non_news_hits: list[dict] = []
    injection_scope = next((item.get("reason_code") for item in excluded), None)
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
        "future_news_dropped": future_dropped,
        "filings_8k": eights,
        "injection_detected": injection_detected,
        # 격리 목록·범위 — 원문·source·url 없이 **기사 ID·본문 해시·사유 코드**만.
        "excluded_news": excluded,
        "injection_scope": injection_scope,
        "injection_integrity_compromised": False,
        "injection_fields": [],
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
    # [감사 08] V11 스캔 경계를 **프롬프트에 실리는 전 필드**로 넓힌다 — news 본문·헤드라인·source·url 은
    # 기사 단위로 이미 격리했고, 여기서는 13F 기관명·업종·median_method·8-K url·thesis_ko 처럼 나머지
    # 자유 문자열을 본다. 컨텍스트로 직렬화되기 **전** 입력(공시 원본 행의 설명 필드 등)도 같이 본다.
    # 장부 필드(격리 목록·해시·사유 코드)는 자기 참조를 피해 건너뛴다.
    scan_targets: list[tuple[str, Any]] = [
        ("", {k: v for k, v in context.items() if k not in PROMPT_SCAN_SKIP_KEYS}),
        ("signal", {k: v for k, v in signal.items() if k != "news"}),
    ]
    if candidates:
        scan_targets.append(("filings", candidates))
    if context.get("holdings_13f"):
        scan_targets.append(("holdings_13f", context["holdings_13f"]))
    seen_hits: set[tuple[str, tuple[str, ...]]] = set()
    for prefix, node in scan_targets:
        for hit in scan_fields(node, skip_keys=(), path=prefix):
            key = (str(hit.get("field")), tuple(hit.get("patterns") or []))
            if key in seen_hits:
                continue
            seen_hits.add(key)
            non_news_hits.append(hit)
    if non_news_hits:
        injection_detected = True
        injection_scope = INTEGRITY_NON_NEWS
        context["injection_detected"] = True
        context["injection_scope"] = INTEGRITY_NON_NEWS
        context["injection_integrity_compromised"] = True
        context["injection_fields"] = non_news_hits[:5]
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
    """서술 검증에 쓰는 '허용 숫자' 집합 — **필드별 단위를 알고** 만든 표기 변형의 합집합.

    예전에는 모든 수치를 한 집합에 섮고 `±`·`×100` 변형을 전부 펼쳤다. 그러면 `PEG 3.5`
    하나로 `-3.5`·`350`·`4` 까지 통과했다(감사 08-d). 지금은 **각 필드의 단위**가
    허용하는 변형만 만든다(`field_number_forms`).
    """
    found: set[float] = set()
    for slot in context_number_table(context).values():
        found.update(field_number_forms(slot["value"], slot["unit"]))
    return found


#: 필드별 단위 — 단위가 다르면 허용 변형이 다르다(비율만 %로 환산, 배수는 환산 금지).
#: `price`·`currency`·`multiple`·`score`·`count` 는 표기 그대로(반올림 제시만), `ratio` 는 ×100,
#: `percent` 는 ÷100 도 허용한다. `_signed` 는 부호를 뒤집는 서술(할인/프리미엄)을 허용한다.
NUMBER_FIELD_UNITS: dict[str, str] = {
    # 가격·통화
    "price": "price", "dma50": "price", "dma200": "price", "atr14": "price",
    "iv_low": "price", "iv_median": "price", "iv_high": "price",
    "dcf_bear": "price", "dcf_base": "price", "dcf_bull": "price", "epv": "price", "graham": "price",
    "lo": "price", "hi": "price", "base_price": "price", "invalidation_price": "price",
    "prices": "price",
    # 비율(소수)
    "margin": "ratio_signed", "pct_b": "ratio", "atr_pct": "ratio",
    "roic": "ratio", "roic_spread": "ratio_signed", "fcf_margin": "ratio",
    "dilution_3y": "ratio_signed", "roe_stdev": "ratio",
    # 배수·성장
    "peg": "multiple", "pegy": "multiple", "net_debt_ebitda": "multiple",
    "interest_cover": "multiple", "fcf_to_ni": "multiple", "vol20": "multiple",
    "growth": "score", "rev_cagr5": "ratio_signed", "eps_cagr5": "ratio_signed",
    # 점수(0~100)
    "q": "score", "paper_grade": "score", "entry_score": "score", "regime_score": "score",
    "structural_risk": "score",
    # 거시(퍼센트)
    "real_rate": "percent_signed", "t10y2y": "percent_signed", "hy_oas": "percent",
    "dxy_3m": "percent_signed", "vix": "multiple",
    # 개수·기간
    "lag_days": "count", "news_window_hours": "count", "max_quote_chars": "count",
    "delta_shares_pct": "percent_signed", "period_end" : "count",
}
#: 숫자로 세지 않는 키 — 원문 발췌·서술은 숫자 검증 대상이 아니다.
NUMBER_SKIP_KEYS: tuple[str, ...] = ("context_notes", "news", "body_excerpt_1500", "headline",
                                    "thesis_ko", "reason", "injection_fields", "excluded_news",
                                    "content_sha256", "content_hash", "context_hash", "prompt_hash",
                                    "body_source", "body_chars")


def field_number_forms(value: float, unit: str = "unknown") -> set[float]:
    """한 필드 값의 **허용 표기 변형** — 단위가 바뀔 수 있는 것만 바꾼다.

    - 모든 단위: 값 그대로 + 소수 1·2자리 반올림 제시
    - `ratio*`: ×100(퍼센트 표기) — `0.15` → `15`
    - `percent*`: ÷100 — `2.0` → `0.02`
    - `_signed`: 부호 반전 — 손실을 −로 쓰는 서술(그 외 단위는 부호를 뒤집지 않는다)
    - 소수 0자리 반올림은 **퍼센트·점수 표기에서 자릿수가 10 이상일 때만** 허용한다.
      그래야 `3.5`(PEG)이 `4`로, `0.35`(비율)가 `0`으로 통과하지 않는다.
    """
    v = float(value)
    unit = str(unit or "unknown")
    forms: set[float] = {v, round(v, 1), round(v, 2)}
    scaled: float | None = None
    if unit.startswith("ratio"):
        scaled = v * 100.0
    elif unit.startswith("percent"):
        scaled = v / 100.0
    if scaled is not None:
        forms |= {scaled, round(scaled, 1), round(scaled, 2)}
        if abs(scaled) >= 10:
            forms.add(round(scaled, 0))
    if unit.startswith("score") and abs(v) >= 10:
        forms.add(round(v, 0))
    if unit.endswith("_signed") or v < 0:
        forms |= {-f for f in list(forms)}
    return {f for f in forms if f == f and abs(f) != float("inf")}


def number_forms(value: float) -> set[float]:
    """한 수치의 표기 변형 — 단위를 모르는 호출자용 **보수적 기본값**(`unknown`)."""
    return field_number_forms(value, "unknown")


def context_number_table(context: dict) -> dict[str, dict]:
    """컨텍스트의 수치를 **경로 → {값, 단위}** 로 구조화한다(숫자 검증의 출처 표).

    경로를 남기는 이유는 검증이 "어느 필드의 숫자인가"를 물을 수 있게 하기 위해서다 — 예컨대
    `round2` 의 `quote_or_number_ref` 가 `peg` 라고 하면 그 필드의 허용 변형만 본다.
    """
    table: dict[str, dict] = {}

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in NUMBER_SKIP_KEYS:
                    continue
                walk(value, f"{path}.{key}" if path else str(key))
            return
        if isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
            return
        if isinstance(node, str) or isinstance(node, bool):
            return
        number = _num(node)
        if number is None:
            return
        leaf = path.rsplit(".", 1)[-1]
        leaf = leaf.split("[", 1)[0]
        table[path or "$"] = {"value": number, "unit": NUMBER_FIELD_UNITS.get(leaf, "unknown")}

    walk(context, "")
    return table


def field_numbers(context: dict, field: str) -> set[float]:
    """한 **필드 이름**의 허용 숫자 집합 — 이름이 같으면 단위도 같다고 본다."""
    name = str(field or "").strip()
    if not name:
        return set()
    found: set[float] = set()
    for path, slot in context_number_table(context).items():
        leaf = path.rsplit(".", 1)[-1].split("[", 1)[0]
        if leaf == name:
            found.update(field_number_forms(slot["value"], slot["unit"]))
    return found


__all__ = [
    "ALL_SKILLS", "BLOCKING_VETO_CODES", "BODY_EXCERPT_CHARS", "DEFAULT_CONTEXT_DIR", "GRADE_RANK",
    "GRADES", "INJECTION_PATTERNS", "INTENTS", "INTEGRITY_NON_NEWS", "INVESTOR_SKILLS", "MAX_QUOTE_CHARS",
    "NEWS_WINDOW_HOURS", "NUMBER_FIELD_UNITS", "PROMPT_SCAN_SKIP_KEYS", "QUARANTINE_BODY",
    "QUARANTINE_HEADLINE", "QUARANTINE_REASONS", "QUARANTINE_SOURCE", "QUARANTINE_URL", "REPO_ROOT",
    "VETO_CODES", "build_context", "content_hash", "content_sha256", "context_hash", "context_number_table",
    "context_numbers", "detect_prompt_injection", "excerpt_body", "fetch_article_body",
    "fetch_article_body_detail", "field_numbers",
    "field_number_forms", "holdings_for_ticker", "load_prompt", "load_schema", "load_skill",
    "load_snapshot_inputs", "news_id_for", "normalize_text", "number_forms", "parse_front_matter",
    "prompt_hash", "quote_in_body", "quote_sha256", "save_context", "scan_fields", "schema_path",
    "select_targets", "skill_meta", "to_strict_schema",
]
