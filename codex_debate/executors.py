"""codex_debate.executors — PC Codex CLI / OpenAI API 어댑터 (기획서 §3.9.0·§3.11.6).

두 실행기는 **같은 출력 스키마**를 강제하는 교체 부품이다. 기본값은 Codex CLI(구독 사용량, 달러 0원)이고,
API 어댑터는 PC 없이 무인으로 돌릴 때만 쓴다(`MAX_MONTHLY_USD=5` 하드 캡).

실행 규칙(어느 쪽이든 같다)
- 콜당 타임아웃 180초, 스키마 실패·타임아웃은 백오프로 2회 재시도(총 3회).
- 사용량 한도·로그인 만료는 재시도하지 않고 **즉시** 상태로 돌려준다(오케스트레이터가 중단·안내).
- 모델이 스키마를 어겨도 코드가 마지막에 다시 검증한다(`validate.schema_errors`).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from codex_debate.context import load_schema, schema_path, to_strict_schema
from codex_debate.validate import schema_errors

#: 사용량 한도(5시간·주간 창) 신호.
LIMIT_MARKERS: tuple[str, ...] = (
    "usage limit", "rate limit", "rate-limit", "quota", "too many requests",
    "5-hour", "weekly limit", "limit reached", "한도",
)
#: 로그인 만료 신호.
AUTH_MARKERS: tuple[str, ...] = (
    "not logged in", "please log in", "please login", "codex login", "unauthorized",
    "authentication", "401", "auth.json", "session expired",
)


class ExecutorError(RuntimeError):
    """실행기 공통 오류."""


class ExecutorUnavailable(ExecutorError):
    """CLI 미설치·API 키 없음 — 이 경로로는 실행할 수 없다."""


class RunLimitReached(ExecutorError):
    """사용량·예산 한도 도달 — 그 실행을 멈추고 저장한다(`stopped_by_limit`)."""


class AuthExpired(ExecutorError):
    """로그인 만료 — 아무것도 push 하지 않고 안내한다."""


@dataclass
class CallResult:
    payload: dict | None
    attempts: int
    status: str  # ok | schema_failed | timeout | limit | auth | unavailable | error
    error: str | None = None
    raw: str = ""


def _strip_fences(text: str) -> str:
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else body
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
    start, end = body.find("{"), body.rfind("}")
    if start >= 0 and end > start:
        return body[start:end + 1]
    return body


class BaseExecutor:
    """재시도·스키마 검증·상태 판정을 한 곳에 모은 공통 골격."""

    provider = "base"

    def __init__(
        self,
        *,
        timeout_s: int = 180,
        max_attempts: int = 3,
        backoff_s: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        out_dir: str | Path = "out/rounds",
    ) -> None:
        self.timeout_s = int(timeout_s)
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_s = float(backoff_s)
        self._sleep = sleep
        self.out_dir = Path(out_dir)
        self.calls = 0

    # 하위 클래스가 구현한다 -------------------------------------------------
    def _invoke(
        self,
        round_name: str,
        prompt: str,
        schema: dict,
        context_payload: dict | None,
        out_name: str | None,
    ) -> tuple[str, str]:
        raise NotImplementedError

    # 공통 ---------------------------------------------------------------
    def _parse(self, raw: str, schema: dict) -> tuple[dict | None, str | None]:
        if not raw or not raw.strip():
            return None, "빈 출력"
        try:
            payload = json.loads(_strip_fences(raw))
        except json.JSONDecodeError as exc:
            return None, f"JSON 파싱 실패: {exc}"
        if not isinstance(payload, dict):
            return None, "출력이 객체가 아니다"
        errors = schema_errors(payload, schema)
        if errors:
            return None, "; ".join(errors[:5])
        return payload, None

    def run(
        self,
        *,
        round_name: str,
        prompt: str,
        schema: dict | None = None,
        context_payload: dict | None = None,
        out_name: str | None = None,
        budget: Callable[[], None] | None = None,
    ) -> CallResult:
        schema = schema or load_schema(round_name)
        errors: list[str] = []
        parse_failures = 0
        for attempt in range(1, self.max_attempts + 1):
            if budget is not None:
                budget()
            self.calls += 1
            try:
                raw, status = self._invoke(round_name, prompt, schema, context_payload, out_name)
            except subprocess.TimeoutExpired:
                raw, status = "", "timeout"
            except ExecutorUnavailable as exc:
                return CallResult(None, attempt, "unavailable", str(exc))
            except RunLimitReached as exc:
                return CallResult(None, attempt, "limit", str(exc))
            except AuthExpired as exc:
                return CallResult(None, attempt, "auth", str(exc))
            if status in ("limit", "auth", "unavailable"):
                return CallResult(None, attempt, status, raw.strip()[:400] or status)
            if status == "ok":
                payload, parse_error = self._parse(raw, schema)
                if payload is not None:
                    return CallResult(payload, attempt, "ok", None, raw)
                parse_failures += 1
                errors.append(parse_error or "스키마 불일치")
                status = "schema_failed"
            elif status == "timeout":
                errors.append(f"타임아웃({self.timeout_s}s)")
            else:
                errors.append(raw.strip()[:200] or status)
            if attempt < self.max_attempts:
                self._sleep(self.backoff_s * (2 ** (attempt - 1)))
        final_status = "schema_failed" if parse_failures else "error"
        return CallResult(None, self.max_attempts, final_status, "; ".join(errors)[:600])


class CodexCliExecutor(BaseExecutor):
    """`codex exec --output-schema … -o … --ephemeral` (영서 님 PC, ChatGPT 구독 로그인)."""

    provider = "codex"

    def __init__(
        self,
        *,
        binary: str = "codex",
        model: str | None = None,
        sandbox: str = "read-only",
        extra_args: tuple[str, ...] = (),
        runner: Callable[..., Any] = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.binary = binary
        self.model = model
        self.sandbox = sandbox
        self.extra_args = tuple(extra_args)
        self._runner = runner
        self._which = which

    def _resolve_binary(self) -> str:
        found = self._which(self.binary)
        if found:
            return found
        if Path(self.binary).exists():
            return str(Path(self.binary))
        raise ExecutorUnavailable(
            "codex CLI 를 찾을 수 없다. `codex login` 후 PATH 를 확인하거나 --provider openai 로 실행한다."
        )

    @staticmethod
    def _status_from_output(text: str) -> str | None:
        lowered = text.lower()
        if any(marker in lowered for marker in LIMIT_MARKERS):
            return "limit"
        if any(marker in lowered for marker in AUTH_MARKERS):
            return "auth"
        return None

    def strict_schema_file(self, round_name: str, schema: dict) -> Path:
        """`codex exec` 에 넘길 strict 스키마 파일(받지 않는 검증 키워드 제거)."""
        path = self.out_dir / f"{round_name}.strict.schema.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(to_strict_schema(schema), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)  # 1R 4콜이 병렬로 써도 반쯤 쓰인 파일을 읽지 않는다
        return path

    def build_command(self, prompt: str, out_path: Path, schema_file: Path) -> list[str]:
        cmd = [
            self._resolve_binary(), "exec",
            "--output-schema", str(schema_file),
            "-o", str(out_path),
            "--ephemeral",
        ]
        if self.model:
            cmd += ["--model", self.model]
        if self.sandbox:
            cmd += ["--sandbox", self.sandbox]
        cmd += list(self.extra_args)
        cmd += [prompt]
        return cmd

    def _invoke(self, round_name: str, prompt: str, schema: dict, context_payload: dict | None,
                out_name: str | None) -> tuple[str, str]:
        out_path = self.out_dir / (out_name or f"{round_name}.json")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.unlink(missing_ok=True)  # 이전 시도의 출력을 성공으로 오인하지 않는다
        schema_file = self.strict_schema_file(round_name, schema) if schema else schema_path(round_name)
        cmd = self.build_command(prompt, out_path, schema_file)
        try:
            proc = self._runner(
                cmd,
                input=json.dumps(context_payload or {}, ensure_ascii=False),
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except OSError as exc:  # 실행 파일이 사라졌거나 권한이 없다
            raise ExecutorUnavailable(f"codex 실행 실패: {exc}") from exc
        combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        marker = self._status_from_output(combined)
        if marker:
            return combined, marker
        if getattr(proc, "returncode", 0) != 0:
            return combined, "error"
        if out_path.exists():
            return out_path.read_text(encoding="utf-8"), "ok"
        return proc.stdout or "", "ok"


class OpenAiAdapterExecutor(BaseExecutor):
    """옵션 경로 — `LLM_API_KEY` + `MAX_MONTHLY_USD=5` 하드 캡. 같은 스키마를 `json_schema` 로 강제한다."""

    provider = "openai"
    MONTHLY_HARD_CAP_USD = 5.0

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        max_monthly_usd: float | None = None,
        usage_path: str | Path = "data/debate/usage.json",
        poster: Callable[..., Any] | None = None,
        price_in_per_mtok: float = 1.25,
        price_out_per_mtok: float = 10.0,
        estimated_in_tokens: int = 6000,
        estimated_out_tokens: int = 1500,
        base_url: str = "https://api.openai.com/v1/chat/completions",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.api_key = api_key or os.environ.get("LLM_API_KEY")
        if not self.api_key:
            raise ExecutorUnavailable("LLM_API_KEY 가 없다 — API 어댑터는 이 키 없이 실행할 수 없다")
        self.model = model or os.environ.get("LLM_MODEL") or "gpt-5"
        env_cap = os.environ.get("MAX_MONTHLY_USD")
        cap = float(env_cap) if env_cap else (max_monthly_usd if max_monthly_usd is not None else self.MONTHLY_HARD_CAP_USD)
        self.max_monthly_usd = min(cap, self.MONTHLY_HARD_CAP_USD)
        self.usage_path = Path(usage_path)
        self.price_in_per_mtok = float(price_in_per_mtok)
        self.price_out_per_mtok = float(price_out_per_mtok)
        self.estimated_in_tokens = int(estimated_in_tokens)
        self.estimated_out_tokens = int(estimated_out_tokens)
        self.base_url = base_url
        self._poster = poster

    # 사용량 원장(월 단위, 로컬 전용) --------------------------------------
    def _load_usage(self) -> dict:
        if not self.usage_path.exists():
            return {}
        try:
            return json.loads(self.usage_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _month_key(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def spent_usd(self) -> float:
        return float(self._load_usage().get(self._month_key(), 0.0) or 0.0)

    def _record_usage(self, usd: float) -> None:
        usage = self._load_usage()
        key = self._month_key()
        usage[key] = round(float(usage.get(key, 0.0) or 0.0) + float(usd), 6)
        self.usage_path.parent.mkdir(parents=True, exist_ok=True)
        self.usage_path.write_text(json.dumps(usage, ensure_ascii=False, indent=2), encoding="utf-8")

    def estimated_cost(self) -> float:
        return (self.estimated_in_tokens / 1_000_000) * self.price_in_per_mtok + (
            self.estimated_out_tokens / 1_000_000
        ) * self.price_out_per_mtok

    def _http_post(self, payload: dict) -> Any:
        if self._poster is not None:
            return self._poster(self.base_url, payload, self.timeout_s)
        import requests  # 지역 import — Codex CLI 경로에서 네트워크 의존을 만들지 않는다

        return requests.post(
            self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=self.timeout_s,
        )

    def _invoke(self, round_name: str, prompt: str, schema: dict, context_payload: dict | None,
                out_name: str | None) -> tuple[str, str]:
        if self.spent_usd() + self.estimated_cost() > self.max_monthly_usd:
            raise RunLimitReached(f"월 상한 ${self.max_monthly_usd:.2f} 도달 — API 경로를 멈춘다")
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(context_payload or {}, ensure_ascii=False)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": round_name, "strict": True, "schema": to_strict_schema(schema)},
            },
        }
        try:
            resp = self._http_post(body)
        except Exception as exc:  # noqa: BLE001 — 네트워크 오류는 재시도 대상
            return str(exc), "error"
        status_code = int(getattr(resp, "status_code", 200) or 200)
        text = getattr(resp, "text", "") or ""
        if status_code == 401:
            return text, "auth"
        if status_code == 429:
            return text, "limit"
        if status_code >= 400:
            return text, "error"
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return text, "error"
        usage = (data or {}).get("usage") or {}
        cost = (
            float(usage.get("prompt_tokens") or 0) / 1_000_000 * self.price_in_per_mtok
            + float(usage.get("completion_tokens") or 0) / 1_000_000 * self.price_out_per_mtok
        )
        if cost > 0:
            self._record_usage(cost)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return text, "error"
        return str(content), "ok"


def make_executor(provider: str = "codex", **kwargs: Any) -> BaseExecutor:
    if provider == "codex":
        return CodexCliExecutor(**kwargs)
    if provider == "openai":
        return OpenAiAdapterExecutor(**kwargs)
    raise ValueError(f"알 수 없는 provider: {provider}")


__all__ = [
    "AUTH_MARKERS", "LIMIT_MARKERS", "AuthExpired", "BaseExecutor", "CallResult", "CodexCliExecutor",
    "ExecutorError", "ExecutorUnavailable", "OpenAiAdapterExecutor", "RunLimitReached", "make_executor",
]
