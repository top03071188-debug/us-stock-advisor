"""engine.backtest.calibration — 완만한 좌표 하강 보정 (4.2.5~4.2.7).

[고정]
- 파라미터 8개만, 1회 1~2개, 스텝 제한, L2 페널티, 합 1 재정규화.
- 보정 코퍼스는 `maturity < holdout_start` 인 폴드만. **잘린 폴드는 목적함수에서 제외**.
- 적격 폴드 3개 미만인 기간(이번 분기 6개월)은 **보정 보류**(기본값 유지).
- **후보마다 폴드를 다시 실행한다**(R10c). 기본 파라미터의 폴드 결과를 모든 후보에 돌려주면
  Agreement·IC·MDD 항이 상수라 목적함수가 L2 거리만 반영하고, 판정이 파라미터 성과와 무관해진다.
- 수락 계약(감사 10 — v1.3.3 강화):
  1. 폴드 관측은 **`(horizon_td, fold_id, maturity_date)` 키로 짝을 맞춘다** — `zip()` 위치 비교 금지.
     중복·누락·ID/만기 불일치·홀드아웃 경계 침범(`maturity ≥ holdout_start`)·폴드당 신호
     `< min_signals`(25)·코드/스냅샷 버전 불일치는 **후보를 즉시 버린다**.
  2. Agreement 평균 **+2.0점 이상**, **2개 이상 폴드에서 개선**, **스트레스 테스트 통과**.
     `stress_pass` 를 주지 않으면 수락하지 않는다(`None` 을 `True` 로 바꾸지 않는다).
  3. 후보의 **평면 키**(`w_valuation`, `rsi_buy`)는 `map_candidate_to_params()` 로 엔진 설정
     (`entry_weights.valuation`, `entry_gates.rsi_buy`)에 매핑한다 — 평면 키를 그대로 넘기면
     후보가 엔진에 닿지 않는다.
- 이 모듈은 `data/holdout/` 를 읽지 않는다(import·경로 접근 금지). 테스트가 이를 검사한다.
"""

from __future__ import annotations

import copy
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

#: 보정 대상 평면 키 → 엔진이 실제로 읽는 중첩 설정 경로.
FLAT_TO_ENGINE: dict[str, tuple[str, str]] = {
    "w_quality": ("entry_weights", "quality"),
    "w_valuation": ("entry_weights", "valuation"),
    "w_timing": ("entry_weights", "timing"),
    "w_macro": ("entry_weights", "macro"),
    "w_growth": ("entry_weights", "growth"),
    "rsi_buy": ("entry_gates", "rsi_buy"),
    "dma50_dev": ("entry_gates", "dma50_deviation"),
    "structural_risk_block": ("entry_gates", "structural_risk_block"),
    "risk_off_multiplier": ("macro", "risk_off_multiplier"),
    "min_margin": ("entry_gates", "margin_of_safety"),
}

#: 폴드당 최소 신호 수(4.2.7 `calibration.min_signals_per_fold`)
MIN_SIGNALS_PER_FOLD = 25
WEIGHT_KEYS = ("w_quality", "w_valuation", "w_timing", "w_macro", "w_growth")


@dataclass(frozen=True)
class FoldObservation:
    """목적함수 입력. truncated=True 인 폴드는 호출자가 넣지 않아야 한다.

    `maturity_date`·`holdout_start`·`signal_count`·`code_version`·`snapshot_id` 는 **수락 계약**의
    근거다(감사 10) — 예전에는 `fold_id` 만 있어 다른 폴드·중복 폴드·버전이 다른 후보를 구분하지 못했다.
    """

    fold_id: str
    agreement_mean: float
    ic: float | None
    mdd: float
    eligible: bool = True
    truncated: bool = False
    maturity_date: str | None = None
    holdout_start: str | None = None
    signal_count: int | None = None
    code_version: str | None = None
    snapshot_id: str | None = None
    horizon_td: int | None = None

    def key(self) -> tuple:
        """폴드 동일성 키 — 기간·폴드 ID·만기일. 셋 중 하나라도 다르면 다른 폴드다."""
        return (self.horizon_td, self.fold_id, self.maturity_date)


