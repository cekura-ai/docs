"""Tests for the MCP HTTP client's request-body shaping."""
import gzip
import json

import httpx
import pytest

from http_client import (
    DEFAULT_MAX_UPSTREAM_RESPONSE_BYTES,
    CekuraAPIClient,
    ResponseTooLargeError,
)


@pytest.fixture
def client():
    c = CekuraAPIClient(base_url="http://example.invalid", credential="test")
    yield c


class TestParseJsonField:
    def test_string_typed_field_with_json_payload_is_not_parsed(self, client):
        # scenarios.instructions: schema is `type: string`, payload is a stringified JSON.
        # We must NOT silently coerce it into a dict.
        payload = json.dumps({"role": "customer", "conditions": [{"id": 0}]})
        out = client._parse_json_field("instructions", payload, target_type="string")
        assert out == payload
        assert isinstance(out, str)

    def test_string_typed_field_matching_legacy_pattern_is_not_parsed(self, client):
        # `metadata` matches the legacy json_field_patterns heuristic, but if the
        # schema says string, schema wins and the heuristic is suppressed.
        payload = '{"x": 1}'
        out = client._parse_json_field("metadata", payload, target_type="string")
        assert out == payload

    def test_object_typed_field_with_string_payload_is_parsed(self, client):
        payload = json.dumps({"role": "customer"})
        out = client._parse_json_field(
            "conditional_actions", payload, target_type="object"
        )
        assert out == {"role": "customer"}

    def test_array_typed_field_with_string_payload_is_parsed(self, client):
        payload = json.dumps([{"a": 1}, {"b": 2}])
        out = client._parse_json_field("items", payload, target_type="array")
        assert out == [{"a": 1}, {"b": 2}]

    def test_unknown_type_with_json_string_is_parsed(self, client):
        # oneOf/anyOf with no clear single non-null type → target_type is None.
        # Recovery path must still kick in for object/array-shaped strings.
        out = client._parse_json_field(
            "dynamic_variables", '{"x": 1}', target_type=None
        )
        assert out == {"x": 1}

    def test_legacy_pattern_match_when_no_target_type(self, client):
        # No type info, plain identifier-shaped string that just *contains* a
        # pattern keyword should still parse via the legacy heuristic.
        out = client._parse_json_field("user_metadata", '{"k": "v"}', target_type=None)
        assert out == {"k": "v"}

    def test_plain_string_passthrough(self, client):
        out = client._parse_json_field("name", "scenario name", target_type="string")
        assert out == "scenario name"

    def test_non_string_value_passthrough(self, client):
        out = client._parse_json_field("count", 42, target_type="integer")
        assert out == 42

    def test_invalid_json_with_brace_prefix_falls_through(self, client):
        # Looks like JSON but isn't — recovery must not raise; return the
        # original string unchanged.
        out = client._parse_json_field("payload", "{not json", target_type=None)
        assert out == "{not json"

    @pytest.mark.parametrize("primitive", ["integer", "number", "boolean"])
    def test_other_primitive_types_are_not_parsed(self, client, primitive):
        # A user passing a literal-looking JSON string into a primitive field is
        # almost always a mistake or sentinel value — never coerce.
        out = client._parse_json_field("field", '{"x": 1}', target_type=primitive)
        assert out == '{"x": 1}'

    def test_array_target_with_object_payload_falls_back_to_string(self, client):
        # The parsed value doesn't match the declared array type — better to
        # forward the raw string than to silently change the shape.
        out = client._parse_json_field("items", '{"a": 1}', target_type="array")
        assert out == '{"a": 1}'

    def test_object_target_with_array_payload_falls_back_to_string(self, client):
        out = client._parse_json_field(
            "conditional_actions", "[1, 2, 3]", target_type="object"
        )
        assert out == "[1, 2, 3]"


