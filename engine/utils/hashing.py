"""engine.utils.hashing — 스냅샷·근거 검증용 해시."""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def quote_sha256(text: str) -> str:
    """인용문 원문 대신 저장하는 해시(공백·유니코드 정규화 후)."""
    import unicodedata

    norm = " ".join(unicodedata.normalize("NFKC", text).split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()
