"""engine.decide.pipeline — 수집 스냅샷 → 지표 → 1·2층 신호 → 발행 레코드 (기획서 2.6 ⑧·⑪).

이 모듈은 **입력 데이터셋**(네트워크 수집이든 픽스처든)을 받아 `data/quant_signals.json` 의
본문을 만든다. 시각·파일 쓰기·커밋은 `scripts/run_daily.py` 가 한다(순수 함수 유지 → 결정론).

[고정]
- 판단(B2·B3)은 `split_adj_ohlc`, 화면·주문·체결은 `raw_as_traded` + `atr_pct` 비율.
- Veto 로 차단된 종목은 삭제하지 않고 `blocked[]` 에 사유와 함께 남긴다("안 산 이유도 정보").
- 신호 0건은 정상이다 — `signals: []` 로 발행하고 화면이 "오늘 신호 없음"을 쓴다.
- 발행 레코드는 `publish_record` 로만 만든다(체결가 비어 있음, 잠정 분할가).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from engine.decide.macro import classify_regime
from engine.decide.signals import (EntryContext, build_entry_plan, evaluate_entry, evaluate_exit,
                                   expected_mdd, margin_of_safety, target_band)
from engine.decide.veto import VetoInputs, veto_check
from engine.ledger.record import publish_record
from engine.metrics.fundamentals import growth_score, qc_wacc, quality_score, sanitize_fundamentals
from engine.metrics.technical import dma, snapshot_indicators
from engine.metrics.valuation import classify_sector, v_score
from engine.models import PriceKind
from engine.utils.dates import next_trading_day, trading_day_count

DEFAULT_PARAMS = Path("config/params_v001.yaml")
EVIDENCE_REQUIRED = ("fundamental", "sec_filing")

#: A 등급 요건(3.10 · 감사 P1-1).
#: - `MIN_VERIFIED_QUOTES`: 검증된 인용(해시 대조 통과)이 최소 1건 있어야 한다.
#: - `MIN_INDEPENDENT_SOURCES`: 독립 출처(서로 다른 `source`/`url`)가 최소 2곳.
#: - `MIN_REGIME_SAMPLE`: 해당 국면 표본 수 하한 — **미전달이면 A 가 아니다**(기획서 3.10 "미전달 시 B 상한").
MIN_VERIFIED_QUOTES = 1
MIN_INDEPENDENT_SOURCES = 2
MIN_REGIME_SAMPLE = 30
#: 8-K 대체 경로에서 **허용되지 않는** Item — 구조적 악재(상장폐지·재무제표 신뢰 상실·구조조정·중요 계약 해지).
#: 이 Item 이 있으면 뉴스 판독을 대체하는 근거가 될 수 없다(부록 E 위험 점수 55 이상과 같은 경계).
ALT_8K_BLOCKING_ITEMS = frozenset({"3.01", "4.02", "2.06", "1.05"})
#: 토론이 건드리지 않는 정량 기본 구조적 리스크(가드 V09 기준 70 미만).
QUANT_STRUCTURAL_RISK = 10.0


def load_params(path: str | Path = DEFAULT_PARAMS) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _f(value, nd: int = 4):
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(out):
        return None
    return round(out, nd)


def daily_vol(panel, window: int = 60) -> float | None:
    closes = panel.series(PriceKind.split_adj_ohlc).close().dropna()
    if len(closes) < 21:
        return None
    rets = closes.pct_change().dropna().tail(window)
    if len(rets) < 10:
        return None
    return float(rets.std(ddof=1))


def dma_slope(panel, window: int = 200, span: int = 20) -> float | None:
    sadj = panel.series(PriceKind.split_adj_ohlc)
    line = dma(sadj, window).dropna()
    if len(line) < span + 1:
        return None
    return float(line.iloc[-1] / line.iloc[-1 - span] - 1.0)


def dma20_sigma(panel, window: int = 20) -> float | None:
    sadj = panel.series(PriceKind.split_adj_ohlc)
    closes = sadj.close().dropna()
    if len(closes) < window:
        return None
    d20 = dma(sadj, window).dropna()
    if d20.empty:
        return None
    recent = closes.tail(window)
    sigma = float(recent.std(ddof=0))
    if sigma <= 0:
        return None
    return float((closes.iloc[-1] - d20.iloc[-1]) / sigma)


def chart_series(panel, weeks: int = 52) -> dict:
    """공개용 차트 — **주 1회 간격 정규화 지수(기준일=100)** + 50/200일선(같은 기준).

    원시 OHLC·거래량 시계열은 내보내지 않는다(2.2.4 대안 규칙).
    """
    sadj = panel.series(PriceKind.split_adj_ohlc)
    frame = sadj.frame
    if frame.empty:
        return {"labels": [], "index": [], "dma50": [], "dma200": []}
    weekly = frame.resample("W-FRI").last().dropna(subset=["sadj_close"])
    weekly = weekly.tail(weeks)
    if weekly.empty:
        return {"labels": [], "index": [], "dma50": [], "dma200": []}
    base = float(weekly["sadj_close"].iloc[0])
    d50 = dma(sadj, 50).resample("W-FRI").last().reindex(weekly.index).ffill()
    d200 = dma(sadj, 200).resample("W-FRI").last().reindex(weekly.index).ffill()
    return {
        "basis": "split_adj_index_base100",
        "labels": [d.date().isoformat() for d in weekly.index],
        "index": [round(float(v) / base * 100.0, 3) for v in weekly["sadj_close"]],
        "dma50": [None if pd.isna(v) else round(float(v) / base * 100.0, 3) for v in d50],
        "dma200": [None if pd.isna(v) else round(float(v) / base * 100.0, 3) for v in d200],
    }


def evidence_items(ticker: str, filings: list[dict], news: list[dict], fundamentals: dict | None,
                   news_state: str, as_of: str,
                   valuation_detail: dict | None = None) -> list[dict]:
    """공개 허용 근거 — 종류·지표명·시각·링크·인용 해시만(원문 문자열 금지, 2.2.3).

    R04: 재무 근거는 **실제 계산에 쓴 회계연도 10-K** 의 `accn`·`period_end`·`filed`·접수 시각을 단다.
    최근 10-Q(참고 공시)는 `role: "reference"` 로 **별도 항목**이고, 날짜를 모르면 `as_of` 로 대신하지 않는다.
    """
    items: list[dict] = []
    vdetail = valuation_detail or {}
    sources = [s for s in (vdetail.get("fundamental_sources") or []) if s.get("accn")]
    src = sources[0] if sources else {}
    if fundamentals:
        base = {"type": "fundamental", "period_end": src.get("fy"), "form": src.get("form"),
                "accession": src.get("accn"), "filed_date": src.get("filed"),
                "available_at": src.get("available_at")}
        # 모르는 날짜는 `as_of` 로 대신하지 않고 **키를 비운다**(스키마는 string 만 받는다).
        base = {k: v for k, v in base.items() if v is not None}
        items.append({**base, "metric": "quality_composite", "value_kind": "score_0_100"})
        items.append({**base, "metric": "fcf_margin_5y", "value_kind": "ratio"})
    reference = next((f for f in filings if f.get("form") == "10-Q"), None) \
        or next((f for f in filings if f.get("form") == "10-K"), None)
    if reference is not None:
        items.append({"type": "sec_filing", "form": reference["form"], "url": reference.get("url"),
                      "filed_date": reference.get("filed_date"),
                      "available_at": reference.get("acceptance_datetime"),
                      "accession": reference.get("accession"), "role": "reference"})
    if news and news_state != "not_read":
        # 기사 **버전**(해시·수집 시각·정규화 버전)을 근거에 함께 남긴다 — 나중에 "어떤 판을 읽었나"를
        # 대조할 수 없으면 인용 검증도 재현할 수 없다(감사 P1-1).
        for n in news[:3]:
            item = {"type": "news", "url": n.get("url"), "headline": n.get("headline"),
                    "source": n.get("source"), "published_at": n.get("published_at"),
                    "available_at": n.get("published_at")}
            sha = n.get("quote_sha256")
            # 해시가 없으면 **명시적으로** `False` 를 쓴다 — 키 누락을 "통과"로 읽지 않게(감사 P1-1).
            item["quote_verified"] = bool(sha) and n.get("quote_verified") is not False
            if sha:
                item["quote_sha256"] = sha
            for key in ("content_hash", "retrieved_at", "normalizer_version"):
                if n.get(key):
                    item[key] = n[key]
            items.append(item)
    if len(items) >= 3:
        items.append({"type": "market", "metric": "atr_pct", "value_kind": "ratio"})
    return items


def verified_quote_items(evidence: list[dict]) -> list[dict]:
    """`quote_verified is True` **엄격 검사**를 통과한 인용 항목만(누락은 통과가 아니다)."""
    return [e for e in evidence or []
            if e.get("type") == "news" and e.get("quote_verified") is True and e.get("quote_sha256")]


def failed_quote_items(evidence: list[dict]) -> list[dict]:
    """해시를 주장했는데 검증되지 않은 인용 — 있으면 등급은 C(3.10)."""
    return [e for e in evidence or []
            if e.get("type") == "news" and e.get("quote_sha256") and e.get("quote_verified") is not True]


def independent_source_count(evidence: list[dict], sources=None) -> int:
    """독립 출처 수 — 같은 매체의 기사 2건은 1곳으로 센다(`source`, 없으면 `url`)."""
    if sources is not None:
        return len({str(s).strip().lower() for s in sources if str(s or "").strip()})
    seen = set()
    for e in evidence or []:
        if e.get("type") != "news":
            continue
        key = str(e.get("source") or "").strip().lower() or str(e.get("url") or "").strip().lower()
        if key:
            seen.add(key)
    return len(seen)


def regime_sample_ok(regime_sample=None) -> tuple[bool, str | None]:
    """해당 국면 표본 충족 여부 — **미전달(`None`)은 통과가 아니라 미충족**이다(기획서 3.10).

    A 등급의 조건은 "해당 국면 백테스트 표본 ≥ 30" 이고, 기획서는 **미전달 시 B 상한**을 명시한다.
    그래서 예전의 `None → True` 하위 호환은 버그였다(감사 07-b). 값이 오면 `int` 또는
    `{"n": 30, "min_n": 30}`·`{"count": 30, "min": 30}` 형태를 받는다. `min_n` 을 주더라도
    기획서 하한 30 아래로 내려가지 않는다(표본을 적게 잡아 A 를 얻는 우회 금지).
    """
    if regime_sample is None:
        return False, "regime_sample_missing"
    if isinstance(regime_sample, dict):
        n = regime_sample.get("n", regime_sample.get("count", regime_sample.get("samples")))
        minimum = regime_sample.get("min_n", regime_sample.get("min", MIN_REGIME_SAMPLE))
    else:
        n, minimum = regime_sample, MIN_REGIME_SAMPLE
    try:
        n_int, min_int = int(n), int(minimum)
    except (TypeError, ValueError):
        return False, "regime_sample_invalid"
    min_int = max(MIN_REGIME_SAMPLE, min_int)
    if n_int < min_int:
        return False, f"regime_sample_lt_{min_int}"
    return True, None


def eight_k_allowed_for_alt(filing: dict) -> bool:
    """A 등급의 8-K 대체 경로에 쓸 수 있는 8-K 인가 — 구조적 악재 Item 이 없어야 한다."""
    if str(filing.get("form") or "").upper() != "8-K":
        return False
    items = {str(item).strip() for item in (filing.get("items") or [])}
    return not (items & ALT_8K_BLOCKING_ITEMS)


def _verified_financial_items(evidence: list[dict]) -> list[dict]:
    """검증된 재무 근거 — 접수번호·공시 시각이 붙은 재무 항목.

    `fundamental`(XBRL 산출) 뿐 아니라 **10-K·10-Q 공시 참조**도 재무 근거로 인정한다.
    8-K 자신은 대체 경로의 "공시" 쪽 조건이므로 여기에 넣지 않는다(순환 방지).
    """
    out: list[dict] = []
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        if not (item.get("accession") or item.get("available_at")):
            continue
        kind = item.get("type")
        form = str(item.get("form") or "").upper()
        if kind == "fundamental" or (kind == "sec_filing" and form in ("10-K", "10-Q")):
            out.append(item)
    return out


def evidence_source_keys(evidence: list[dict], sources=None) -> set[str]:
    """독립 출처 키 — 뉴스는 매체, 공시·재무는 접수번호(또는 URL).

    8-K 대체 경로는 뉴스 인용이 없으므로 "독립 출처 ≥2곳"을 공시·재무까지 넓혀 센다.
    같은 10-K 에서 나온 두 항목은 접수번호가 같아 **1곳**으로 센다.
    """
    if sources is not None:
        return {str(s).strip().lower() for s in sources if str(s or "").strip()}
    seen: set[str] = set()
    for e in evidence or []:
        kind = e.get("type")
        if kind == "news":
            key = str(e.get("source") or "").strip().lower() or str(e.get("url") or "").strip().lower()
        elif kind in ("sec_filing", "fundamental"):
            key = (str(e.get("accession") or "").strip().lower()
                   or str(e.get("url") or "").strip().lower())
        else:
            key = ""
        if key:
            seen.add(key)
    return seen


def alt_evidence_ok(evidence: list[dict], alt_8k_evidence=None, sources=None) -> tuple[bool, str | None]:
    """뉴스 미판독이어도 A 를 줄 수 있는 **8-K·재무 대체 경로**(기획서 3.10).

    조건: ① 구조적 악재가 아닌 8-K ② 접수번호가 확인된 재무 근거 ③ 독립 출처 ≥2곳.
    (신선도·국면 표본은 호출자 쪽 조건으로 따로 본다.)
    """
    filings = [f for f in (alt_8k_evidence or []) if isinstance(f, dict)]
    if not any(eight_k_allowed_for_alt(f) for f in filings):
        return False, "alt_8k_missing"
    if not _verified_financial_items(evidence):
        return False, "alt_fundamental_unverified"
    if len(evidence_source_keys(evidence, sources)) < MIN_INDEPENDENT_SOURCES:
        return False, "alt_sources_lt_2"
    return True, None


def confidence_grade(evidence: list[dict], news_state: str, *, regime_sample=None,
                     verified_quotes=None, sources=None, data_fresh: bool = True,
                     alt_8k_evidence=None,
                     grade_basis: dict | None = None) -> str:
    """신뢰도 A/B/C (3.10) — `A 데이터·근거 충족` 라벨의 근거.

    A 는 **주장이 아니라 검증으로** 얻는다(감사 P1-1):
    ① 핵심 근거(`fundamental`·`sec_filing`) ② 데이터 신선(`data_fresh`) ③ 국면 표본 ≥30
    ④ 검증된 인용(해시 대조 통과) ≥1 ⑤ 독립 출처 ≥2 ⑥ 근거 3건 이상.
    뉴스 판독이 **없을 때**는 ④를 8-K·재무 대체 경로(`alt_evidence_ok`)로 바꿔 볼 수 있다.
    하나라도 모자라면 B, 근거 구조가 없거나 인용 검증이 실패하면 C.

    `verified_quotes`·`sources` 를 주면 그 값을 쓴다(토론 리포트처럼 근거 목록이 따로 있는 경로).
    주지 않으면 `evidence` 에서 센다 — **해시 없는 뉴스는 통과가 아니라 미검증**이다.
    `grade_basis` 를 주면 A 를 막은 이유를 그 dict 에 남긴다(화면·감사용).
    """
    basis = grade_basis if grade_basis is not None else {}
    items = list(evidence or [])
    types = {e.get("type") for e in items}
    core = all(t in types for t in EVIDENCE_REQUIRED)
    verified = list(verified_quotes) if verified_quotes is not None else verified_quote_items(items)
    distinct = independent_source_count(items, sources)
    sample_ok, sample_reason = regime_sample_ok(regime_sample)
    basis.update({"data_fresh": bool(data_fresh), "regime_sample_ok": bool(sample_ok),
                  "verified_quotes": len(verified), "independent_sources": distinct,
                  "articles": len(items), "news_state": news_state})
    if failed_quote_items(items) or news_state == "insufficient_evidence":
        basis["reason"] = ("quote_unverified" if failed_quote_items(items)
                           else "insufficient_evidence")
        return "C"
    if not core:
        basis["reason"] = "core_evidence_missing"
        return "C"
    common: list[str] = []
    if not data_fresh:
        common.append("data_stale")
    if not sample_ok:
        common.append(sample_reason or "regime_sample_missing")
    if len(items) < 3:
        common.append("evidence_lt_3")
    if news_state == "read_done":
        blocking = list(common)
        if len(verified) < MIN_VERIFIED_QUOTES:
            blocking.append("quote_unverified")
        if distinct < MIN_INDEPENDENT_SOURCES:
            blocking.append("sources_lt_2")
        if not blocking:
            basis["reason"] = None
            return "A"
        basis["reason"] = blocking[0]
        return "B"
    # 뉴스 미판독 — 8-K·재무 근거로 대체할 수 있는가
    alt_ok, alt_reason = alt_evidence_ok(items, alt_8k_evidence, sources)
    basis["alt_8k"] = bool(alt_ok)
    blocking = list(common)
    if not alt_ok:
        blocking.append(alt_reason or "alt_path_unavailable")
    if not blocking:
        basis["reason"] = None
        return "A"
    basis["reason"] = blocking[0]
    return "B"


CONFIDENCE_RANK = {"A": 3, "B": 2, "C": 1}


def worse_confidence(*grades: str) -> str:
    """토론은 신뢰도를 **올리지 않는다**(3.10) — 더 낮은 등급만 남긴다."""
    values = [g for g in grades if g]
    if not values:
        return "C"
    return min(values, key=lambda g: CONFIDENCE_RANK.get(g, 0))


def earnings_days(filings: list[dict], as_of: date) -> int | None:
    """8-K Item 2.02 이력으로 다음 실적까지 남은 거래일을 추정한다(V01 입력)."""
    dates = []
    for f in filings:
        if f.get("form") == "8-K" and "2.02" in (f.get("items") or []):
            try:
                dates.append(date.fromisoformat(str(f["filed_date"])[:10]))
            except (KeyError, ValueError):
                continue
    if not dates:
        return None
    last = max(dates)
    estimate = last + timedelta(days=91)
    while estimate < as_of:
        estimate += timedelta(days=91)
    return trading_day_count(as_of, estimate) if estimate >= as_of else -trading_day_count(estimate, as_of)


def close_lag_business_days(last_bar: date | None, as_of: date) -> int | None:
    if last_bar is None:
        return None
    target = as_of - timedelta(days=1)
    if last_bar >= target:
        return 0
    return trading_day_count(last_bar, target)


@dataclass
class TickerView:
    ticker: str
    sector: str
    indicators: dict
    panel: object
    iv_low: float | None
    iv_median: float | None
    iv_high: float | None
    margin: float | None
    q: float
    v: float | None
    g: float | None
    news_state: str
    evidence: list[dict]
    filings: list[dict]
    news: list[dict]
    valuation_suspended: bool
    suspended_reason: str | None
    structure: dict
    valuation_detail: dict | None = None
    iv_band_basis: str | None = None
    split_factor: float = 1.0
    news_state_base: str = "not_read"          # 토론 반영 전 상태 — 신뢰도 승격 금지(3.10)
    v_penalties: float = 0.0                   # v1.3.2: DCF 민감도 −4 · 역사 백분위 괴리 −10
    ev_ebit_percentile: float | None = None    # 10년 EV/EBIT 백분위(0~100) → S1 과열
    #: 오늘 스냅샷에 **실제로 묶인** 토론 요약(감사 07). 스냅샷·신호가 다른 행은 여기 오지 않는다.
    debate: dict = field(default_factory=dict)
    #: A 등급 판정에 넘길 데이터 신선도·국면 표본(감사 07-c). 없으면 `None` → A 상한이 걸린다.
    regime_sample: dict | int | None = None


def _num(value) -> float | None:
    """양수 유한값만 숫자로 넘긴다(0·NaN·문자열은 None)."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if np.isfinite(out) and out > 0 else None


