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
                        tags=operation_data.get("tags", [])
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

        if operation.request_body:
            content = operation.request_body.get("content", {})
            json_content = content.get("application/json", {})
            schema = json_content.get("schema", {})

            if schema:
                body_props = self._extract_schema_properties(schema)
                for prop_name, prop_schema in body_props.items():
                    properties[prop_name] = prop_schema

                if "required" in schema:
                    required.extend(schema["required"])

        return {
            "type": "object",
            "properties": properties,
            "required": list(set(required))
        }

    def _extract_schema_properties(self, schema: Dict[str, Any]) -> Dict[str, Any]:
        if "$ref" in schema:
            schema = self.resolve_schema_ref(schema["$ref"])

        properties = {}
        for prop_name, prop_schema in schema.get("properties", {}).items():
            prop_type = prop_schema.get("type", "string")
            prop_description = prop_schema.get("description", "")

            properties[prop_name] = {
                "type": self._convert_openapi_type(prop_type),
                "description": prop_description or f"Property: {prop_name}",
            }

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
