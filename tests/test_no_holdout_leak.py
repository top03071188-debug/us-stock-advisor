"""test_no_holdout_leak — `maturity_date < holdout_start` 적격 조건과 6M 보류 (4.2.1, LA-13)."""

from __future__ import annotations

import inspect
import json
import re
from datetime import date
from pathlib import Path

import pytest

from engine.backtest import calibration
from engine.backtest.calibration import FoldObservation, calibrate, guard_truncated, objective
from engine.backtest.walk_forward import FoldResult, plan_and_attempts, truncated_excluded_from_objective
from engine.utils.dates import fold_summary


ROOT = Path(__file__).resolve().parents[1]


def test_fold_dates_match_document():
    s = fold_summary(date(2026, 11, 19))
    h1 = s["horizons"]["1m"]
    assert h1["count"] == 4 and h1["eligible_folds"] == ["F1", "F2", "F3", "F4"]
    mats = {f["fold_id"]: f["maturity"] for f in h1["folds"]}
    assert mats["F1"] == "2025-09-18" and mats["F4"] == "2026-06-18"
    h3 = s["horizons"]["3m"]
    assert h3["count"] == 3
    assert {f["fold_id"]: f["maturity"] for f in h3["folds"]}["F4"] == "2026-08-19"  # 홀드아웃 첫 봉
    assert h3["folds"][3]["eligible"] is False and h3["folds"][3]["truncated"] is True
    h6 = s["horizons"]["6m"]
    assert h6["count"] == 2 and h6["sufficient"] is False
    assert {f["fold_id"]: f["maturity"] for f in h6["folds"]}["F3"] == "2026-08-20"  # 하루 초과


def test_every_eligible_fold_matures_before_holdout():
    s = fold_summary(date(2026, 11, 19))
    holdout = s["holdout_start"]
    for h in s["horizons"].values():
        for f in h["folds"]:
            if f["eligible"]:
                assert f["maturity"] < holdout, f
            else:
                assert f["maturity"] >= holdout


def test_6m_calibration_deferred_with_reason():
    plan = plan_and_attempts(date(2026, 11, 19))
    acts = {a["horizon"]: a for a in plan["attempts"]["attempts"]}
    assert acts["6m"]["action"] == "deferred"
    assert acts["6m"]["reason"] == "INSUFFICIENT_ELIGIBLE_FOLDS"
    assert "15개월" in acts["6m"]["note"]
    # `--plan-only` 는 **날짜 계획**이다 — "calibrate"(보정을 돌렸다)가 아니라 "planned"(감사 10)
    assert acts["1m"]["action"] == "planned" and acts["3m"]["action"] == "planned"
    assert acts["1m"]["sufficient"] is True and plan["attempts"]["kind"] == "plan"


def test_min_folds_argument_is_respected():
    """`--min-folds 5` 를 주면 4·3폴드 구간이 계획에서 빠진다 — `>= 3` 하드코딩 금지(감사 10)."""
    plan = plan_and_attempts(date(2026, 11, 19), min_folds=5)
    acts = {a["horizon"]: a for a in plan["attempts"]["attempts"]}
    assert acts["1m"]["count"] == 4 and acts["1m"]["sufficient"] is False
    assert acts["1m"]["action"] == "deferred"
    assert acts["3m"]["action"] == "deferred"
    assert plan["summary"]["horizons"]["1m"]["min_folds"] == 5
    assert set(plan["summary"]["blocked"]) == {"1m", "3m", "6m"}


#: [v1.3.4 · P0-4] 관측은 만기·홀드아웃 경계·신호 수·코드·스냅샷 없이는 수락되지 않는다.
OBS_META = dict(maturity_date="2026-02-15", holdout_start="2026-08-20", signal_count=30,
                code_version="abc123", snapshot_id="sha256:s1", horizon_td=63)


def _obs(fold_id: str, score: float, ic: float = 0.05, mdd: float = -0.10,
         **kw) -> FoldObservation:
    meta = dict(OBS_META)
    meta.update({k: v for k, v in kw.items() if k in meta})
    extra = {k: v for k, v in kw.items() if k not in meta}
    return FoldObservation(fold_id, score, ic, mdd, **meta, **extra)


def test_truncated_fold_cannot_enter_objective():
    obs = [_obs("F1", 62.0, ic=0.08, mdd=-0.09),
           _obs("F4", 58.0, ic=0.05, mdd=-0.12, eligible=False, truncated=True)]
    with pytest.raises(ValueError):
        objective(obs, {}, {})
    with pytest.raises(ValueError):
        guard_truncated(obs)


def test_truncated_results_are_reference_only():
    good = FoldResult("F1", 63, date(2025, 8, 19), date(2025, 11, 17), True, False, 63)
    cut = FoldResult("F4", 63, date(2026, 5, 19), date(2026, 8, 19), False, True, 62)
    assert truncated_excluded_from_objective([good, cut]) == [good]


def _constant_evaluator(bonus: float = 0.0):
    """후보와 무관하게 같은 폴드를 돌려주는 러너(개선이 없다 → 수락되면 안 된다)."""
    obs = [_obs(f"F{i}", 60.0 + i + bonus, ic=0.04) for i in range(1, 4)]

    def _run(_params: dict) -> list[FoldObservation]:
        return obs

    return _run


def test_calibration_skips_when_folds_insufficient():
    # 폴드가 3개 미만이면 보류 — 고정 관측을 직접 넘기는 구 호출도 같은 규칙이다
    rec = calibrate([_obs("F1", 60.0)], {}, min_folds=3)
    assert rec["accepted"] is False
    assert rec["reason"] == "INSUFFICIENT_FOLDS"


def test_calibration_returns_rejection_reason():
    rec = calibrate(_constant_evaluator(), {}, min_folds=3, accept_gain=99.0)
    assert rec["accepted"] is False
    assert rec["reason"].startswith("GAIN_TOO_SMALL")
    rec2 = calibrate(_constant_evaluator(), {}, min_folds=3, accept_gain=-99.0)
    assert rec2["accepted"] is False
    assert rec2["reason"].startswith("TOO_FEW_IMPROVED_FOLDS")   # 개선 폴드 0 < 2 (4.2.6)


def test_calibration_module_does_not_touch_holdout():
    """보정 모듈은 홀드아웃 폴더를 import·참조하지 않는다(구조적 분리)."""
    src = inspect.getsource(calibration)
    body = src.split('"""')[2] if src.count('"""') >= 2 else src   # 모듈 docstring 제외
    assert not re.search(r"(^|\n)\s*(import|from)\s+.*holdout", body)
    assert "holdout_" not in body
    assert "data/holdout" not in body


def test_attempts_json_is_written(tmp_path):
    plan = plan_and_attempts(date(2026, 11, 19))
    from engine.backtest.walk_forward import write_fold_output

    paths = write_fold_output(tmp_path, plan["summary"], plan["attempts"])
    data = json.loads(Path(paths["summary_path"]).read_text(encoding="utf-8"))
    assert data["horizons"]["6m"]["sufficient"] is False
    att = json.loads(Path(paths["attempts_path"]).read_text(encoding="utf-8"))
    assert any(a["action"] == "deferred" for a in att["attempts"])
