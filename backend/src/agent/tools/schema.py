"""轻量 JSON Schema 校验器（不引入 jsonschema 依赖）。

仅支持管线输出校验所需的最小子集：
    object / string / number / integer / boolean / array
    required / properties / items
未知关键字与 additionalProperties 一律宽松（只校验声明的属性）。
返回错误信息列表，空列表 = 校验通过。
"""

from __future__ import annotations

from typing import Any

_TYPE_ERROR = "type"


def validate(value: Any, schema: dict | None, path: str = "$") -> list[str]:
    """校验 value 是否符合 schema；返回错误描述列表（空 = 通过）。"""
    if not schema:
        return []
    schema_type = schema.get("type")
    if schema_type is None:
        errors: list[str] = []
        if "properties" in schema or "required" in schema:
            errors.extend(_validate_object(value, schema, path))
        return errors

    if schema_type == "object":
        if not isinstance(value, dict):
            return [_type_error(path, "object", value)]
        return _validate_object(value, schema, path)
    if schema_type == "string":
        if not isinstance(value, str):
            return [_type_error(path, "string", value)]
        return []
    if schema_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [_type_error(path, "number", value)]
        return []
    if schema_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return [_type_error(path, "integer", value)]
        return []
    if schema_type == "boolean":
        if not isinstance(value, bool):
            return [_type_error(path, "boolean", value)]
        return []
    if schema_type == "array":
        if not isinstance(value, list):
            return [_type_error(path, "array", value)]
        errors: list[str] = []
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                errors.extend(validate(item, items, f"{path}[{index}]"))
        return errors
    # 未知类型：宽松放行
    return []


def _validate_object(value: dict, schema: dict, path: str) -> list[str]:
    errors: list[str] = []
    for key in schema.get("required", []):
        if key not in value:
            errors.append(f"{path}.{key}: required field missing")
    for key, sub in (schema.get("properties") or {}).items():
        if key in value:
            errors.extend(validate(value[key], sub, f"{path}.{key}"))
    return errors


def _type_error(path: str, expected: str, value: Any) -> str:
    got = type(value).__name__
    return f"{path}: expected {expected}, got {got}"
