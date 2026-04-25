import json
from typing import Dict, List, Optional, Any
from dataclasses import dataclass


@dataclass
class Operation:
    path: str
    method: str
    operation_id: Optional[str]
    summary: Optional[str]
    description: Optional[str]
    parameters: List[Dict[str, Any]]
    request_body: Optional[Dict[str, Any]]
    responses: Dict[str, Any]
    tags: List[str]
    deprecated: bool = False


class OpenAPIParser:
    def __init__(self, spec_path: str):
        self.spec_path = spec_path
        self.spec: Dict[str, Any] = {}
        self.schema_cache: Dict[str, Any] = {}

    def load_spec(self) -> Dict[str, Any]:
        with open(self.spec_path, 'r') as f:
            self.spec = json.load(f)
        return self.spec

    def extract_operations(self) -> List[Operation]:
        operations = []
        paths = self.spec.get("paths", {})

        for path, path_item in paths.items():
            for method in ["get", "post", "put", "patch", "delete"]:
                if method in path_item:
                    operation_data = path_item[method]
                    operation = Operation(
                        path=path,
                        method=method.upper(),
                        operation_id=operation_data.get("operationId"),
                        summary=operation_data.get("summary"),
                        description=operation_data.get("description"),
                        parameters=operation_data.get("parameters", []),
                        request_body=operation_data.get("requestBody"),
                        responses=operation_data.get("responses", {}),
                        tags=operation_data.get("tags", []),
                        deprecated=operation_data.get("deprecated", False),
                    )
                    operations.append(operation)

        return operations

    def resolve_schema_ref(self, ref: str) -> Dict[str, Any]:
        if ref in self.schema_cache:
            return self.schema_cache[ref]

        if not ref.startswith("#/"):
            raise ValueError(f"Unsupported $ref format: {ref}")

        parts = ref[2:].split("/")
        schema = self.spec

        for part in parts:
            if part not in schema:
                raise ValueError(f"Cannot resolve $ref: {ref}")
            schema = schema[part]

        self.schema_cache[ref] = schema
        return schema

    def get_schema_properties(self, schema: Dict[str, Any]) -> Dict[str, Any]:
        if "$ref" in schema:
            schema = self.resolve_schema_ref(schema["$ref"])

        return schema.get("properties", {})

    def build_parameter_schema(self, operation: Operation) -> Dict[str, Any]:
        properties = {}
        required = []

        for param in operation.parameters:
            param_name = param.get("name")
            param_schema = param.get("schema", {})
            param_required = param.get("required", False)
            param_description = param.get("description", "")
            param_in = param.get("in")

            if param_in == "path":
                required.append(param_name)

            properties[param_name] = {
                "type": self._convert_openapi_type(param_schema.get("type", "string")),
                "description": param_description or f"Parameter: {param_name}",
            }

            if "enum" in param_schema:
                properties[param_name]["enum"] = param_schema["enum"]

            if "default" in param_schema:
                properties[param_name]["default"] = param_schema["default"]

            if param_required and param_in != "path":
                required.append(param_name)

        openapi_examples: List[Dict[str, Any]] = []

        if operation.request_body:
            content = operation.request_body.get("content", {})
            json_content = content.get("application/json", {})
            schema = json_content.get("schema", {})

            if schema:
                # Top-level array schema (e.g. bulk_create endpoints that accept a JSON array).
                # Expose as a single `items` parameter typed as array so the agent can pass
                # the array directly rather than seeing an empty properties object.
                resolved = schema
                if "$ref" in resolved:
                    resolved = self.resolve_schema_ref(resolved["$ref"])
                if resolved.get("type") == "array":
                    item_schema = resolved.get("items", {})
                    item_desc = "Array items"
                    if "$ref" in item_schema:
                        ref_name = item_schema["$ref"].split("/")[-1]
                        item_desc = f"{ref_name} object"
                    properties["items"] = {
                        "type": "array",
                        "description": (
                            resolved.get("description") or
                            f"List of {item_desc}s to submit. Each element has the same shape as a single create request."
                        ),
                    }
                    required.append("items")
                else:
                    body_props = self._extract_schema_properties(schema)
                    for prop_name, prop_schema in body_props.items():
                        properties[prop_name] = prop_schema

                    if "required" in schema:
                        required.extend(schema["required"])
                    elif "required" in resolved:
                        required.extend(resolved["required"])

            # drf-spectacular puts @extend_schema(examples=[OpenApiExample(...)])
            # under requestBody.content.application/json.examples as a dict keyed by
            # a CamelCased version of the example name. Each entry has:
            #   { value, summary, description }
            # We flatten into a list so downstream precedence (filter / cap) is simple.
            examples_dict = json_content.get("examples", {})
            if isinstance(examples_dict, dict):
                for key, entry in examples_dict.items():
                    if not isinstance(entry, dict):
                        continue
                    if "value" not in entry:
                        continue
                    openapi_examples.append({
                        "name": key,
                        "summary": entry.get("summary", ""),
                        "description": entry.get("description", ""),
                        "value": entry["value"],
                    })

        result: Dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "required": list(set(required)),
        }
        if openapi_examples:
            # Private key — overlay-apply layer reads this, filters/caps, and
            # replaces it with the canonical `examples` array before the schema
            # is registered. Consumers never see this key.
            result["_openapi_examples"] = openapi_examples
        return result

    def _extract_schema_properties(self, schema: Dict[str, Any]) -> Dict[str, Any]:
        if "$ref" in schema:
            schema = self.resolve_schema_ref(schema["$ref"])

        properties = {}
        for prop_name, prop_schema in schema.get("properties", {}).items():
            prop_description = prop_schema.get("description", "")

            # Resolve oneOf/anyOf to a concrete type. When one option is {} (any-type),
            # the field accepts multiple JSON types — omit the type constraint so the MCP
            # client passes values natively instead of coercing to string.
            prop_type = prop_schema.get("type")
            if prop_type is None:
                entries = prop_schema.get("oneOf") or prop_schema.get("anyOf") or []
                non_null = [e for e in entries if e and e.get("type") != "null"]
                if len(non_null) == 1 and non_null[0].get("type"):
                    prop_type = non_null[0]["type"]
                # else: leave prop_type None → no type constraint (any JSON value)

            # Resolve allOf: [{$ref: ...}] — a property typed as a named schema object.
            # Pull the description from the referenced schema if the property has none.
            if prop_type is None and prop_schema.get("allOf"):
                all_of_entries = prop_schema["allOf"]
                # Common pattern: allOf with a single $ref (possibly alongside a description).
                refs = [e for e in all_of_entries if "$ref" in e]
                if len(refs) == 1:
                    try:
                        ref_schema = self.resolve_schema_ref(refs[0]["$ref"])
                        prop_type = "object"
                        if not prop_description:
                            prop_description = ref_schema.get("description", "")
                    except (ValueError, KeyError):
                        pass

            prop_entry: Dict[str, Any] = {
                "description": prop_description or f"Property: {prop_name}",
            }
            if prop_type is not None:
                prop_entry["type"] = self._convert_openapi_type(prop_type)

            properties[prop_name] = prop_entry

            if "enum" in prop_schema:
                properties[prop_name]["enum"] = prop_schema["enum"]

            if "default" in prop_schema:
                properties[prop_name]["default"] = prop_schema["default"]

            if prop_type == "array" and "items" in prop_schema:
                properties[prop_name]["items"] = {
                    "type": self._convert_openapi_type(prop_schema["items"].get("type", "string"))
                }

        return properties

    def _convert_openapi_type(self, openapi_type) -> str:
        if isinstance(openapi_type, list):
            for t in openapi_type:
                if t != "null":
                    openapi_type = t
                    break
            else:
                openapi_type = "string"

        type_mapping = {
            "integer": "integer",
            "number": "number",
            "string": "string",
            "boolean": "boolean",
            "array": "array",
            "object": "object"
        }
        return type_mapping.get(openapi_type, "string")


def load_openapi_spec(file_path: str) -> OpenAPIParser:
    parser = OpenAPIParser(file_path)
    parser.load_spec()
    return parser
