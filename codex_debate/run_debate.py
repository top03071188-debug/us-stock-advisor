"""codex_debate.run_debate — 3라운드 토론 오케스트레이터 (기획서 §3.11.6·§3.11.7).

```text
git pull --ff-only (실패하면 시작하지 않는다)
1R 독립 4콜(병렬) → 2R 교차 반박 4콜(병렬) → 3R risk_guard 1콜 → 3R committee_chair 1콜
→ 검증 → 종목 단위 즉시 `data/debate_reports.json` append → 파일 전체 검증 → 통과 시에만 commit/push
```

- 호출 예산: 1회 80콜 · 1일 120콜. 초과하면 그 실행을 멈추고 `status: stopped_by_limit` 로 저장한다.
  사용량은 **콜 직후마다** 체크포인트로 적는다(중단·동시 실행에도 차감이 남는다).
- V11 무결성 훼손(`injection_integrity_compromised`)이면 **Codex 를 부르기 전에** 그 종목을 접는다(P-3).
- `--resume`: 같은 `as_of` 의 `partial`/`stopped_by_limit` 리포트를 이어서 2R/3R 부터 진행한다.
- `--no-git`/`--no-push`: Codex Cloud PR 모드·테스트용(커밋·푸시를 하지 않는다).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

if __package__ in (None, ""):  # `python codex_debate/run_debate.py` 직접 실행 지원
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codex_debate.context import (  # noqa: E402
    INVESTOR_SKILLS,
    build_context,
    load_prompt,
    load_snapshot_inputs,
    load_schema,
    load_skill,
    prompt_hash,
    save_context,
    select_targets,
)
from codex_debate.executors import (  # noqa: E402
    AuthExpired,
    BaseExecutor,
    ExecutorUnavailable,
    RunLimitReached,
    make_executor,
)
from codex_debate.validate import (  # noqa: E402
    Validated,
    assemble_validated,
    integrity_compromised,
    resume_key,
    validate_round_output,
    validate_reports_file,
)

DEFAULT_SIGNALS = Path("data/quant_signals.json")
DEFAULT_REPORTS = Path("data/debate_reports.json")
DEFAULT_CONTEXT_DIR = Path("data/debate/context")
DEFAULT_USAGE = Path("data/debate/usage.json")
DEFAULT_OUT_DIR = Path("out/rounds")
CALLS_PER_TICKER = 10  # 1R 4 + 2R 4 + 3R 2


@dataclass
class RunConfig:
    signals_path: Path = DEFAULT_SIGNALS
    reports_path: Path = DEFAULT_REPORTS
    context_dir: Path = DEFAULT_CONTEXT_DIR
    usage_path: Path = DEFAULT_USAGE
    out_dir: Path = DEFAULT_OUT_DIR
    max_tickers: int = 6
    max_calls_per_run: int = 80
    max_calls_per_day: int = 120
    min_valid_skills: int = 3
    auto_downgrade_enabled: bool = False
    provider: str = "codex"
    timeout_s: int = 180
    max_attempts: int = 3
    fetch_body: bool = False
    model: str | None = None
    #: 세션 마감까지의 전체 시간 제한 — 이 시각을 넘으면 남은 종목을 돌리지 않고 멈춘다(감사 P1-1).
    #: `deadline_at` 이 없으면 신호의 `order_cutoff_at` − `deadline_margin_s` 를 쓴다.
    deadline_at: str | None = None
    deadline_margin_s: int = 300


def resolve_deadline(cfg: "RunConfig", quant_signals: dict | None = None) -> datetime | None:
    """토론 전체 시간 제한 — `--deadline` 우선, 없으면 문 마감 5분 전(감사 P1-1).

    토론이 주문 마감을 넘겨 끝나면 그 결정에 붙지 못한다(`late`). 그래서 **시작 전에** 전체 한도를
    정해 두고, 넘으면 남은 종목을 포기하며 그때까지의 라운드만 저장한다.

    `--deadline` 문자열을 해석할 수 없으면 **무제한(제한 없음)으로 되돌리지 않고** `ValueError` 로 막는다 —
    오타 하나로 마감이 사라지면 늦은 결정이 정상처럼 저장된다(감사 09-4).
    """
    if cfg.deadline_at:
        try:
            parsed = datetime.fromisoformat(str(cfg.deadline_at).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"--deadline 을 해석할 수 없다: {cfg.deadline_at!r} (ISO8601 예: 2026-11-20T13:00:00Z)") from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    cutoff = (quant_signals or {}).get("order_cutoff_at")
    if not cutoff:
        return None
    try:
        parsed = datetime.fromisoformat(str(cutoff).replace("Z", "+00:00"))
    except ValueError:
        return None
    if not parsed.tzinfo:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed - timedelta(seconds=int(cfg.deadline_margin_s))


@dataclass
class RunSummary:
    status: str = "complete"
    done: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    #: V11 무결성 훼손으로 **호출 전에** 접은 종목(P-3).
    blocked: list[str] = field(default_factory=list)
    calls: int = 0
    reports_path: str = str(DEFAULT_REPORTS)
    errors: list[str] = field(default_factory=list)
    committed: bool = False
    deadline_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status, "done": self.done, "failed": self.failed, "skipped": self.skipped,
            "blocked": self.blocked,
            "calls": self.calls, "reports_path": self.reports_path, "errors": self.errors,
            "committed": self.committed, "deadline_at": self.deadline_at,
        }


@dataclass
class CallBudget:
    """1회·1일 호출 예산. 소비는 **시도(재시도 포함)** 마다 일어난다."""

    max_run: int = 80
    max_day: int = 120
    used_run: int = 0
    used_day: int = 0

    def consume(self) -> None:
        if self.used_run + 1 > self.max_run:
            raise RunLimitReached(f"회당 호출 상한 {self.max_run} 도달")
        if self.used_day + 1 > self.max_day:
            raise RunLimitReached(f"1일 호출 상한 {self.max_day} 도달")
        self.used_run += 1
        self.used_day += 1


# ── 사용량 원장 ───────────────────────────────────────────────────────────
def reload_signals(path: str | Path, fallback: dict | None = None) -> dict:
    """pull 뒤 신호 파일을 **다시 읽는다** — 파일이 없으면 받은 값을 그대로 쓴다(네트워크·테스트)."""
    p = Path(path)
    if not p.exists():
        return dict(fallback or {})
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(fallback or {})
    return doc if isinstance(doc, dict) else dict(fallback or {})


def load_daily_usage(path: str | Path) -> dict[str, int]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {str(k): int(v) for k, v in raw.items() if isinstance(v, (int, float))}


def save_daily_usage(path: str | Path, usage: dict[str, int]) -> None:
    """일일 사용량을 **원자적으로** 쓴다 — 1R 4콜이 동시에 체크포인트를 찍어도 반쯤 쓰인 파일이 없다."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(usage, ensure_ascii=False, indent=2))
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ── git ──────────────────────────────────────────────────────────────────
def git_pull_or_abort(cwd: str | Path | None = None) -> None:
    """`git pull --ff-only` 실패 시 **시작하지 않는다**(남의 변경과 섞이면 기록이 오염된다)."""
    proc = subprocess.run(["git", "pull", "--ff-only"], capture_output=True, text=True, cwd=cwd)
    if proc.returncode != 0:
        raise SystemExit(
            "git pull --ff-only 실패 — 토론을 시작하지 않는다. "
            f"충돌·원격 변경을 정리한 뒤 다시 실행하라.\n{(proc.stdout or '')}{(proc.stderr or '')}".strip()
        )


