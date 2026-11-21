"""engine.data.holdings_13f — SEC 13F 보유 파싱·분기 델타·45일 시차 워터마크 (기획서 2.4.4·5.5).

[고정]
- 13F 는 분기말(`period_end`) 뒤 **45일 안에** 제출된다. 그래서 어떤 분기의 보유는
  `acceptance_datetime ≤ decision_at` 을 만족할 때만 쓸 수 있고, 화면에는 항상
  **"45일 전 분기말 기준"** 워터마크를 붙인다(레이더에서 최신인 척하지 않는다).
- 보유 수량(`shares`)·평단은 공개 커밋·Pages 산출물 금지 필드다(2.2.3). 레이더는
  **평가액(`value_usd`)과 변화율(`delta_pct`)** 만 쓴다 — 변화의 방향과 크기는 그 둘로 충분하다.
- 13F 는 신호 계산에 쓰지 않는다(레이더 전용). 그래서 지연이 신호를 막지 않는다(2.7).
"""

from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

#: 13F 제출 기한(분기말 + 45일) — 이 날짜 전에는 그 분기의 보유를 "알 수 없다".
FILING_LAG_DAYS = 45
#: 레이더 지연 표시 기준(2.7).
STALE_DAYS = 135
WATERMARK_KO = "45일 전 분기말 기준"

PUT_CALL = ("", "Put", "Call")

#: 수집 대상 펀드 목록(CIK·표시 이름·투자 스타일).
FILERS_PATH = "config/13f_filers.yaml"
#: 13F 원문 양식 — `13F-NT`(보유 없는 통지)는 대상이 아니다.
FORM_HR = "13F-HR"
FORM_HR_A = "13F-HR/A"
#: `company_tickers.json` 캐시(워크플로가 `data/cache/sec_cik` 을 복원한다).
COMPANY_TICKERS_CACHE = "data/cache/sec_cik/company_tickers.json"


class Filing13FError(ValueError):
    """13F 입력이 해석 불가(형식·필수 필드)."""


@dataclass(frozen=True)
class Holding13F:
    """한 펀드·한 분기의 종목 보유 1행(공개 가능한 값만)."""

    cusip: str
    issuer: str
    value_usd: float
    put_call: str = ""
    unit: str = ""

    def as_dict(self) -> dict:
        return {"cusip": self.cusip, "issuer": self.issuer,
                "value_usd": round(float(self.value_usd), 2), "put_call": self.put_call or None}


@dataclass
class Filer13F:
    """수집 대상 펀드 1개 — `config/13f_filers.yaml` 의 한 항목.

    `value_unit` 은 그 펀드가 13F 정보표 `<value>` 를 적는 단위다. 대부분은 **달러**를 그대로
    적지만(2026-11 실측: 버크셔 커버 `tableValueTotal` 304,196,581,325), 지금도 **천 달러**
    관례로 제출하는 펀드가 있다(바우포스트 커버 `tableValueTotal` 5,045,853 = 약 $5.05B).
    단위가 틀리면 평가액이 1,000배 틀어지므로 추정하지 않고 펀드별로 명시한다.
    """

    cik: str
    name: str
    style: str = ""
    value_unit: str = "dollars"

    def scale(self) -> float:
        return 1000.0 if self.value_unit == "thousands" else 1.0

    def as_dict(self) -> dict:
        return {"cik": self.cik, "name": self.name, "style": self.style or None,
                "value_unit": self.value_unit}


