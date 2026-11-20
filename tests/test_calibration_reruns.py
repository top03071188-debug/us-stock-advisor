"""test_calibration_reruns — 후보별 폴드 재실행과 4.2.6 수락 규칙 (R10c).

감사 재현: 후보 16개가 **모두 같은 폴드 결과**를 받아 Agreement·IC·MDD 항이 상수가 되고,
목적함수는 L2 거리만 반영했다(판정 `GAIN_TOO_SMALL(-1.39 < 2.0)`). 수락 규칙도 목적함수 단위를
"Agreement +2.0점" 과 비교하고, 개선 폴드 수·스트레스 테스트를 보지 않았다.
"""

from __future__ import annotations

from engine.backtest.calibration import FoldObservation, calibrate, neighbors

BASE = {"w_quality": 0.30, "w_valuation": 0.25, "w_timing": 0.20, "w_macro": 0.15, "w_growth": 0.10}
BOOST_PARAM = "w_valuation"


#: [v1.3.4 · P0-4] 수락 계약이 **필수 메타**를 요구한다 — 테스트 관측도 실제와 같은 모양으로 만든다.
OBS_META = dict(maturity_date="2026-02-15", holdout_start="2026-08-20", signal_count=30,
                code_version="abc123", snapshot_id="sha256:s1", horizon_td=63)


def _obs(fold_id: str, score: float, ic: float = 0.05, mdd: float = -0.10,
         **kw) -> FoldObservation:
    meta = dict(OBS_META)
    meta.update({k: v for k, v in kw.items() if k in meta})
    extra = {k: v for k, v in kw.items() if k not in meta}
    return FoldObservation(fold_id, score, ic, mdd, **meta, **extra)


def evaluator(*, fold_scores, calls: list | None = None):
    """파라미터가 바뀌면 폴드 점수가 달라지는 가짜 러너.

    `fold_scores(lifted: bool) -> list[float]` 가 폴드별 Agreement 를 만든다.
    """

    def _run(params: dict) -> list[FoldObservation]:
        if calls is not None:
            calls.append(dict(params))
        lifted = abs(float(params.get(BOOST_PARAM, BASE[BOOST_PARAM])) - BASE[BOOST_PARAM]) > 1e-12
        return [_obs(f"F{i}", score) for i, score in enumerate(fold_scores(lifted), start=1)]

    return _run


# ── 후보마다 폴드를 다시 돌린다 ────────────────────────────────────────────────
def test_calibration_reruns_folds():
    calls: list[dict] = []
    runner = evaluator(fold_scores=lambda lifted: [63.0, 63.0, 63.0] if lifted else [60.0, 60.0, 60.0],
                       calls=calls)
    record = calibrate(runner, BASE, min_folds=3, stress_pass=lambda _params: True)
    assert len(calls) == 1 + len(neighbors(BASE)), "후보마다 폴드를 재실행하지 않았다"
    assert record["n_folds"] == 3
    assert record["accepted"] is True
    assert record["reason"] == "ACCEPTED"
    assert record["gain"] == 3.0 and record["improved_folds"] == 3
    assert record["params"][BOOST_PARAM] != BASE[BOOST_PARAM]
    # 후보별 Agreement 가 실제로 달라진다(예전에는 모두 같은 값이었다)
    gains = {round(c["agreement_gain"], 2) for c in record["candidates"] if "agreement_gain" in c}
    assert gains == {0.0, 3.0}
    assert all("objective" in c for c in record["candidates"] if "objective" in c)


def test_calibration_rejects_when_no_candidate_improves():
    runner = evaluator(fold_scores=lambda _lifted: [60.0, 60.0, 60.0])
    record = calibrate(runner, BASE, min_folds=3, stress_pass=lambda _params: True)
    assert record["accepted"] is False
    assert record["reason"].startswith("GAIN_TOO_SMALL")


def test_calibration_requires_stress_pass():
    """`stress_pass` 미제공은 **통과가 아니다** — None 을 True 로 바꾸지 않는다(감사 10)."""
    runner = evaluator(fold_scores=lambda lifted: [63.0, 63.0, 63.0] if lifted else [60.0, 60.0, 60.0])
    record = calibrate(runner, BASE, min_folds=3)
    assert record["accepted"] is False
    assert record["reason"] == "STRESS_NOT_PROVIDED"
    assert record["stress_pass"] is False


def test_calibration_defers_with_insufficient_folds():
    runner = evaluator(fold_scores=lambda _lifted: [60.0])
    record = calibrate(runner, BASE, min_folds=3, stress_pass=lambda _params: True)
    assert record["accepted"] is False
    assert record["reason"] == "INSUFFICIENT_FOLDS"


# ── 4.2.6 수락 규칙 세 가지 ────────────────────────────────────────────────────
def test_calibration_accept_rules():
    # (i) Agreement +2.0점 미만 → 기각
    small = evaluator(fold_scores=lambda lifted: [61.0, 61.0, 61.0] if lifted else [60.0, 60.0, 60.0])
    rec = calibrate(small, BASE, min_folds=3)
    assert rec["accepted"] is False and rec["reason"].startswith("GAIN_TOO_SMALL")

    # (ii) 평균 +2.0 은 넘지만 개선 폴드가 1개 → 기각
    one_fold = evaluator(fold_scores=lambda lifted: [66.0, 60.0, 60.0] if lifted else [60.0, 60.0, 60.0])
    rec = calibrate(one_fold, BASE, min_folds=3)
    assert rec["accepted"] is False
    assert rec["reason"].startswith("TOO_FEW_IMPROVED_FOLDS")

    # (iii) 조건을 만족해도 스트레스 테스트가 실패하면 기각
    good = evaluator(fold_scores=lambda lifted: [63.0, 63.0, 63.0] if lifted else [60.0, 60.0, 60.0])
    rec = calibrate(good, BASE, min_folds=3, stress_pass=lambda _params: False)
    assert rec["accepted"] is False and rec["reason"] == "STRESS_FAILED"

    # 스트레스 통과 시에만 수락
    rec = calibrate(good, BASE, min_folds=3, stress_pass=lambda _params: True)
    assert rec["accepted"] is True and rec["stress_pass"] is True


def test_calibration_rejects_candidate_with_truncated_fold():
    def runner(params: dict) -> list[FoldObservation]:
        lifted = abs(float(params.get(BOOST_PARAM, BASE[BOOST_PARAM])) - BASE[BOOST_PARAM]) > 1e-12
        if lifted:
            return [_obs("F1", 63.0),
                    _obs("F4", 63.0, eligible=False, truncated=True)]
        return [_obs(f"F{i}", 60.0) for i in range(1, 4)]

    record = calibrate(runner, BASE, min_folds=3, stress_pass=lambda _params: True)
    assert record["accepted"] is False
    assert any("error" in c for c in record["candidates"])