def commit_and_push(
    paths: Sequence[str | Path],
    message: str,
    *,
    push: bool = True,
    cwd: str | Path | None = None,
) -> bool:
    """검증을 통과한 리포트만 커밋한다. 커밋할 변경이 없으면 False.

    [감사 10] `git commit` 은 **이미 스테이징된 다른 파일까지** 함께 삼킨다. 그래서 경로를 지정한
    `git commit --only -- <paths>` 로 커밋해, 다른 작업이 올려 둔 인덱스를 건드리지 않는다
    (인덱스 사전 검증도 한다 — 우리 경로 밖의 파일이 섞이면 실패로 끝난다).
    """
    targets = [str(p) for p in paths]
    subprocess.run(["git", "add", "--", *targets], check=True, cwd=cwd)
    ours = subprocess.run(["git", "diff", "--cached", "--name-only", "--", *targets],
                          capture_output=True, text=True, cwd=cwd, check=True)
    staged_ours = {line.strip() for line in (ours.stdout or "").splitlines() if line.strip()}
    if not staged_ours:
        return False
    subprocess.run(["git", "commit", "--only", "-m", message, "--", *targets], check=True, cwd=cwd)
    committed = subprocess.run(["git", "show", "--pretty=format:", "--name-only", "HEAD"],
                               capture_output=True, text=True, cwd=cwd, check=True)
    names = {line.strip() for line in (committed.stdout or "").splitlines() if line.strip()}
    unexpected = names - staged_ours
    if unexpected:
        raise subprocess.CalledProcessError(
            1, ["git", "commit", "--only"],
            output=f"커밋에 허용 밖 파일이 섞였다: {sorted(unexpected)}")
    if push:
        subprocess.run(["git", "push"], check=True, cwd=cwd)
    return True


