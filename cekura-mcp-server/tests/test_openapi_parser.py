"""Regression tests for OpenAPI-to-MCP input-schema conversion."""

from pathlib import Path

from openapi_parser import OpenAPIParser, Operation, load_openapi_spec


ROOT = Path(__file__).resolve().parent.parent


def _operation(request_schema):
    return Operation(
        path="/test",
        method="POST",
        operation_id="test-create",
        summary=None,
        description=None,
        parameters=[],
        request_body={
            "content": {"application/json": {"schema": request_schema}}
        },
        responses={},
        tags=[],
        extensions={"x-mcp-expose": True},
    )


def _missing_array_items(schema, path="$"):
    missing = []
    if isinstance(schema, dict):
        if schema.get("type") == "array" and "items" not in schema:
            missing.append(path)
        for key, value in schema.items():
            missing.extend(_missing_array_items(value, f"{path}.{key}"))
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            missing.extend(_missing_array_items(value, f"{path}[{index}]"))
    return missing


def test_nullable_array_property_keeps_items():
    parser = OpenAPIParser("unused")
    parser.spec = {
        "components": {
            "schemas": {
                "Request": {
                    "type": "object",
                    "properties": {
                        "metric_names": {
                            "type": ["array", "null"],
                            "items": {"type": "string"},
                        }
                    },
                }
            }
        }
    }

    schema = parser.build_parameter_schema(
        _operation({"$ref": "#/components/schemas/Request"})
    )

    assert schema["properties"]["metric_names"] == {
        "description": "Property: metric_names",
        "type": "array",
        "items": {"type": "string"},
    }


def test_top_level_array_body_keeps_item_shape():
    parser = OpenAPIParser("unused")
    parser.spec = {
        "components": {
            "schemas": {
                "Metric": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                }
            }
        }
    }

    schema = parser.build_parameter_schema(
        _operation({
            "type": "array",
            "items": {"$ref": "#/components/schemas/Metric"},
        })
    )

    assert schema["properties"]["items"]["items"] == {
        "type": "object",
        "properties": {
            "name": {"description": "Property: name", "type": "string"}
        },
        "required": ["name"],
    }


def test_nullable_any_of_array_keeps_items():
    parser = OpenAPIParser("unused")
    parser.spec = {}

    schema = parser.build_parameter_schema(
        _operation({
            "type": "object",
            "properties": {
                "ids": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "integer"}},
                        {"type": "null"},
                    ]
                }
            },
        })
    )

    assert schema["properties"]["ids"]["items"] == {"type": "integer"}


def test_array_query_parameter_keeps_items():
    parser = OpenAPIParser("unused")
    parser.spec = {}
    operation = _operation({})
    operation.parameters = [{
        "name": "ids",
        "in": "query",
        "schema": {"type": "array", "items": {"type": "integer"}},
    }]

    schema = parser.build_parameter_schema(operation)

    assert schema["properties"]["ids"]["items"] == {"type": "integer"}


def test_array_without_declared_items_accepts_any_element():
    parser = OpenAPIParser("unused")
    parser.spec = {}

    schema = parser.build_parameter_schema(
        _operation({
            "type": "object",
            "properties": {
                "tags": {"type": "array"},
                "matrix": {"type": "array", "items": {"type": "array"}},
            },
        })
    )

    assert schema["properties"]["tags"]["items"] == {}
    assert schema["properties"]["matrix"]["items"] == {"type": "array", "items": {}}
    assert _missing_array_items(schema) == []


def test_every_exposed_tool_array_declares_items():
    parser = load_openapi_spec(str(ROOT.parent / "openapi.json"))
    invalid = {}

    for operation in parser.extract_operations():
        if operation.deprecated or not operation.extensions.get("x-mcp-expose"):
            continue
        schema = parser.build_parameter_schema(operation)
        missing = _missing_array_items(schema)
        if missing:
            invalid[operation.operation_id] = missing

    assert not invalid, f"MCP array schemas missing items: {invalid}"
