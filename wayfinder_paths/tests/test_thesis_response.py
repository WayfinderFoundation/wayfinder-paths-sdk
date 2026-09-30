import json

import pytest

from wayfinder_paths.core.theses.response import parse_proposal_response


@pytest.mark.parametrize(
    "wrapper",
    [
        "{}",
        "  {}\n",
        "```json\n{}\n```",
        "```\n{}\n```",
        "Research is complete.\n{}",
        "Research is complete.\n\n```json\n{}\n```",
        "Research is complete.\n```json\r\n{}\r\n```",
    ],
)
def test_presentation_does_not_change_portfolio_data(wrapper: str) -> None:
    # Invalid schema is deliberately retained for the caller's precise feedback.
    payload = {
        "assumptions": [f"Assumption {i}" for i in range(9)],
        "capital_bps": 1234,
        "cash_bps": 8766,
        "rationale": 'Braces { and } and ``` inside a "quoted" value stay intact.',
    }
    assert parse_proposal_response(wrapper.format(json.dumps(payload))) == payload


@pytest.mark.parametrize(
    "text",
    [
        "Want me to build this?",
        "",
        "null",
        "[]",
        '[{"title":"array"}]',
        '{"title":"first"} {"title":"second"}',
        '```json\n{"title":"unfinished"}',
        '```python\n{"title":"not json"}\n```',
        '```json\n{"title":"first"}\n```\n```json\n{"title":"second"}\n```',
        '{"title":"truncated',
        '{"title":"candidate"}\nDo not use this portfolio.',
        '```json\n{"title":"candidate"}\n```\nUse different allocations instead.',
    ],
)
def test_malformed_or_ambiguous_payload_requires_agent_repair(text: str) -> None:
    with pytest.raises(ValueError):
        parse_proposal_response(text)