# ── 프롬프트·호출 ─────────────────────────────────────────────────────────
def _parallel(tasks: Sequence[Callable[[], Any]]) -> tuple[list[Any], BaseException | None]:
    """네 스킬을 병렬로 실행하고 **(성공 결과, 첫 예외)** 를 돌려준다.

    한도로 중간에 끊겨도 이미 끝난 라운드 결과는 버리지 않는다(`--resume` 이 그 지점부터 이어간다).
    """
    results: list[Any] = []
    error: BaseException | None = None
    if not tasks:
        return results, error
    with ThreadPoolExecutor(max_workers=min(4, len(tasks))) as pool:
        futures = [pool.submit(task) for task in tasks]
        for future in futures:
            try:
                results.append(future.result())
            except BaseException as exc:  # noqa: BLE001 — 예외는 모아서 호출자에게 넘긴다
                if error is None:
                    error = exc
    return results, error


def _rounds_payload(state: dict) -> dict:
    guard = state.get("guard")
    return {
        "round1": [v.data for v in state.get("round1") or [] if v.ok and v.data],
        "round2": [v.data for v in state.get("round2") or [] if v.ok and v.data],
        "guard": guard.data if guard and guard.ok else None,
    }


def _call_skill(
    executor: BaseExecutor,
    budget: CallBudget,
    round_name: str,
    skill: str,
    context: dict,
    state: dict,
    deadline: datetime | None = None,
) -> Validated:
    schema = load_schema(round_name)
    skill_body = load_skill(skill)
    if round_name == "round1":
        prompt = load_prompt("round1", SKILL=skill_body, CONTEXT_JSON=context, SCHEMA_JSON=schema)
    elif round_name == "round2":
        others = [v.data for v in state.get("round1") or [] if v.ok and v.data and v.data.get("skill") != skill]
        prompt = load_prompt("round2", SKILL=skill_body, CONTEXT_JSON=context,
                             OTHERS_ROUND1_JSON=others, SCHEMA_JSON=schema)
    elif round_name == "round3_guard":
        prompt = load_prompt("round3_guard", SKILL=skill_body, CONTEXT_JSON=context,
                             ROUNDS_JSON=_rounds_payload(state), SCHEMA_JSON=schema)
    elif round_name == "round3_chair":
        prompt = load_prompt("round3_chair", SKILL=skill_body, CONTEXT_JSON=context,
                             ROUNDS_JSON=_rounds_payload(state), SCHEMA_JSON=schema)
    else:
        raise KeyError(f"알 수 없는 라운드: {round_name}")

    result = executor.run(
        round_name=round_name,
        prompt=prompt,
        schema=schema,
        context_payload=context,
        out_name=f"{round_name}_{skill}.json",
        budget=budget.consume,
        deadline=deadline,
    )
    if result.status == "limit":
        raise RunLimitReached(result.error or "사용량 한도")
    if result.status == "auth":
        raise AuthExpired(result.error or "로그인 만료")
    if result.status == "unavailable":
        raise ExecutorUnavailable(result.error or "실행기를 사용할 수 없다")
    if result.payload is None:
        return Validated(round_name, None, False, [f"executor:{result.status}", (result.error or "")[:200]])
    expected = skill if round_name in ("round1", "round2") else None
    return validate_round_output(round_name, result.payload, context, expected_skill=expected)


