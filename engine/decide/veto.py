"""engine.decide.veto — 리스크 가드 12종 (기획서 3.6) [고정].

규칙 본문(임계값·문구·해제 조건)은 `config/veto_rules.yaml` 하나에만 있다.
코드는 입력을 판정하고 `veto[]` 를 돌려준다 — Veto 는 신호를 **삭제하지 않는다**.

V12 만 `evidence[]` 를 본다. 뉴스 판독의 품질 부족(폐기율>30%·유효<2건)은 V12 가 아니라
뉴스 상태(`insufficient_evidence`) → 신뢰도 C 상한 + 매수 등급 상한 WATCH 로 반영한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

DEFAULT_RULES_PATH = Path("config/veto_rules.yaml")
CODE_ORDER = [f"V{i:02d}" for i in range(1, 13)]


@lru_cache(maxsize=4)
def load_veto_rules(path: str | Path = DEFAULT_RULES_PATH) -> dict:
    p = Path(path)
    with open(p, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    rules = {r["code"]: r for r in data["rules"]}
    missing = [c for c in CODE_ORDER if c not in rules]
    if missing:
        raise ValueError(f"veto_rules.yaml 에 규칙이 빠졌다: {missing}")
    return data


@dataclass
class VetoInputs:
    """한 종목·한 시점의 판정 입력. 없는 값은 None(=그 규칙은 판정하지 않음)."""

    # V01
    days_to_earnings: int | None = None
    # V02
    close_lag_business_days: int | None = None
    # V03
    filing_items: list[str] = field(default_factory=list)
    filing_items_verified: list[str] = field(default_factory=list)
    # V04
    going_concern: bool | None = None
    # V05 / V06
    adv20_usd: float | None = None
    vix: float | None = None
    hy_oas_bp: float | None = None
    # V07 / V08 (보유 + 신규 매수 후 **예상 비중**)
    projected_sector_weight: float | None = None
    projected_position_weight: float | None = None
    # V09
    thesis_break: bool = False
    structural_risk: float | None = None
    # V10
    one_day_return: float | None = None
    split_announced: bool = False
    # V11
    news_injection: bool = False
    # V12
    evidence: list[dict] = field(default_factory=list)


@dataclass
class VetoResult:
    vetoes: list[dict]
    blocking: list[str]

    @property
    def blocked(self) -> bool:
        return bool(self.blocking)

    @property
    def codes(self) -> list[str]:
        return [v["code"] for v in self.vetoes]

    def as_list(self) -> list[dict]:
        return list(self.vetoes)

    def as_dict(self) -> dict:
        return {"vetoes": self.vetoes, "blocking": self.blocking, "blocked": self.blocked}


def _veto(rule: dict, detail: str) -> dict:
    return {
        "code": rule["code"],
        "name": rule["name"],
        "group": rule["group"],
        "message_ko": rule["message_ko"],
        "release_ko": rule["release_ko"],
        "detail": detail,
        "blocking": bool(rule.get("blocking", True)),
    }


def veto_check(inputs: VetoInputs, rules: dict | None = None) -> VetoResult:
    """12종을 모두 평가해 **발동한 것만** 돌려준다(발동하지 않은 규칙은 기록하지 않는다)."""
    data = rules or load_veto_rules()
    by_code = {r["code"]: r for r in data["rules"]}
    fired: list[dict] = []

    p = by_code["V01"]["params"]
    if inputs.days_to_earnings is not None:
        d = int(inputs.days_to_earnings)
        if -int(p["days_after"]) <= d <= int(p["days_before"]):
            fired.append(_veto(by_code["V01"], f"실적 발표 {d:+d}거래일"))

    p = by_code["V02"]["params"]
    if inputs.close_lag_business_days is not None and inputs.close_lag_business_days > int(p["max_lag_business_days"]):
        fired.append(_veto(by_code["V02"], f"종가 지연 {inputs.close_lag_business_days}거래일"))

    p = by_code["V03"]["params"]
    killed = [i for i in inputs.filing_items if i in p["items"] and i not in p.get("verified_only", [])]
    killed += [i for i in inputs.filing_items
               if i in p["items"] and i in p.get("verified_only", []) and i in inputs.filing_items_verified]
    if killed:
        fired.append(_veto(by_code["V03"], f"8-K Item {sorted(set(killed))}"))

    if inputs.going_concern:
        fired.append(_veto(by_code["V04"], "10-K 계속기업 불확실성"))

    p = by_code["V05"]["params"]
    if inputs.adv20_usd is not None and inputs.adv20_usd < float(p["min_adv20_usd"]):
        fired.append(_veto(by_code["V05"], f"20일 평균 거래대금 ${inputs.adv20_usd:,.0f}"))

    p = by_code["V06"]["params"]
    if (inputs.vix is not None and inputs.vix > float(p["vix_max"])) or \
       (inputs.hy_oas_bp is not None and inputs.hy_oas_bp > float(p["hy_oas_max_bp"])):
        fired.append(_veto(by_code["V06"], f"VIX {inputs.vix} / HY OAS {inputs.hy_oas_bp}bp"))

    p = by_code["V07"]["params"]
    if inputs.projected_sector_weight is not None and \
            inputs.projected_sector_weight > float(p["max_sector_weight"]):
        fired.append(_veto(by_code["V07"], f"섹터 예상 비중 {inputs.projected_sector_weight:.2%}"))

    p = by_code["V08"]["params"]
    if inputs.projected_position_weight is not None and \
            inputs.projected_position_weight > float(p["max_position_weight"]):
        fired.append(_veto(by_code["V08"], f"종목 예상 비중 {inputs.projected_position_weight:.2%}"))

    p = by_code["V09"]["params"]
    if inputs.thesis_break or (inputs.structural_risk is not None and
                               inputs.structural_risk >= float(p["structural_risk_exit"])):
        fired.append(_veto(by_code["V09"], "S3 구조적 훼손"))

    p = by_code["V10"]["params"]
    if inputs.one_day_return is not None and abs(inputs.one_day_return) > float(p["max_abs_daily_return"]) \
            and not inputs.split_announced:
        fired.append(_veto(by_code["V10"], f"1일 수익률 {inputs.one_day_return:+.1%} · 분할 공시 없음"))

    if inputs.news_injection:
        fired.append(_veto(by_code["V11"], "뉴스 본문 지시문 패턴 감지"))

    p = by_code["V12"]["params"]
    required = set(p["required_evidence_types"])
    types = {e.get("type") for e in inputs.evidence}
    core = [e for e in inputs.evidence if e.get("type") in required]
    invalid = [e for e in core if e.get("quote_verified") is False]
    if not core:
        fired.append(_veto(by_code["V12"], f"핵심 근거 없음(필요: {sorted(required)})"))
    elif invalid:
        fired.append(_veto(by_code["V12"], "핵심 근거 검증 실패"))

    fired.sort(key=lambda v: CODE_ORDER.index(v["code"]))
    blocking = [v["code"] for v in fired if v["blocking"]]
    return VetoResult(vetoes=fired, blocking=blocking)


def veto_summary(result: VetoResult) -> dict:
    """화면·기록용 요약 — 발동 코드와 그룹별 개수(차단 사유를 지우지 않는다)."""
    groups: dict[str, int] = {}
    for v in result.vetoes:
        groups[v["group"]] = groups.get(v["group"], 0) + 1
    return {
        "codes": result.codes,
        "blocked": result.blocked,
        "blocking": list(result.blocking),
        "by_group": groups,
        "messages_ko": [v["message_ko"] for v in result.vetoes],
    }
