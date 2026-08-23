from __future__ import annotations

import copy
from typing import Any, Dict, Set

ALLOWED_STRING_FORMATS = {
    "email",
    "hostname",
    "ipv4",
    "ipv6",
    "uuid",
}

STRIP_KEYS = {
    "title",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "uniqueItems",
    "examples",
    "deprecated",
    "readOnly",
    "writeOnly",
    "prefixItems",
    "unevaluatedProperties",
    "default",
    "pattern",
}


def to_strict_json_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Rewrite a Pydantic JSON Schema into DeepSeek strict-tool subset."""
    data = copy.deepcopy(schema)
    defs = data.pop("$defs", None) or data.pop("definitions", None)
    if defs:
        data["$def"] = {
            name: _strict_node(node) for name, node in defs.items()
        }
    data = _strict_node(data)
    _rewrite_refs(data)
    defs = data.get("$def") or {}
    _fix_anyof_types(data, defs)
    return data


def _rewrite_refs(node: Any) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            node["$ref"] = (
                ref.replace("#/$defs/", "#/$def/")
                .replace("#/definitions/", "#/$def/")
            )
        for value in node.values():
            _rewrite_refs(value)
    elif isinstance(node, list):
        for item in node:
            _rewrite_refs(item)


def _strict_node(node: Any) -> Any:
    if not isinstance(node, dict):
        return node

    for key in list(node):
        if key in STRIP_KEYS:
            node.pop(key)

    fmt = node.get("format")
    if isinstance(fmt, str) and fmt not in ALLOWED_STRING_FORMATS:
        node.pop("format")

    if "oneOf" in node and "anyOf" not in node:
        node["anyOf"] = node.pop("oneOf")

    for key in ("anyOf", "allOf"):
        if key in node and isinstance(node[key], list):
            node[key] = [_strict_node(item) for item in node[key]]

    if "$ref" in node:
        return node

    if "items" in node:
        node["items"] = _strict_node(node["items"])

    if "$def" in node and isinstance(node["$def"], dict):
        node["$def"] = {
            name: _strict_node(item) for name, item in node["$def"].items()
        }

    if _is_object(node):
        node["type"] = "object"
        node["additionalProperties"] = False
        properties = node.get("properties")
        if not isinstance(properties, dict):
            node["properties"] = {}
            node["required"] = []
        else:
            node["properties"] = {
                name: _strict_node(item) for name, item in properties.items()
            }
            node["required"] = list(node["properties"].keys())

    return node


def _inline_ref(item: Dict[str, Any], defs: Dict[str, Any]) -> Dict[str, Any]:
    name = str(item["$ref"]).split("/")[-1]
    target = copy.deepcopy(defs.get(name) or {"type": "object"})
    extra = {key: value for key, value in item.items() if key != "$ref"}
    extra.pop("type", None)
    target.update(extra)
    return target


def _fix_anyof_types(node: Any, defs: Dict[str, Any]) -> None:
    """DeepSeek rejects anyOf branches (and the field itself) without `type`.

    `$ref` plus `type: object` is treated as an empty object, so refs inside
    anyOf are inlined instead of annotated.
    """
    if isinstance(node, dict):
        if isinstance(node.get("anyOf"), list):
            inlined = []
            for item in node["anyOf"]:
                if isinstance(item, dict) and "$ref" in item:
                    inlined.append(_inline_ref(item, defs))
                else:
                    inlined.append(item)
            node["anyOf"] = inlined
        for value in node.values():
            _fix_anyof_types(value, defs)
    elif isinstance(node, list):
        for item in node:
            _fix_anyof_types(item, defs)


def _is_object(node: Dict[str, Any]) -> bool:
    if "anyOf" in node or "allOf" in node:
        return "properties" in node
    if "properties" in node:
        return True
    types = node.get("type")
    if types == "object":
        return True
    if isinstance(types, list) and "object" in types:
        return True
    return False


def assert_strict_compatible(schema: Dict[str, Any]) -> None:
    """Raise ValueError if a schema is not DeepSeek-strict compatible."""
    seen: Set[int] = set()
    _assert_node(schema, seen, schema.get("$def") or {})


def _assert_node(
    node: Any, seen: Set[int], defs: Dict[str, Any]
) -> None:
    if not isinstance(node, dict):
        return
    marker = id(node)
    if marker in seen:
        return
    seen.add(marker)

    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$def/"):
        name = ref.split("/")[-1]
        if name in defs:
            _assert_node(defs[name], seen, defs)
        return

    for key in ("anyOf", "allOf"):
        if isinstance(node.get(key), list):
            for item in node[key]:
                if isinstance(item, dict) and "type" not in item:
                    raise ValueError("anyOf/allOf branch missing type")
                _assert_node(item, seen, defs)

    if _is_object(node):
        if node.get("additionalProperties") is not False:
            raise ValueError("object must set additionalProperties=false")
        properties = node.get("properties") or {}
        required = node.get("required") or []
        missing = [name for name in properties if name not in required]
        if missing:
            raise ValueError(
                "object properties must all be required: {}".format(missing)
            )
        for item in properties.values():
            _assert_node(item, seen, defs)

    if "items" in node:
        _assert_node(node["items"], seen, defs)
    if "$def" in node and isinstance(node["$def"], dict):
        for item in node["$def"].values():
            _assert_node(item, seen, defs)