def _run_ticker(
    context: dict,
    executor: BaseExecutor,
    budget: CallBudget,
    cfg: RunConfig,
    state: dict,
    deadline: datetime | None = None,
    on_call: Callable[[], None] | None = None,
) -> dict:
    """1R → 2R → 3R 을 수행한다. 정족수 미달이면 3R 을 건너뛴다(등급 권고 미적용)."""

    def call(round_name: str, skill: str) -> Validated:
        result = _call_skill(executor, budget, round_name, skill, context, state, deadline=deadline)
        if on_call is not None:
            on_call()  # 콜 직후 예산 체크포인트 — 프로세스가 죽어도 차감이 남는다(감사 09-4)
        return result

    have1 = {v.data["skill"] for v in state.get("round1") or [] if v.ok and v.data}
    missing1 = [skill for skill in INVESTOR_SKILLS if skill not in have1]
    gathered1, error1 = _parallel([
        (lambda s=skill: call("round1", s)) for skill in missing1
    ])
    state["round1"] = list(state.get("round1") or []) + gathered1
    if error1 is not None:
        raise error1
    valid1 = [v for v in state["round1"] if v.ok]
    if len(valid1) < cfg.min_valid_skills:
        return state

    have2 = {v.data["skill"] for v in state.get("round2") or [] if v.ok and v.data}
    targets = [v.data["skill"] for v in valid1 if v.data["skill"] not in have2]
    gathered2, error2 = _parallel([
        (lambda s=skill: call("round2", s)) for skill in targets
    ])
    state["round2"] = list(state.get("round2") or []) + gathered2
    if error2 is not None:
        raise error2

    guard = state.get("guard")
    if not (guard and guard.ok and guard.data):
        state["guard"] = call("round3_guard", "risk_guard")
    chair = state.get("chair")
    if not (chair and chair.ok and chair.data):
        state["chair"] = call("round3_chair", "committee_chair")
    return state


# ── 리포트 파일 ──────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_reports_doc(path: str | Path, signals: dict | None = None) -> dict:
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    signals = signals or {}
    return {
        "schema_version": "1.0",
        "generated_at": _now_iso(),
        "codex": {"version": os.environ.get("CODEX_VERSION") or "unknown", "model": "default"},
        "inputs": {
            "quant_signals_commit": str(signals.get("code_commit") or ""),
            "as_of": str(signals.get("as_of") or ""),
            "context_hash": "",
            "prompt_hash": prompt_hash(),
        },
        "reports": [],
    }