#: [v1.3.4 · P0-4] 수락 계약의 **필수 메타** — 하나라도 없으면 그 후보는 버린다.
#: 예전에는 전부 기본값 `None` 이라 빠진 항목의 검사가 **조용히 생략**됐다(감사 P0-4).
REQUIRED_OBSERVATION_FIELDS = ("maturity_date", "holdout_start", "signal_count",
                               "code_version", "snapshot_id")


#: 후보 파라미터 → 폴드 관측 목록. 보정은 이 러너를 **후보마다** 부른다.
FoldEvaluator = Callable[[dict], list[FoldObservation]]


def guard_truncated(observations: list[FoldObservation]) -> list[FoldObservation]:
    """잘린 폴드가 섞여 들어오면 즉시 실패 — 조용히 섞이는 것이 가장 위험하다."""
    leaked = [o.fold_id for o in observations if o.truncated or not o.eligible]
    if leaked:
        raise ValueError(f"잘린/부적격 폴드가 목적함수에 들어왔다: {leaked}")
    return observations


def check_observation_contract(observations: list[FoldObservation],
                               min_signals: int = MIN_SIGNALS_PER_FOLD,
                               require_metadata: bool = True) -> None:
    """폴드 관측의 수락 계약 검사 — 위반이면 `ValueError`(그 후보는 버린다).

    - **[v1.3.4 · P0-4] 필수 메타 누락 금지** — `maturity_date`·`holdout_start`·`signal_count`·
      `code_version`·`snapshot_id` 가 비어 있으면 그 관측으로는 경계·표본·버전을 검사할 수 없다.
      예전 코드는 기본값 `None` 을 "검사 안 함"으로 읽어 조용히 통과시켰다. 이제 **거절**한다.
    - 같은 키(`horizon, fold_id, maturity`)의 중복 관측 금지
    - `maturity_date >= holdout_start` 는 홀드아웃 경계 침범
    - `signal_count` 가 `min_signals` 미만이면 표본 부족
    """
    if require_metadata:
        missing = [(o.fold_id, [name for name in REQUIRED_OBSERVATION_FIELDS
                                if getattr(o, name, None) in (None, "")]) for o in observations]
        incomplete = [(fold, names) for fold, names in missing if names]
        if incomplete:
            raise ValueError(
                "폴드 관측 메타 누락: " + "; ".join(f"{fold}:{','.join(names)}" for fold, names in incomplete[:6])
                + " — 만기·홀드아웃 경계·신호 수·코드·스냅샷이 없으면 적격성을 판정할 수 없다(4.2.7·P0-4)")
    keys = [o.key() for o in observations]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ValueError(f"중복 폴드 관측: {duplicates}")
    for obs in observations:
        if obs.maturity_date and obs.holdout_start and str(obs.maturity_date) >= str(obs.holdout_start):
            raise ValueError(
                f"홀드아웃 경계 침범: {obs.fold_id} maturity {obs.maturity_date} >= holdout {obs.holdout_start}")
        if obs.signal_count is not None and int(obs.signal_count) < int(min_signals):
            raise ValueError(
                f"폴드당 신호 부족: {obs.fold_id} {obs.signal_count} < {min_signals}(4.2.7)")