def _debate_news_state(base: str, debate_row: dict) -> str:
    """토론이 읽어 준 뉴스 상태를 반영한다 — 근거 부족은 내리고, 판독 완료는 미판독일 때만."""
    value = debate_row.get("news_state")
    if value == "insufficient_evidence":
        return "insufficient_evidence"
    if value == "read_done" and base == "not_read":
        return "read_done"
    return base


def _debate_link_ok(row: dict, *, ticker: str, as_of: date | None,
                    snapshot_id: str | None) -> tuple[bool, str | None]:
    """이 토론 행이 **오늘 이 종목의 스냅샷**에 묶여 있는가(기획서 §4.8.1).

    `ticker + as_of` 만으로는 같은 날 다른 스냅샷의 토론이 섞인다. 그래서 행에 적힌
    `snapshot_id` 가 현재 스냅샷과 **같아야** 하고, `signal_id` 가 있으면 회차 키
    (`{as_of}-{ticker}-…`)도 같아야 한다. 현재 스냅샷을 모르는 호출(픽스처·구 경로)에서는
    비교할 기준이 없으므로 행을 그대로 쓴다.
    """
    if not isinstance(row, dict) or not row:
        return False, "empty"
    current_snapshot = str(snapshot_id or "").strip()
    row_snapshot = str(row.get("snapshot_id") or "").strip()
    if current_snapshot and row_snapshot != current_snapshot:
        return False, "snapshot_mismatch"
    row_signal = str(row.get("signal_id") or "").strip()
    if row_signal and as_of is not None:
        prefix = f"{as_of.isoformat()}-{str(ticker).upper()}-"
        if not row_signal.upper().startswith(prefix.upper()):
            return False, "signal_mismatch"
    row_ticker = str(row.get("ticker") or "").strip().upper()
    if row_ticker and row_ticker != str(ticker).upper():
        return False, "ticker_mismatch"
    if row.get("eligible") is False:
        return False, "not_eligible"
    return True, None


