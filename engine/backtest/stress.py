"""engine.backtest.stress — 하락장 스트레스 3구간 + 5년 확장 검증 틀 (기획서 4.3 가드3·4.4).

[고정 — "데이터가 없으면 통과가 아니다"]
- 2022/2020/2018Q4 구간은 수집 시작 전 과거다. `universe_history`·`delisted_securities`(2.4.5)가
  없으면 **당시 구성종목**을 알 수 없으므로 결과는 `verified: false` 이고 **게이트 통과로 계산하지 않는다**.
  (`pass: None`, `stress_pass: None`, `gate_passed: False`)
- 하이일드 스프레드(`BAMLH0A0HYM2`)는 2023-11-20 이전 구간이 API 에서 오지 않는다(2.4.3).
  그 구간은 4개 신호로만 레짐을 판정하며, 그 사실을 `macro_signals_available` 에 남긴다.
- 5년 확장은 20폴드를 채우지 못하면 `"장기 확장 미완료 — n/20폴드"` 로 표기한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
import json

#: 스트레스 구간(기획서 4.3 가드3 표)
STRESS_WINDOWS = (
    {"key": "2022_rate_hike", "label": "2022 금리인상 하락장",
     "start": date(2022, 1, 3), "end": date(2022, 10, 14),
     "checks": "Risk-Off 전환 속도, 신규 매수 중단, 손절 작동",
     "criteria_ko": "전략 MDD ≤ SPY MDD + 5%p, V06 발동 확인"},
    {"key": "2020_covid", "label": "2020 COVID 급락",
     "start": date(2020, 2, 19), "end": date(2020, 4, 30),
     "checks": "급락 후 반등 국면에서 매수 재개 타이밍",
     "criteria_ko": "급락 중 신규 매수 0건, 반등 후 신호 재개"},
    {"key": "2018q4", "label": "2018 4분기",
     "start": date(2018, 10, 1), "end": date(2018, 12, 24),
     "checks": "금리·변동성 상승 초기 대응",
     "criteria_ko": "손실 신호 비율 < 60%"},
)
EXTENDED_YEARS = 5
EXTENDED_FOLDS = 20
HY_OAS_START = date(2023, 11, 20)


@dataclass
class StressWindowResult:
    key: str
    label: str
    start: date
    end: date
    verified: bool
    universe_verified: bool
    passed: bool | None = None
    metrics: dict = field(default_factory=dict)
    reason_ko: str | None = None

    def as_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label,
            "start": self.start.isoformat(), "end": self.end.isoformat(),
            "verified": self.verified, "universe_verified": self.universe_verified,
            "pass": self.passed, "metrics": self.metrics,
            "reason_ko": self.reason_ko,
        }


def stress_plan(universe_history: object | None = None, delisted: object | None = None) -> dict:
    """3구간 계획 + 데이터 확보 여부. 확보하지 못한 구간은 "미검증"으로 시작한다."""
    has_universe = universe_history is not None
    has_delisted = delisted is not None
    missing: list[str] = []
    if not has_universe:
        missing.append("universe_history")
    if not has_delisted:
        missing.append("delisted_securities")
    windows = []
    for w in STRESS_WINDOWS:
        windows.append({
            "key": w["key"], "label": w["label"],
            "start": w["start"].isoformat(), "end": w["end"].isoformat(),
            "checks": w["checks"], "criteria_ko": w["criteria_ko"],
            "universe_verified": bool(has_universe and has_delisted),
            "macro_signals_available": 5 if w["start"] >= HY_OAS_START else 4,
        })
    return {
        "windows": windows,
        "universe_verified": bool(has_universe and has_delisted),
        "missing_inputs": missing,
        "note_ko": ("미확보: " + ", ".join(missing) + " — 5년·스트레스 결과는 게이트 통과로 쓰지 않는다.")
        if missing else "입력 확보 — 스트레스를 실행할 수 있다.",
    }


def evaluate_window(window: dict, *, universe_verified: bool,
                    strategy_mdd: float | None = None, benchmark_mdd: float | None = None,
                    new_buys_during_crash: int | None = None,
                    v06_fired: bool | None = None,
                    loss_ratio: float | None = None) -> StressWindowResult:
    """한 구간 판정. `universe_verified=False` 면 다른 인자와 무관하게 `verified=False`, `pass=None`."""
    start = date.fromisoformat(window["start"])
    end = date.fromisoformat(window["end"])
    if not universe_verified:
        return StressWindowResult(
            key=window["key"], label=window["label"], start=start, end=end,
            verified=False, universe_verified=False, passed=None,
            reason_ko="미검증 — 과거 구성종목·상장폐지 데이터(universe_history·delisted_securities) 미확보",
        )
    checks: list[bool] = []
    metrics: dict = {}
    if strategy_mdd is not None and benchmark_mdd is not None:
        metrics["strategy_mdd"] = round(float(strategy_mdd), 6)
        metrics["benchmark_mdd"] = round(float(benchmark_mdd), 6)
        checks.append(float(strategy_mdd) >= float(benchmark_mdd) - 0.05)
    if window["key"] == "2022_rate_hike" and v06_fired is not None:
        metrics["v06_fired"] = bool(v06_fired)
        checks.append(bool(v06_fired))
    if window["key"] == "2020_covid" and new_buys_during_crash is not None:
        metrics["new_buys_during_crash"] = int(new_buys_during_crash)
        checks.append(int(new_buys_during_crash) == 0)
    if window["key"] == "2018q4" and loss_ratio is not None:
        metrics["loss_ratio"] = round(float(loss_ratio), 4)
        checks.append(float(loss_ratio) < 0.60)
    if not checks:
        return StressWindowResult(
            key=window["key"], label=window["label"], start=start, end=end,
            verified=False, universe_verified=True, passed=None,
            reason_ko="필요한 결과값이 없어 판정하지 않는다(빈 입력을 통과로 처리하지 않는다)",
        )
    return StressWindowResult(
        key=window["key"], label=window["label"], start=start, end=end,
        verified=True, universe_verified=True, passed=all(checks), metrics=metrics,
    )


def stress_report(universe_history: object | None = None, delisted: object | None = None,
                  results: dict | None = None) -> dict:
    """3구간 리포트. 하나라도 미검증이면 `stress_pass` 는 **None**(게이트 통과 아님)."""
    plan = stress_plan(universe_history, delisted)
    rows: list[dict] = []
    for window in plan["windows"]:
        override = (results or {}).get(window["key"])
        result = evaluate_window(window, universe_verified=window["universe_verified"], **(override or {}))
        rows.append(result.as_dict())
    verified_all = all(r["verified"] for r in rows)
    stress_pass = None
    if verified_all:
        stress_pass = all(bool(r["pass"]) for r in rows)
    return {
        **plan,
        "results": rows,
        "verified_all": verified_all,
        "stress_pass": stress_pass,
        "gate_passed": bool(stress_pass is True),
        "n_verified": sum(1 for r in rows if r["verified"]),
        "n_windows": len(rows),
        "note_ko": plan["note_ko"] if not verified_all else (
            "스트레스 통과 — 보정 채택 조건 충족" if stress_pass else "스트레스 미통과 — 보정 결과를 채택하지 않는다(4.3)"),
    }


def extended_plan(today: date, folds_available: int = 0,
                  universe_verified: bool = False) -> dict:
    """5년 확장(20폴드) 계획 — 폴드 수가 모자라면 "장기 확장 미완료 — n/20폴드"."""
    per = f"{EXTENDED_FOLDS}폴드"
    complete = folds_available >= EXTENDED_FOLDS and universe_verified
    return {
        "years": EXTENDED_YEARS,
        "folds_target": EXTENDED_FOLDS,
        "folds_available": int(folds_available),
        "universe_verified": bool(universe_verified),
        "status": "complete" if complete else "incomplete",
        "label_ko": "장기 확장 완료" if complete else f"장기 확장 미완료 — {folds_available}/{per}",
        "note_ko": (None if complete else
                    "5년 폴드는 universe_history·delisted_securities 없이는 만들 수 없다(4.4). "
                    "오늘의 유니버스로 돌린 5년 결과는 상한 추정이며 성적표·게이트에 쓰지 않는다."),
    }


def write_stress_report(payload: dict, root: str | Path = "data/walkforward") -> Path:
    p = Path(root) / "stress.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p
