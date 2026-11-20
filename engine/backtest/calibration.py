"""engine.backtest.calibration — 완만한 좌표 하강 보정 (4.2.5~4.2.7).

[고정]
- 파라미터 8개만, 1회 1~2개, 스텝 제한, L2 페널티, 합 1 재정규화.
- 보정 코퍼스는 `maturity < holdout_start` 인 폴드만. **잘린 폴드는 목적함수에서 제외**.
- 적격 폴드 3개 미만인 기간(이번 분기 6개월)은 **보정 보류**(기본값 유지).
- **후보마다 폴드를 다시 실행한다**(R10c). 기본 파라미터의 폴드 결과를 모든 후보에 돌려주면
  Agreement·IC·MDD 항이 상수라 목적함수가 L2 거리만 반영하고, 판정이 파라미터 성과와 무관해진다.
- 수락 규칙은 **4.2.6 문언**을 따른다: Agreement 평균 **+2.0점 이상**, **2개 이상 폴드에서 개선**,
  **스트레스 테스트 통과**. 목적함수 값은 기록·비교 참고용이다(단위가 달라 게이트로 쓰지 않는다).
- 이 모듈은 `data/holdout/` 를 읽지 않는다(import·경로 접근 금지). 테스트가 이를 검사한다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

TUNABLE = {
    "w_valuation": (0.25, 0.15, 0.35, 0.05),
    "w_timing": (0.20, 0.10, 0.30, 0.05),
    "w_macro": (0.15, 0.05, 0.25, 0.05),
    "min_margin": (0.20, 0.10, 0.30, 0.04),
    "rsi_buy": (35.0, 25.0, 40.0, 3.0),
    "dma50_dev": (-0.08, -0.12, -0.05, 0.015),
    "structural_risk_block": (55.0, 45.0, 70.0, 5.0),
    "risk_off_multiplier": (0.4, 0.3, 0.6, 0.05),
}


@dataclass(frozen=True)
class FoldObservation:
    """목적함수 입력. truncated=True 인 폴드는 호출자가 넣지 않아야 한다."""

    fold_id: str
    agreement_mean: float
    ic: float | None
    mdd: float
    eligible: bool = True
    truncated: bool = False


#: 후보 파라미터 → 폴드 관측 목록. 보정은 이 러너를 **후보마다** 부른다.
FoldEvaluator = Callable[[dict], list[FoldObservation]]


def guard_truncated(observations: list[FoldObservation]) -> list[FoldObservation]:
    """잘린 폴드가 섞여 들어오면 즉시 실패 — 조용히 섞이는 것이 가장 위험하다."""
    leaked = [o.fold_id for o in observations if o.truncated or not o.eligible]
    if leaked:
        raise ValueError(f"잘린/부적격 폴드가 목적함수에 들어왔다: {leaked}")
    return observations


def mdd_guard_score(mdd: float, benchmark_mdd: float = -0.14) -> float:
    """전략 MDD 가 벤치마크+5%p 이내면 1.0, 더 나쁘면 감점."""
    allowance = benchmark_mdd - 0.05
    if mdd >= allowance:
        return 1.0
    return float(max(0.0, 1.0 - (allowance - mdd) / 0.10))


def objective(observations: list[FoldObservation], params: dict, base: dict,
              l2_lambda: float = 0.5) -> float:
    """참고용 목적함수(0 이 좋다) — 수락 게이트가 아니라 후보 비교·기록에 쓴다."""
    obs = guard_truncated(observations)
    if not obs:
        return float("inf")
    agreements = [o.agreement_mean / 100.0 for o in obs]
    ics = [max(0.0, min(1.0, ((o.ic if o.ic is not None else 0.0) + 1) / 2)) for o in obs]
    mdds = [mdd_guard_score(o.mdd) for o in obs]
    distance = sum(
        ((params.get(k, v[0]) - v[0]) / (v[2] - v[1] or 1.0)) ** 2 for k, v in TUNABLE.items()
    )
    return float(
        0.45 * (1 - np.mean(agreements))
        + 0.35 * (1 - np.mean(ics))
        + 0.20 * (1 - np.mean(mdds))
        + l2_lambda * distance
    )


def neighbors(params: dict, step: float = 1.0) -> list[dict]:
    """8개 파라미터 × ±1스텝 = 최대 16개 후보(한 번에 1개만 움직인다)."""
    out = []
    for key, (default, lo, hi, step_size) in TUNABLE.items():
        cur = params.get(key, default)
        for sign in (+1 if key not in ("dma50_dev",) else -1, -1 if key not in ("dma50_dev",) else +1):
            new = float(np.clip(cur + sign * step_size * step, lo, hi))
            if abs(new - cur) < 1e-12:
                continue
            cand = dict(params)
            cand[key] = new
            out.append(cand)
    return out


def rebalance_weights(params: dict) -> dict:
    """가중치 합 1 유지 — w_Q:w_G = 3:1 비율로 차감(4.2.5)."""
    out = dict(params)
    total = out.get("w_quality", 0.30) + out.get("w_valuation", 0.25) + out.get("w_timing", 0.20) \
        + out.get("w_macro", 0.15) + out.get("w_growth", 0.10)
    if abs(total - 1.0) < 1e-9:
        return out
    excess = total - 1.0
    out["w_quality"] = out.get("w_quality", 0.30) - excess * 0.75
    out["w_growth"] = out.get("w_growth", 0.10) - excess * 0.25
    return out


def _as_evaluator(evaluate) -> FoldEvaluator:
    """구 호출(`calibrate(observations, …)`)도 받아 준다 — 고정 관측은 '모든 후보가 같다'는 뜻."""
    if callable(evaluate):
        return evaluate
    fixed = list(evaluate or [])

    def _fixed(_params: dict) -> list[FoldObservation]:
        return fixed

    return _fixed


def _mean_agreement(obs: list[FoldObservation]) -> float:
    return float(np.mean([o.agreement_mean for o in obs])) if obs else float("nan")


def calibrate(evaluate, base_params: dict, min_folds: int = 3, accept_gain: float = 2.0,
              accept_folds: int = 2, l2_lambda: float = 0.5,
              stress_pass: Callable[[dict], bool] | None = None) -> dict:
    """후보별 폴드 재실행 → 수락 규칙. 수락하지 않아도 **이유를 남긴다**.

    `evaluate(params) -> list[FoldObservation]` 가 후보마다 폴드를 다시 돌린다.
    """
    runner = _as_evaluator(evaluate)
    base_obs = guard_truncated(runner(dict(base_params)))
    record: dict = {"n_folds": len(base_obs), "accepted": False, "reason": None,
                    "candidates": [], "params": base_params}
    if len(base_obs) < min_folds:
        record["reason"] = "INSUFFICIENT_FOLDS"
        return record

    base_agreement = _mean_agreement(base_obs)
    best: dict | None = None
    for cand in neighbors(base_params):
        cand = rebalance_weights(cand)
        try:
            obs = guard_truncated(runner(dict(cand)))
        except ValueError as exc:            # 후보 실행이 부적격 폴드를 낳으면 그 후보만 버린다
            record["candidates"].append({"params": cand, "error": str(exc)[:120]})
            continue
        if not obs:
            continue
        gain = _mean_agreement(obs) - base_agreement
        improved = sum(1 for b, c in zip(base_obs, obs, strict=False) if c.agreement_mean > b.agreement_mean)
        obj = objective(obs, cand, base_params, l2_lambda)
        row = {
            "params": cand,
            "agreement_gain": round(gain, 2),
            "improved_folds": int(improved),
            "objective": round(obj, 5),
            "base_agreement": round(base_agreement, 2),
            "candidate_agreement": round(_mean_agreement(obs), 2),
        }
        record["candidates"].append(row)
        if best is None or row["agreement_gain"] > best["agreement_gain"]:
            best = row

    if best is None:
        record["reason"] = "NO_CANDIDATE"
        return record

    stress_ok = True if stress_pass is None else bool(stress_pass(best["params"]))
    if best["agreement_gain"] < accept_gain:
        record["reason"] = f"GAIN_TOO_SMALL({best['agreement_gain']:.2f} < {accept_gain})"
        return record
    if best["improved_folds"] < accept_folds:
        record["reason"] = f"TOO_FEW_IMPROVED_FOLDS({best['improved_folds']} < {accept_folds})"
        return record
    if not stress_ok:
        record["reason"] = "STRESS_FAILED"
        return record
    record.update({
        "accepted": True,
        "reason": "ACCEPTED",
        "params": {**base_params, **best["params"]},
        "candidate": best,
        "objective": best["objective"],
        "gain": best["agreement_gain"],
        "improved_folds": best["improved_folds"],
        "base_agreement": round(base_agreement, 2),
        "stress_pass": stress_ok,
    })
    return record


def write_attempts(root: str | Path, record: dict) -> str:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "attempts.json"
    history = []
    if path.exists():
        history = json.loads(path.read_text(encoding="utf-8")).get("attempts", [])
    history.append(record)
    path.write_text(json.dumps({"attempts": history}, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)
