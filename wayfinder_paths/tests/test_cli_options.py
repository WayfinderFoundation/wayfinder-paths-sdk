"""Tests for CLI Click option generation."""

from __future__ import annotations

import click

from wayfinder_paths.mcp.cli_builder import make_click_option


def test_make_click_option_boolean_flag():
    opt = make_click_option("verbose", {"type": "boolean", "default": False})
    # Boolean flags have secondary_opts for the --no- variant
    assert opt.is_flag is True
    assert opt.default is False


def test_make_click_option_boolean_flag_required():
    opt = make_click_option("force", {"type": "boolean"}, required=True)
    assert opt.is_flag is True
    assert opt.required is True


def test_make_click_option_enum_choice():
    opt = make_click_option("mode", {"enum": ["a", "b", "c"]})
    assert isinstance(opt.type, click.Choice)
    assert list(opt.type.choices) == ["a", "b", "c"]


def test_make_click_option_string_type():
    opt = make_click_option("name", {"type": "string", "description": "A name"})
    # Click accepts str as a valid type
    assert opt.type.convert("hello", None, None) == "hello"
    assert "A name" in opt.help


def test_make_click_option_integer_type():
    opt = make_click_option("count", {"type": "integer", "default": 5})
    assert opt.type.convert("42", None, None) == 42
    assert opt.default == 5


def test_make_click_option_number_type():
    opt = make_click_option("rate", {"type": "number"})
    assert opt.type.convert("3.14", None, None) == 3.14


def test_make_click_option_complex_type_as_string():
    # Arrays and objects are accepted as strings for JSON parsing
    opt = make_click_option("items", {"type": "array"})
    assert opt.type.convert('["a"]', None, None) == '["a"]'

    opt = make_click_option("config", {"type": "object"})
    assert opt.type.convert('{"k": "v"}', None, None) == '{"k": "v"}'


def test_make_click_option_anyof_as_string():
    # anyOf schemas are accepted as strings
    opt = make_click_option("value", {"anyOf": [{"type": "null"}, {"type": "integer"}]})
    assert opt.type.convert("123", None, None) == "123"


def test_make_click_option_required_with_no_default():
    opt = make_click_option("name", {"type": "string"}, required=True)
    assert opt.required is True


def test_make_click_option_required_with_default_is_not_required():
    # If there's a default, required is ignored
    opt = make_click_option(
        "name", {"type": "string", "default": "default"}, required=True
    )
    assert opt.required is False
