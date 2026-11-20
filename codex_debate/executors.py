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
import tempfile
import threading
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


class JsonParseError(ValueError):
    """모델 출력이 **단일 JSON 객체**가 아니다(여러 객체·배열·중복 키·NaN 등)."""


@dataclass
class CallResult:
    payload: dict | None
    attempts: int
    status: str  # ok | schema_failed | timeout | limit | auth | unavailable | error
    error: str | None = None
    raw: str = ""


def _reject_constant(name: str) -> None:
    """`NaN`·`Infinity` 는 JSON 이 아니다 — 통과시키지 않는다."""
    raise JsonParseError(f"허용되지 않는 상수: {name}")


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise JsonParseError(f"중복 키: {key}")
        seen[key] = value
    return seen


def _strict_loads(text: str) -> Any:
    """단일 JSON 값만 허용하는 파서 — 상수·중복 키를 거부한다."""
    decoder = json.JSONDecoder(object_pairs_hook=_pairs_no_duplicates,
                              parse_constant=_reject_constant)
    value, end = decoder.raw_decode(text, 0)
    if text[end:].strip():
        raise JsonParseError(f"JSON 객체 뒤에 추가 내용이 있다: {text[end:].strip()[:40]!r}")
    return value


def _strip_fences(text: str) -> str:
    """단일 JSON 객체(또는 **하나의** ```json 펜스)만 꺼낸다.

    예전에는 첫 `{` ~ 마지막 `}` 를 무조건 잘라 배열·설명 혼입·다중 객체를 한 객체처럼 읽었다.
    지금은 형식이 어긋나면 `JsonParseError` 로 거부한다(감사 09).
    """
    body = (text or "").strip()
    if not body:
        raise JsonParseError("빈 출력")
    if body.startswith("```"):
        head, newline, rest = body.partition("\n")
        lang = head[3:].strip().lower()
        if lang not in ("", "json"):
            raise JsonParseError(f"지원하지 않는 코드 펜스: {lang}")
        if not newline:
            raise JsonParseError("코드 펜스 형식 오류")
        closing = rest.rfind("```")
        if closing < 0:
            raise JsonParseError("코드 펜스가 닫히지 않았다")
        if rest[closing + 3:].strip():
            raise JsonParseError("코드 펜스 뒤에 내용이 있다")
        inner = rest[:closing].strip()
        if "```" in inner:
            raise JsonParseError("코드 펜스가 여러 개다")
        body = inner
    if not body:
        raise JsonParseError("빈 출력")
    if not body.startswith("{"):
        raise JsonParseError("최상위가 JSON 객체가 아니다")
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
        timeout: float | None = None,
    ) -> tuple[str, str]:
        raise NotImplementedError

    # 공통 ---------------------------------------------------------------
    def _parse(self, raw: str, schema: dict) -> tuple[dict | None, str | None]:
        if not raw or not raw.strip():
            return None, "빈 출력"
        try:
            payload = _strict_loads(_strip_fences(raw))
        except JsonParseError as exc:
            return None, f"JSON 형식 거부: {exc}"
        except json.JSONDecodeError as exc:
            return None, f"JSON 파싱 실패: {exc}"
        if not isinstance(payload, dict):
            return None, "출력이 객체가 아니다"
        errors = schema_errors(payload, schema)
        if errors:
            return None, "; ".join(errors[:5])
        return payload, None

    def _remaining(self, deadline: datetime | None) -> float | None:
        """남은 시간(초) — 마감이 없으면 `None`(무제한)."""
        if deadline is None:
            return None
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return (deadline - datetime.now(timezone.utc)).total_seconds()

    def run(
        self,
        *,
        round_name: str,
        prompt: str,
        schema: dict | None = None,
        context_payload: dict | None = None,
        out_name: str | None = None,
        budget: Callable[[], None] | None = None,
        deadline: datetime | None = None,
    ) -> CallResult:
        schema = schema or load_schema(round_name)
        errors: list[str] = []
        parse_failures = 0
        for attempt in range(1, self.max_attempts + 1):
            # 만감 확인은 **콜 직전마다** — 한 콜이 3회 재시도로 9분을 쓸 수 있으면 마감을 넘긴다.
            remaining = self._remaining(deadline)
            if remaining is not None and remaining <= 0:
                return CallResult(None, attempt, "timeout", "전체 마감 도달 — 콜을 시작하지 않는다")
            call_timeout = float(self.timeout_s) if remaining is None else max(1.0, min(float(self.timeout_s),
                                                                                    remaining))
            if budget is not None:
                budget()
            self.calls += 1
            try:
                raw, status = self._invoke(round_name, prompt, schema, context_payload, out_name,
                                           timeout=call_timeout)
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
                errors.append(f"타임아웃({call_timeout:.0f}s)")
            else:
                errors.append(raw.strip()[:200] or status)
            if attempt < self.max_attempts:
                self._sleep(self.backoff_s * (2 ** (attempt - 1)))
                after = self._remaining(deadline)
                if after is not None and after <= 0:
                    return CallResult(None, attempt, "timeout", "전체 마감 도달 — 재시도를 중단한다")
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
        #: strict 스키마 파일 병렬 생성 잠금·캐시(라운드명 → (내용, 경로)).
        self._schema_lock = threading.Lock()
        self._schema_files: dict[str, tuple[str, Path]] = {}

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
        """`codex exec` 에 넘길 strict 스키마 파일(받지 않는 검증 키워드 제거).

        1R 4콜이 **병렬로** 부른다. 같은 `.tmp` 이름을 쓰면 한 쓰기가 다른 쓰기의 `replace` 와
        엮여 `FileNotFoundError` 가 난다(감사 09-1). 그래서 ① 한 프로세스에서 같은 라운드는 한 번만
        쓰고(메모 캐시) ② 그래도 쓰는 경우에는 **고유 임시 파일 + `os.replace`** 로 원자적으로 바꿔 낸다.
        """
        path = self.out_dir / f"{round_name}.strict.schema.json"
        payload = json.dumps(to_strict_schema(schema), ensure_ascii=False, indent=2)
        with self._schema_lock:
            cached = self._schema_files.get(round_name)
            if cached is not None and cached[0] == payload and cached[1].exists():
                return cached[1]
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f"{round_name}.strict.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        with self._schema_lock:
            self._schema_files[round_name] = (payload, path)
        return path

    def build_command(self, prompt: str, out_path: Path, schema_file: Path) -> list[str]:
        """argv 에는 **프롬프트를 넣지 않는다** — Windows 32k 인수 한도·cp949 를 피해 stdin 으로 넘긴다.

        `codex exec … -` 는 프롬프트를 표준입력에서 읽는다(감사 09-6).
        """
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
        cmd += ["-"]
        return cmd

    def _invoke(self, round_name: str, prompt: str, schema: dict, context_payload: dict | None,
                out_name: str | None, timeout: float | None = None) -> tuple[str, str]:
        out_path = self.out_dir / (out_name or f"{round_name}.json")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.unlink(missing_ok=True)  # 이전 시도의 출력을 성공으로 오인하지 않는다
        schema_file = self.strict_schema_file(round_name, schema) if schema else schema_path(round_name)
        cmd = self.build_command(prompt, out_path, schema_file)
        # 프롬프트(컨텍스트 포함)는 표준입력으로, **UTF-8 로 명시**해 넘긴다.
        stdin_text = prompt if prompt else json.dumps(context_payload or {}, ensure_ascii=False)
        try:
            proc = self._runner(
                cmd,
                input=stdin_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s if timeout is None else max(1.0, float(timeout)),
            )
        except OSError as exc:  # 실행 파일이 사라졌거나 권한이 없다
            raise ExecutorUnavailable(f"codex 실행 실패: {exc}") from exc
        except subprocess.TimeoutExpired:
            raise
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

    def _http_post(self, payload: dict, timeout: float | None = None) -> Any:
        limit = self.timeout_s if timeout is None else max(1.0, float(timeout))
        if self._poster is not None:
            return self._poster(self.base_url, payload, limit)
        import requests  # 지역 import — Codex CLI 경로에서 네트워크 의존을 만들지 않는다

        return requests.post(
            self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=limit,
        )

    def _invoke(self, round_name: str, prompt: str, schema: dict, context_payload: dict | None,
                out_name: str | None, timeout: float | None = None) -> tuple[str, str]:
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
            resp = self._http_post(body, timeout=timeout)
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
