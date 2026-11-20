"""engine.backtest.holdout — 홀드아웃 1회 개봉·잠금 게이트 (기획서 4.3 가드1).

[고정]
- 홀드아웃은 **분기 1회만** 평가한다. 여러 번 보면서 파라미터를 고치면 그 순간 홀드아웃은
  훈련 데이터가 된다.
- 평가 결과는 `data/walkforward/holdout/{YYYY-MM-DD}.json` 에 **append-only** 로 남긴다.
  같은 날짜의 기록을 다른 내용으로 덮어쓰려 하면 실패한다(`HoldoutLocked`).
- 보정 코퍼스 적격 조건은 `maturity_date < holdout_start` 다. 경계에서 잘린 폴드는 참고용이며
  목적함수에서 빠진다(4.2.1). `validate_boundary` 가 이 조건을 검사한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from engine.utils.dates import is_trading_day, next_trading_day

DEFAULT_HOLDOUT_MONTHS = 3
OPEN_MONTHS = (1, 4, 7, 10)
DEFAULT_THRESHOLD = 55.0
ROLLBACK_DROP = 5.0


class HoldoutLocked(RuntimeError):
    """이미 평가된 홀드아웃(또는 같은 날짜의 다른 내용)을 다시 열려 했다."""


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    for day in range(d.day, 27, -1):
        try:
            return date(y, m, day)
        except ValueError:
            continue
    return date(y, m, d.day)


@dataclass(frozen=True)
class HoldoutWindow:
    holdout_start: date
    holdout_end: date
    months: int = DEFAULT_HOLDOUT_MONTHS

    def as_dict(self) -> dict:
        return {"holdout_start": self.holdout_start.isoformat(),
                "holdout_end": self.holdout_end.isoformat(),
                "months": self.months}


def window_for(today: date, months: int = DEFAULT_HOLDOUT_MONTHS) -> HoldoutWindow:
    """최근 N개월 = 홀드아웃. 시작일이 휴장이면 다음 거래일로 민다(기획서 4.2.1 예: 08-19)."""
    start = _add_months(today, -months)
    if not is_trading_day(start):
        start = next_trading_day(start)
    end = today - timedelta(days=1)
    while not is_trading_day(end):
        end -= timedelta(days=1)
    return HoldoutWindow(start, end, months)


def next_open_date(today: date) -> date:
    """다음 개봉일 = 다음 분기 첫날(1·4·7·10월 1일). 오늘이 개봉일이면 다음 분기."""
    year, month = today.year, today.month
    for m in OPEN_MONTHS:
        if month < m:
            return date(year, m, 1)
    return date(year + 1, OPEN_MONTHS[0], 1)


def is_open_day(today: date) -> bool:
    """오늘이 분기 개봉일인가(1·4·7·10월 1일)."""
    return today.month in OPEN_MONTHS and today.day == 1


def is_eligible_fold(maturity: date | str, holdout_start: date | str) -> bool:
    """보정 코퍼스 적격: `maturity < holdout_start` (LA-13)."""
    mat = maturity if isinstance(maturity, date) else date.fromisoformat(str(maturity)[:10])
    start = holdout_start if isinstance(holdout_start, date) else date.fromisoformat(str(holdout_start)[:10])
    return mat < start


def validate_boundary(folds: list[dict], holdout_start: date | str) -> list[str]:
    """폴드 목록의 경계 위반을 찾는다 — 적격인데 만기가 홀드아웃에 걸친 폴드는 오류다."""
    problems: list[str] = []
    for fold in folds:
        maturity = fold.get("maturity")
        if maturity is None:
            continue
        eligible = bool(fold.get("eligible", True))
        ok = is_eligible_fold(maturity, holdout_start)
        if eligible and not ok:
            problems.append(f"{fold.get('fold_id')}: eligible=True 인데 maturity({maturity}) ≥ holdout_start")
        if not eligible and ok:
            problems.append(f"{fold.get('fold_id')}: eligible=False 인데 maturity({maturity}) < holdout_start")
        if not ok and not fold.get("truncated", False):
            problems.append(f"{fold.get('fold_id')}: 잘린 폴드인데 truncated 표시가 없다")
    return problems


def evaluate(agreement_mean: float | None, threshold: float = DEFAULT_THRESHOLD,
             previous_agreement: float | None = None) -> dict:
    """홀드아웃 1회 평가 결과. 하락 폭이 크면 보수 모드 플래그를 남긴다(4.3 가드1)."""
    passed = None if agreement_mean is None else bool(agreement_mean >= threshold)
    delta = None if (agreement_mean is None or previous_agreement is None) else agreement_mean - previous_agreement
    return {
        "agreement": agreement_mean,
        "threshold": threshold,
        "passed": passed,
        "delta_vs_previous": None if delta is None else round(delta, 2),
        "conservative_mode": bool(delta is not None and delta <= -ROLLBACK_DROP),
        "rollback_candidate": bool(delta is not None and delta <= -2 * ROLLBACK_DROP),
        "note_ko": ("홀드아웃 성과가 직전 분기보다 크게 하락 — 보수 모드 전환 조건(4.3)"
                    if delta is not None and delta <= -ROLLBACK_DROP else
                    ("표본 없음 — 판정하지 않는다" if agreement_mean is None else None)),
    }


class HoldoutLedger:
    """`data/walkforward/holdout/` 의 append-only 잠금 기록."""

    def __init__(self, root: str | Path = "data/walkforward/holdout", threshold: float = DEFAULT_THRESHOLD):
        self.root = Path(root)
        self.threshold = threshold

    def paths(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(p for p in self.root.glob("*.json") if p.name != "index.json")

    def load(self) -> list[dict]:
        out: list[dict] = []
        for p in self.paths():
            try:
                row = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            row.setdefault("date", p.stem)
            out.append(row)
        return sorted(out, key=lambda r: str(r.get("date")))

    def last(self) -> dict | None:
        rows = self.load()
        return rows[-1] if rows else None

    def evaluated_quarters(self) -> set[str]:
        return {str(r.get("date", ""))[:7] for r in self.load() if r.get("date")}

    def can_open(self, today: date) -> dict:
        """오늘 홀드아웃을 열 수 있는가 — 분기 1회, 이미 이 분기에 열었으면 불가."""
        quarter = today.isoformat()[:7]
        already = sorted(q for q in self.evaluated_quarters() if q == quarter)
        if already:
            return {"allowed": False, "reason": "ALREADY_EVALUATED_THIS_QUARTER",
                    "note_ko": f"{quarter} 홀드아웃은 이미 평가됐다 — 다음 개봉 {next_open_date(today).isoformat()}",
                    "next_open": next_open_date(today).isoformat()}
        if not is_open_day(today):
            return {"allowed": False, "reason": "NOT_OPEN_DAY",
                    "note_ko": f"홀드아웃은 분기 1회만 열린다 — 다음 개봉 {next_open_date(today).isoformat()}",
                    "next_open": next_open_date(today).isoformat()}
        return {"allowed": True, "reason": "OPEN_DAY",
                "note_ko": "분기 개봉일 — 1회 평가를 기록한다.",
                "next_open": next_open_date(today).isoformat()}

    def record(self, today: date, payload: dict) -> Path:
        """평가 결과를 append-only 로 남긴다. 같은 날짜의 다른 내용은 거부한다."""
        if not self.can_open(today)["allowed"]:
            raise HoldoutLocked(self.can_open(today)["note_ko"])
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{today.isoformat()}.json"
        body = {
            "date": today.isoformat(),
            "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "locked": True,
            **payload,
        }
        if path.exists():
            old = json.loads(path.read_text(encoding="utf-8"))
            comparable = {k: v for k, v in body.items() if k != "recorded_at"}
            old_comparable = {k: v for k, v in old.items() if k != "recorded_at"}
            if comparable == old_comparable:
                return path
            raise HoldoutLocked(f"{today} 홀드아웃 기록이 다른 내용으로 이미 잠겨 있다 — append-only")
        path.write_text(json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def status(self, today: date) -> dict:
        """대시보드 배너용 잠금 상태 — 다음 개봉일을 항상 함께 보여 준다(5.4)."""
        window = window_for(today)
        last = self.last()
        return {
            **window.as_dict(),
            "locked": True,
            "evaluations": len(self.load()),
            "last_evaluation": None if last is None else {
                "date": last.get("date"),
                "agreement": (last.get("result") or {}).get("agreement"),
                "passed": (last.get("result") or {}).get("passed"),
            },
            "next_open": next_open_date(today).isoformat(),
            "note_ko": f"홀드아웃은 분기 1회만 열립니다 — 다음 개봉 {next_open_date(today).isoformat()}",
        }
