"""engine.utils.dates — NYSE 거래일 캘린더와 세 시각(data_cutoff / signal_published / order_cutoff).

기획서 4.2.1 의 폴드 표(2025-08-19 기준 1M 4·3M 3·6M 2)를 그대로 재현하는 것이 목표다.
외부 라이브러리(pandas_market_calendars) 없이 NYSE 규칙을 직접 구현한다:
주말 + 정규 휴장일(관측일 규칙) + 특별 휴장(2001·2004·2007·2012·2018·2025).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
KST = ZoneInfo("Asia/Seoul")

#: 하루 두 번의 데이터 마감 (UTC 시각). postclose = 07:00 KST, premarket = 21:50 KST.
DECISION_CUTOFF_UTC_HOURS = (12, 22)
DECISION_CUTOFF_MINUTES = {12: 50, 22: 0}

HORIZONS_TD = {"1w": 5, "1m": 21, "3m": 63, "6m": 126}


# ────────────────────────────── 부활절/휴장일 ──────────────────────────────
def easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def _observed_fixed(d: date, *, observe_saturday: bool) -> date | None:
    """고정일 휴장의 관측일. 토요일이면 금요일(단, 신정은 예외), 일요일이면 월요일."""
    if d.weekday() == 5:  # Saturday
        return d - timedelta(days=1) if observe_saturday else None
    if d.weekday() == 6:  # Sunday
        return d + timedelta(days=1)
    return d


#: 거래소가 별도로 공지한 휴장(장례·허리케인·테러)
SPECIAL_CLOSURES: frozenset[date] = frozenset(
    {
        date(2001, 9, 11), date(2001, 9, 12), date(2001, 9, 13), date(2001, 9, 14),
        date(2004, 6, 11),   # 레이건 전 대통령 장례
        date(2007, 1, 2),    # 포드 전 대통령 장례
        date(2012, 10, 29), date(2012, 10, 30),   # 허리케인 샌디
        date(2018, 12, 5),   # 조지 H.W. 부시 전 대통령 장례
        date(2025, 1, 9),    # 지미 카터 전 대통령 장례
    }
)


@lru_cache(maxsize=64)
def market_holidays(year: int) -> frozenset[date]:
    out: set[date] = set()
    ny = _observed_fixed(date(year, 1, 1), observe_saturday=False)  # 신정은 토요일이면 관측하지 않는다
    if ny:
        out.add(ny)
    out.add(_nth_weekday(year, 1, 0, 3))            # MLK
    out.add(_nth_weekday(year, 2, 0, 3))            # Presidents' Day
    out.add(easter_sunday(year) - timedelta(days=2))  # Good Friday
    out.add(_last_weekday(year, 5, 0))              # Memorial Day
    if year >= 2022:
        j = _observed_fixed(date(year, 6, 19), observe_saturday=True)
        if j:
            out.add(j)                              # Juneteenth (2022~)
    ind = _observed_fixed(date(year, 7, 4), observe_saturday=True)
    if ind:
        out.add(ind)
    out.add(_nth_weekday(year, 9, 0, 1))            # Labor Day
    out.add(_nth_weekday(year, 11, 3, 4))           # Thanksgiving
    xmas = _observed_fixed(date(year, 12, 25), observe_saturday=True)
    if xmas:
        out.add(xmas)
    out |= {d for d in SPECIAL_CLOSURES if d.year == year}
    return frozenset(out)


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    return d not in market_holidays(d.year)


def trading_days(start: date, end: date) -> list[date]:
    """[start, end] 양끝 포함 거래일."""
    if end < start:
        return []
    days: list[date] = []
    d = start
    while d <= end:
        if is_trading_day(d):
            days.append(d)
        d += timedelta(days=1)
    return days


def prev_trading_day(d: date) -> date:
    d = d - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def next_trading_day(d: date) -> date:
    d = d + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def add_trading_days(d: date, n: int) -> date:
    """d 로부터 n 거래일 뒤(음수면 앞). n=0 이면 d(거래일이 아니면 다음 거래일)."""
    if n == 0:
        return d if is_trading_day(d) else next_trading_day(d)
    step = 1 if n > 0 else -1
    remaining = abs(n)
    cur = d
    while remaining:
        cur = cur + timedelta(days=step)
        if is_trading_day(cur):
            remaining -= 1
    return cur


def trading_day_count(start: date, end: date) -> int:
    """start **다음**부터 end 까지 거래일 수(경과 거래일).

    기획서의 “2025-08-19~2026-08-18 = 250거래일”, “2026-08-19~2026-11-18 = 64거래일” 과
    같은 정의다 — 시작일은 세지 않고, T+21·T+63 이 이 값에 맞는다.
    """
    if end <= start:
        return 0
    return len([d for d in trading_days(start, end) if d != start])


# ────────────────────────────── 장 시각 ──────────────────────────────
def market_open_at(d: date) -> datetime:
    """정규장 개장 09:30 ET → UTC (EDT 13:30 / EST 14:30)."""
    return datetime.combine(d, time(9, 30), tzinfo=ET).astimezone(UTC)


def market_close_at(d: date) -> datetime:
    """정규장 마감 16:00 ET → UTC (EDT 20:00 / EST 21:00). 봉의 available_at."""
    return datetime.combine(d, time(16, 0), tzinfo=ET).astimezone(UTC)


def bar_available_at(d: date) -> datetime:
    return market_close_at(d)


def order_cutoff_at(d: date) -> datetime:
    """주문 판단 마감 = 09:25 ET (EST 14:25 UTC / EDT 13:25 UTC)."""
    return datetime.combine(d, time(9, 25), tzinfo=ET).astimezone(UTC)


def et_day_end(d: date) -> datetime:
    """출처 시간대(US/Eastern) 기준 그 날짜의 끝 — 23:59:59 ET."""
    return datetime.combine(d, time(23, 59, 59), tzinfo=ET).astimezone(UTC)


def data_cutoff_at(as_of: date, mode: str = "postclose") -> datetime:
    """데이터 마감 시각.

    postclose: as_of 07:00 KST(= 전일 22:00 UTC) — 전일 장 마감 봉까지 본다.
    premarket: as_of 21:50 KST(= 12:50 UTC) — 당일 개장 전.
    """
    if mode == "postclose":
        return datetime.combine(as_of, time(7, 0), tzinfo=KST).astimezone(UTC)
    if mode == "premarket":
        return datetime.combine(as_of, time(21, 50), tzinfo=KST).astimezone(UTC)
    raise ValueError(f"mode 는 postclose|premarket: {mode!r}")


def signal_published_at(cutoff: datetime, lag_seconds: int = 131) -> datetime:
    """파이프라인이 신호 JSON 을 커밋한 시각(기본 2분 11초 뒤)."""
    return cutoff + timedelta(seconds=lag_seconds)


def decision_cutoff_on(d: date, hour_utc: int) -> datetime:
    if hour_utc not in DECISION_CUTOFF_MINUTES:
        raise ValueError(f"지원하지 않는 결정 시각: {hour_utc} UTC")
    return datetime.combine(d, time(hour_utc, DECISION_CUTOFF_MINUTES[hour_utc]), tzinfo=UTC)


def first_decision_after(instant: datetime) -> datetime:
    """주어진 순간 **이후** 첫 의사결정(12:50 / 22:00 UTC) 시각.

    거시 발표 시각을 모를 때 `D 23:59:59 ET` 뒤 첫 결정을 `available_at` 으로 쓰는 규칙(2.4.3).
    """
    if instant.tzinfo is None:
        raise ValueError("timezone-aware datetime 이 필요하다")
    instant = instant.astimezone(UTC)
    d = instant.date()
    candidates = [decision_cutoff_on(d, 12), decision_cutoff_on(d, 22),
                  decision_cutoff_on(d + timedelta(days=1), 12)]
    for c in candidates:
        if c > instant:
            return c
    raise AssertionError("unreachable")


# ────────────────────────────── 폴드 ──────────────────────────────
def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    # 말일 보정
    for day in range(d.day, 27, -1):
        try:
            return date(y, m, day)
        except ValueError:
            continue
    return date(y, m, d.day)


def _fold_start(year: int, month: int, dom: int = 19) -> date:
    d = date(year, month, dom)
    return d if is_trading_day(d) else next_trading_day(d)


def fold_starts(window_start: date, window_end: date, cadence_months: int = 3, dom: int = 19) -> list[date]:
    """창 안의 분기 폴드 시작일(19일, 휴장이면 다음 거래일)."""
    starts: list[date] = []
    cur = date(window_start.year, window_start.month, min(dom, 28))
    while cur <= window_end:
        cand = _fold_start(cur.year, cur.month, dom)
        if window_start <= cand <= window_end:
            starts.append(cand)
        cur = _add_months(cur, cadence_months)
    return starts


@dataclass(frozen=True)
class Fold:
    fold_id: str
    start: date
    horizon_td: int
    maturity: date
    eligible: bool          # maturity < holdout_start
    observed_days: int      # 자격 없을 때 잘린 거래일 수(참고용)
    truncated: bool


def build_folds(today: date, holdout_months: int = 3, window_months: int = 12,
                horizons: tuple[int, ...] = (21, 63, 126), dom: int = 19,
                holdout_start: date | None = None) -> dict[int, list[Fold]]:
    """오늘 날짜 기준 폴드 계획(기획서 4.2.1).

    - 홀드아웃: `today` 에서 `holdout_months` 개월 거슬러 올라간 날(2026-11-19 → 2026-08-19)
    - 보정 창: 홀드아웃 시작일에서 `window_months` 개월 전 ~ 홀드아웃 전날(250거래일)
    - 폴드 시작: 분기 19일(휴장이면 다음 거래일)
    - 적격 조건: `maturity = add_trading_days(start, h)` 가 `holdout_start` **보다 이전**.
      경계에서 잘린 폴드는 `truncated=True`, `observed_days` 로 참고 기록만 남긴다.
    """
    if holdout_start is None:
        holdout_start = _add_months(today, -holdout_months)
        if not is_trading_day(holdout_start):
            holdout_start = next_trading_day(holdout_start)
    window_start = _add_months(holdout_start, -window_months)
    if not is_trading_day(window_start):
        window_start = next_trading_day(window_start)
    window_end = prev_trading_day(holdout_start)
    starts = fold_starts(window_start, window_end, cadence_months=3, dom=dom)
    out: dict[int, list[Fold]] = {}
    for h in horizons:
        folds: list[Fold] = []
        for i, s in enumerate(starts, start=1):
            maturity = add_trading_days(s, h)
            eligible = maturity < holdout_start
            if eligible:
                observed = h
                truncated = False
            else:
                observed = trading_day_count(s, window_end)
                truncated = True
            folds.append(Fold(f"F{i}", s, h, maturity, eligible, observed, truncated))
        out[h] = folds
    return out


def fold_summary(today: date, holdout_months: int = 3, window_months: int = 12) -> dict:
    """`data/walkforward/folds/summary.json` 형태의 요약(1M/3M/6M 적격 폴드 수)."""
    holdout_start = _add_months(today, -holdout_months)
    if not is_trading_day(holdout_start):
        holdout_start = next_trading_day(holdout_start)
    folds = build_folds(today, holdout_months, window_months)
    summary: dict = {
        "as_of": today.isoformat(),
        "holdout_start": holdout_start.isoformat(),
        "holdout_end": prev_trading_day(today).isoformat(),
        "window_months": window_months,
        "horizons": {},
    }
    short = {21: "1m", 63: "3m", 126: "6m"}
    for h, items in folds.items():
        eligible = [f for f in items if f.eligible]
        summary["horizons"][short[h]] = {
            "horizon_td": h,
            "eligible_folds": [f.fold_id for f in eligible],
            "count": len(eligible),
            "min_folds": 3,
            "sufficient": len(eligible) >= 3,
            "folds": [
                {
                    "fold_id": f.fold_id, "start": f.start.isoformat(),
                    "maturity": f.maturity.isoformat(), "eligible": f.eligible,
                    "truncated": f.truncated, "observed_days": f.observed_days,
                }
                for f in items
            ],
        }
    summary["blocked"] = [k for k, v in summary["horizons"].items() if not v["sufficient"]]
    return summary