def align_observations(base: list[FoldObservation],
                       candidate: list[FoldObservation]) -> list[tuple[FoldObservation, FoldObservation]]:
    """두 관측 목록을 **키로** 짝짓는다 — `zip()` 위치 비교를 대신한다.

    누락·추가 폴드, 같은 `fold_id` 인데 만기가 다른 폴드, 코드/스냅샷 버전이 다른 후보는 거절한다.
    """
    base_keys = {o.key() for o in base}
    cand_keys = {o.key() for o in candidate}
    if base_keys != cand_keys:
        missing = sorted(str(k) for k in base_keys - cand_keys)
        extra = sorted(str(k) for k in cand_keys - base_keys)
        raise ValueError(f"폴드 키 불일치 — 누락 {missing}, 추가 {extra}")
    by_key = {o.key(): o for o in candidate}
    pairs: list[tuple[FoldObservation, FoldObservation]] = []
    for before in base:
        after = by_key[before.key()]
        if before.code_version and after.code_version and before.code_version != after.code_version:
            raise ValueError(f"코드 버전 불일치: {before.fold_id} {before.code_version} != {after.code_version}")
        if before.snapshot_id and after.snapshot_id and before.snapshot_id != after.snapshot_id:
            raise ValueError(f"스냅샷 버전 불일치: {before.fold_id}")
        pairs.append((before, after))
    return pairs


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


def map_candidate_to_params(candidate: dict, base: dict | None = None) -> dict:
    """평면 후보 키 → **엔진이 실제로 읽는 중첩 설정**.

    엔진(`evaluate_entry`·`build_entry_plan`)은 `params["entry_weights"]["valuation"]`·
    `params["entry_gates"]["rsi_buy"]`·`params["macro"]["risk_off_multiplier"]` 를 읽는다.
    후보의 평면 키(`w_valuation`, `rsi_buy`)를 그대로 넘기면 **입력이 엔진에 닿지 않는다**(감사 10).

    `min_margin` 은 레짐별 dict(`entry_gates.margin_of_safety`)로 바꾼다 — `base` 가 있으면
    base 의 neutral 값과의 **차이**를 세 레짐에 같이 적용해 위험선호별 간격을 유지한다.
    `base` 를 주면 base 를 깊은 복사한 뒤 매핑값을 덮어쓴 **엔진용 전체 설정**을 돌려준다.
    """
    mapped: dict = {}
    margin_value: float | None = None
    for key, value in (candidate or {}).items():
        target = FLAT_TO_ENGINE.get(key)
        if target is None:
            raise ValueError(f"엔진 설정으로 매핑되지 않는 보정 키: {key!r} (허용: {sorted(FLAT_TO_ENGINE)})")
        section, name = target
        if key == "min_margin":
            margin_value = float(value)
            continue
        mapped.setdefault(section, {})[name] = float(value)
    flat_weights = {k: float(candidate[k]) for k in WEIGHT_KEYS if k in (candidate or {})}
    if len(flat_weights) == len(WEIGHT_KEYS) and abs(sum(flat_weights.values()) - 1.0) > 1e-6:
        raise ValueError(f"가중치 합이 1 이 아니다: {sum(flat_weights.values()):.6f}")
    if margin_value is not None:
        gates = mapped.setdefault("entry_gates", {})
        base_map = ((base or {}).get("entry_gates") or {}).get("margin_of_safety")
        if isinstance(base_map, dict) and base_map:
            anchor = float(base_map.get("neutral", margin_value))
            gates["margin_of_safety"] = {str(k): round(float(v) + (margin_value - anchor), 6)
                                         for k, v in base_map.items()}
        else:
            gates["margin_of_safety"] = {"risk_on": margin_value, "neutral": margin_value,
                                         "risk_off": margin_value}
    if base is None:
        return mapped
    merged = copy.deepcopy(base)
    for section, values in mapped.items():
        current = merged.get(section)
        if isinstance(current, dict):
            current.update(values)
        else:
            merged[section] = values
    return merged


