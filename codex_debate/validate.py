"""codex_debate.validate — 환각 차단 검증·뉴스 상태 판정·리포트 조립 (기획서 §3.9.4·§3.11.5·부록 G).

코드가 마지막에 다시 거른다. 모델이 스키마를 어겨도, 없는 기사를 인용해도, 숫자를 지어내도
리포트에는 **검증을 통과한 것만** 남는다.

[고정] 저장용 리포트에는 원문 `quote` 를 남기지 않는다 — `quote_sha256`·`quote_verified`·`quote_chars` 만.
`data/debate_reports.json` 은 공개 저장소에 커밋되는 파일이다(2.2.3).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from codex_debate.context import (
    BLOCKING_VETO_CODES,
    GRADE_RANK,
    INTEGRITY_NON_NEWS,
    INVESTOR_SKILLS,
    MAX_QUOTE_CHARS,
    NEWS_WINDOW_HOURS,
    VETO_CODES,
    context_numbers,
    load_schema,
    normalize_text,
    prompt_hash,
    quote_in_body,
    quote_sha256,
    context_hash as compute_context_hash,
)

# ── 상수 ─────────────────────────────────────────────────────────────────
FORBIDDEN_KEYS: tuple[str, ...] = (
    "quote", "evidence_quote", "body", "body_excerpt_1500",
    "veto_override", "target_price", "raw_close", "avg_cost", "shares_held",
)

NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
#: 목표가·수익률 예측 패턴 — 발견 시 신뢰도 C 강등(§3.9.4 4).
PRICE_PATTERN_RE = re.compile(
    r"(목표\s*가|목표\s*주가|목표주가|target\s*price|적정\s*가|적정주가|"
    r"수익률|기대\s*수익|업사이드|상승\s*여력|오를\s*것|내릴\s*것)",
    re.IGNORECASE,
)
#: 숫자로 보지 않는 구조적 상수 — Veto 코드 번호(V01~V12)·8-K Item·기간 표기.
STRUCTURAL_NUMBERS: set[float] = (
    {float(i) for i in range(0, 32)}                                   # 날짜·일수·코드 번호(V01~V12)
    | {float(y) for y in range(1990, 2101)}                            # 연도(2026 등) — 날짜 표기
    | {45.0, 48.0, 63.0, 100.0, 126.0, 180.0, 200.0, 250.0, 365.0}     # 13F·뉴스 창·호라이즌
    | {1.01, 1.05, 2.02, 2.06, 3.01, 4.02, 5.02, 7.01, 8.01, 9.01}     # 8-K Item 코드
)
#: 8-K Item → 구조적 위험 점수(부록 E). 같은 코드가 여러 번 있어도 **더하지 않고 최댓값**.
EIGHT_K_RISK: dict[str, float] = {
    "3.01": 100.0, "4.02": 100.0, "2.06": 70.0, "5.02": 20.0,
    "1.05": 55.0, "2.02": 30.0, "1.01": 20.0, "7.01": 10.0, "8.01": 10.0, "9.01": 5.0,
}
MIN_VALID_SKILLS = 3
DROP_RATE_LIMIT = 0.30
UNKNOWN_RATIO_LIMIT = 0.50
#: 격리로 지울 수 있는 인젝션 범위 — 이 범위면 그 기사만 빼고 판단을 계속한다(2단계 격리).
QUARANTINE_SCOPES: tuple[str, ...] = ("prompt_injection_body", "prompt_injection_headline", "news_body")
#: 무결성을 보장할 수 없는 범위 — 이 때만 `NEWS_INJECTION`(V11) 으로 종목 전체를 차단한다.
INTEGRITY_SCOPES: tuple[str, ...] = (INTEGRITY_NON_NEWS, "non_news", "integrity")


def injection_blocks(ctx: dict | None) -> bool:
    """V11 `NEWS_INJECTION` 을 세울지 — **격리 뒤에도 무결성을 보장할 수 없을 때만**(감사 P1-1-c).

    기사 본문·헤드라인에서 찾은 지시문은 그 **기사 하나만** 빼면 판단 경로가 깨끗하다.
    범위 정보가 없는 구형 컨텍스트(`injection_detected` 만 있는 단순 dict)는 기존 동작을 유지한다(차단).
    """
    ctx = ctx or {}
    if ctx.get("injection_integrity_compromised"):
        return True
    scope = ctx.get("injection_scope")
    if scope in INTEGRITY_SCOPES:
        return True
    if scope in QUARANTINE_SCOPES:
        return False
    return bool(ctx.get("injection_detected"))


def resume_key(ctx: dict, *, prompt_version: str | None = None,
               model_version: str | None = None) -> str:
    """재개 키 — `signal_id + snapshot_id + context_hash + prompt_version + model_version`.

    입력·프롬프트·모델 중 하나라도 달라지면 다른 토론이다. 이어 돌릴 때 키가 다르면
    이전 라운드 결과를 재사용하지 않고 새 리포트로 시작한다(감사 P1-1).
    """
    parts = [str(ctx.get("quant_signal_id") or ""), str(ctx.get("snapshot_id") or ""),
             str(ctx.get("context_hash") or ""),
             str(prompt_version or ctx.get("prompt_hash") or ""), str(model_version or "")]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return "sha256:" + digest


def empty_counts() -> dict[str, int]:
    return {
        "quotes_checked": 0, "quotes_dropped": 0,
        "numbers_checked": 0, "numbers_rejected": 0, "sentences_removed": 0,
        "refs_dropped": 0,
    }


@dataclass
class Validated:
    """스킬 출력 1건의 검증 결과. `data` 는 저장용으로 정리된 사본이다."""

    kind: str
    data: dict | None = None
    ok: bool = False
    reasons: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    flags: dict[str, Any] = field(default_factory=dict)

    def count(self, key: str) -> int:
        return int(self.counts.get(key, 0) or 0)


# ── 아주 작은 JSON Schema 검증기(표준 라이브러리만) ────────────────────────
_TYPES: dict[str, type] = {
    "object": dict, "array": list, "string": str, "boolean": bool, "null": type(None),
}


def _resolve_ref(schema: Any, root: Any) -> Any:
    hops = 0
    while isinstance(schema, dict) and "$ref" in schema:
        ref = str(schema["$ref"])
        if not ref.startswith("#/"):
            raise ValueError(f"지원하지 않는 $ref: {ref}")
        node = root
        for part in ref[2:].split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
        schema = node
        hops += 1
        if hops > 20:
            raise ValueError("$ref 순환이 의심된다")
    return schema


def _type_ok(value: Any, kind: str) -> bool:
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    py = _TYPES.get(kind)
    if py is None:
        return True
    return isinstance(value, py)


def schema_errors(instance: Any, schema: Any, *, root: Any = None, path: str = "$") -> list[str]:
    """JSON Schema 부분집합 검증 — type·enum·const·required·properties·items·범위·패턴·$ref."""
    root = root if root is not None else schema
    schema = _resolve_ref(schema, root)
    if not isinstance(schema, dict):
        return []
    errors: list[str] = []
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} 허용값 아님")
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: {instance!r} != {schema['const']!r}")
    kind = schema.get("type")
    if kind is not None:
        kinds = kind if isinstance(kind, list) else [kind]
        if not any(_type_ok(instance, k) for k in kinds):
            return errors + [f"{path}: 타입 {type(instance).__name__} ∉ {kinds}"]
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < int(schema["minLength"]):
            errors.append(f"{path}: 길이 {len(instance)} < {schema['minLength']}")
        if "maxLength" in schema and len(instance) > int(schema["maxLength"]):
            errors.append(f"{path}: 길이 {len(instance)} > {schema['maxLength']}")
        if "pattern" in schema and not re.search(str(schema["pattern"]), instance):
            errors.append(f"{path}: 패턴 불일치")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} < {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: {instance} > {schema['maximum']}")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < int(schema["minItems"]):
            errors.append(f"{path}: 항목 {len(instance)} < {schema['minItems']}")
        if "maxItems" in schema and len(instance) > int(schema["maxItems"]):
            errors.append(f"{path}: 항목 {len(instance)} > {schema['maxItems']}")
        items = schema.get("items")
        if isinstance(items, dict):
            for idx, value in enumerate(instance):
                errors.extend(schema_errors(value, items, root=root, path=f"{path}[{idx}]"))
    if isinstance(instance, dict):
        for key in schema.get("required", []) or []:
            if key not in instance:
                errors.append(f"{path}: 필수 필드 누락 {key}")
        properties = schema.get("properties") or {}
        additional = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in properties:
                errors.extend(schema_errors(value, properties[key], root=root, path=f"{path}.{key}"))
            elif additional is False:
                errors.append(f"{path}: 허용되지 않은 필드 {key}")
            elif isinstance(additional, dict):
                errors.extend(schema_errors(value, additional, root=root, path=f"{path}.{key}"))
    return errors


def forbidden_field_paths(obj: Any, keys: Iterable[str] = FORBIDDEN_KEYS, path: str = "$") -> list[str]:
    """금지 필드(인용 원문·목표가·Veto 해제)가 **한 곳이라도** 있으면 경로를 돌려준다."""
    keys = set(keys)
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{path}.{key}"
            if key in keys:
                found.append(here)
            found.extend(forbidden_field_paths(value, keys, here))
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            found.extend(forbidden_field_paths(value, keys, f"{path}[{idx}]"))
    return found


# ── 숫자 검증 ─────────────────────────────────────────────────────────────
def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def is_allowed_number(value: float, allowed: set[float]) -> bool:
    """`context.json` 의 수치(표기 변형 포함) 또는 구조적 상수와 일치하는가."""
    for candidate in allowed:
        if abs(value - candidate) <= max(0.01, abs(candidate) * 0.002):
            return True
    return False


def allowed_number_set(ctx: dict) -> set[float]:
    return set(context_numbers(ctx)) | set(STRUCTURAL_NUMBERS)


def clean_narrative(text: str | None, allowed: set[float], counts: dict[str, int],
                    removed: list[str]) -> str:
    """문장 단위로 숫자 출처를 검사하고, 불일치 문장은 **지운다**(§3.11.5)."""
    if not text:
        return ""
    kept: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", str(text)):
        sentence = sentence.strip()
        if not sentence:
            continue
        bad = False
        for raw in NUMBER_RE.findall(sentence):
            value = _to_float(raw)
            if value is None:
                continue
            counts["numbers_checked"] = counts.get("numbers_checked", 0) + 1
            if not is_allowed_number(value, allowed):
                counts["numbers_rejected"] = counts.get("numbers_rejected", 0) + 1
                bad = True
        if bad:
            counts["sentences_removed"] = counts.get("sentences_removed", 0) + 1
            removed.append(sentence[:120])
            continue
        kept.append(sentence)
    return " ".join(kept)


def _flag_price_pattern(texts: Iterable[str], flags: dict[str, Any]) -> None:
    hits = sum(1 for text in texts if text and PRICE_PATTERN_RE.search(str(text)))
    if hits:
        flags["confidence_cap"] = "C"
        flags["price_pattern_hits"] = int(flags.get("price_pattern_hits", 0)) + hits


# ── 라운드 1 ─────────────────────────────────────────────────────────────
def _ctx_news(ctx: dict) -> dict[str, dict]:
    return {str(item.get("id")): item for item in ctx.get("news") or [] if item.get("id")}


def _clamp(value: Any, low: float, high: float) -> tuple[float, bool]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low, True
    clamped = max(low, min(high, number))
    return clamped, clamped != number


def validate_round1(payload: Any, ctx: dict, expected_skill: str | None = None) -> Validated:
    """1R 스킬 출력 1건 검증 — 스키마·인용·뉴스 ID·숫자·금지 필드."""
    counts, reasons, flags = empty_counts(), [], {}
    if not isinstance(payload, dict):
        return Validated("round1", None, False, ["not_object"], counts, flags)
    payload = dict(payload)
    if expected_skill:
        payload["skill"] = payload.get("skill") or expected_skill

    score, score_clamped = _clamp(payload.get("score"), -100.0, 100.0)
    confidence, conf_clamped = _clamp(payload.get("confidence"), 0.0, 1.0)
    payload["score"] = score
    payload["confidence"] = confidence
    if score_clamped or conf_clamped:
        flags["confidence_cap"] = "C"
        reasons.append("score_or_confidence_clamped")

    news_in: list[str] = []
    for item in payload.get("news_read") or []:
        if isinstance(item, dict) and item.get("news_id"):
            news_in.append(str(item["news_id"]))
        if isinstance(item, dict):
            item["severity"], _ = _clamp(item.get("severity", 0), 0.0, 100.0)
            quote = str(item.get("quote") or "")
            if len(quote) > MAX_QUOTE_CHARS:
                item["quote"] = quote[:MAX_QUOTE_CHARS]
                flags["confidence_cap"] = "C"
                reasons.append("quote_truncated")
    flags["news_ids_in"] = news_in
    errors = schema_errors(payload, load_schema("round1"))
    if errors:
        return Validated("round1", None, False, [f"schema:{e}" for e in errors][:8], counts, flags)

    if expected_skill and payload["skill"] != expected_skill:
        return Validated("round1", None, False, [f"skill_mismatch:{payload['skill']}"], counts, flags)

    allowed = allowed_number_set(ctx)
    removed: list[str] = []
    reasons_clean = [clean_narrative(text, allowed, counts, removed) for text in payload.get("reasons") or []]
    reasons_clean = [text for text in reasons_clean if text]

    ctx_news = _ctx_news(ctx)
    kept_news: list[dict] = []
    dropped: set[str] = set()
    seen: set[str] = set()
    for item in payload.get("news_read") or []:
        news_id = str(item.get("news_id") or "")
        if news_id in seen:
            continue
        entry = ctx_news.get(news_id)
        if entry is None:
            dropped.add(news_id)
            reasons.append(f"news_id_not_in_context:{news_id}")
            continue
        url = str(item.get("url") or "").strip()
        expected_url = str(entry.get("url") or "").strip()
        if not url or (expected_url and url != expected_url):
            dropped.add(news_id)
            reasons.append(f"news_url_mismatch:{news_id}")
            continue
        quote = str(item.get("quote") or "")
        if not quote:
            dropped.add(news_id)
            reasons.append(f"quote_missing:{news_id}")
            continue
        counts["quotes_checked"] = counts.get("quotes_checked", 0) + 1
        if not quote_in_body(quote, entry.get("body_excerpt_1500") or ""):
            dropped.add(news_id)
            counts["quotes_dropped"] = counts.get("quotes_dropped", 0) + 1
            reasons.append(f"quote_not_found:{news_id}")
            continue
        seen.add(news_id)
        rationale = clean_narrative(item.get("rationale_ko"), allowed, counts, removed)
        kept_news.append({
            "news_id": news_id,
            "durability": item.get("durability"),
            "impact": item.get("impact"),
            "severity": item.get("severity"),
            "quote_sha256": quote_sha256(quote),
            "quote_verified": True,
            "quote_chars": len(normalize_text(quote)),
            "rationale_ko": rationale,
        })
        _flag_price_pattern([rationale], flags)
    flags["news_ids_dropped"] = sorted(dropped)

    _flag_price_pattern(reasons_clean, flags)
    if counts.get("sentences_removed", 0) >= 3:
        return Validated("round1", None, False, reasons + ["numbers_rejected_3plus"], counts, flags)

    data = {
        "skill": payload["skill"],
        "score": payload["score"],
        "stance": payload["stance"],
        "confidence": payload["confidence"],
        "reasons": reasons_clean[:3],
        "news_read": kept_news,
    }
    return Validated("round1", data, True, reasons, counts, flags)


# ── 라운드 2 ─────────────────────────────────────────────────────────────
def validate_round2(payload: Any, ctx: dict, expected_skill: str | None = None) -> Validated:
    """2R 스킬 출력 1건 검증 — 반박 대상·근거 참조·숫자."""
    counts, reasons, flags = empty_counts(), [], {}
    if not isinstance(payload, dict):
        return Validated("round2", None, False, ["not_object"], counts, flags)
    payload = dict(payload)
    if expected_skill:
        payload["skill"] = payload.get("skill") or expected_skill
    score, clamped = _clamp(payload.get("score"), -100.0, 100.0)
    payload["score"] = score
    if clamped:
        flags["confidence_cap"] = "C"
        reasons.append("score_clamped")
    errors = schema_errors(payload, load_schema("round2"))
    if errors:
        return Validated("round2", None, False, [f"schema:{e}" for e in errors][:8], counts, flags)
    if expected_skill and payload["skill"] != expected_skill:
        return Validated("round2", None, False, [f"skill_mismatch:{payload['skill']}"], counts, flags)

    allowed = allowed_number_set(ctx)
    field_names = {str(key) for key in ctx.keys()}
    removed: list[str] = []
    rebuttals: list[dict] = []
    for item in payload.get("rebuttals") or []:
        target = str(item.get("target_skill") or "")
        if target == payload["skill"] or target not in INVESTOR_SKILLS:
            reasons.append(f"rebuttal_target_invalid:{target}")
            continue
        point = clean_narrative(item.get("point"), allowed, counts, removed)
        if not point:
            reasons.append(f"rebuttal_point_emptied:{target}")
            continue
        ref = str(item.get("quote_or_number_ref") or "").strip()
        if ref:
            numbers = NUMBER_RE.findall(ref)
            ref_is_numeric = bool(numbers) and not re.search(r"[A-Za-z가-힣_]", ref)
            if ref_is_numeric:
                value = _to_float(numbers[0])
                counts["numbers_checked"] = counts.get("numbers_checked", 0) + 1
                if value is None or not is_allowed_number(value, allowed):
                    counts["numbers_rejected"] = counts.get("numbers_rejected", 0) + 1
                    reasons.append(f"ref_not_in_context:{target}")
                    continue
            elif ref not in field_names:
                bodies = [item.get("body_excerpt_1500") or "" for item in ctx.get("news") or []]
                if not any(ref in normalize_text(body) for body in bodies):
                    counts["quotes_dropped"] = counts.get("quotes_dropped", 0) + 1
                    reasons.append(f"ref_not_in_context:{target}")
                    continue
        rebuttals.append({"target_skill": target, "point": point, "quote_or_number_ref": ref})
    conflict_points = [clean_narrative(text, allowed, counts, removed) for text in payload.get("conflict_points") or []]
    conflict_points = [text for text in conflict_points if text]
    _flag_price_pattern([r["point"] for r in rebuttals] + conflict_points, flags)
    if counts.get("sentences_removed", 0) >= 3:
        return Validated("round2", None, False, reasons + ["numbers_rejected_3plus"], counts, flags)
    if not rebuttals:
        return Validated("round2", None, False, reasons + ["no_valid_rebuttal"], counts, flags)

    data = {
        "skill": payload["skill"],
        "score": payload["score"],
        "changed": bool(payload.get("changed")),
        "rebuttals": rebuttals[:3],
        "conflict_points": conflict_points[:3],
    }
    return Validated("round2", data, True, reasons, counts, flags)


# ── 라운드 3: 가드 ────────────────────────────────────────────────────────
def validate_guard(payload: Any, ctx: dict) -> Validated:
    """3R 리스크 가드 — 코드 Veto 를 **더할 수만** 있고 해제할 수 없다."""
    counts, reasons, flags = empty_counts(), [], {}
    if not isinstance(payload, dict):
        return Validated("round3_guard", None, False, ["not_object"], counts, flags)
    payload = dict(payload)
    risk, clamped = _clamp(payload.get("structural_risk"), 0.0, 100.0)
    payload["structural_risk"] = risk
    if clamped:
        reasons.append("structural_risk_clamped")
    errors = schema_errors(payload, load_schema("round3_guard"))
    if errors:
        return Validated("round3_guard", None, False, [f"schema:{e}" for e in errors][:8], counts, flags)

    allowed = allowed_number_set(ctx)
    removed: list[str] = []
    reasoning = clean_narrative(payload.get("reasoning"), allowed, counts, removed)
    blocks = [clean_narrative(block, allowed, counts, removed) for block in payload.get("additional_blocks") or []]
    blocks = [block for block in blocks if block]
    _flag_price_pattern([reasoning] + blocks, flags)
    if counts.get("sentences_removed", 0) >= 3:
        return Validated("round3_guard", None, False, reasons + ["numbers_rejected_3plus"], counts, flags)

    veto_codes = {code for code in payload.get("veto_codes") or [] if code in VETO_CODES}
    code_vetoes = {code for code in ctx.get("code_vetoes") or [] if code in VETO_CODES}
    if injection_blocks(ctx):
        # 2단계 격리: 기사 본문 지시문은 그 기사만 빼고 계속하고,
        # 무결성 훼손(공시·자유 텍스트 침투)일 때만 V11 을 세운다.
        veto_codes.add("NEWS_INJECTION")
    veto_codes |= code_vetoes  # 코드 Veto 는 지울 수 없다
    data = {
        "veto_codes": sorted(veto_codes),
        "additional_blocks": blocks[:5],
        "structural_risk": payload["structural_risk"],
        "reasoning": reasoning,
        "opinion": payload["opinion"],
    }
    return Validated("round3_guard", data, True, reasons, counts, flags)


# ── 라운드 3: 의장 ────────────────────────────────────────────────────────
def validate_chair(payload: Any, ctx: dict) -> Validated:
    """3R 의장 — 새 근거 금지, 가격은 `allowed.prices` 안에서만, 등급은 올리지 않는다."""
    counts, reasons, flags = empty_counts(), [], {}
    if not isinstance(payload, dict):
        return Validated("round3_chair", None, False, ["not_object"], counts, flags)
    payload = dict(payload)
    one_line = str(payload.get("one_line_ko") or "")
    if len(one_line) > MAX_QUOTE_CHARS:
        payload["one_line_ko"] = one_line[:MAX_QUOTE_CHARS]
        flags["confidence_cap"] = "C"
        reasons.append("one_line_truncated")
    errors = schema_errors(payload, load_schema("round3_chair"))
    if errors:
        return Validated("round3_chair", None, False, [f"schema:{e}" for e in errors][:8], counts, flags)

    allowed = allowed_number_set(ctx)
    prices = [float(p) for p in (ctx.get("allowed") or {}).get("prices") or []]

    def keep_refs(values: Iterable[Any]) -> list[float]:
        kept: list[float] = []
        for value in values:
            try:
                number = round(float(value), 2)
            except (TypeError, ValueError):
                counts["refs_dropped"] = counts.get("refs_dropped", 0) + 1
                continue
            if any(abs(number - price) <= 0.011 for price in prices):
                kept.append(number)
            else:
                counts["refs_dropped"] = counts.get("refs_dropped", 0) + 1
                reasons.append(f"price_not_in_allowed:{value}")
        return kept

    entry_refs = keep_refs(payload.get("entry_plan_refs") or [])
    exit_refs = keep_refs(payload.get("exit_refs") or [])
    if (payload.get("entry_plan_refs") or payload.get("exit_refs")) and not (entry_refs or exit_refs):
        return Validated("round3_chair", None, False, reasons + ["all_price_refs_fabricated"], counts, flags)

    removed: list[str] = []
    consensus = [clean_narrative(text, allowed, counts, removed) for text in payload.get("consensus") or []]
    dissent = [clean_narrative(text, allowed, counts, removed) for text in payload.get("dissent") or []]
    one_line = clean_narrative(payload.get("one_line_ko"), allowed, counts, removed)
    if not one_line:
        return Validated("round3_chair", None, False, reasons + ["one_line_emptied"], counts, flags)
    _flag_price_pattern(consensus + dissent + [one_line], flags)

    grade = payload["grade_recommendation"]
    quant_grade = str(ctx.get("quant_grade") or "NONE")
    if GRADE_RANK.get(grade, 0) > GRADE_RANK.get(quant_grade, 0):
        grade = quant_grade  # 올리는 권고는 언제나 불가(§3.11.8)
        reasons.append("grade_promotion_blocked")
        flags["confidence_cap"] = "C"

    if counts.get("sentences_removed", 0) >= 3:
        return Validated("round3_chair", None, False, reasons + ["numbers_rejected_3plus"], counts, flags)

    data = {
        "consensus": [text for text in consensus if text][:5],
        "dissent": [text for text in dissent if text][:5],
        "grade_recommendation": grade,
        "entry_plan_refs": entry_refs[:5],
        "exit_refs": exit_refs[:5],
        "conflict": bool(payload.get("conflict")),
        "one_line_ko": one_line,
    }
    return Validated("round3_chair", data, True, reasons, counts, flags)


ROUND_VALIDATORS = {
    "round1": validate_round1,
    "round2": validate_round2,
    "round3_guard": validate_guard,
    "round3_chair": validate_chair,
}


def validate_round_output(round_name: str, payload: Any, ctx: dict, expected_skill: str | None = None) -> Validated:
    """라운드 이름으로 검증기를 고른다(`round1`·`round2`·`round3_guard`·`round3_chair`)."""
    if round_name not in ROUND_VALIDATORS:
        raise KeyError(f"알 수 없는 라운드: {round_name}")
    if round_name in ("round3_guard", "round3_chair"):
        return ROUND_VALIDATORS[round_name](payload, ctx)
    return ROUND_VALIDATORS[round_name](payload, ctx, expected_skill)


# ── 뉴스 상태 판정 (§3.9.0) ───────────────────────────────────────────────
def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def eight_k_risk_score(filings: Iterable[dict] | None) -> float:
    """8-K Item 규칙 점수(부록 E) — 겹쳐도 더하지 않고 최댓값."""
    score = 0.0
    for filing in filings or []:
        for item in filing.get("items") or []:
            score = max(score, EIGHT_K_RISK.get(str(item).strip(), 0.0))
    return score


def articles_in_window(ctx: dict) -> int:
    """데이터 마감 기준 48시간 안에 발행된 기사 수(시각을 모르면 입력으로 센다)."""
    cutoff = _parse_iso(ctx.get("data_cutoff_at"))
    news = ctx.get("news") or []
    if cutoff is None:
        return len(news)
    start = cutoff.timestamp() - NEWS_WINDOW_HOURS * 3600
    count = 0
    for item in news:
        published = _parse_iso(item.get("published_at"))
        if published is None or start <= published.timestamp() <= cutoff.timestamp() + 1:
            count += 1
    return count


def _round1_news(round1: Iterable[Validated] | None) -> tuple[set[str], set[str], dict[str, dict]]:
    ids_in: set[str] = set()
    ids_dropped: set[str] = set()
    kept: dict[str, dict] = {}
    for result in round1 or []:
        ids_in |= {str(x) for x in result.flags.get("news_ids_in") or []}
        ids_dropped |= {str(x) for x in result.flags.get("news_ids_dropped") or []}
        if not result.ok or not result.data:
            continue
        for read in result.data.get("news_read") or []:
            news_id = str(read.get("news_id"))
            slot = kept.setdefault(news_id, {"verified": False, "durability": [], "severity": 0.0})
            slot["verified"] = slot["verified"] or bool(read.get("quote_verified"))
            slot["durability"].append(read.get("durability"))
            try:
                slot["severity"] = max(float(slot["severity"]), float(read.get("severity") or 0))
            except (TypeError, ValueError):
                pass
    return ids_in, ids_dropped, kept


def evaluate_news_state(
    ctx: dict,
    round1: Iterable[Validated] | None = None,
    *,
    debate_ran: bool = True,
    items_in: int | None = None,
    items_kept: int | None = None,
) -> dict:
    """세 상태(`not_read` / `insufficient_evidence` / `read_done`)를 **겹치지 않게** 판정한다."""
    articles = articles_in_window(ctx)
    ids_in, ids_dropped, kept = _round1_news(round1)
    verified = {news_id for news_id, slot in kept.items() if slot["verified"]}
    if items_in is None:
        items_in = len(ids_in | verified)
    if items_kept is None:
        items_kept = len(verified)
    items_dropped = max(0, items_in - items_kept)
    drop_rate = (items_dropped / items_in) if items_in else 0.0
    unknown = sum(1 for news_id in verified
                  if "unknown" in [d for d in kept.get(news_id, {}).get("durability") or []])
    unknown_ratio = (unknown / items_kept) if items_kept else 1.0

    eight_k = eight_k_risk_score(ctx.get("filings_8k"))
    structural = [slot["severity"] for news_id, slot in kept.items()
                  if news_id in verified and "structural" in (slot.get("durability") or [])]
    structural_risk = max([eight_k] + [float(x) for x in structural]) if structural else eight_k

    reasons: list[str] = []
    if not debate_ran or articles == 0:
        state = "not_read"
        structural_risk = eight_k
        reasons.append("debate_not_run" if not debate_ran else "no_articles_in_48h")
    elif items_kept < 2:
        state = "insufficient_evidence"
        reasons.append("valid_items_lt_2")
    elif drop_rate > DROP_RATE_LIMIT:
        state = "insufficient_evidence"
        reasons.append("drop_rate_gt_30pct")
    elif unknown_ratio >= UNKNOWN_RATIO_LIMIT:
        state = "insufficient_evidence"
        reasons.append("unknown_ratio_ge_50pct")
    else:
        state = "read_done"

    return {
        "state": state,
        "articles": articles,
        # 격리된 기사 수 — 판단은 계속했지만 "몇 건을 빼고 읽었나"를 남긴다(2단계 격리).
        "quarantined": len(ctx.get("excluded_news") or []),
        "items_in": int(items_in),
        "items_kept": int(items_kept),
        "items_dropped": int(items_dropped),
        "drop_rate": round(drop_rate, 4),
        "unknown_ratio": round(unknown_ratio, 4),
        "structural_risk": round(float(structural_risk), 2),
        "grade_cap": "WATCH" if state == "insufficient_evidence" else None,
        "confidence_cap": "C" if state == "insufficient_evidence" else None,
        "reasons": reasons,
        "verified_news_ids": sorted(verified),
        "dropped_news_ids": sorted(ids_dropped - verified),
    }


# ── 리포트 조립 (부록 G) ─────────────────────────────────────────────────
def _sum_counts(results: Iterable[Validated]) -> dict[str, int]:
    total = empty_counts()
    for result in results:
        for key in total:
            total[key] += result.count(key)
    return total


def assemble_validated(
    ctx: dict,
    *,
    round1: Iterable[Validated] | None = None,
    round2: Iterable[Validated] | None = None,
    guard: Validated | None = None,
    chair: Validated | None = None,
    ticker: str | None = None,
    completed_at: str | datetime | None = None,
    generated_at: str | datetime | None = None,
    status_override: str | None = None,
    min_valid_skills: int = MIN_VALID_SKILLS,
    auto_downgrade_enabled: bool = False,
    provider: str = "codex",
    report_seq: int = 1,
    news_state: dict | None = None,
    validated_at: str | datetime | None = None,
    model_version: str | None = None,
) -> dict:
    """검증된 라운드 결과를 `data/debate_reports.json` 의 리포트 1건으로 조립한다.

    `validated_at` 은 **편입 프로세스가 그 바이트를 실제로 검증한 시각**이다(감사 P0-2).
    보고서가 스스로 적는 `completed_at` 과 다를 수 있고, 마감 뒤에 검증된 결과는
    `late=True` 가 되어 그날 결정에 붙지 않는다.
    """
    r1 = [x for x in (round1 or [])]
    r2 = [x for x in (round2 or [])]
    valid1 = [x for x in r1 if x.ok and x.data]
    valid2 = [x for x in r2 if x.ok and x.data]
    guard_ok = bool(guard and guard.ok and guard.data)
    chair_ok = bool(chair and chair.ok and chair.data)

    if status_override:
        status = status_override
    elif len(valid1) < min_valid_skills:
        status = "quorum_failed"
    elif not guard_ok or not chair_ok:
        status = "failed"
    else:
        status = "complete"

    if news_state is None:
        news_state = evaluate_news_state(ctx, r1, debate_ran=bool(r1))
    structural_risk = float(news_state.get("structural_risk") or eight_k_risk_score(ctx.get("filings_8k")))

    veto_codes = list((guard.data or {}).get("veto_codes") or []) if guard_ok else []
    code_vetoes = {code for code in ctx.get("code_vetoes") or [] if code in BLOCKING_VETO_CODES}
    if injection_blocks(ctx):
        # 2단계 격리 — 기사 본문 지시문은 그 기사만 빼고 계속하고,
        # 무결성 훼손일 때만 V11 이 종목 전체를 막는다(가드 결과가 없어도 유지된다).
        code_vetoes.add("NEWS_INJECTION")
    blocking = any(code in BLOCKING_VETO_CODES for code in veto_codes) or bool(code_vetoes)
    grade_reco = (chair.data or {}).get("grade_recommendation") if chair_ok else None
    quant_grade = str(ctx.get("quant_grade") or "NONE")
    quant_intent = str(ctx.get("quant_intent") or "no_action")

    auto_applied = False
    display_grade = quant_grade
    lower = bool(grade_reco) and GRADE_RANK.get(grade_reco, 0) < GRADE_RANK.get(quant_grade, 0)
    if auto_downgrade_enabled and lower and not blocking:
        display_grade = str(grade_reco)
        auto_applied = True

    if blocking:
        intent = "no_action"
    elif lower and quant_intent == "pending_buy":
        intent = "pending_hold_cash"
    else:
        intent = quant_intent if quant_intent in ("pending_buy", "pending_hold_cash", "no_action") else "no_action"

    counts = _sum_counts(r1 + r2 + [x for x in (guard, chair) if x is not None])
    numbers_checked = counts["numbers_checked"]
    numbers_rejected = counts["numbers_rejected"]
    drop_reasons: list[str] = []
    for result in list(r1) + list(r2) + [x for x in (guard, chair) if x is not None]:
        drop_reasons.extend(result.reasons)
    confidence_cap = "C" if (news_state.get("confidence_cap") == "C" or any(
        x.flags.get("confidence_cap") == "C" for x in list(r1) + list(r2) + [y for y in (guard, chair) if y]
    )) else None

    completed = completed_at or datetime.now(timezone.utc).isoformat()
    completed_dt = _parse_iso(completed)
    validated_iso = None
    if validated_at is not None:
        validated_iso = validated_at if isinstance(validated_at, str) else validated_at.isoformat()
    validated_dt = _parse_iso(validated_iso) if validated_iso else completed_dt
    order_cutoff = _parse_iso(ctx.get("order_cutoff_at"))
    published = _parse_iso(ctx.get("signal_published_at"))
    # 마감 판정은 **검증된 시각**의 최댓값으로 한다 — 완료는 마감 전·검증은 마감 후인 리포트는 늦다.
    effective = max([dt for dt in (completed_dt, validated_dt) if dt is not None], default=None)
    late = bool(effective and order_cutoff and effective >= order_cutoff)
    invalid = bool(completed_dt and published and completed_dt < published)

    scores: dict[str, dict] = {}
    for skill in INVESTOR_SKILLS:
        r1_score = next((x.data["score"] for x in valid1 if x.data["skill"] == skill), None)
        r2_score = next((x.data["score"] for x in valid2 if x.data["skill"] == skill), None)
        scores[skill] = {"round1": r1_score, "round2": r2_score}

    skills_in = len(r1) + (1 if guard is not None else 0) + (1 if chair is not None else 0)
    skills_kept = len(valid1) + (1 if guard_ok else 0) + (1 if chair_ok else 0)

    as_of = str(ctx.get("as_of") or "")
    report = {
        "report_id": f"{as_of}-{ticker or ctx.get('ticker')}-debate-{int(report_seq):02d}",
        "ticker": ticker or ctx.get("ticker"),
        "signal_id": ctx.get("quant_signal_id"),
        "snapshot_id": ctx.get("snapshot_id"),
        "as_of": as_of,
        "data_cutoff_at": ctx.get("data_cutoff_at"),
        "signal_published_at": ctx.get("signal_published_at"),
        "order_cutoff_at": ctx.get("order_cutoff_at"),
        "completed_at": completed if isinstance(completed, str) else completed.isoformat(),
        "late": late,
        "invalid": invalid,
        "status": status,
        "round1": [x.data for x in valid1],
        "round2": [x.data for x in valid2],
        "round3": {
            "guard": guard.data if guard_ok else None,
            "chair": chair.data if chair_ok else None,
        },
        "scores": scores,
        "validation": {
            "skills_in": skills_in,
            "skills_kept": skills_kept,
            "quotes_checked": counts["quotes_checked"],
            "quotes_dropped": counts["quotes_dropped"],
            "numbers_checked": numbers_checked,
            "numbers_rejected": numbers_rejected,
            "refs_dropped": counts.get("refs_dropped", 0),
            "drop_reasons": drop_reasons[:20],
            "confidence_cap": confidence_cap,
            # 감사 P0-2: 검증 시각·재개 키 — 보고서 자기 신고(completed_at)와 다른 값이다.
            "report_validated_at": validated_iso or (completed if isinstance(completed, str) else completed.isoformat()),
            "resume_key": resume_key(ctx, model_version=model_version),
            "injection_scope": ctx.get("injection_scope"),
            "injection_blocking": injection_blocks(ctx),
            "excluded_news": [{"id": item.get("id"), "reason": item.get("reason"),
                                "patterns": list(item.get("patterns") or [])[:4]}
                               for item in (ctx.get("excluded_news") or [])[:10]],
        },
        "quant_grade": quant_grade,
        "display_grade": display_grade,
        "intent": intent,
        "auto_downgrade_applied": auto_applied,
        "track": "codex_debated",
        "news_state": news_state.get("state"),
        "structural_risk": structural_risk,
        "injection_detected": bool(ctx.get("injection_detected")),
        "context_hash": ctx.get("context_hash") or compute_context_hash(ctx),
        "prompt_hash": ctx.get("prompt_hash") or prompt_hash(),
        "provider": provider,
    }
    if status == "quorum_failed":
        report["display_grade"] = quant_grade
        report["intent"] = "no_action" if blocking else quant_intent
        report["auto_downgrade_applied"] = False
        report["round3"] = {"guard": None, "chair": None}
    return report


def validate_and_assemble(
    ticker: str | None = None,
    ctx: dict | None = None,
    round1: Iterable[Any] | None = None,
    round2: Iterable[Any] | None = None,
    guard: Any = None,
    chair: Any = None,
    **kwargs: Any,
) -> dict:
    """원시 라운드 출력을 검증한 뒤 리포트 1건을 조립한다(`Validated` 가 섞여 있어도 된다)."""
    if ctx is None:
        raise ValueError("ctx 가 필요하다")
    r1 = [p if isinstance(p, Validated) else validate_round1(p, ctx) for p in (round1 or [])]
    r2 = [p if isinstance(p, Validated) else validate_round2(p, ctx) for p in (round2 or [])]
    g = guard if isinstance(guard, Validated) or guard is None else validate_guard(guard, ctx)
    c = chair if isinstance(chair, Validated) or chair is None else validate_chair(chair, ctx)
    return assemble_validated(ctx, round1=r1, round2=r2, guard=g, chair=c, ticker=ticker, **kwargs)


# ── 파일 전체 검증 (부록 G) ──────────────────────────────────────────────
def _load_reports(source: Any) -> dict:
    if isinstance(source, dict):
        return source
    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"리포트 파일이 없다: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_report(rep: dict, auto_downgrade_enabled: bool) -> list[str]:
    errors: list[str] = []
    rid = rep.get("report_id", "?")
    if not auto_downgrade_enabled and rep.get("display_grade") != rep.get("quant_grade"):
        errors.append(f"{rid}: auto_downgrade_enabled=false 인데 display_grade≠quant_grade")
    if auto_downgrade_enabled and GRADE_RANK.get(str(rep.get("display_grade")), 0) > GRADE_RANK.get(
        str(rep.get("quant_grade")), 0
    ):
        errors.append(f"{rid}: 등급을 올린 리포트는 허용되지 않는다")
    completed = _parse_iso(rep.get("completed_at"))
    validated = _parse_iso((rep.get("validation") or {}).get("report_validated_at")) or completed
    order = _parse_iso(rep.get("order_cutoff_at"))
    published = _parse_iso(rep.get("signal_published_at"))
    if completed and order:
        effective = max([dt for dt in (completed, validated) if dt is not None])
        expect_late = effective >= order
        if bool(rep.get("late")) != expect_late:
            errors.append(f"{rid}: late={rep.get('late')} 가 completed_at/report_validated_at·order_cutoff_at 과 불일치")
    if completed and published:
        expect_invalid = completed < published
        if bool(rep.get("invalid")) != expect_invalid:
            errors.append(f"{rid}: invalid={rep.get('invalid')} 가 completed_at/signal_published_at 과 불일치")
    if not rep.get("signal_id") or not rep.get("snapshot_id"):
        errors.append(f"{rid}: signal_id·snapshot_id 필수")
    if rep.get("status") == "complete" and not (rep.get("round3") or {}).get("chair"):
        errors.append(f"{rid}: status=complete 인데 의장 결과가 없다")
    if int(rep.get("validation", {}).get("skills_kept", 0)) < MIN_VALID_SKILLS and rep.get("status") == "complete":
        errors.append(f"{rid}: 정족수 미달인데 status=complete")
    for entry in rep.get("round1") or []:
        for read in entry.get("news_read") or []:
            if "quote" in read:
                errors.append(f"{rid}: round1.news_read 에 원문 quote 가 남아 있다")
    return errors


def validate_reports_file(source: Any, *, auto_downgrade_enabled: bool = False) -> list[str]:
    """`data/debate_reports.json` 전체 검증. 오류 목록을 돌려준다(빈 리스트 = 통과)."""
    try:
        data = _load_reports(source)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        return [f"파일 로드 실패: {exc}"]
    errors = schema_errors(data, load_schema("debate_reports"))
    if errors:
        return [f"schema:{e}" for e in errors][:20]
    bad = forbidden_field_paths(data)
    if bad:
        errors.extend([f"금지 필드: {path}" for path in bad])
    for rep in data.get("reports") or []:
        errors.extend(_validate_report(rep, auto_downgrade_enabled))
    return errors
