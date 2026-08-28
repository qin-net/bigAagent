from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Tuple, Type
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, create_model

JsonSchema = Dict[str, Any]


def model_from_json_schema(
    schema: JsonSchema, *, name: Optional[str] = None
) -> Type[BaseModel]:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("output_schema must be a JSON object schema")
    model_name = name or "CallOutput{}".format(uuid4().hex[:8])
    return _object_model(schema, model_name)


def _object_model(schema: JsonSchema, name: str) -> Type[BaseModel]:
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict) or not properties:
        raise ValueError("output_schema.properties is required")
    required = set(schema.get("required") or [])
    fields: Dict[str, Tuple[Any, Any]] = {}
    for key, spec in properties.items():
        if not isinstance(spec, dict):
            raise ValueError("property {} must be an object".format(key))
        annotation = _annotation(spec, "{}_{}".format(name, key))
        if key in required:
            fields[key] = (annotation, ...)
        else:
            fields[key] = (Optional[annotation], Field(default=None))

    class Forbidden(BaseModel):
        model_config = ConfigDict(extra="forbid")

    return create_model(name, __base__=Forbidden, **fields)


def _annotation(spec: JsonSchema, name: str) -> Any:
    enum_values = spec.get("enum")
    if isinstance(enum_values, list) and enum_values:
        if not all(isinstance(item, str) for item in enum_values):
            raise ValueError("only string enums are supported")
        return Literal.__getitem__(tuple(str(item) for item in enum_values))

    type_name = spec.get("type")
    if type_name == "string":
        return str
    if type_name == "boolean":
        return bool
    if type_name == "integer":
        return int
    if type_name == "number":
        return float
    if type_name == "array":
        items = spec.get("items") or {"type": "string"}
        return List[_annotation(items, name + "Item")]  # type: ignore[name-defined]
    if type_name == "object" or "properties" in spec:
        return _object_model(spec, name)
    raise ValueError("unsupported schema type: {}".format(type_name))