def write_reports_doc(
    path: str | Path,
    doc: dict,
    signals: dict,
    context: dict,
    cfg: RunConfig,
    *,
    quant_signals_sha256: str | None = None,
    provenance: dict | None = None,
) -> Path:
    """리포트 파일을 쓴다 — `inputs` 는 **이어 쓴다**(다종목 누적에서 앞 종목의 출처를 지우지 않는다).

    `inputs.quant_signals_commit` 은 신호 파일이 스스로 적는 코드 커밋(`signals.code_commit`)이고,
    **파일 바이트 해시**는 별도 키 `quant_signals_sha256` 에 적는다 — 둘은 다른 값이다(감사 07).
    종목별 값이 섞이는 `context_hash` 는 마지막 종목 값 대신 `context_hash_by_ticker` 로도 남기고,
    리포트별 출처(`provenance`)는 `reports_provenance[report_id]` 에 건별로 저장한다.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc["generated_at"] = _now_iso()
    doc["codex"] = {"version": os.environ.get("CODEX_VERSION") or "unknown", "model": cfg.model or "default"}
    inputs = dict(doc.get("inputs") or {})
    inputs.update({
        "quant_signals_commit": str(signals.get("code_commit") or ""),
        "as_of": str(signals.get("as_of") or ""),
        "context_hash": str(context.get("context_hash") or ""),
        "prompt_hash": prompt_hash(),
    })
    digest = quant_signals_sha256 or str(signals.get("quant_signals_sha256") or "")
    if digest:
        inputs["quant_signals_sha256"] = str(digest)
    by_ticker = dict(inputs.get("context_hash_by_ticker") or {})
    ticker = str(context.get("ticker") or "").upper()
    if ticker:
        by_ticker[ticker] = str(context.get("context_hash") or "")
    if by_ticker:
        inputs["context_hash_by_ticker"] = by_ticker
    if provenance:
        report_id = str(provenance.get("report_id") or "")
        if report_id:
            reports_provenance = dict(inputs.get("reports_provenance") or {})
            reports_provenance[report_id] = {key: value for key, value in provenance.items()
                                             if key != "report_id"}
            inputs["reports_provenance"] = reports_provenance
    doc["inputs"] = inputs
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def upsert_report(doc: dict, report: dict) -> None:
    reports = doc.setdefault("reports", [])
    for idx, existing in enumerate(reports):
        if existing.get("report_id") == report.get("report_id"):
            reports[idx] = report
            return
    reports.append(report)


def _report_seq(doc: dict, ticker: str, as_of: str, reuse: dict | None = None) -> int:
    if reuse and reuse.get("report_id"):
        tail = str(reuse["report_id"]).rsplit("-", 1)[-1]
        if tail.isdigit():
            return int(tail)
    seqs = []
    for rep in doc.get("reports") or []:
        if rep.get("ticker") == ticker and rep.get("as_of") == as_of:
            tail = str(rep.get("report_id", "")).rsplit("-", 1)[-1]
            if tail.isdigit():
                seqs.append(int(tail))
    return (max(seqs) + 1) if seqs else 1


def _initial_state(report: dict | None) -> dict:
    """이어 돌릴 리포트의 라운드 상태 복원 — **저장된 검증 상태·카운트 그대로** 쓴다.

    예전에는 데이터가 있으면 `ok=True` 로 되살렸다. 그러면 1R 에서 스키마검사로 떨어진 출력이
    통과로 둔갑해 정족수·리포트 상태가 실제와 달라졌다(감사 09-3).
    저장된 상태(`validation.round_state`)가 없는 리포트는 **이어받지 않는다** — 모르면 다시 돌린다.
    """
    if not report:
        return {"round1": [], "round2": [], "guard": None, "chair": None}
    round3 = report.get("round3") or {}
    states = (report.get("validation") or {}).get("round_state") or {}

    def restore(kind: str, data: dict | None, slot: dict | None) -> Validated | None:
        if not data or not isinstance(slot, dict) or not slot:
            return None
        return Validated(kind, data, bool(slot.get("ok")), list(slot.get("reasons") or []),
                         dict(slot.get("counts") or {}), dict(slot.get("flags") or {}))

    round1_states = states.get("round1") or []
    round2_states = states.get("round2") or []
    round1 = [restore("round1", entry, round1_states[i] if i < len(round1_states) else None)
              for i, entry in enumerate(report.get("round1") or [])]
    round2 = [restore("round2", entry, round2_states[i] if i < len(round2_states) else None)
              for i, entry in enumerate(report.get("round2") or [])]
    return {
        "round1": [v for v in round1 if v is not None],
        "round2": [v for v in round2 if v is not None],
        "guard": restore("round3_guard", round3.get("guard"), states.get("guard")),
        "chair": restore("round3_chair", round3.get("chair"), states.get("chair")),
    }


def _save_report(
    doc: dict,
    path: str | Path,
    cfg: RunConfig,
    signals: dict,
    context: dict,
    state: dict,
    *,
    status_override: str | None = None,
    seq: int = 1,
    blocked_before_calls: bool = False,
) -> dict:
    report = assemble_validated(
        context,
        round1=state.get("round1") or [],
        round2=state.get("round2") or [],
        guard=state.get("guard"),
        chair=state.get("chair"),
        completed_at=_now_iso(),
        status_override=status_override,
        min_valid_skills=cfg.min_valid_skills,
        auto_downgrade_enabled=cfg.auto_downgrade_enabled,
        provider=cfg.provider,
        report_seq=seq,
        # 검증 시각 — 이 프로세스가 결과를 받아 검증한 순간. `completed_at`(모델 자기 신고)과 별개다.
        # 두 값 모두 `_now_iso()`(초 단위)지만 앞의 것이 먼저 계산되므로 `validated >= completed` 이고,
        # 초 절사는 두 값을 같이 내리므로 순서를 뒤집지 않는다. 그래도 `assemble_validated` 가
        # `validated < completed` 를 완료 시각으로 올려 막는다(수정 A — 공유 계약).
        validated_at=_now_iso(),
        model_version=cfg.model,
        blocked_before_calls=blocked_before_calls,
    )
    upsert_report(doc, report)
    write_reports_doc(path, doc, signals, context, cfg)
    return report


# ── 실행 ─────────────────────────────────────────────────────────────────
def run_debate(
    quant_signals: dict,
    executor: BaseExecutor,
    *,
    cfg: RunConfig | None = None,
    tickers: Iterable[str] | None = None,
    top: int | None = None,
    resume: bool = False,
    dry_run: bool = False,
    pull: bool = True,
    push: bool = True,
    cwd: str | Path | None = None,
    news_by_ticker: dict[str, list[dict]] | None = None,
    snapshot: dict | None = None,
    radar: dict | None = None,
) -> RunSummary:
    """토론 1회 실행. 예산·한도·검증에 따라 멈추고 `RunSummary` 로 결과를 남긴다."""
    cfg = cfg or RunConfig()
    summary = RunSummary(status="complete", reports_path=str(cfg.reports_path))
    if pull:
        # [감사 09-2] **먼저** 원격을 당긴다 — 대상 선정·컨텍스트가 pull 전 파일로 정해지면
        # 방금 올라온 신호를 놓치고, 반대로 퇴근한 신호를 토론한다.
        git_pull_or_abort(cwd)
        quant_signals = reload_signals(cfg.signals_path, quant_signals)
    as_of = str(quant_signals.get("as_of") or "")
    if snapshot is None:
        # 로컬 비공개 스냅샷(`data/snapshots/{as_of}/inputs.json`)이 있으면 그쪽 뉴스 행을 쓴다 —
        # Finnhub `summary` 가 남아 있어 본문이 막힌 기사에도 인용할 문장이 생긴다(v1.3.7).
        # CI 처럼 파일이 없는 곳에서는 None 그대로다(공개 신호 행으로 폴백).
        snapshot = load_snapshot_inputs(as_of)
    targets = select_targets(quant_signals, tickers=tickers, top=top, max_tickers=cfg.max_tickers)
    if not targets:
        summary.status = "no_targets"
        return summary

    usage = load_daily_usage(cfg.usage_path)
    budget = CallBudget(cfg.max_calls_per_run, cfg.max_calls_per_day, used_day=int(usage.get(as_of, 0) or 0))

    def checkpoint_usage() -> None:
        """콜 하나가 끝날 때마다 일일 사용량을 적는다 — 마지막에 한 번만 쓰면 중단 시 차감이 사라진다."""
        usage[as_of] = budget.used_day
        save_daily_usage(cfg.usage_path, usage)

    doc = load_reports_doc(cfg.reports_path, quant_signals)
    deadline = resolve_deadline(cfg, quant_signals)
    summary.deadline_at = deadline.isoformat() if deadline else None
    resume_reports = {}
    if resume:
        for rep in doc.get("reports") or []:
            if rep.get("as_of") == as_of and rep.get("status") in ("partial", "stopped_by_limit", "failed"):
                resume_reports[rep.get("ticker")] = rep

    for index, target in enumerate(targets):
        if deadline is not None and datetime.now(timezone.utc) >= deadline:
            # 세션 마감 전에 끝나야 그 결정에 붙는다 — 남은 종목은 포기하고 지금까지를 저장한다.
            summary.status = "stopped_by_deadline"
            summary.errors.append(
                f"deadline {deadline.isoformat()} 도달 — 남은 {len(targets) - index}종목을 시작하지 않았다")
            break
        ticker = target["ticker"]
        if any(
            rep.get("ticker") == ticker and rep.get("as_of") == as_of and rep.get("status") == "complete"
            for rep in doc.get("reports") or []
        ):
            summary.skipped.append(ticker)
            continue
        reuse = resume_reports.get(ticker)
        context = build_context(
            ticker,
            quant_signals,
            news_items=(news_by_ticker or {}).get(ticker) if news_by_ticker else None,
            fetch_body=cfg.fetch_body,
            snapshot=snapshot,
            radar=radar,
        )
        if reuse is not None:
            # 재개 키(signal_id + snapshot_id + context_hash + prompt_hash + model)가
            # **비어 있지 않고 정확히 같을 때만** 이어간다. 키가 없는 구 리포트는 무엇을 이어받는지
            # 알 수 없으므로 처음부터 다시 돈다(감사 09-3).
            stored = str((reuse.get("validation") or {}).get("resume_key") or "").strip()
            if not stored or stored != resume_key(context, model_version=cfg.model):
                reuse = None
        seq = _report_seq(doc, ticker, as_of, reuse)
        state = _initial_state(reuse)
        if dry_run:
            summary.done.append(ticker)
            continue
        if integrity_compromised(context):
            # P-3: 무결성 훼손이면 **Codex 를 부르기 전에** V11 차단 상태로 접는다 —
            # 10콜을 돌리면 모델이 지시문을 그대로 읽고 예산도 쓴다(감사 08 · 정책 P-3).
            report = _save_report(doc, cfg.reports_path, cfg, quant_signals, context, state,
                                  status_override="failed", seq=seq, blocked_before_calls=True)
            summary.failed.append(ticker)
            summary.errors.append(f"{ticker}: V11 무결성 훼손 — 호출 전 종료")
            summary.blocked.append(ticker)
            summary.status = "blocked_injection"
            continue
        save_context(context, cfg.context_dir)
        try:
            state = _run_ticker(context, executor, budget, cfg, state, deadline=deadline,
                                on_call=checkpoint_usage)
        except RunLimitReached as exc:
            _save_report(doc, cfg.reports_path, cfg, quant_signals, context, state,
                         status_override="stopped_by_limit", seq=seq)
            summary.status = "stopped_by_limit"
            summary.failed.append(ticker)
            summary.errors.append(str(exc))
            break
        except (AuthExpired, ExecutorUnavailable) as exc:
            _save_report(doc, cfg.reports_path, cfg, quant_signals, context, state,
                         status_override="partial", seq=seq)
            summary.status = "auth_expired" if isinstance(exc, AuthExpired) else "executor_unavailable"
            summary.errors.append(str(exc))
            summary.failed.append(ticker)
            break

        report = _save_report(doc, cfg.reports_path, cfg, quant_signals, context, state, seq=seq)
        if report["status"] in ("complete", "partial"):
            summary.done.append(ticker)
            if report["status"] == "partial" and summary.status == "complete":
                # 2R 전멸 등으로 완주하지 못한 리포트 — 파일에는 partial 로 적혔고 실행 요약도 같다.
                summary.status = "partial"
        else:
            summary.failed.append(ticker)

    summary.calls = executor.calls
    checkpoint_usage()

    if dry_run:
        summary.status = "dry_run"
        return summary

    if summary.status == "stopped_by_deadline" and not summary.done and not summary.failed:
        # 마감 때문에 **아무 종목도 시작하지 못했다** — 리포트 파일이 없으니 파일 검증은 건너뛴다.
        return summary

    errors = validate_reports_file(cfg.reports_path, auto_downgrade_enabled=cfg.auto_downgrade_enabled)
    if errors:
        summary.status = "validation_failed"
        summary.errors.extend(errors[:10])
        return summary

    if summary.blocked and summary.done and summary.status == "blocked_injection":
        summary.status = "partial"
    if summary.failed and summary.status == "complete":
        summary.status = "partial"

    if (summary.done or summary.blocked) and (pull or push):
        message = (f"debate: {','.join(summary.done)} @ {as_of}" if summary.done
                   else f"debate: blocked by V11 @ {as_of}")
        try:
            summary.committed = commit_and_push([cfg.reports_path], message, push=push, cwd=cwd)
        except subprocess.CalledProcessError as exc:
            summary.status = "git_failed"
            summary.errors.append(str(exc))
    return summary


# ── CLI ──────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_debate",
        description="Codex 스킬 3라운드 토론 (기획서 §3.11) — 기본은 Codex CLI(구독 사용량, 0원)",
    )
    parser.add_argument("--tickers", help="쉼표 구분 종목(신호·워치리스트 안에서만)")
    parser.add_argument("--top", type=int, help="신호 점수 상위 N종목")
    parser.add_argument("--provider", choices=["codex", "openai"], default="codex")
    parser.add_argument("--resume", action="store_true", help="같은 as_of 의 partial/stopped_by_limit 이어서")
    parser.add_argument("--no-git", action="store_true", help="git pull/commit/push 를 하지 않는다")
    parser.add_argument("--no-push", action="store_true", help="커밋만 하고 push 하지 않는다(Codex Cloud PR 모드)")
    parser.add_argument("--dry-run", action="store_true", help="대상·예상 호출 수만 출력")
    parser.add_argument("--validate-only", action="store_true", help="리포트 파일만 검증하고 끝낸다")
    parser.add_argument("--signals", default=str(DEFAULT_SIGNALS))
    parser.add_argument("--reports", default=str(DEFAULT_REPORTS))
    parser.add_argument("--context-dir", default=str(DEFAULT_CONTEXT_DIR))
    parser.add_argument("--max-tickers", type=int, default=6)
    parser.add_argument("--max-calls-per-run", type=int, default=80)
    parser.add_argument("--max-calls-per-day", type=int, default=120)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--model", default=None)
    parser.add_argument("--deadline", default=None,
                        help="전체 시간 제한(ISO8601). 없으면 order_cutoff_at − 5분")
    parser.add_argument("--deadline-margin", type=int, default=300,
                        help="주문 마감 몇 초 전에 멈출지(기본 300초)")
    parser.add_argument("--fetch-body", dest="fetch_body", action="store_true", default=True,
                        help="기사 원문 본문을 받아 1,500자 발췌한다(기본 켜짐, 로컬 전용)")
    parser.add_argument("--no-fetch-body", dest="fetch_body", action="store_false",
                        help="본문 수집을 끈다(인용할 문장이 없어 뉴스 상태가 근거 부족이 된다)")
    return parser


def _split_tickers(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [part.strip().upper() for part in value.split(",") if part.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _main(args)
    except ValueError as exc:
        # `--deadline` 형식 오류 같은 **설정 오류** — 무제한으로 되돌아가지 않고 분명히 실패한다.
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2


def _main(args: argparse.Namespace) -> int:
    signals_path = Path(args.signals)
    if not signals_path.exists():
        print(f"신호 파일이 없다: {signals_path}", file=sys.stderr)
        return 2
    quant_signals = json.loads(signals_path.read_text(encoding="utf-8"))

    if args.validate_only:
        errors = validate_reports_file(args.reports, auto_downgrade_enabled=False)
        for error in errors:
            print(error, file=sys.stderr)
        print(json.dumps({"errors": len(errors), "path": args.reports}, ensure_ascii=False))
        return 0 if not errors else 2

    cfg = RunConfig(
        signals_path=signals_path,
        reports_path=Path(args.reports),
        context_dir=Path(args.context_dir),
        max_tickers=args.max_tickers,
        max_calls_per_run=args.max_calls_per_run,
        max_calls_per_day=args.max_calls_per_day,
        provider=args.provider,
        timeout_s=args.timeout,
        fetch_body=args.fetch_body,
        model=args.model,
        deadline_at=args.deadline,
        deadline_margin_s=args.deadline_margin,
    )

    if args.dry_run:
        # [감사 09-2] 대상·컨텍스트는 **pull 뒤 파일**로 정한다 — 미리보기도 실행과 같은 순서를 따른다.
        if not args.no_git:
            git_pull_or_abort(None)
            quant_signals = reload_signals(signals_path, quant_signals)
        targets = select_targets(quant_signals, tickers=_split_tickers(args.tickers), top=args.top,
                                 max_tickers=cfg.max_tickers)
        deadline = resolve_deadline(cfg, quant_signals)
        print(json.dumps({
            "as_of": quant_signals.get("as_of"),
            "targets": targets,
            "estimated_calls": len(targets) * CALLS_PER_TICKER,
            "deadline_at": deadline.isoformat() if deadline else None,
            "order_cutoff_at": quant_signals.get("order_cutoff_at"),
        }, ensure_ascii=False, indent=2))
        return 0

    try:
        executor = make_executor(args.provider, model=args.model, timeout_s=args.timeout,
                                 max_attempts=cfg.max_attempts, out_dir=cfg.out_dir)
    except ExecutorUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 4

    summary = run_debate(
        quant_signals,
        executor,
        cfg=cfg,
        tickers=_split_tickers(args.tickers),
        top=args.top,
        resume=args.resume,
        pull=not args.no_git,
        push=not (args.no_git or args.no_push),
    )
    print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    if summary.status in ("complete", "partial", "no_targets", "dry_run"):
        return 0
    if summary.status in ("stopped_by_limit", "stopped_by_deadline", "auth_expired", "executor_unavailable",
                          "blocked_injection"):
        return 3
    return 2


__all__ = [
    "CALLS_PER_TICKER", "CallBudget", "RunConfig", "RunSummary", "build_parser", "commit_and_push",
    "git_pull_or_abort", "load_daily_usage", "load_reports_doc", "main", "reload_signals", "resolve_deadline",
    "run_debate", "save_daily_usage", "upsert_report", "write_reports_doc",
]

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
