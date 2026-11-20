"""engine.data.macro — FRED 거시 수집과 **시점(빈티지) 규칙**.

[고정] 시점 규칙(v1.3.3 — 라이브·재생 **같은 경로**)
- `D` 는 관측 날짜(`date`)가 아니라 **공개 시점**이다. CPI(관측월 10월 → 11월 중순 발표)를 관측월
  기준으로 쓰면 발표 전에 값을 안 것처럼 재생된다.
- **모든 실행이 마감 빈티지 점조회**다: `realtime_start = realtime_end = V`(마감일 **ET 전날**).
  라이브도 같은 점조회를 쓴다 — 배치가 마감보다 늦게 돌아도 **마감 시점에 실제로 확인 가능했던 값만**
  판단 입력이 된다. 예전 라이브 경로는 `available_at = 최초 수집 시각` 으로 두고 `ref = ingest_at`
  으로 필터해, 마감 6시간 39분 뒤 수집한 값이 레짐·WACC 무위험금리에 그대로 들어갔다(감사 01:
  `status: normal`·`regime.verified: true`). 수집 지연은 `ingest`(lag_seconds·after_cutoff) 메타로만
  남고 판단 입력에서는 빠진다.
- 응답이 스냅샷(`realtime_start` 가 요청한 빈티지보다 나중) 이면 `MacroVintageError` 로 그 시리즈를
  **버리고** `inputs_missing` 에 남긴다 — 관측일로 역산해 과거 결정에 앞당기지 않는다.
- 마감 시각 이후에만 존재하는 값은 `as_of_macro(df, decision_at)` 의 `available_at <= decision_at`
  필터에서 빠진다(시점 필터는 **언제나 컷오프**다 — 수집 시각이 아니다).
- 발표 시각을 모르면 **출처 시간대(US/Eastern) 기준 D 의 `23:59:59 ET` 이후 첫 의사결정**부터 쓴다.
- `classify_regime` 의 3개월 추세·CPI YoY 는 **같은 빈티지 프레임에서 계산**한다(추가 조회 없음).
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


class MacroVintageError(RuntimeError):
    """재생 모드에서 스냅샷 응답(요청 당일 빈티지)이 왔다 — 그 시리즈를 쓸 수 없다."""

#: 월간 주기 시리즈 — 관측일(매월 1일)은 발표일보다 한 달 앞서므로 "다음 달 20일" 보수 규칙을 쓴다.
MONTHLY_SERIES = frozenset({"CPIAUCSL", "CPILFESL", "UNRATE"})
#: 월간 시리즈 보수 지연(일) — 관측월 다음 달 20일
MONTHLY_LAG_DAYS = 20


def _as_date(value) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return pd.Timestamp(value).date()


def _as_dt(value) -> datetime | None:
    if value in (None, ""):
        return None
    ts = pd.Timestamp(value)
    ts = ts.tz_localize(UTC) if ts.tzinfo is None else ts.tz_convert(UTC)
    return ts.to_pydatetime()


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


def cutoff_vintage(decision_at: datetime) -> str:
    """마감 시각에 실제로 확인 가능했던 FRED 빈티지 날짜(`YYYY-MM-DD`).

    마감일의 **ET 날짜 하루 전**을 쓴다 — 마감일 당일 발표분은 발표 시각을 알 수 없으므로(FRED 응답에
    발표 시각이 없다) 통째로 배제하는 것이 보수적이다. 라이브와 재생이 같은 값을 쓴다.
    """
    if decision_at.tzinfo is None:
        raise ValueError("timezone-aware datetime 이 필요하다")
    ts = pd.Timestamp(decision_at)
    ts = ts.tz_convert("America/New_York") if ts.tzinfo is not None else ts.tz_localize("UTC")
    return (ts.date() - timedelta(days=1)).isoformat()


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


def parse_observations(payload: dict, series_id: str | None = None, *, mode: str = "auto",
                       first_ingested_at: datetime | str | None = None,
                       vintage: str | None = None) -> pd.DataFrame:
    """FRED `/series/observations` 응답 → 정규화 DataFrame.

    `mode`: `auto`(기본) = 응답 모양으로 판단, `replay` = 빈티지 점조회 전용(스냅샷 응답이면
    `MacroVintageError`). `live` 는 저수준 호환 모드다 — `collect_macro` 는 **쓰지 않는다**(v1.3.3부터
    라이브도 마감 빈티지 점조회를 쓴다).
    """
    rows = payload.get("observations", []) if isinstance(payload, dict) else list(payload)
    mode = str(mode or "auto")
    snapshot = is_snapshot_response(rows) if mode == "auto" else False
    if mode == "replay":
        # 요청한 빈티지보다 **나중** 빈티지가 오면 그건 스냅샷(=오늘의 최신값)이다.
        limit = _as_date(vintage) if vintage else None
        stamps = [_as_date(r.get("realtime_start")) for r in rows if r.get("realtime_start")]
        if any(limit is not None and s is not None and s > limit for s in stamps):
            raise MacroVintageError(
                f"{series_id}: 스냅샷 응답(realtime_start {max(stamps).isoformat()} > {limit.isoformat()}) — 재생에 쓸 수 없다")
    if mode == "live" and first_ingested_at is None:
        raise ValueError("live 모드는 first_ingested_at 이 필요하다")
    out = []
    for r in rows:
        raw = r.get("value")
        try:
            val = float(raw) if raw not in (None, ".", "") else np.nan
        except (TypeError, ValueError):
            val = np.nan
        sid = series_id or r.get("series_id")
        if mode == "live":
            D = _as_date(r.get("realtime_start") or r.get("date"))
            available_at, source = resolve_available_at(D, None, first_ingested_at)
            available_at = _as_dt(first_ingested_at) or available_at
        elif snapshot:
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
                     realtime_start: str | None = None, limit: int = 100000,
                     realtime_end: str | None = None, mode: str = "auto",
                     first_ingested_at: datetime | str | None = None) -> pd.DataFrame:
        params = {
            "series_id": series_id, "api_key": self.api_key, "file_type": "json",
            "observation_start": observation_start, "limit": limit, "sort_order": "asc",
        }
        if realtime_start:
            params["realtime_start"] = realtime_start
        if realtime_end:
            params["realtime_end"] = realtime_end
        r = self.session.get(f"{FRED_BASE}/series/observations", params=params, timeout=self.timeout)
        r.raise_for_status()
        # 발표시각은 응답에 없다 — 라이브/재생 모드가 시점 기준을 정한다(R07).
        return parse_observations(r.json(), series_id, mode=mode,
                                  first_ingested_at=first_ingested_at,
                                  vintage=realtime_start if mode == "replay" else None)

    def fetch_many(self, series_ids: Iterable[str], observation_start: str | None = "2018-01-01",
                   realtime_start: str | None = None, realtime_end: str | None = None,
                   mode: str = "auto", first_ingested_at: datetime | str | None = None
                   ) -> dict[str, pd.DataFrame]:
        """여러 시리즈를 받는다. 재생에서는 실패한 시리즈를 **건너뛰고** 사유를 남긴다.

        한 시리즈의 빈티지 응답이 스냅샷이면 그 시리즈만 빼고 나머지를 계속 쓴다(부분 실패 격리).
        """
        out: dict[str, pd.DataFrame] = {}
        errors: dict[str, str] = {}
        for sid in series_ids:
            try:
                out[sid] = self.observations(
                    sid, observation_start=observation_start, realtime_start=realtime_start,
                    realtime_end=realtime_end, mode=mode, first_ingested_at=first_ingested_at,
                )
            except MacroVintageError as exc:
                errors[sid] = str(exc)[:160]
            except Exception as exc:  # noqa: BLE001 — 한 시리즈 실패가 전체를 비우지 않는다
                errors[sid] = f"{type(exc).__name__}: {str(exc)[:120]}"
        if errors:
            self.last_errors = errors
        else:
            self.last_errors = {}
        return out