def observations_from_fold_results(results, *, holdout_start: str | None = None,
                                   code_version: str | None = None,
                                   snapshot_id: str | None = None) -> list[FoldObservation]:
    """폴드 실행 결과(`FoldResult`) → 보정 관측 목록.

    자격 없는/잘린 폴드(`eligible=False`·`truncated=True`)는 **넣지 않는다**(4.2.1).
    `maturity`·`fold_id`·`horizon_td`·신호 수를 그대로 옮겨 수락 계약의 키가 되게 한다.
    """
    out: list[FoldObservation] = []
    for result in results or []:
        if getattr(result, "truncated", False) or not getattr(result, "eligible", True):
            continue
        if getattr(result, "agreement_mean", None) is None:
            continue
        maturity = getattr(result, "maturity", None)
        out.append(FoldObservation(
            fold_id=str(getattr(result, "fold_id", "")),
            agreement_mean=float(result.agreement_mean),
            ic=getattr(result, "ic", None),
            mdd=float(getattr(result, "mdd", 0.0) or 0.0),
            maturity_date=maturity.isoformat() if hasattr(maturity, "isoformat") else maturity,
            holdout_start=holdout_start,
            signal_count=len(getattr(result, "signals", []) or []),
            code_version=code_version,
            snapshot_id=snapshot_id,
            horizon_td=getattr(result, "horizon_td", None),
        ))
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
              stress_pass: Callable[[dict], bool] | None = None,
              min_signals: int = MIN_SIGNALS_PER_FOLD,
              require_metadata: bool = True) -> dict:
    """후보별 폴드 재실행 → 수락 계약(감사 10 강화). 수락하지 않아도 **이유를 남긴다**.

    `evaluate(params) -> list[FoldObservation]` 가 후보마다 폴드를 다시 돌린다.

    [v1.3.4 · P0-4] `require_metadata=True`(기본)면 필수 메타가 빠진 관측은 **후보를 버린다**.
    빠진 항목을 "검사 안 함"으로 읽지 않는다 — 경계·표본·버전을 확인할 수 없는 후보는 수락할 수 없다.
    `require_metadata=False` 는 **단위테스트·구 호출 전용**이다(운용 경로에서 쓰면 된다).
    """
    runner = _as_evaluator(evaluate)
    base_obs = guard_truncated(runner(dict(base_params)))
    record: dict = {"n_folds": len(base_obs), "accepted": False, "reason": None,
                    "candidates": [], "params": base_params,
                    "require_metadata": bool(require_metadata)}
    try:
        check_observation_contract(base_obs, min_signals=min_signals,
                                   require_metadata=require_metadata)
    except ValueError as exc:
        record["reason"] = f"INVALID_FOLDS({exc})"
        return record
    if len(base_obs) < min_folds:
        record["reason"] = "INSUFFICIENT_FOLDS"
        return record

    base_agreement = _mean_agreement(base_obs)
    best: dict | None = None
    for cand in neighbors(base_params):
        cand = rebalance_weights(cand)
        try:
            obs = guard_truncated(runner(dict(cand)))
            check_observation_contract(obs, min_signals=min_signals,
                                       require_metadata=require_metadata)
            pairs = align_observations(base_obs, obs)
        except ValueError as exc:            # 후보 실행이 계약을 어기면 그 후보만 버린다
            record["candidates"].append({"params": cand, "error": str(exc)[:160]})
            continue
        if not obs:
            continue
        gain = _mean_agreement(obs) - base_agreement
        improved = sum(1 for b, c in pairs if c.agreement_mean > b.agreement_mean)
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

    if best["agreement_gain"] < accept_gain:
        record["reason"] = f"GAIN_TOO_SMALL({best['agreement_gain']:.2f} < {accept_gain})"
        return record
    if best["improved_folds"] < accept_folds:
        record["reason"] = f"TOO_FEW_IMPROVED_FOLDS({best['improved_folds']} < {accept_folds})"
        return record
    # 스트레스 미검증은 통과가 아니다 — `None` 을 `True` 로 바꾸지 않는다(감사 10).
    if stress_pass is None:
        record["reason"] = "STRESS_NOT_PROVIDED"
        record["stress_pass"] = False
        return record
    stress_ok = bool(stress_pass(best["params"]))
    if not stress_ok:
        record["reason"] = "STRESS_FAILED"
        record["stress_pass"] = False
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
        "stress_pass": True,
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
