"""engine.schema — 의존성 없는 JSON Schema(draft-07 부분집합) 검증기.

`jsonschema` 패키지를 추가하지 않기 위해 스키마 파일이 실제로 쓰는 키워드만 구현한다:
`type` · `required` · `properties` · `additionalProperties` · `items` · `enum` ·
`minimum` · `maximum` · `pattern` · `minItems` · `maxItems` · `$ref`(#/$defs/*) · `anyOf`.

스키마 파일은 `schemas/*.schema.json` 이 유일한 정의이고, 코드는 여기서 읽어 강제한다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "null": type(None),
}


class SchemaError(ValueError):
    pass


def load_schema(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise SchemaError(f"스키마 파일이 없다: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _resolve(ref: str, root: dict) -> dict:
    if not ref.startswith("#/"):
        raise SchemaError(f"외부 $ref 는 지원하지 않는다: {ref}")
    node = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _type_ok(value, expected: str) -> bool:
    if expected == "integer" and isinstance(value, bool):
        return False
    if expected == "number" and isinstance(value, bool):
        return False
    return isinstance(value, TYPES[expected])


def validate(value, schema: dict, root: dict | None = None, path: str = "$") -> list[str]:
    """오류 메시지 목록을 돌려준다(빈 목록 = 통과)."""
    root = root or schema
    errors: list[str] = []
    if "$ref" in schema:
        return validate(value, _resolve(schema["$ref"], root), root, path)
    if "anyOf" in schema:
        if not any(not validate(value, s, root, path) for s in schema["anyOf"]):
            errors.append(f"{path}: anyOf 불일치")
        return errors
    if "type" in schema:
        expected = schema["type"]
        expected_list = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(value, t) for t in expected_list):
            return [f"{path}: type {expected_list} 기대, 실제 {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} 가 enum {schema['enum']} 에 없다")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > maximum {schema['maximum']}")
    if isinstance(value, str) and "pattern" in schema:
        if not re.search(schema["pattern"], value):
            errors.append(f"{path}: pattern {schema['pattern']} 불일치")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: 필수 필드 누락")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in value:
                errors.extend(validate(value[key], sub, root, f"{path}.{key}"))
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(props))
            for key in extra:
                errors.append(f"{path}.{key}: 허용되지 않은 필드")
        elif isinstance(schema.get("additionalProperties"), dict):
            for key in sorted(set(value) - set(props)):
                errors.extend(validate(value[key], schema["additionalProperties"], root, f"{path}.{key}"))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: 항목 {len(value)} < minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: 항목 {len(value)} > maxItems {schema['maxItems']}")
        if "items" in schema:
            for i, item in enumerate(value):
                errors.extend(validate(item, schema["items"], root, f"{path}[{i}]"))
    return errors