class TestCoerceBody:
    """The HTTP client only applies field-level JSON coercion now. Classification
    (path/query/body) is done upstream in the MCP server."""

    def test_string_typed_field_not_coerced(self, client):
        # `instructions` is `type: string` — even a JSON-looking value stays a string.
        payload = json.dumps({"role": "x"})
        result = client._coerce_body(
            {"name": "n", "instructions": payload},
            property_types={"name": "string", "instructions": "string"},
        )
        assert result["instructions"] == payload
        assert isinstance(result["instructions"], str)

    def test_object_typed_field_coerced_from_string(self, client):
        payload = json.dumps({"role": "x"})
        result = client._coerce_body(
            {"conditional_actions": payload},
            property_types={"conditional_actions": "object"},
        )
        assert result["conditional_actions"] == {"role": "x"}

    def test_array_body_string_coerced(self, client):
        # Top-level array body — caller unwrapped `items`, so we get a string here.
        result = client._coerce_body(
            json.dumps([{"a": 1}, {"b": 2}]),
            property_types=None,
        )
        assert result == [{"a": 1}, {"b": 2}]

    def test_array_body_list_passthrough(self, client):
        result = client._coerce_body([{"a": 1}], property_types=None)
        assert result == [{"a": 1}]

    def test_no_property_types_legacy_parse(self, client):
        # Without type info, JSON-looking strings still get parsed (legacy heuristic).
        result = client._coerce_body(
            {"instructions": '{"role": "x"}'}, property_types=None
        )
        assert result["instructions"] == {"role": "x"}


class TestSerializeQuery:
    def test_list_comma_separated(self, client):
        assert client._serialize_query({"run_ids": [1, 2, 3]}) == {"run_ids": "1,2,3"}

    def test_dict_json_serialized(self, client):
        result = client._serialize_query({"filters": {"x": 1}})
        assert result == {"filters": '{"x": 1}'}

    def test_none_dropped(self, client):
        assert client._serialize_query({"page": None, "size": 10}) == {"size": 10}

    def test_scalar_passthrough(self, client):
        assert client._serialize_query({"page": 1, "name": "x"}) == {"page": 1, "name": "x"}


def _client_returning(body: bytes, *, status_code=200, headers=None, **kwargs):
    """A client whose upstream always answers with `body`."""
    client = CekuraAPIClient(base_url="http://example.invalid", credential="test", **kwargs)

    def handler(request):
        return httpx.Response(
            status_code,
            content=body,
            headers=headers or {"Content-Type": "application/json"},
        )

    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


class TestResponseSizeCap:
    async def test_response_under_cap_is_returned(self):
        payload = {"results": [{"id": 1, "call_id": "abc"}]}
        client = _client_returning(json.dumps(payload).encode())
        assert await client.execute_request("GET", "/observability/v2/call-logs/") == payload

    async def test_response_over_cap_is_refused(self, monkeypatch):
        monkeypatch.setenv("CEKURA_MAX_UPSTREAM_RESPONSE_BYTES", "1024")
        client = _client_returning(b"x" * 2048)
        assert client.max_response_bytes == 1024
        with pytest.raises(ResponseTooLargeError) as excinfo:
            await client.execute_request("GET", "/observability/v2/call-logs/")
        # The caller is an LLM: the error has to say what to do next.
        message = str(excinfo.value)
        assert "1024" in message
        assert "page_size" in message and "ql" in message
        # And an operator has to be able to size the next cap.
        assert excinfo.value.bytes_read > 1024
        assert excinfo.value.path == "/observability/v2/call-logs/"

    async def test_default_cap_applies_without_configuration(self, monkeypatch):
        monkeypatch.delenv("CEKURA_MAX_UPSTREAM_RESPONSE_BYTES", raising=False)
        client = CekuraAPIClient(base_url="http://example.invalid", credential="test")
        assert client.max_response_bytes == DEFAULT_MAX_UPSTREAM_RESPONSE_BYTES

    async def test_error_status_still_reports_upstream_detail(self):
        # Bounding the read must not swallow the upstream error body.
        client = _client_returning(json.dumps({"detail": "bad range"}).encode(), status_code=400)
        with pytest.raises(Exception) as excinfo:
            await client.execute_request("GET", "/observability/v2/call-logs/")
        assert "bad range" in str(excinfo.value)

    async def test_gzipped_response_is_decoded_once(self):
        payload = {"results": []}
        client = _client_returning(
            gzip.compress(json.dumps(payload).encode()),
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
        )
        assert await client.execute_request("GET", "/observability/v2/call-logs/") == payload
