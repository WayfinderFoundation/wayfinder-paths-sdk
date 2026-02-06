"""Tests for CLI type coercion functions."""

from __future__ import annotations

from wayfinder_paths.mcp.cli_builder import (
    coerce_bool_string,
    coerce_by_type,
    coerce_value,
)

# --- coerce_by_type tests ---


def test_coerce_by_type_boolean_passthrough():
    assert coerce_by_type(True, "boolean") is True
    assert coerce_by_type(False, "boolean") is False


def test_coerce_by_type_string_passthrough():
    assert coerce_by_type("hello", "string") == "hello"


def test_coerce_by_type_none_type_passthrough():
    assert coerce_by_type("value", None) == "value"


def test_coerce_by_type_integer_conversion():
    assert coerce_by_type("42", "integer") == 42
    assert coerce_by_type("0", "integer") == 0
    assert coerce_by_type("-10", "integer") == -10


def test_coerce_by_type_number_conversion():
    assert coerce_by_type("3.14", "number") == 3.14
    assert coerce_by_type("0.0", "number") == 0.0
    assert coerce_by_type("-2.5", "number") == -2.5


def test_coerce_by_type_array_json_parse():
    assert coerce_by_type('["a", "b"]', "array") == ["a", "b"]
    assert coerce_by_type("[1, 2, 3]", "array") == [1, 2, 3]


def test_coerce_by_type_object_json_parse():
    assert coerce_by_type('{"key": "value"}', "object") == {"key": "value"}


def test_coerce_by_type_array_already_parsed():
    original = ["a", "b"]
    assert coerce_by_type(original, "array") is original


def test_coerce_by_type_unknown_type_passthrough():
    assert coerce_by_type("value", "unknown_type") == "value"


# --- coerce_bool_string tests ---


def test_coerce_bool_string_true_variants():
    assert coerce_bool_string("true") is True
    assert coerce_bool_string("True") is True
    assert coerce_bool_string("TRUE") is True
    assert coerce_bool_string("1") is True
    assert coerce_bool_string("yes") is True
    assert coerce_bool_string("YES") is True


def test_coerce_bool_string_false_variants():
    assert coerce_bool_string("false") is False
    assert coerce_bool_string("0") is False
    assert coerce_bool_string("no") is False
    assert coerce_bool_string("") is False


def test_coerce_bool_string_bool_passthrough():
    assert coerce_bool_string(True) is True
    assert coerce_bool_string(False) is False


# --- coerce_value tests ---


def test_coerce_value_none_passthrough():
    assert coerce_value(None, {"type": "string"}) is None


def test_coerce_value_enum_passthrough():
    # Enums already validated by Click, no conversion needed
    schema = {"type": "string", "enum": ["a", "b"]}
    assert coerce_value("a", schema) == "a"


def test_coerce_value_simple_type():
    assert coerce_value("42", {"type": "integer"}) == 42


def test_coerce_value_anyof_with_null():
    # anyOf with null and integer - should pick integer
    schema = {"anyOf": [{"type": "null"}, {"type": "integer"}]}
    assert coerce_value("10", schema) == 10


def test_coerce_value_anyof_with_boolean():
    schema = {"anyOf": [{"type": "null"}, {"type": "boolean"}]}
    assert coerce_value("true", schema) is True
    assert coerce_value("false", schema) is False


def test_coerce_value_anyof_with_number():
    schema = {"anyOf": [{"type": "null"}, {"type": "number"}]}
    assert coerce_value("3.14", schema) == 3.14


def test_coerce_value_empty_schema_passthrough():
    assert coerce_value("value", {}) == "value"