def debate_row_for(debate: dict | None, *, ticker: str, as_of: date | None,
                   snapshot_id: str | None = None) -> dict:
    """토론 요약에서 **이 종목·이 스냅샷**의 행을 고른다.

    입력은 `{(signal_id, snapshot_id): row}`·`{signal_id: row}`·`{ticker: row}` 어느 형태든 받는다
    (호출자 호환). 형태와 무관하게 `_debate_link_ok` 를 통과한 행만 돌려주고, 여럿이면
    `(signal_id, snapshot_id)` 키로 온 **가장 구체적인 행**을 쓴다.
    """
    if isinstance(debate, (list, tuple)):
        # 행 목록으로 오는 호출도 받는다 — 키가 없으므로 행 안의 식별자로만 검증한다.
        debate = {i: row for i, row in enumerate(debate)}
    if not isinstance(debate, dict):
        return {}
    ticker = str(ticker).upper().strip()
    best: dict = {}
    best_rank = -1
    for key, value in debate.items():
        if isinstance(key, tuple):
            parts = [str(p) for p in key]
            key_signal = parts[0] if parts else ""
            key_snapshot = parts[1] if len(parts) > 1 else ""
            rank = 2
        else:
            text = str(key or "").strip()
            key_signal = "" if text.upper() == ticker else text
            key_snapshot = ""
            rank = 2 if key_signal else 1
        items = list(value) if isinstance(value, (list, tuple)) else [value]
        for row in items:
            if not isinstance(row, dict):
                continue
            merged = dict(row)
            if key_signal:
                merged.setdefault("signal_id", key_signal)
            if key_snapshot:
                merged.setdefault("snapshot_id", key_snapshot)
            ok, _ = _debate_link_ok(merged, ticker=ticker, as_of=as_of, snapshot_id=snapshot_id)
            if not ok or rank <= best_rank:
                continue
            best, best_rank = merged, rank
    return best