@dataclass
class Filing13F:
    filer_cik: str
    filer_name: str
    period_end: date
    filed_date: date | None = None
    acceptance_datetime: datetime | None = None
    holdings: list[Holding13F] = field(default_factory=list)
    #: 원문 양식(`13F-HR` / `13F-HR/A`) — 개정판으로 채운 분기인지 화면·진단이 알 수 있게.
    form: str = ""
    #: 펀드 스타일(`value`·`growth`·`activist` …) — `config/13f_filers.yaml` 의 메타.
    style: str = ""

    def available_from(self) -> date:
        """이 분기의 보유를 처음 알 수 있는 날 = 분기말 + 45일(제출 기한)."""
        return self.period_end + timedelta(days=FILING_LAG_DAYS)

    def available_at(self) -> datetime:
        """실제 사용 가능 시각 — 제출 접수 시각이 있으면 그것, 없으면 기한일 23:59 UTC."""
        if self.acceptance_datetime is not None:
            return self.acceptance_datetime
        return datetime.combine(self.available_from(), datetime.max.time(), tzinfo=timezone.utc)

    def is_available_at(self, decision_at: datetime) -> bool:
        """`acceptance_datetime ≤ decision_at` **그리고** 기한이 지났는가(LA-7)."""
        if decision_at.tzinfo is None:
            raise Filing13FError("decision_at 에 시간대가 없다(Z 또는 오프셋 필요)")
        return self.available_at() <= decision_at

    def as_dict(self) -> dict:
        return {
            "filer_cik": self.filer_cik,
            "filer_name": self.filer_name,
            "period_end": self.period_end.isoformat(),
            "filed_date": self.filed_date.isoformat() if self.filed_date else None,
            "acceptance_datetime": _iso(self.acceptance_datetime),
            "available_from": self.available_from().isoformat(),
            "n_holdings": len(self.holdings),
            "total_value_usd": round(sum(h.value_usd for h in self.holdings), 2),
            "form": self.form or None,
            "style": self.style or None,
        }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise Filing13FError("시각에 시간대가 없다")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_dt(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if re.fullmatch(r"\d{8}", text):                      # SEC acceptanceDateTime 예: 20260814
        return datetime(int(text[:4]), int(text[4:6]), int(text[6:8]), tzinfo=timezone.utc)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _parse_date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if re.fullmatch(r"\d{8}", text):
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    return date.fromisoformat(text[:10])


def _local(tag: str) -> str:
    """XML 네임스페이스(`{http://...}infoTable`) → 로컬 이름."""
    return tag.rsplit("}", 1)[-1].lower()


def _text(node: ET.Element, names: tuple[str, ...]) -> str:
    for child in node:
        if _local(child.tag) in names:
            return (child.text or "").strip()
    return ""


def _to_value_usd(raw: str, value_scale: float) -> float:
    """`<value>` 문자열 → 달러. `$`·천 단위 쉼표가 붙은 표기도 받는다.

    `$` 가 붙은 값은 이미 달러 표기라 배수하지 않는다(기존 동작 유지).
    """
    text = (raw or "").strip()
    if text.startswith("$"):
        value_scale = 1.0
    text = text.replace("$", "").replace(",", "").replace(" ", "")
    if not text:
        return 0.0
    try:
        return float(text) * value_scale
    except ValueError:
        return 0.0


def parse_information_table(xml: str | bytes, *, value_scale: float = 1000.0) -> list[Holding13F]:
    """13F 정보표 XML(`<infoTable>` 반복) → 보유 행 목록.

    SEC 원문은 네임스페이스가 붙어 오므로 로컬 이름으로 비교한다. 값이 `""`/`0` 인
    행(옵션·비상장)은 `put_call`·`unit` 으로 구분해 남긴다.

    `value_scale` 은 `<value>` 의 단위다. 13F 양식의 고전 관례는 **천 달러**라 기본값이 1,000 이고,
    지금 EDGAR 에 올라오는 정보표는 `<value>` 에 **달러**를 그대로 적으므로(2026-11 실측: 버크셔
    합계 304,196,581,325 = $304.2B) 라이브 수집기는 1.0 을 넘긴다. 잘못 곱하면 평가액·변화율이
    1,000배 틀어지므로 호출자가 단위를 정한다.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise Filing13FError(f"13F XML 파싱 실패: {exc}") from exc
    rows: list[Holding13F] = []
    for node in root.iter():
        if _local(node.tag) != "infotable":
            continue
        cusip = _text(node, ("cusip",))
        issuer = _text(node, ("nameofissuer", "issuer"))
        value = _to_value_usd(_text(node, ("value",)), value_scale)
        put_call = _text(node, ("putcall",)).title()
        unit = _text(node, ("sshprnamttype",)).upper()
        if not cusip:
            continue
        rows.append(Holding13F(cusip=cusip.upper(), issuer=issuer, value_usd=value,
                               put_call=put_call if put_call in PUT_CALL else "", unit=unit))
    return rows


def has_info_table(xml: str | bytes) -> bool:
    """정보표 XML 인가 — `primary_doc.xml`(표지)을 걸러내는 판정."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return False
    return any(_local(node.tag) == "infotable" for node in root.iter())


def aggregate_holdings(holdings: Iterable[Holding13F]) -> list[Holding13F]:
    """같은 `(cusip, put_call)` 행을 합산한다 — 한 종목을 여러 운용사·계좌로 나눠 신고한 13F 행이 흔하다.

    버크셔 2026-Q3 정보표는 ALLY FINL 을 3행(운용사별)으로 신고한다. 행을 그대로 두고
    `cusip` 로 dict 를 만들면 **마지막 행만 남아** 평가액이 과소 계산되고, 직전 분기 대비 변화율이
    거짓 신호가 된다. 합계는 `value_usd` 만 더한다(수량은 공개 필드가 아니다).
    """
    merged: dict[tuple[str, str], Holding13F] = {}
    for holding in holdings:
        key = (holding.cusip.upper(), holding.put_call or "")
        current = merged.get(key)
        if current is None:
            merged[key] = Holding13F(cusip=holding.cusip.upper(), issuer=holding.issuer,
                                     value_usd=float(holding.value_usd), put_call=holding.put_call or "",
                                     unit=holding.unit or "")
            continue
        merged[key] = Holding13F(
            cusip=current.cusip,
            issuer=current.issuer or holding.issuer,
            value_usd=current.value_usd + float(holding.value_usd),
            put_call=current.put_call,
            unit=current.unit or holding.unit or "",
        )
    return list(merged.values())


def _sum_by_cusip(holdings: Iterable[Holding13F]) -> dict[str, tuple[str, float]]:
    """`cusip → (issuer, 평가액 합계)` — 같은 종목의 다중 행을 더한다(`compute_deltas` 용)."""
    out: dict[str, list] = {}
    for holding in holdings:
        cusip = holding.cusip.upper()
        row = out.setdefault(cusip, [holding.issuer, 0.0])
        row[1] = float(row[1]) + float(holding.value_usd)
        if not row[0] and holding.issuer:
            row[0] = holding.issuer
    return {cusip: (str(row[0]), float(row[1])) for cusip, row in out.items()}


def parse_filing(payload: dict) -> Filing13F:
    """정규화 dict(또는 `xml` 문자열 포함) → `Filing13F`.

    필수: `filer_cik`(또는 `cik`), `period_end`. `holdings` 가 없고 `xml` 이 있으면 정보표를 파싱한다.
    """
    if not isinstance(payload, dict):
        raise Filing13FError("13F 입력은 객체여야 한다")
    cik = str(payload.get("filer_cik") or payload.get("cik") or "").strip()
    period_end = _parse_date(payload.get("period_end"))
    if not cik:
        raise Filing13FError("filer_cik 이 없다")
    if period_end is None:
        raise Filing13FError("period_end 가 없다")
    holdings: list[Holding13F] = []
    if payload.get("xml"):
        holdings = parse_information_table(payload["xml"])
    else:
        for row in payload.get("holdings") or []:
            cusip = str(row.get("cusip") or "").strip().upper()
            if not cusip:
                continue
            holdings.append(Holding13F(
                cusip=cusip,
                issuer=str(row.get("issuer") or row.get("name") or ""),
                value_usd=float(row.get("value_usd") or row.get("value") or 0.0),
                put_call=str(row.get("put_call") or "").title(),
                unit=str(row.get("unit") or ""),
            ))
    return Filing13F(
        filer_cik=cik.zfill(10) if cik.isdigit() else cik,
        filer_name=str(payload.get("filer_name") or payload.get("name") or cik),
        period_end=period_end,
        filed_date=_parse_date(payload.get("filed_date")),
        acceptance_datetime=_parse_dt(payload.get("acceptance_datetime") or payload.get("acceptance")),
        holdings=holdings,
    )


def filings_available(filings: list[Filing13F], decision_at: datetime) -> list[Filing13F]:
    """`acceptance_datetime ≤ decision_at` 인 제출만 — 미래 분기의 보유를 섞지 않는다(LA-7)."""
    return [f for f in filings if f.is_available_at(decision_at)]


def latest_by_filer(filings: list[Filing13F]) -> dict[str, Filing13F]:
    out: dict[str, Filing13F] = {}
    for f in filings:
        cur = out.get(f.filer_cik)
        if cur is None or (f.period_end, f.available_at()) > (cur.period_end, cur.available_at()):
            out[f.filer_cik] = f
    return out


def previous_by_filer(filings: list[Filing13F], latest: dict[str, Filing13F]) -> dict[str, Filing13F]:
    out: dict[str, Filing13F] = {}
    for f in filings:
        cur = latest.get(f.filer_cik)
        if cur is None or f.period_end >= cur.period_end:
            continue
        prev = out.get(f.filer_cik)
        if prev is None or f.period_end > prev.period_end:
            out[f.filer_cik] = f
    return out


KIND_KO = {"new": "신규 편입", "increase": "확대", "reduce": "축소", "exit": "청산", "unchanged": "변동 없음"}


def compute_deltas(previous: Filing13F | None, current: Filing13F,
                   min_change_pct: float = 0.05) -> list[dict]:
    """같은 펀드의 직전 분기 대비 변화 — 신규 편입·확대·축소·청산 (5.5).

    `value_usd` 기준으로 비교한다(수량은 공개 필드가 아니다). 변동이 `min_change_pct` 미만이면
    `unchanged` 로 남겨 "왜 안 보이는지"를 남긴다(조용한 누락 금지).
    """
    prev = _sum_by_cusip(previous.holdings if previous else [])
    cur = _sum_by_cusip(current.holdings)
    rows: list[dict] = []
    for cusip, (issuer, value) in cur.items():
        before = prev.get(cusip)
        base = None if before is None else before[1]
        if before is None:
            kind, delta_pct = "new", None
        else:
            delta_pct = None if not base or base <= 0 else (value - base) / base
            if delta_pct is None:
                kind = "unchanged"
            elif delta_pct >= min_change_pct:
                kind = "increase"
            elif delta_pct <= -min_change_pct:
                kind = "reduce"
            else:
                kind = "unchanged"
        rows.append({
            "cusip": cusip,
            "issuer": issuer,
            "kind": kind,
            "kind_ko": KIND_KO[kind],
            "value_usd": round(value, 2),
            "prev_value_usd": None if base is None else round(base, 2),
            "delta_pct": None if delta_pct is None else round(delta_pct, 4),
        })
    for cusip, (issuer, before_value) in prev.items():
        if cusip in cur:
            continue
        rows.append({
            "cusip": cusip, "issuer": issuer, "kind": "exit", "kind_ko": KIND_KO["exit"],
            "value_usd": 0.0, "prev_value_usd": round(before_value, 2), "delta_pct": -1.0,
        })
    order = {"new": 0, "increase": 1, "reduce": 2, "exit": 3, "unchanged": 4}
    rows.sort(key=lambda r: (order[r["kind"]], -(r["value_usd"] or 0.0)))
    return rows


def watermark(decision_at: datetime, filings: list[Filing13F]) -> dict:
    """가장 최근 **알 수 있는** 분기말과 그 지연일 — 화면 워터마크 값(2.7·5.5)."""
    usable = [f for f in filings if f.is_available_at(decision_at)]
    if not usable:
        return {
            "watermark_ko": WATERMARK_KO,
            "period_end": None,
            "lag_days": FILING_LAG_DAYS,
            "stale": True,
            "stale_days": None,
            "note_ko": "사용 가능한 13F 제출이 없다 — 분기말 +45일이 지나야 반영된다.",
        }
    period_end = max(f.period_end for f in usable)
    as_of = decision_at.date()
    age = (as_of - period_end).days
    return {
        "watermark_ko": WATERMARK_KO,
        "period_end": period_end.isoformat(),
        "lag_days": FILING_LAG_DAYS,
        "stale": age > STALE_DAYS,
        "stale_days": age,
        "note_ko": (f"{period_end.isoformat()} 분기말 기준 · 제출 기한 +{FILING_LAG_DAYS}일 반영"
                    + (" · 지연" if age > STALE_DAYS else "")),
    }


#: 회사명 정규화에서 **끝에서부터** 걷어낼 법인격·클래스 표기.
_NAME_STOPWORDS = frozenset({
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED", "PLC",
    "LLC", "LP", "LLP", "NV", "SA", "AG", "SE", "HOLDINGS", "HOLDING", "GROUP", "DEL",
    "DELAWARE", "NEW", "MD", "COM", "COMMON", "ORD", "ORDINARY", "SHS", "SHARES", "SPONSORED",
    "ADR", "ADS", "CLASS", "CL", "TR",
})


def normalize_issuer_name(name: str) -> str:
    """회사명 비교용 정규화 — `Apple Inc.` · `APPLE INC` · `APPLE INC CLASS A` → `APPLE`.

    13F 정보표의 `nameOfIssuer` 와 `company_tickers.json` 의 `title` 은 표기가 다르다(대소문자·
    마침표·`INC/CORP/DEL`·`CLASS A` 유무). 이 함수가 둘을 같은 키로 만든다.
    """
    text = str(name or "").upper().replace("&", " ")
    text = re.sub(r"[^A-Z0-9 ]+", " ", text)
    tokens = [t for t in text.split() if t]
    while tokens and tokens[0] == "THE":
        tokens.pop(0)

    def _trim() -> None:
        while tokens and tokens[-1] in _NAME_STOPWORDS:
            tokens.pop()

    _trim()
    while len(tokens) > 1 and len(tokens[-1]) == 1 and tokens[-1] in "ABCD":   # CLASS A → A
        tokens.pop()
        _trim()
    return " ".join(tokens)


def build_issuer_ticker_index(rows: Any) -> dict[str, list[str]]:
    """`company_tickers` 행(DataFrame · dict 목록) → `정규화 회사명 → [티커…]` 색인.

    한 회사가 여러 클래스(GOOGL·GOOG)를 가지면 한 키에 모두 담는다 — 어느 쪽이든 워치리스트에
    있으면 겹침으로 본다(`issuer_to_ticker` 가 단일 문자열이면 그 하나만).
    """
    if hasattr(rows, "to_dict"):
        rows = rows.to_dict("records")
    index: dict[str, list[str]] = {}
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        ticker = str(row.get("ticker") or "").strip().upper()
        key = normalize_issuer_name(row.get("title") or row.get("name") or "")
        if not ticker or not key:
            continue
        bucket = index.setdefault(key, [])
        if ticker not in bucket:
            bucket.append(ticker)
    return {key: sorted(value) for key, value in index.items()}


def _index_from_mapping(mapping: Mapping[str, str | Iterable[str]] | None,
                        base: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    """사용자 제공 매핑(회사명 → 티커)을 정규화 색인으로."""
    index = {key: list(value) for key, value in (base or {}).items()}
    for key, value in (mapping or {}).items():
        norm = normalize_issuer_name(key)
        if not norm:
            continue
        tickers = [value] if isinstance(value, str) else list(value or [])
        bucket = index.setdefault(norm, [])
        for ticker in tickers:
            text = str(ticker).strip().upper()
            if text and text not in bucket:
                bucket.append(text)
    return index


def tickers_for_issuer(issuer: str, index: Mapping[str, Iterable[str]] | None) -> list[str]:
    """정보표 회사명 → 티커 후보. 접두 일치는 **후보가 하나뿐일 때만** 받아들인다(오매칭 방지)."""
    key = normalize_issuer_name(issuer)
    if not key or not index:
        return []
    if key in index:
        return [str(t).upper() for t in index[key]]
    cands: set[str] = set()
    for name, tickers in index.items():
        if name.startswith(key + " ") or key.startswith(name + " "):
            cands.update(str(t).upper() for t in tickers)
    return sorted(cands) if len(cands) == 1 else []


def _row_ticker(issuer: str, cusip: str, cusip_index: Mapping[str, str],
                issuer_index: Mapping[str, Iterable[str]]) -> str:
    """한 변화 행의 티커 — CUSIP 색인이 있으면 먼저, 없으면 회사명 정규화."""
    mapped = cusip_index.get(str(cusip).upper())
    if mapped:
        return mapped
    tickers = tickers_for_issuer(issuer, issuer_index)
    return tickers[0] if tickers else ""


def radar_payload(payloads: list[dict] | list[Filing13F], decision_at: datetime,
                  watchlist: set[str] | None = None, min_change_pct: float = 0.05, *,
                  issuer_to_ticker: Mapping[str, str | Iterable[str]] | None = None,
                  cusip_to_ticker: Mapping[str, str] | None = None) -> dict:
    """13F 레이더 산출물(`web/data/radar/13f.json` 의 원본).

    워치리스트와 겹치는 종목을 `in_watchlist` 로 강조하고 `watchlist_overlap` 에 모은다.

    [중요] 13F 는 **회사명**(`APPLE INC`)으로, 워치리스트는 **티커**(`AAPL`)로 적힌다. 이름 문자열만
    비교하면 실데이터에서 겹침이 사실상 0건이 된다(기존 동작). 그래서 `issuer_to_ticker`(회사명 → 티커
    색인)와 `cusip_to_ticker` 를 **선택 인자**로 받아 정규화 매칭을 한다. 인자를 주지 않으면 기존과
    같은 이름·CUSIP 직접 비교로 돌아간다(하위 호환).

    색인은 `build_issuer_ticker_index(load_company_tickers())` 로 만든다.
    """
    filings = [p if isinstance(p, Filing13F) else parse_filing(p) for p in payloads]
    usable = filings_available(filings, decision_at)
    latest = latest_by_filer(usable)
    previous = previous_by_filer(usable, latest)
    watch = {w.upper() for w in (watchlist or set())}
    issuer_index = _index_from_mapping(issuer_to_ticker)
    cusip_index = {str(k).upper(): str(v).upper() for k, v in (cusip_to_ticker or {}).items() if k and v}
    filers: list[dict] = []
    counts = {"new": 0, "increase": 0, "reduce": 0, "exit": 0}
    overlap: set[str] = set()
    for cik, current in sorted(latest.items(), key=lambda kv: kv[1].filer_name):
        changes = compute_deltas(previous.get(cik), current, min_change_pct)
        for row in changes:
            if row["kind"] in counts:
                counts[row["kind"]] += 1
            ticker = _row_ticker(row.get("issuer") or "", row.get("cusip") or "", cusip_index, issuer_index)
            label = (row["issuer"] or "").upper()
            row["ticker"] = ticker or None
            row["in_watchlist"] = bool(row["cusip"].upper() in watch
                                       or label in watch
                                       or (ticker and ticker.upper() in watch))
            if row["in_watchlist"]:
                overlap.add(ticker.upper() if ticker else (row["issuer"] or row["cusip"]))
        filers.append({
            **current.as_dict(),
            "changes": changes,
            "n_changes": sum(1 for r in changes if r["kind"] != "unchanged"),
        })
    mark = watermark(decision_at, usable)
    return {
        "as_of": decision_at.date().isoformat(),
        **mark,
        "lag_days": FILING_LAG_DAYS,
        "filers": filers,
        "n_filers": len(filers),
        "counts": counts,
        "watchlist_overlap": sorted(overlap),
        "status": "ok" if filers else "empty",
        "note_ko": "13F 는 신호에 쓰지 않는다(레이더 전용)." if filers else
                   "13F 제출이 아직 없다 — 레이더는 비어 있는 것이 정상이다.",
    }


# ══════════════════════════ 라이브 수집 (SEC EDGAR) ══════════════════════════
# 13F-HR 공시를 SEC EDGAR 에서 직접 받아 `Filing13F` 로 만든다. 네트워크·형식 오류는
# **예외로 죽이지 않고** `status`·`errors` 진단으로 돌려준다(주간 배치가 13F 때문에 멈추면 안 된다).
# 지켜야 하는 SEC 규칙(User-Agent·초당 10요청)은 `SecClient` 가 갖고 있다.

def load_13f_filers(path: str | Path = FILERS_PATH) -> list[Filer13F]:
    """`config/13f_filers.yaml` → 수집 대상 펀드 목록(CIK 10자리 정규화).

    파일이 없거나 항목이 깨졌으면 `Filing13FError` — 조용히 빈 목록으로 넘기지 않는다
    (설정 오타로 레이더가 비는 것과 13F 가 정말 없는 것을 구분해야 한다).
    """
    target = Path(path)
    if not target.exists():
        raise Filing13FError(f"13F 펀드 목록이 없다: {target}")
    try:
        body = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise Filing13FError(f"13F 펀드 목록을 읽을 수 없다({target}): {exc}") from exc
    rows = body.get("filers") if isinstance(body, Mapping) else body
    out: list[Filer13F] = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, Mapping):
            raise Filing13FError(f"13F 펀드 항목은 객체여야 한다: {row!r}")
        cik = str(row.get("cik") or "").strip()
        name = str(row.get("name") or "").strip()
        if not cik.isdigit():
            raise Filing13FError(f"13F 펀드 CIK 가 숫자가 아니다: {row!r}")
        if not name:
            raise Filing13FError(f"13F 펀드 이름이 없다: {row!r}")
        normalized = cik.zfill(10)
        if normalized in seen:
            raise Filing13FError(f"13F 펀드 CIK 중복: {normalized}")
        seen.add(normalized)
        unit = str(row.get("value_unit") or "dollars").strip().lower()
        if unit not in ("dollars", "thousands"):
            raise Filing13FError(f"13F value_unit 은 dollars|thousands: {row!r}")
        out.append(Filer13F(cik=normalized, name=name, style=str(row.get("style") or ""),
                            value_unit=unit))
    if not out:
        raise Filing13FError(f"13F 펀드 목록이 비었다: {target}")
    return out


def _at(values, index: int):
    """`submissions.filings.recent` 의 열 목록에서 i번째 값 — 열 길이가 달라도 죽지 않는다."""
    if not isinstance(values, (list, tuple)) or index >= len(values):
        return None
    return values[index]


def recent_13f_filings(submissions: dict, *, quarters: int = 2) -> list[dict]:
    """`submissions/CIK{10d}.json` → 최근 N개 분기의 **13F-HR 공시 1건씩**(최신순).

    - `13F-NT`(보유 없이 다른 운용사 지분을 통지하는 양식)는 정보표가 없으므로 대상이 아니다.
    - 같은 분기에 `13F-HR` 과 `13F-HR/A` 가 있으면 **원본 `13F-HR`** 을 쓴다(개정판은 부분 수정일 수 있다).
      원본이 없을 때만 개정판을 쓰고, 같은 양식이 여러 건이면 접수 시각이 가장 늦은 것을 쓴다.
    - `reportDate` 가 곧 `period_end`(분기말)이고, 시점 판정은 `acceptanceDateTime` 으로 한다.
    """
    recent = ((submissions or {}).get("filings") or {}).get("recent") or {}
    accns = recent.get("accessionNumber") or []
    by_period: dict[date, dict[str, list[dict]]] = {}
    for i, accn in enumerate(accns):
        form = str(_at(recent.get("form"), i) or "").strip()
        if form not in (FORM_HR, FORM_HR_A):
            continue
        period_end = _parse_date(_at(recent.get("reportDate"), i))
        if period_end is None or not accn:
            continue
        row = {
            "accession": str(accn),
            "form": form,
            "period_end": period_end,
            "filed_date": _parse_date(_at(recent.get("filingDate"), i)),
            "acceptance_datetime": _parse_dt(_at(recent.get("acceptanceDateTime"), i)),
            "primary_doc": str(_at(recent.get("primaryDocument"), i) or ""),
        }
        by_period.setdefault(period_end, {}).setdefault(form, []).append(row)
    out: list[dict] = []
    for period_end in sorted(by_period, reverse=True)[:max(int(quarters), 1)]:
        forms = by_period[period_end]
        pool = forms.get(FORM_HR) or forms.get(FORM_HR_A) or []
        if not pool:
            continue
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        out.append(max(pool, key=lambda r: (r["acceptance_datetime"] or epoch, r["accession"])))
    return out


def infotable_candidates(index_payload: dict, cik: str, accession: str) -> list[str]:
    """공시 폴더 `index.json` → 정보표 XML 후보 URL(우선순위 순).

    `primary_doc.xml` 은 **표지**(지분 내역이 없다). 실제 정보표 파일명은 지금 EDGAR 에서
    `infotable.xml` 이나 `60471.xml` 처럼 제각각이므로, 이름에 `infotable` 이 든 것을 먼저 두고
    나머지 `.xml` 을 뒤에 둔다 — 최종 판정은 받아서 `<infoTable>` 이 있는지로 한다.
    """
    from engine.data.sec_edgar import SEC_ARCHIVES  # noqa: PLC0415

    items = ((index_payload or {}).get("directory") or {}).get("item") or []
    base = f"{SEC_ARCHIVES}/{int(cik)}/{str(accession).replace('-', '')}"
    preferred: list[str] = []
    others: list[str] = []
    for item in items:
        name = str((item or {}).get("name") or "") if isinstance(item, Mapping) else str(item or "")
        lowered = name.lower()
        if not lowered.endswith(".xml"):
            continue
        basename = lowered.rsplit("/", 1)[-1]
        if basename == "primary_doc.xml":          # 표지 — 보유 내역이 없다
            continue
        if "infotable" in basename.replace("-", "").replace("_", ""):
            preferred.append(name)
        else:
            others.append(name)
    return [f"{base}/{name}" for name in preferred + others]


def _client_timeout(client) -> int:
    return int(getattr(client, "timeout", 25) or 25)


def _wait(client) -> None:
    limiter = getattr(client, "limiter", None)
    if limiter is not None and hasattr(limiter, "wait"):
        limiter.wait()


def _get_json(client, url: str) -> dict:
    """SEC JSON 1건 — rate limiter 를 지키고 `session.get` 으로 받는다(mock 교체 가능)."""
    _wait(client)
    response = client.session.get(url, timeout=_client_timeout(client))
    response.raise_for_status()
    return response.json()


def _get_text(client, url: str) -> str:
    """SEC XML 원문 — rate limiter 를 지키고 `session.get` 으로 받는다."""
    _wait(client)
    response = client.session.get(url, timeout=_client_timeout(client))
    response.raise_for_status()
    text = getattr(response, "text", None)
    if text is None:                                     # bytes 만 주는 응답
        content = getattr(response, "content", b"")
        text = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
    return text


def _short(exc: BaseException | None, limit: int = 160) -> str:
    if exc is None:
        return ""
    return f"{type(exc).__name__}: {str(exc)[:limit]}"


STATUS_KO = {
    "ok": "13F 수집 완료",
    "partial": "일부 펀드 수집 실패",
    "empty": "사용 가능한 13F 제출 없음",
    "failed": "13F 수집 실패",
    "missing_key": "SEC_USER_AGENT 없음 — 13F 미수집",
    "no_filers": "13F 펀드 목록 없음 — 13F 미수집",
}


def collect_13f_filings(decision_at: datetime, filers: list[Filer13F] | None = None,
                        client=None, config_path: str | Path = FILERS_PATH, *,
                        quarters: int = 2, max_xml_candidates: int = 3) -> dict:
    """SEC EDGAR 라이브 13F 수집 — `Filing13F` 목록 + 진단(`status`·`errors`).

    반환: `{"filings": [Filing13F…], "status": ok|partial|empty|failed|missing_key|no_filers,
    "errors": [{"cik","filer_name","stage","error"}…], "skipped": […], "n_filers",
    "n_requested", "n_filings", "note_ko"}`

    `decision_at` 시점에 **알 수 있는** 공시만 남긴다(`acceptance_datetime ≤ decision_at`, LA-7).
    아직 쓸 수 없는 분기는 `skipped` 에 남겨 "왜 안 보이는지"를 지운 채 지나가지 않는다.
    `SEC_USER_AGENT` 가 없으면 예외 없이 `missing_key` 진단과 빈 목록을 돌려준다.
    """
    if decision_at.tzinfo is None:
        raise Filing13FError("decision_at 에 시간대가 없다(Z 또는 오프셋 필요)")
    errors: list[dict] = []
    skipped: list[dict] = []

    def _result(rows: list[Filing13F], status: str) -> dict:
        filer_ciks = {f.filer_cik for f in rows}
        note = STATUS_KO.get(status, status)
        if status == "ok":
            note = f"{len(filer_ciks)}개 펀드 · 최근 {quarters}개 분기 13F"
        elif status == "partial":
            note = f"일부 실패 {len(errors)}건 — {len(filer_ciks)}개 펀드만 반영"
        return {
            "filings": rows,
            "status": status,
            "errors": errors,
            "skipped": skipped,
            "n_requested": len(filers or []),
            "n_filers": len(filer_ciks),
            "n_filings": len(rows),
            "note_ko": note,
        }

    if filers is None:
        try:
            filers = load_13f_filers(config_path)
        except Filing13FError as exc:
            errors.append({"stage": "config", "error": _short(exc)})
            return _result([], "no_filers")

    if client is None:
        if not os.environ.get("SEC_USER_AGENT"):
            errors.append({"stage": "config", "error": "SEC_USER_AGENT 없음"})
            return _result([], "missing_key")
        try:
            from engine.data.sec_edgar import SecClient  # noqa: PLC0415

            client = SecClient(cache_dir="data/cache/sec_facts")
        except Exception as exc:  # noqa: BLE001 — 키 형식 오류 등
            errors.append({"stage": "client", "error": _short(exc)})
            return _result([], "failed")

    rows_out: list[Filing13F] = []
    for filer in filers:
        try:
            submissions = client.submissions(int(filer.cik))
        except Exception as exc:  # noqa: BLE001
            errors.append({"cik": filer.cik, "filer_name": filer.name, "stage": "submissions",
                           "error": _short(exc)})
            continue
        for row in recent_13f_filings(submissions, quarters=quarters):
            try:
                index_url = (f"https://www.sec.gov/Archives/edgar/data/{int(filer.cik)}/"
                             f"{row['accession'].replace('-', '')}/index.json")
                index_payload = _get_json(client, index_url)
            except Exception as exc:  # noqa: BLE001
                errors.append({"cik": filer.cik, "filer_name": filer.name, "stage": "index",
                               "period_end": row["period_end"].isoformat(), "error": _short(exc)})
                continue
            candidates = infotable_candidates(index_payload, filer.cik, row["accession"])
            if not candidates:
                errors.append({"cik": filer.cik, "filer_name": filer.name, "stage": "infotable",
                               "period_end": row["period_end"].isoformat(),
                               "error": f"정보표 XML 없음({row['form']}) — 13F-NT 등 보유 내역 미제출"})
                continue
            holdings: list[Holding13F] | None = None
            last_error: BaseException | None = None
            for url in candidates[:max(int(max_xml_candidates), 1)]:
                try:
                    xml = _get_text(client, url)
                    parsed = parse_information_table(xml, value_scale=filer.scale())
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    continue
                if parsed:
                    holdings = aggregate_holdings(parsed)
                    break
            if not holdings:
                errors.append({"cik": filer.cik, "filer_name": filer.name, "stage": "infotable",
                               "period_end": row["period_end"].isoformat(),
                               "error": _short(last_error) if last_error else "정보표가 비었다"})
                continue
            filing = Filing13F(
                filer_cik=filer.cik,
                filer_name=filer.name,
                period_end=row["period_end"],
                filed_date=row["filed_date"],
                acceptance_datetime=row["acceptance_datetime"],
                holdings=holdings,
                form=row["form"],
                style=filer.style,
            )
            total_value = sum(h.value_usd for h in holdings)
            if total_value < 1e6 or total_value > 1e13:
                # 단위가 틀리면 1,000배 틀어진다 — 추정해 고치지 않고 설정 확인을 요구한다.
                errors.append({
                    "cik": filer.cik, "filer_name": filer.name, "stage": "value_unit",
                    "period_end": row["period_end"].isoformat(),
                    "error": (f"평가액 합계 ${total_value:,.0f} 이 비현실적 — "
                              "config/13f_filers.yaml 의 value_unit(dollars|thousands) 확인"),
                })
            if not filing.is_available_at(decision_at):
                skipped.append({
                    "cik": filer.cik, "filer_name": filer.name,
                    "period_end": row["period_end"].isoformat(),
                    "acceptance_datetime": _iso(row["acceptance_datetime"]), "form": row["form"],
                    "reason": "decision_at 이후 접수 — 미래 분기 보유를 섞지 않는다(LA-7)",
                })
                continue
            rows_out.append(filing)

    if rows_out and errors:
        return _result(rows_out, "partial")
    if rows_out:
        return _result(rows_out, "ok")
    return _result([], "failed" if errors else "empty")


def load_company_tickers(client=None, cache_path: str | Path | None = COMPANY_TICKERS_CACHE,
                         max_age_hours: float = 12.0) -> list[dict]:
    """SEC `company_tickers.json` → `[{ticker, cik, title}…]` (디스크 캐시).

    레이더의 회사명→티커 매칭에 필요한 색인 재료다. 캐시가 `max_age_hours` 안이면 요청하지 않는다
    (같은 날 두 번 도는 배치가 SEC 를 두 번 두드리지 않게). 캐시 실패는 수집 실패가 아니다.
    """
    path = Path(cache_path) if cache_path else None
    if path is not None and path.exists():
        age_h = (time.time() - path.stat().st_mtime) / 3600.0
        if age_h <= max_age_hours:
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(cached, list) and cached:
                    return cached
            except (OSError, json.JSONDecodeError):
                pass
    if client is None:
        from engine.data.sec_edgar import SecClient  # noqa: PLC0415

        client = SecClient()
    table = client.company_tickers()
    rows = table.to_dict("records") if hasattr(table, "to_dict") else [dict(r) for r in table]
    out = [{"ticker": str(r.get("ticker") or "").upper(), "cik": r.get("cik"),
            "title": str(r.get("title") or "")} for r in rows if r.get("ticker")]
    if path is not None and out:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass                                          # 캐시 실패는 수집 실패가 아니다
    return out

