"""engine.data.macro — FRED 거시 수집과 **시점(빈티지) 규칙**.

v1.3.2 [고정]:
- `D` 는 관측 날짜(`date`)가 아니라 **공개 빈티지 날짜(`realtime_start`)** 다.
  CPI(관측월 10월 → 11월 중순 발표)를 관측월 기준으로 쓰면 발표 전에 값을 안 것처럼 재생된다.
- 발표 시각을 모르면 **출처 시간대(US/Eastern) 기준 `D` 의 `23:59:59 ET` 이후 첫 의사결정**부터 쓴다.
- 라이브는 `release_at` → `first_ingested_at` 순으로 정한다. `available_at_source` 에 남긴다.
- **스냅샷 응답 예외(v1.3.2)**: FRED `/series/observations` 를 빈티지 파라미터 없이 부르면
  모든 행의 `realtime_start` 가 **요청 당일**(예: 2026-11-19)로 같게 내려온다. 이 값은 발표일이
  아니라 "오늘 본 최신 빈티지"이므로 발표일로 쓰면 11시리즈가 전부 마감 뒤로 밀려 탈락한다.
  그래서 응답의 모든 행에서 `realtime_start` 가 같고 `release_at`·`first_ingested_at` 이 없으면
  **관측일(`date`) 기준 보수 규칙**으로 계산한다:
    * 일간·주간 시리즈: `obs_date` 의 `23:59:59 ET` 이후 첫 의사결정(`obs_date_plus_one`)
    * 월간 시리즈(`CPIAUCSL`·`CPILFESL`·`UNRATE`): 발표 전 누수를 막기 위해 **관측월 다음 달
      20일** `23:59:59 ET` 이후 첫 의사결정(`obs_month_lag_20d`)
  행마다 `realtime_start` 가 다르거나 `release_at`/`first_ingested_at` 이 오면(ALFRED 빈티지·테스트)
  기존 v1.3.1 규칙을 **그대로** 쓴다.
- `BAMLH0A0HYM2` 제공 시작일은 API 응답에서 실제로 확인해 `data/limits.json` 에 적는다(추정 금지).
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Iterable

import numpy as np
import pandas as pd
import requests

from engine.utils.dates import UTC, et_day_end, first_decision_after

FRED_BASE = "https://api.stlouisfed.org/fred"
DEFAULT_SERIES = [
    "DFF", "CPIAUCSL", "CPILFESL", "DGS10", "DGS2", "T10Y2Y",
    "BAMLH0A0HYM2", "VIXCLS", "DTWEXBGS", "UNRATE", "WALCL",
]

REGIME_MULTIPLIERS = {"risk_on": 1.0, "neutral": 0.7, "risk_off": 0.4}

#: 월간 주기 시리즈 — 관측일(매월 1일)은 발표일보다 한 달 앞서므로 "다음 달 20일" 보수 규칙을 쓴다.
MONTHLY_SERIES = frozenset({"CPIAUCSL", "CPILFESL", "UNRATE"})
#: 월간 시리즈 보수 지연(일) — 관측월 다음 달 20일
MONTHLY_LAG_DAYS = 20


def _as_date(value) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return pd.Timestamp(value).date()


def resolve_available_at(
    realtime_start,
    release_at: datetime | str | None = None,
    first_ingested_at: datetime | str | None = None,
) -> tuple[datetime, str]:
    """거시 레코드의 `available_at` 과 그 출처를 정한다(2.4.3).

    Returns: (available_at in UTC, source ∈ {release_at, first_ingested_at, date_plus_one})
    """
    if release_at is not None:
        ts = pd.Timestamp(release_at)
        ts = ts.tz_localize(UTC) if ts.tzinfo is None else ts.tz_convert(UTC)
        return ts.to_pydatetime(), "release_at"
    if first_ingested_at is not None:
        ts = pd.Timestamp(first_ingested_at)
        ts = ts.tz_localize(UTC) if ts.tzinfo is None else ts.tz_convert(UTC)
        return ts.to_pydatetime(), "first_ingested_at"
    d = _as_date(realtime_start)
    return first_decision_after(et_day_end(d)), "date_plus_one"


def is_snapshot_response(rows: list[dict]) -> bool:
    """`realtime_start` 가 모든 행에서 같고 발표시각 필드가 없으면 **오늘의 스냅샷 응답**이다.

    빈티지 파라미터 없이 부른 `/series/observations` 의 `realtime_start` 는 발표일이 아니라
    요청 당일(최신 빈티지)이다 — 예: 2026-11-19 조회 시 2020년 관측치도 `2026-11-19`.
    """
    if not rows:
        return False
    if any(r.get("release_at") or r.get("first_ingested_at") for r in rows):
        return False
    starts = {str(r.get("realtime_start")) for r in rows if r.get("realtime_start") is not None}
    # 한 행짜리 응답은 스냅샷인지 빈티지 한 줄인지 구분할 수 없다 — 빈티지 규칙이 더 보수적이다.
    return len(rows) >= 2 and len(starts) == 1 and bool(starts)


def _next_month_day(d: date, day: int = MONTHLY_LAG_DAYS) -> date:
    """`d` 의 다음 달 `day` 일(그 달에 없으면 말일)."""
    year, month = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    try:
        return date(year, month, day)
    except ValueError:
        nxt = date(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1)
        return nxt - timedelta(days=1)


def conservative_available_at(obs_date: date, series_id: str | None) -> tuple[datetime, str]:
    """스냅샷 응답용 보수 규칙 — 관측일(또는 관측월 다음 달 20일) 종료 뒤 첫 의사결정."""
    if series_id in MONTHLY_SERIES:
        anchor = _next_month_day(obs_date)
        return first_decision_after(et_day_end(anchor)), "obs_month_lag_20d"
    return first_decision_after(et_day_end(obs_date)), "obs_date_plus_one"


def parse_observations(payload: dict, series_id: str | None = None) -> pd.DataFrame:
    """FRED `/series/observations` 응답 → 정규화 DataFrame."""
    rows = payload.get("observations", []) if isinstance(payload, dict) else list(payload)
    snapshot = is_snapshot_response(rows)
    out = []
    for r in rows:
        raw = r.get("value")
        try:
            val = float(raw) if raw not in (None, ".", "") else np.nan
        except (TypeError, ValueError):
            val = np.nan
        sid = series_id or r.get("series_id")
        if snapshot:
            D = _as_date(r.get("date"))
            available_at, source = conservative_available_at(D, sid)
        else:
            D = _as_date(r.get("realtime_start") or r.get("date"))
            available_at, source = resolve_available_at(D, r.get("release_at"), r.get("first_ingested_at"))
        out.append(
            {
                "series_id": sid,
                "date": _as_date(r.get("date")),
                "value": val,
                "realtime_start": D,
                "realtime_end": _as_date(r.get("realtime_end")) if r.get("realtime_end") else None,
                "release_at": r.get("release_at"),
                "first_ingested_at": r.get("first_ingested_at"),
                "available_at": available_at,
                "available_at_source": source,
            }
        )
    df = pd.DataFrame(out)
    if not df.empty:
        df = df.sort_values(["date", "realtime_start"]).reset_index(drop=True)
    return df


def as_of_macro(df: pd.DataFrame, decision_at: datetime) -> pd.DataFrame:
    """`available_at ≤ decision_at` 만 남긴다. 같은 관측일은 가장 늦게 알 수 있었던 빈티지를 쓴다.

    스냅샷 응답(v1.3.2)은 관측일당 행이 하나이고 `realtime_start` 가 전부 같으므로
    `tail(1)` 은 그 행을 그대로 고른다(중복 관측일이 생기면 빈티지가 늦은 쪽).
    """
    if df.empty:
        return df
    mask = pd.to_datetime(df["available_at"], utc=True) <= decision_at
    sub = df.loc[mask].copy()
    if sub.empty:
        return sub
    # 관측일별로 decision_at 시점에 알 수 있던 최신 빈티지
    idx = sub.sort_values(["date", "realtime_start"]).groupby("date").tail(1).index
    return sub.loc[idx].sort_values("date").reset_index(drop=True)


def series_limits(df: pd.DataFrame) -> dict:
    """제공 범위(첫·마지막 관측일, 빈티지 시작)."""
    if df.empty:
        return {"start": None, "end": None, "vintage_start": None, "rows": 0}
    return {
        "start": _as_date(df["date"].min()).isoformat(),
        "end": _as_date(df["date"].max()).isoformat(),
        "vintage_start": _as_date(df["realtime_start"].min()).isoformat(),
        "rows": int(len(df)),
    }


def build_limits(frames: dict[str, pd.DataFrame]) -> dict:
    """`data/limits.json` — 시리즈별 제공 범위와 하이일드 스프레드 의존 구간 표시."""
    limits = {"generated_at": datetime.now(UTC).isoformat(), "series": {}, "notes": []}
    for sid, df in frames.items():
        limits["series"][sid] = series_limits(df)
    hy = limits["series"].get("BAMLH0A0HYM2")
    if hy and hy["start"]:
        limits["hy_oas_available_from"] = hy["start"]
        limits["notes"].append(
            f"BAMLH0A0HYM2 실제 API 제공 시작 {hy['start']} — 이전 구간은 4신호 레짐으로만 판정하고 '미검증'으로 표시"
        )
    missing = [s for s in DEFAULT_SERIES if s not in frames or frames[s].empty]
    if missing:
        limits["missing_series"] = missing
    return limits


def classify_regime(inputs: dict) -> dict:
    """5개 신호 중 3개 이상 같은 방향이면 그 레짐, 아니면 neutral(3.7).

    inputs: real_rate_trend(up/flat/down), t10y2y(%), hy_oas_bp, vix, dxy_3m(변화율)
    하이일드 스프레드가 없으면 **0으로 채우지 않고** `inputs_missing` 에 남기고 4신호로 판정한다.
    """
    votes: dict[str, list[str]] = {"risk_on": [], "risk_off": []}
    missing: list[str] = []

    def add(signal: str, direction: str | None) -> None:
        if direction:
            votes[direction].append(signal)

    rt = inputs.get("real_rate_trend")
    add("real_rate", {"down": "risk_on", "up": "risk_off"}.get(rt))

    t = inputs.get("t10y2y")
    if t is not None and np.isfinite(t):
        add("t10y2y", "risk_on" if t > 0.5 else ("risk_off" if t < -0.2 else None))
    else:
        missing.append("t10y2y")

    hy = inputs.get("hy_oas_bp")
    if hy is not None and np.isfinite(hy):
        add("hy_oas", "risk_on" if hy < 350 else ("risk_off" if hy > 500 else None))
    else:
        missing.append("BAMLH0A0HYM2")

    vix = inputs.get("vix")
    if vix is not None and np.isfinite(vix):
        add("vix", "risk_on" if vix < 18 else ("risk_off" if vix > 25 else None))
    else:
        missing.append("VIXCLS")

    dxy = inputs.get("dxy_3m")
    if dxy is not None and np.isfinite(dxy):
        add("dxy", "risk_off" if dxy >= 0.05 else ("risk_on" if dxy < 0 else None))
    else:
        missing.append("DTWEXBGS")

    regime = "neutral"
    if len(votes["risk_off"]) >= 3:
        regime = "risk_off"
    elif len(votes["risk_on"]) >= 3:
        regime = "risk_on"

    extreme = bool((vix is not None and np.isfinite(vix) and vix > 35)
                   or (hy is not None and np.isfinite(hy) and hy > 700))
    return {
        "regime": regime,
        "multiplier": REGIME_MULTIPLIERS[regime],
        "votes": votes,
        "inputs_missing": missing,
        "macro_extreme": extreme,
    }


class FredClient:
    """FRED API 최소 클라이언트. 키는 환경변수/` .env ` 에서만 온다(코드 하드코딩 금지)."""

    def __init__(self, api_key: str | None = None, session: requests.Session | None = None, timeout: int = 25):
        self.api_key = api_key or os.environ.get("FRED_API_KEY")
        if not self.api_key:
            raise RuntimeError("FRED_API_KEY 가 없다 — .env 또는 환경변수로만 주입한다")
        self.session = session or requests.Session()
        self.timeout = timeout

    def observations(self, series_id: str, observation_start: str | None = "2018-01-01",
                     realtime_start: str | None = None, limit: int = 100000) -> pd.DataFrame:
        params = {
            "series_id": series_id, "api_key": self.api_key, "file_type": "json",
            "observation_start": observation_start, "limit": limit, "sort_order": "asc",
        }
        if realtime_start:
            params["realtime_start"] = realtime_start
        r = self.session.get(f"{FRED_BASE}/series/observations", params=params, timeout=self.timeout)
        r.raise_for_status()
        # 발표시각은 응답에 없다 — parse_observations 가 스냅샷/빈티지 응답을 구분해 보수 규칙을 정한다.
        return parse_observations(r.json(), series_id)

    def fetch_many(self, series_ids: Iterable[str], observation_start: str | None = "2018-01-01") -> dict[str, pd.DataFrame]:
        return {sid: self.observations(sid, observation_start=observation_start) for sid in series_ids}