def build_views(data: dict, as_of: date, debate: dict | None = None, *,
                snapshot_id: str | None = None) -> list[TickerView]:
    """데이터셋 → 종목별 지표·가치·점수(신호 판단의 입력).

    `debate` 는 **오늘 스냅샷에 묶인 토론 요약만** 반영한다 — `(signal_id, snapshot_id)` 로
    확인되지 않는 구 스냅샷 행은 조용히 버린다(`debate_row_for`). 그래야 어제의 토론이 오늘
    정량 주문(`evaluate_entry` 의 C/WATCH)을 바꾸지 못한다(감사 07-a).
    """
    views: list[TickerView] = []
    current_snapshot = snapshot_id if snapshot_id is not None else data.get("snapshot_id")
    panels = data["panels"]
    for row in data["universe"]:
        ticker = row["ticker"]
        panel = panels.get(ticker)
        if panel is None or panel.frame.empty:
            continue
        ind = snapshot_indicators(panel)
        if not ind:
            continue
        sector = row.get("sector") or ""
        sec = classify_sector(sector)
        fundamentals = sanitize_fundamentals((data.get("fundamentals") or {}).get(ticker))
        q = quality_score(fundamentals)["q"] if fundamentals else 50.0
        vdetail = (data.get("valuation_inputs") or {}).get(ticker)
        if not isinstance(vdetail, dict):
            vdetail = {}   # 픽스처는 ValuationInputs 를 직접 넣는다 — 배관 대상은 dict 뿐이다
        iv_median = iv_low = iv_high = None
        band_basis = None
        if not sec["valuation_suspended"]:
            iv_median = _num(vdetail.get("iv_median")) or _num(row.get("iv_median"))
            if iv_median is None:
                mult = row.get("iv_mult")
                if mult is not None:
                    iv_median = ind["sadj_close"] * float(mult)
            if iv_median is not None:
                # R04: `value_company` 가 계산한 bear/base/bull 밴드를 그대로 쓴다.
                iv_low = _num(vdetail.get("iv_low")) or _num(row.get("iv_low"))
                iv_high = _num(vdetail.get("iv_high")) or _num(row.get("iv_high"))
                band_basis = vdetail.get("iv_band_basis") or row.get("iv_band_basis")
                if iv_low is None or iv_high is None:
                    iv_low = iv_median * 0.85
                    iv_high = iv_median * 1.15
                    band_basis = "fallback_15pct"
        margin = None if (iv_median is None or sec["valuation_suspended"]) else margin_of_safety(
            iv_median, ind["sadj_close"])
        # v1.3.2: 감점은 1층(워치리스트)과 **같은 값**을 쓴다 — 두 층의 V 가 갈라지면 순위와 등급이 모순된다.
        v_penalties = float(vdetail.get("v_penalties") or 0.0)
        ev_ebit_percentile = (float(vdetail["ev_ebit_percentile"])
                              if vdetail.get("ev_ebit_percentile") is not None else None)
        v = None if margin is None else round(v_score(margin, v_penalties), 2)
        g = None
        if not sec["valuation_suspended"]:
            # 데이터셋이 G 점수를 직접 주면 그것을 쓴다(픽스처). 라이브는 PEG·성장률로 계산한다.
            g = row.get("g")
            if g is None and (row.get("peg") is not None or row.get("revenue_cagr") is not None):
                g = growth_score(row.get("peg"), row.get("revenue_cagr")).get("g")
        news_state_base = row.get("news_state") or "not_read"
        # 정량 입력(검증된 뉴스·공시 상태)과 토론 권고를 분리한다: 여기서 붙는 것은
        # "오늘 스냅샷의 판독 상태" 뿐이고, 권고(등급·보류)는 `debate` 로만 실어 보낸다.
        debate_row = debate_row_for(debate, ticker=ticker, as_of=as_of, snapshot_id=current_snapshot)
        news_state = _debate_news_state(news_state_base, debate_row)
        filings = (data.get("filings") or {}).get(ticker, [])
        news = (data.get("news") or {}).get(ticker, [])
        evidence = evidence_items(ticker, filings, news, row, news_state, as_of.isoformat(), vdetail)
        split_factor = 1.0
        if len(panel.frame.index) and "split_factor" in panel.frame.columns:
            value = panel.frame["split_factor"].iloc[-1]
            if np.isfinite(value):
                split_factor = float(value)
        structure = {
            "daily_vol_60d": daily_vol(panel),
            "dma200_slope": dma_slope(panel),
            "dma20_sigma": dma20_sigma(panel),
            "last_bar": panel.frame.index[-1].date() if len(panel.frame.index) else None,
            "chart": chart_series(panel),
        }
        views.append(TickerView(
            ticker=ticker, sector=sector, indicators=ind, panel=panel,
            iv_low=iv_low, iv_median=iv_median, iv_high=iv_high, margin=margin,
            q=round(float(q), 2), v=v, g=None if g is None else round(float(g), 2),
            news_state=news_state, evidence=evidence, filings=filings, news=news,
            news_state_base=news_state_base,
            valuation_suspended=bool(sec["valuation_suspended"]),
            suspended_reason=sec["reason"], structure=structure,
            valuation_detail=vdetail, iv_band_basis=band_basis, split_factor=split_factor,
            v_penalties=v_penalties, ev_ebit_percentile=ev_ebit_percentile,
            debate=debate_row, regime_sample=_regime_sample_for(data, regime_hint=None),
        ))
    return views


