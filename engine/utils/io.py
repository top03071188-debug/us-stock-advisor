"""engine.utils.io — 파일·환경 로딩 유틸.

`.env` 는 **로컬 전용**이다(.gitignore). 코드는 키를 하드코딩하지 않고
`load_dotenv()` → `os.environ` 순서로만 읽는다(6.4).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from engine.utils.hashing import sha256_bytes, sha256_file  # noqa: F401  (재노출)


def load_dotenv(path: str | Path = ".env", override: bool = False) -> dict:
    """아주 단순한 .env 파서(`KEY=VALUE`, `#` 주석, 따옴표 제거)."""
    p = Path(path)
    loaded: dict[str, str] = {}
    if not p.exists():
        return loaded
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


def read_json(path: str | Path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(path: str | Path, payload) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def append_jsonl(path: str | Path, record: dict) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return p
