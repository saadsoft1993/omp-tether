"""A small JSON Schema validator for the subset the schemas in `omp_dashboard/schemas/` use.

The schema files are standard JSON Schema (draft 2020-12 vocabulary), so any other validator accepts them too; this one
exists because the plugin has no dependencies. It supports: $ref (local "#/$defs/..."), type, enum, const, properties,
required, additionalProperties (false or a schema), items, minItems, maxItems, uniqueItems, minimum, maximum,
minLength, maxLength, pattern and oneOf. A schema that uses any other validating keyword is refused, so a schema
cannot silently rely on a check this validator does not perform.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Any

IGNORED = {"$schema", "$id", "$defs", "$comment", "title", "description", "default", "examples"}
SUPPORTED = {
    "$ref",
    "type",
    "enum",
    "const",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "pattern",
    "oneOf",
}

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


class SchemaError(Exception):
    """The schema itself is unusable (unknown keyword, bad reference)."""


@functools.cache
def load_schema(name: str) -> dict[str, Any]:
    """A schema from `omp_dashboard/schemas/`, read once per process (callers must not modify it)."""
    return json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))


def _anchor(pattern: str) -> str:
    """Python's `$` also matches just before a final newline, which would let "name\\n" pass a `^...$` pattern that the
    rest of the code (and every other validator) treats as a plain name. A trailing `$` is made `\\Z`."""
    return pattern[:-1] + r"\Z" if pattern.endswith("$") and not pattern.endswith("\\$") else pattern


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "null":
        return value is None
    raise SchemaError(f"unknown type {expected!r}")


def _resolve(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise SchemaError(f"only local references are supported: {ref}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def check_schema_keywords(schema: Any, where: str = "$") -> None:
    """Raise SchemaError when `schema` uses a keyword this validator does not implement."""
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        raise SchemaError(f"{where}: a schema must be an object")
    for key, value in schema.items():
        if key in IGNORED and key != "$defs":
            continue
        if key == "$defs":
            for name, sub in value.items():
                check_schema_keywords(sub, f"{where}/$defs/{name}")
        elif key not in SUPPORTED:
            raise SchemaError(f"{where}: unsupported keyword {key!r}")
        elif key == "properties":
            for name, sub in value.items():
                check_schema_keywords(sub, f"{where}/properties/{name}")
        elif key == "items":
            check_schema_keywords(value, f"{where}/items")
        elif key == "additionalProperties":
            check_schema_keywords(value, f"{where}/additionalProperties")
        elif key == "oneOf":
            for index, sub in enumerate(value):
                check_schema_keywords(sub, f"{where}/oneOf/{index}")


def validate(instance: Any, schema: dict[str, Any], root: dict[str, Any] | None = None, path: str = "$") -> list[str]:
    """Every way `instance` fails `schema`, as 'path: message' strings (empty when it is valid)."""
    root = root if root is not None else schema
    if schema is True:
        return []
    if schema is False:
        return [f"{path}: not allowed"]
    if "$ref" in schema:
        return validate(instance, _resolve(schema["$ref"], root), root, path)
    errors: list[str] = []
    expected = schema.get("type")
    if expected is not None:
        names = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(instance, n) for n in names):
            return [f"{path}: expected {' or '.join(names)}, got {_describe(instance)}"]
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: must be {json.dumps(schema['const'])}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: must be one of {', '.join(json.dumps(v) for v in schema['enum'])}")
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']} characters")
        if "pattern" in schema and re.search(_anchor(schema["pattern"]), instance) is None:
            errors.append(f"{path}: does not match the required pattern")
    if isinstance(instance, int | float) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: below the minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: above the maximum {schema['maximum']}")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if schema.get("uniqueItems") and len({json.dumps(v, sort_keys=True) for v in instance}) != len(instance):
            errors.append(f"{path}: items must be unique")
        if "items" in schema:
            for index, item in enumerate(instance):
                errors.extend(validate(item, schema["items"], root, f"{path}[{index}]"))
    if isinstance(instance, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in instance:
                errors.append(f"{path}: missing required field {name!r}")
        for name, value in instance.items():
            if name in props:
                errors.extend(validate(value, props[name], root, f"{path}.{name}"))
            elif schema.get("additionalProperties", True) is False:
                errors.append(f"{path}: unknown field {name!r}")
            elif isinstance(schema.get("additionalProperties"), dict):
                errors.extend(validate(value, schema["additionalProperties"], root, f"{path}.{name}"))
    if "oneOf" in schema:
        errors.extend(_one_of(instance, schema["oneOf"], root, path))
    return errors


def _one_of(instance: Any, options: list[dict[str, Any]], root: dict[str, Any], path: str) -> list[str]:
    results = [validate(instance, option, root, path) for option in options]
    matching = [r for r in results if not r]
    if len(matching) == 1:
        return []
    if len(matching) > 1:
        return [f"{path}: matches more than one alternative"]
    # No alternative fits. Report the one the `type` discriminator points at, if there is one.
    if isinstance(instance, dict) and "type" in instance:
        for option, errors in zip(options, results, strict=True):
            resolved = _resolve(option["$ref"], root) if "$ref" in option else option
            const = ((resolved.get("properties") or {}).get("type") or {}).get("const")
            if const == instance["type"]:
                return errors
        return [f"{path}: unknown type {_describe(instance['type'])}"]
    return [f"{path}: matches none of the allowed shapes"]


def _describe(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"