def _regime_sample_for(data: dict, regime_hint=None):
    """국면 표본 입력 — 데이터셋이 그 국면의 표본을 주면 그대로, 없으면 `None`(→ A 상한, 기획서 3.10).

    `collect()` 는 `regime_sample` 에 `by_regime`(국면별 `n`·`min_n`·`matured_as_of`)를 넘긴다.
    국면 이름을 모르는 호출(`regime_hint=None`)에서 국면별 문서를 통째로 넘기면 `n` 이 없어
    `regime_sample_invalid` 가 되거나 엉뚱한 국면 표본으로 A 가 열릴 수 있다 — 그래서 그때는
    `None` 을 돌려 B 상한을 유지한다. 레거시 형태(`{"n": 30}`·`30`)는 그대로 받는다(기존 호출 호환).
    """
    value = (data or {}).get("regime_sample")
    if value is None:
        value = (data or {}).get("regime_samples")
    if isinstance(value, dict):
        per_regime = value.get("by_regime")
        if isinstance(per_regime, dict):
            if not regime_hint:
                return None
            picked = per_regime.get(str(regime_hint))
            return picked if isinstance(picked, (dict, int)) else None
        if regime_hint and isinstance(value.get(str(regime_hint)), (dict, int)):
            return value[str(regime_hint)]
    return value


def _sample_fields(regime_sample, regime_name: str | None) -> dict:
    """신뢰도 판정 근거에 남길 국면 표본 수·기준일(감사 5331ab7-12)."""
    if isinstance(regime_sample, dict):
        n = regime_sample.get("n", regime_sample.get("count", regime_sample.get("samples")))
        try:
            n_int = None if n is None else int(n)
        except (TypeError, ValueError):
            n_int = None
        return {"regime_sample_n": n_int,
                "regime_sample_min_n": regime_sample.get("min_n", MIN_REGIME_SAMPLE),
                "regime_sample_horizon": regime_sample.get("horizon"),
                "matured_as_of": regime_sample.get("matured_as_of"),
                "regime_sample_source": regime_sample.get("source")}
    if isinstance(regime_sample, (int, float)):
        return {"regime_sample_n": int(regime_sample), "regime_sample_min_n": MIN_REGIME_SAMPLE,
                "regime_sample_horizon": None, "matured_as_of": None, "regime_sample_source": None}
    return {"regime_sample_n": None, "regime_sample_min_n": MIN_REGIME_SAMPLE,
            "regime_sample_horizon": None, "matured_as_of": None, "regime_sample_source": None}


def _last_return(panel) -> float | None:
    closes = panel.series(PriceKind.split_adj_ohlc).close().dropna()
    if len(closes) < 2:
        return None
    return float(closes.iloc[-1] / closes.iloc[-2] - 1.0)


