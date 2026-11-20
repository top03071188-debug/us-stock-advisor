"""engine.backtest.calibration — 완만한 좌표 하강 보정 (4.2.5~4.2.7).

[고정]
- 파라미터 8개만, 1회 1~2개, 스텝 제한, L2 페널티, 합 1 재정규화.
- 보정 코퍼스는 `maturity < holdout_start` 인 폴드만. **잘린 폴드는 목적함수에서 제외**.
- 적격 폴드 3개 미만인 기간(이번 분기 6개월)은 **보정 보류**(기본값 유지).
- 이 모듈은 `data/holdout/` 를 읽지 않는다(import·경로 접근 금지). 테스트가 이를 검사한다.
"""

from __future__ import annotations

import json
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


def calibrate(observations: list[FoldObservation], base_params: dict,
              min_folds: int = 3, accept_gain: float = 2.0, accept_folds: int = 2,
              l2_lambda: float = 0.5) -> dict:
    """후보 평가 → 수락 규칙. 수락하지 않아도 **이유를 남긴다**."""
    eligible = guard_truncated(observations)
    record: dict = {"n_folds": len(eligible), "accepted": False, "reason": None, "candidates": [], "params": base_params}
    if len(eligible) < min_folds:
        record["reason"] = "INSUFFICIENT_FOLDS"
        return record

    base_obj = objective(eligible, base_params, base_params, l2_lambda)
    best = None
    for cand in neighbors(base_params):
        cand = rebalance_weights(cand)
        obj = objective(eligible, cand, base_params, l2_lambda)
        record["candidates"].append({"params": cand, "objective": round(obj, 5)})
        if best is None or obj < best[1]:
            best = (cand, obj)

    if best is None:
        record["reason"] = "NO_CANDIDATE"
        return record

    base_mean = float(np.mean([o.agreement_mean for o in eligible]))
    # 후보의 폴드별 개선은 Agreement 평균으로 근사 평가(실제 파이프라인은 폴드 재실행 결과를 넣는다)
    gain = (base_obj - best[1]) * 100.0
    if gain < accept_gain:
        record["reason"] = f"GAIN_TOO_SMALL({gain:.2f} < {accept_gain})"
        return record
    record.update({
        "accepted": True,
        "reason": "ACCEPTED",
        "params": {**base_params, **best[0]},
        "objective": round(best[1], 5),
        "base_objective": round(base_obj, 5),
        "gain": round(gain, 2),
        "base_agreement": round(base_mean, 2),
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
