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

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

#: 13F 제출 기한(분기말 + 45일) — 이 날짜 전에는 그 분기의 보유를 "알 수 없다".
FILING_LAG_DAYS = 45
#: 레이더 지연 표시 기준(2.7).
STALE_DAYS = 135
WATERMARK_KO = "45일 전 분기말 기준"

PUT_CALL = ("", "Put", "Call")


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
class Filing13F:
    filer_cik: str
    filer_name: str
    period_end: date
    filed_date: date | None = None
    acceptance_datetime: datetime | None = None
    holdings: list[Holding13F] = field(default_factory=list)

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


def parse_information_table(xml: str | bytes) -> list[Holding13F]:
    """13F 정보표 XML(`<infoTable>` 반복) → 보유 행 목록.

    SEC 원문은 네임스페이스가 붙어 오므로 로컬 이름으로 비교한다. 값이 `""`/`0` 인
    행(옵션·비상장)은 `put_call`·`unit` 으로 구분해 남긴다.
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
        raw_value = _text(node, ("value",))
        try:
            value = float(raw_value) * 1000.0 if raw_value and not raw_value.startswith("$") else 0.0
        except ValueError:
            value = 0.0
        if raw_value.startswith("$"):
            try:
                value = float(raw_value.replace("$", "").replace(",", ""))
            except ValueError:
                value = 0.0
        put_call = _text(node, ("putcall",)).title()
        unit = _text(node, ("sshprnamttype",)).upper()
        if not cusip:
            continue
        rows.append(Holding13F(cusip=cusip.upper(), issuer=issuer, value_usd=value,
                               put_call=put_call if put_call in PUT_CALL else "", unit=unit))
    return rows


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
    prev = {h.cusip: h for h in (previous.holdings if previous else [])}
    cur = {h.cusip: h for h in current.holdings}
    rows: list[dict] = []
    for cusip, holding in cur.items():
        before = prev.get(cusip)
        if before is None:
            kind, delta_pct = "new", None
        else:
            base = before.value_usd
            delta_pct = None if base <= 0 else (holding.value_usd - base) / base
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
            "issuer": holding.issuer,
            "kind": kind,
            "kind_ko": KIND_KO[kind],
            "value_usd": round(holding.value_usd, 2),
            "prev_value_usd": None if before is None else round(before.value_usd, 2),
            "delta_pct": None if delta_pct is None else round(delta_pct, 4),
        })
    for cusip, before in prev.items():
        if cusip in cur:
            continue
        rows.append({
            "cusip": cusip, "issuer": before.issuer, "kind": "exit", "kind_ko": KIND_KO["exit"],
            "value_usd": 0.0, "prev_value_usd": round(before.value_usd, 2), "delta_pct": -1.0,
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


def radar_payload(payloads: list[dict] | list[Filing13F], decision_at: datetime,
                  watchlist: set[str] | None = None, min_change_pct: float = 0.05) -> dict:
    """13F 레이더 산출물(`web/data/radar/13f.json` 의 원본).

    워치리스트와 겹치는 종목(`issuer` 또는 `ticker` 가 워치리스트에 있는 행)을 강조한다.
    """
    filings = [p if isinstance(p, Filing13F) else parse_filing(p) for p in payloads]
    usable = filings_available(filings, decision_at)
    latest = latest_by_filer(usable)
    previous = previous_by_filer(usable, latest)
    watch = {w.upper() for w in (watchlist or set())}
    filers: list[dict] = []
    counts = {"new": 0, "increase": 0, "reduce": 0, "exit": 0}
    overlap: set[str] = set()
    for cik, current in sorted(latest.items(), key=lambda kv: kv[1].filer_name):
        changes = compute_deltas(previous.get(cik), current, min_change_pct)
        for row in changes:
            if row["kind"] in counts:
                counts[row["kind"]] += 1
            label = (row["issuer"] or "").upper()
            row["in_watchlist"] = bool(row["cusip"].upper() in watch or label in watch)
            if row["in_watchlist"]:
                overlap.add(row["issuer"] or row["cusip"])
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
        "note_ko": "13F 는 신호에 쓰지 않는다(레이더 전용)." if filers else
                   "13F 제출이 아직 없다 — 레이더는 비어 있는 것이 정상이다.",
    }