def build_payload(data: dict, *, as_of: date, mode: str, params: dict, params_version: str,
                  code_commit: str, snapshot_id: str, status: str,
                  three_times: dict, held: list[dict] | None = None,
                  sector_weights: dict | None = None, source: str | None = None,
                  inputs_hash: str | None = None, debate: dict | None = None) -> dict:
    """수집 스냅샷 → `quant_signals` 본문(레저 요약은 호출자가 붙인다).

    `debate` 는 편입 자격을 통과한 토론 요약(`{ticker: {...}}`)이다. 토론은 **뉴스 상태와
    구조적 리스크만** 건드리고 신뢰도 등급을 올리지 않는다(3.10 · 3.11.8).
    """
    views = build_views(data, as_of, debate, snapshot_id=snapshot_id)
    regime = classify_regime(data.get("macro"), data.get("macro_previous"))
    # 국면 표본은 A 등급 판정에만 쓰인다 — 넘기지 않으면 `regime_sample_missing` 으로 B 상한.
    regime_sample = _regime_sample_for(data, regime_hint=getattr(regime, "regime", None))
    sample_fields = _sample_fields(regime_sample, getattr(regime, "regime", None))
    debate_cfg = (params or {}).get("debate") or {}
    auto_downgrade_enabled = bool(debate_cfg.get("auto_downgrade_enabled", False))
    held = held or []
    held_tickers = {h["ticker"] for h in held}
    sector_weights = dict(sector_weights or {})

    signals: list[dict] = []
    blocked: list[dict] = []
    watchlist: list[dict] = []
    diagnostics: list[dict] = []
    charts: dict[str, dict] = {}
    contexts: dict[str, EntryContext] = {}

    for view in views:
        ind = view.indicators
        last_bar = view.structure.get("last_bar")
        lag = close_lag_business_days(last_bar, as_of)
        earnings = row_days = next((r.get("days_to_earnings") for r in data["universe"]
                                    if r["ticker"] == view.ticker), None)
        if row_days is None:
            earnings = earnings_days(view.filings, as_of)
        veto_in = VetoInputs(
            days_to_earnings=earnings,
            close_lag_business_days=lag,
            filing_items=[i for f in view.filings if f.get("form") == "8-K" for i in (f.get("items") or [])],
            filing_items_verified=[],
            adv20_usd=next((r.get("adv20_usd") for r in data["universe"] if r["ticker"] == view.ticker), None),
            vix=(data.get("macro") or {}).get("vix"),
            hy_oas_bp=(data.get("macro") or {}).get("hy_oas_bp"),
            projected_sector_weight=sector_weights.get(view.sector, 0.0) + 0.05,
            projected_position_weight=0.05,
            one_day_return=_last_return(view.panel),
            news_injection=False,
            evidence=view.evidence,
        )
        veto = veto_check(veto_in)
        # 토론은 **이 뷰에 묶인 행**만 쓴다 — ticker 로 다시 찾으면 스냅샷 검증을 우회한다(감사 07-a).
        deb = view.debate or {}
        # P1-1 정책 분리: 토론의 `structural_risk` 는 **자동 하향이 켜졌을 때만** 정량 게이트에 들어간다.
        # 기본값(auto_downgrade_enabled=false)에서는 `debate_structural_risk` 로만 남겨
        # `quant_only` 기준선(B5·quant_grade)을 토론이 오염시키지 않게 한다(감사 P1-1-b).
        debate_risk: float | None = None
        if deb.get("structural_risk") is not None:
            try:
                debate_risk = max(0.0, min(100.0, float(deb["structural_risk"])))
            except (TypeError, ValueError):
                debate_risk = None
        apply_debate_risk = bool(auto_downgrade_enabled) and debate_risk is not None
        structural_risk = debate_risk if apply_debate_risk else QUANT_STRUCTURAL_RISK
        ctx = EntryContext(
            ticker=view.ticker,
            raw_close=ind["raw_close"],
            sadj_close=ind["sadj_close"],
            split_factor=view.split_factor,
            sector=view.sector,
            dma50=ind.get("dma50"),
            dma200=ind.get("dma200"),
            dma200_slope=view.structure.get("dma200_slope"),
            rsi14=ind.get("rsi14"),
            pct_b=ind.get("pct_b"),
            dma20_sigma=view.structure.get("dma20_sigma"),
            atr_pct=ind.get("atr_pct"),
            iv_median=view.iv_median,
            iv_high=view.iv_high,
            ev_ebit_percentile=view.ev_ebit_percentile,
            q=view.q,
            v=view.v,
            g=view.g,
            structural_risk=structural_risk,
            in_watchlist=view.ticker not in held_tickers,
            held=view.ticker in held_tickers,
            valuation_suspended=view.valuation_suspended,
            news_state=view.news_state,
            evidence=view.evidence,
            vetoes=veto.as_list(),
            debate_advice={"recommendation": deb.get("recommendation"),
                           "structural_risk": debate_risk,
                           "divergent": deb_with_flag(deb)} if deb else None,
        )
        contexts[view.ticker] = ctx
        evaluation = evaluate_entry(ctx, regime=regime, params=params)
        watchlist.append({
            "ticker": view.ticker, "sector": view.sector, "q": view.q, "v": view.v, "g": view.g,
            "v_raw": None if view.margin is None else round(v_score(view.margin, 0.0), 2),
            "margin": _f(view.margin),
            "v_penalties": round(float((view.valuation_detail or {}).get("v_penalties") or 0.0), 2),
            "ev_ebit_percentile": (view.valuation_detail or {}).get("ev_ebit_percentile"),
            # v1.3.3 06: 표본 품질(개수·연수·실제 범위·사유)을 워치리스트 행에도 같이 낸다.
            "ev_ebit_samples": (view.valuation_detail or {}).get("ev_ebit_samples"),
            "ev_ebit_years": (view.valuation_detail or {}).get("ev_ebit_years"),
            "ev_ebit_window": (view.valuation_detail or {}).get("ev_ebit_window"),
            "ev_ebit_reason": (view.valuation_detail or {}).get("ev_ebit_reason"),
            "hist_divergence_signed": _f((view.valuation_detail or {}).get("hist_divergence_signed")),
            "growth_clipped_to_zero": (view.valuation_detail or {}).get("growth_clipped_to_zero"),
            "wacc_sensitivity": _f((view.valuation_detail or {}).get("wacc_sensitivity")),
            "rank_score": None if (view.v is None or view.g is None) else round(
                0.5 * view.q + 0.3 * float(view.v) + 0.2 * float(view.g), 3),
            "valuation_suspended": view.valuation_suspended,
            # R03: Q 점수 경로의 WACC 와 가치평가 경로의 WACC 는 다른 값이다 — 이름을 나눠 내보낸다.
            "wacc_qc": _f(qc_wacc(data["fundamentals"][view.ticker]))
            if (data.get("fundamentals") or {}).get(view.ticker) else None,
            "wacc_valuation": _f((view.valuation_detail or {}).get("wacc")),
            "iv_band_basis": view.iv_band_basis,
        })
        charts[view.ticker] = view.structure.get("chart") or {}
        diag = {
            "ticker": view.ticker,
            "grade": evaluation.get("grade"),
            "entry_score": evaluation.get("entry_score"),
            "failed_gates": evaluation.get("failed_gates"),
            "veto": [v["code"] for v in veto.vetoes],
            "close_lag": lag,
            "days_to_earnings": earnings,
            "news_state": view.news_state,
        }
        diagnostics.append(diag)

        if veto.blocked:
            blocked.append({
                "ticker": view.ticker,
                "reason": ",".join(veto.blocking),
                "codes": veto.blocking,
                "message_ko": " · ".join(v["message_ko"] for v in veto.vetoes),
                "grade": evaluation.get("grade"),
            })
            continue
        if evaluation.get("signal") == "SUSPENDED":
            blocked.append({"ticker": view.ticker, "reason": "SECTOR_FORMULA_PENDING",
                            "codes": ["SUSPENDED"],
                            "message_ko": "가치평가 보류 업종 — 신호를 발행하지 않는다(3.2.1)."})
            continue
        if evaluation.get("grade") in ("NONE", None):
            continue

        # 신뢰도 등급 + 분할 계획 — 토론이 붙어도 등급은 **내려가기만** 한다(3.10)
        # 신선도: `last_bar` 가 직전 영업일이면 신선(0). 지연·미상이면 A 를 줄 수 없다(감사 07-b).
        data_fresh = bool(lag == 0)
        grade_basis: dict = {}
        conf_live = confidence_grade(view.evidence, view.news_state, regime_sample=regime_sample,
                                     data_fresh=data_fresh, alt_8k_evidence=view.filings,
                                     grade_basis=grade_basis)
        conf = worse_confidence(conf_live,
                                confidence_grade(view.evidence, view.news_state_base,
                                                 regime_sample=regime_sample, data_fresh=data_fresh,
                                                 alt_8k_evidence=view.filings))
        # C + 뉴스 근거 부족은 **등급·의도 모두 WATCH 상한**이다(감사 P1-1).
        # 신호 등급은 `signals.evaluate_entry` 가 이미 내리지만, 발행 레코드의 `quant_intent`
        # 까지 같은 규칙으로 묶어야 화면·원장·토론 트랙이 서로 다른 말을 하지 않는다.
        evidence_cap = conf == "C" and view.news_state == "insufficient_evidence"
        buy = evaluation.get("signal") == "BUY"
        if evidence_cap and buy:
            evaluation = {**evaluation, "signal": "WATCH",
                          "failed_gates": list(evaluation.get("failed_gates") or []) + ["CONFIDENCE_C_CAP"],
                          "cap_reasons": list(evaluation.get("cap_reasons") or [])
                          + ["뉴스 근거 부족(C) → 매수 상한 WATCH(3.9.4)"]}
            buy = False
        plan = None
        if buy:
            plan = build_entry_plan(ctx, evaluation, regime=regime, params=params,
                                    total_asset=1.0, allow_watch=True)
        plan_weight = 0.0
        stop_distance = 0.0
        sizing_note = None
        if plan and plan.get("placeable"):
            plan_weight = float(plan["plan_weight"])
            stop_distance = float(plan["stop_distance"])
        else:
            sizing_note = "주문 범위 밖 — 관찰만(3.4.4)"
        band = target_band(iv_median=view.iv_median or ind["raw_close"] * 1.2, price=ind["raw_close"])
        # 채점의 기준 봉을 **발행 시점에 함께 고정**한다 — 나중에 알게 되는 `as_of` 당일 종가를
        # 기준으로 삼으면 밴드(`base_price`)와 수익률(`r_base`)이 다른 봉을 가리킨다(감사 09-d).
        band["return_base"] = "base_price"
        band["base_bar_date"] = last_bar.isoformat() if hasattr(last_bar, "isoformat") else last_bar
        record = publish_record(
            signal_id=f"{as_of.isoformat()}-{view.ticker}-{('BUY' if buy else 'WATCH')}-001",
            ticker=view.ticker,
            type="BUY" if buy else "WATCH",
            grade=conf,
            entry_score=float(evaluation.get("entry_score") or 0.0),
            as_of=as_of.isoformat(),
            data_cutoff_at=three_times["data_cutoff_at"],
            signal_published_at=three_times["signal_published_at"],
            order_cutoff_at=three_times["order_cutoff_at"],
            target_session=three_times.get("target_session"),
            generated_price=ind["raw_close"],
            target_band=band,
            expected_mdd=expected_mdd(view.structure.get("daily_vol_60d")),
            thesis_ko=_thesis(view, evaluation),
            evidence=view.evidence,
            investor_scores=evaluation.get("investor_scores") or {},
            conflict=bool(evaluation.get("conflict")),
            plan_weight=plan_weight,
            stop_distance=stop_distance,
            veto=[],
            params_version=params_version,
            snapshot_id=snapshot_id,
            code_commit=code_commit,
            invalidation={"price": round(float(ind["dma200"] or ind["sadj_close"]) * 0.97, 2),
                          "reason": "200일선 −3% 이탈 + ROIC<WACC"} if ind.get("dma200") else None,
            quant_intent="pending_buy" if (buy and plan and plan.get("placeable")) else "no_action",
            inputs_hash=inputs_hash,
        )
        record["trade_grade"] = evaluation.get("grade")
        record["components"] = evaluation.get("components")
        # P1-1 정책 분리 배지: 토론 리스크는 **기록**하되 자동 하향이 꺼져 있으면 게이트를 바꾸지 않는다.
        record["debate_structural_risk"] = None if debate_risk is None else round(float(debate_risk), 2)
        record["debate_risk_gate_applied"] = bool(apply_debate_risk)
        record["confidence_cap"] = ("WATCH" if evidence_cap else None)
        # 신뢰도 판정 근거를 레코드에 남긴다 — 왜 A 가 아닌지 화면·감사가 바로 읽는다.
        # 감사 5331ab7-12: 어떤 국면의 몇 건 표본으로 A 를 판단했는지(또는 표본이 없었는지)를 남긴다.
        record["confidence_detail"] = {"grade": conf, **grade_basis, **sample_fields}
        # 발행 시점 국면을 레코드에 남긴다 — 이 값이 나중에 국면별 표본(만기 전 표본)의 근거가 된다.
        # 사후에 최신 거시로 다시 분류하지 않기 위해 발행 시점에 그대로 확정한다.
        record["regime"] = regime.regime
        record["regime_verified"] = bool(regime.verified)
        # 토론은 **권고**로만 붙는다(스냅샷·신호가 같은 행). 정량 판단 필드(news_state·등급·의도)는
        # 위에서 이미 확정됐고, 여기서는 "어떤 토론이 붙었나"만 기록한다(감사 07).
        record["debate_ref"] = ({"signal_id": deb.get("signal_id"),
                                 "snapshot_id": deb.get("snapshot_id"),
                                 "report_id": deb.get("report_id"),
                                 "news_state": deb.get("news_state")} if deb else None)
        record["debate_advice"] = ({"recommendation": deb.get("recommendation"),
                                    "quant_action": deb.get("quant_action"),
                                    "debate_action": deb.get("debate_action"),
                                    "divergent": deb_with_flag(deb)}
                                   if deb else None)
        detail = view.valuation_detail or {}
        dcf_detail = detail.get("iv_dcf") or {}
        record["valuation"] = {
            "iv_low": _f(view.iv_low), "iv_median": _f(view.iv_median), "iv_high": _f(view.iv_high),
            "price": _f(ind["raw_close"]), "margin": _f(view.margin),
            # R04: 방법별 IV·시나리오 밴드·중앙값 선정 근거를 레코드까지 그대로 넘긴다.
            "iv_band_basis": view.iv_band_basis,
            "median_method": detail.get("median_method"),
            "methods": {k: _f(v) for k, v in (detail.get("methods") or {}).items()},
            "iv_dcf_bear": _f(dcf_detail.get("bear")), "iv_dcf_bull": _f(dcf_detail.get("bull")),
            "fcff_normalized": _f(detail.get("fcff_normalized")),
            "growth": _f(detail.get("growth")), "growth_basis": detail.get("growth_basis"),
            "net_debt": _f(detail.get("net_debt")),
            "wacc_valuation": _f(detail.get("wacc")),
            "v_confidence": detail.get("v_confidence"),
            "v_confidence_reason": detail.get("v_confidence_reason"),
            "v_penalties": _f(detail.get("v_penalties")),
            "v_penalty_dcf": _f(detail.get("v_penalty_dcf")),
            "v_penalty_hist": _f(detail.get("v_penalty_hist")),
            "dcf_sensitivity": _f(detail.get("dcf_sensitivity")),
            "ev_ebit_percentile": _f(detail.get("ev_ebit_percentile")),
            "ev_ebit_median_10y": _f(detail.get("ev_ebit_median_10y")),
            "iv_hist_ev_ebit": _f(detail.get("iv_hist_ev_ebit")),
            "hist_divergence": _f(detail.get("hist_divergence")),
            "dcf_dropped": detail.get("dcf_dropped"),
            # ── v1.3.3 (트랙 B 04·05·06·08) 배관 ──────────────────────────────────────────
            # 화면·JSON 이 "왜 이 V 인가"를 보여 줄 수 있게 감점 전 V·원시 값·표본 품질을 레코드까지 넘긴다.
            "v_raw": None if view.margin is None else round(v_score(view.margin, 0.0), 2),
            "growth_raw_cagr": _f(detail.get("growth_raw_cagr")),
            "growth_clipped_to_zero": detail.get("growth_clipped_to_zero"),
            "effective_fcff_years": detail.get("effective_fcff_years"),
            "normalization_method": detail.get("normalization_method"),
            "wacc_sensitivity": _f(detail.get("wacc_sensitivity")),
            "terminal_g_sensitivity": _f(detail.get("terminal_g_sensitivity")),
            "rate_sensitivity": _f(detail.get("rate_sensitivity")),
            "hist_divergence_signed": _f(detail.get("hist_divergence_signed")),
            "ev_ebit_current": _f(detail.get("ev_ebit_current")),
            "ev_ebit_samples": detail.get("ev_ebit_samples"),
            "ev_ebit_years": detail.get("ev_ebit_years"),
            "ev_ebit_window": detail.get("ev_ebit_window"),
            "ev_ebit_reason": detail.get("ev_ebit_reason"),
            "minority_interest": _f(detail.get("minority_interest")),
            "debt_reporting": detail.get("debt_reporting"),
        }
        record["news_state"] = view.news_state
        record["news"] = [{"headline": n.get("headline"), "source": n.get("source"),
                           "published_at": n.get("published_at"), "url": n.get("url"),
                           "impact": n.get("impact"), "durability": n.get("durability"),
                           "quote_verified": n.get("quote_verified")} for n in view.news[:5]]
        record["filings"] = [{k: f.get(k) for k in ("form", "filed_date", "url", "accession", "items")}
                             for f in view.filings[:5]]
        record["risk"] = {"veto": [], "note_ko": None}
        record["indicators"] = {k: _f(ind.get(k)) for k in ("rsi14", "pct_b", "dma50", "dma200", "atr_pct")}
        record["gate_summary"] = {"failed": evaluation.get("failed_gates") or [],
                                  "reasons": evaluation.get("gate_reasons") or {}}
        record["sizing_note_ko"] = None if (plan and plan.get("placeable")) else sizing_note
        record["decision"] = "BUY" if buy else "WATCH"
        record["sector"] = view.sector
        record["entry_band"] = (plan or {}).get("entry_band")
        signals.append(record)

    position_checks = []
    if held:
        for h in held:
            ctx = contexts.get(h["ticker"])
            if ctx is None:
                position_checks.append({"ticker": h["ticker"], "verdict": "NO_DATA"})
                continue
            position_checks.append(evaluate_exit(ctx, h, regime=regime, params=params))

    return {
        "schema_version": 1,
        "generated_at": _iso(datetime.now(timezone.utc)),
        "data_cutoff_at": three_times["data_cutoff_at"],
        "signal_published_at": three_times["signal_published_at"],
        "order_cutoff_at": three_times["order_cutoff_at"],        "as_of": as_of.isoformat(),
        "params_version": params_version,
        "snapshot_id": snapshot_id,
        "code_commit": code_commit,
        "status": status,
        "mode": mode,
        "source": source or data.get("source", "live"),
        "regime": regime.as_dict(),
        # 감사 5331ab7-12 — 이 페이로드의 신뢰도 A 판정에 쓰인 국면 표본 공급 상태.
        "regime_sample": _public_sample(regime_sample, sample_fields, regime.regime,
                                        (data or {}).get("regime_sample")),
        "ingest": data.get("macro_ingest"),
        "signals": signals,
        "blocked": blocked,
        "watchlist": watchlist,
        "watchlist_count": len(watchlist),
        "position_checks": position_checks,
        "diagnostics": diagnostics,
        "charts": charts,
        "next_trading_day": next_trading_day(as_of).isoformat(),
    }


def _public_sample(regime_sample, sample_fields: dict, regime_name: str | None, doc) -> dict:
    """이 페이로드가 쓴 국면 표본 공급 상태(감사 5331ab7-12) — 화면·감사가 숫자와 기준일을 바로 본다."""
    by_regime: dict = {}
    counts = None
    if isinstance(doc, dict):
        for name, row in (doc.get("by_regime") or {}).items():
            if isinstance(row, dict):
                by_regime[str(name)] = {"n": row.get("n"), "min_n": row.get("min_n")}
        counts = doc.get("counts")
    n = sample_fields.get("regime_sample_n")
    return {
        "regime": regime_name,
        "n": n,
        "min_n": sample_fields.get("regime_sample_min_n"),
        "matured_as_of": sample_fields.get("matured_as_of"),
        "horizon": sample_fields.get("regime_sample_horizon"),
        "source": sample_fields.get("regime_sample_source"),
        "reason": None if n is not None else "regime_sample_missing",
        "by_regime": by_regime or None,
        "counts": counts,
    }


def deb_with_flag(debate_row: dict) -> bool | None:
    """토론과 정량이 갈렸는가 — 행에 없으면 `None`(모름), 있으면 bool."""
    if "divergent" not in (debate_row or {}):
        return None
    return bool(debate_row.get("divergent"))


def _thesis(view: TickerView, evaluation: dict) -> str:
    """한 문장 이유 — 수치를 그대로 인용한다(창작 금지).

    부호 규칙(R04): `margin = (IV − 가격)/IV` 이다. 양수면 가격이 IV 보다 **낮다(할인)**,
    음수면 **프리미엄**이고 그 크기는 절대값으로 쓴다(과거 `-233% 프리미엄` 오확).
    """
    parts: list[str] = []
    if view.margin is not None:
        if view.margin >= 0:
            parts.append(f"IV 대비 {view.margin:.0%} 할인")
        else:
            parts.append(f"IV 대비 {-view.margin:.0%} 프리미엄(안전마진 {view.margin:.0%})")
    rsi = view.indicators.get("rsi14")
    if rsi is not None:
        parts.append(f"RSI {rsi:.0f}")
    if view.g is not None and view.g <= 0:
        parts.append("성장률 0 이하 → 등급 상한 WATCH")
    if not parts:
        parts.append("근거 수치 부족 — 관찰만")
    return " · ".join(parts)
