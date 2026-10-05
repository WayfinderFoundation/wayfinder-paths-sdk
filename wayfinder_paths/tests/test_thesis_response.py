import json

import pytest

from wayfinder_paths.core.theses.response import (
    parse_proposal_response,
    select_response_message,
)


@pytest.fixture
def continued_turn():
    return [
        {"info": {"id": "request", "role": "user"}, "parts": []},
        {
            "info": {
                "id": "working",
                "role": "assistant",
                "parentID": "request",
                "finish": "tool-calls",
            },
            "parts": [],
        },
        {"info": {"id": "compact", "role": "user"}, "parts": [{"type": "compaction"}]},
        {
            "info": {
                "id": "summary",
                "role": "assistant",
                "parentID": "request",
                "agent": "compaction",
                "summary": True,
                "finish": "stop",
            },
            "parts": [{"type": "text", "text": "An internal summary"}],
        },
        {
            "info": {"id": "continue", "role": "user"},
            "parts": [
                {
                    "type": "text",
                    "synthetic": True,
                    "metadata": {"compaction_continue": True},
                }
            ],
        },
        {
            "info": {
                "id": "answer",
                "role": "assistant",
                "parentID": "continue",
                "finish": "stop",
            },
            "parts": [{"type": "text", "text": "The requested portfolio"}],
        },
    ]


@pytest.mark.parametrize("message_id", [None, "request"])
def test_native_continuation_can_finish_an_unanswered_request(
    continued_turn, message_id
):
    assert select_response_message(continued_turn, message_id) is continued_turn[-1]
    assert select_response_message(continued_turn, "missing") == {}


@pytest.mark.parametrize("finish", ["stop", "length"])
def test_direct_terminal_answer_wins_over_post_answer_continuation(
    continued_turn, finish
):
    continued_turn[1]["info"]["finish"] = finish
    assert select_response_message(continued_turn) is continued_turn[1]


def test_continuation_cannot_finish_a_new_real_request(continued_turn):
    continued_turn.insert(-1, {"info": {"id": "repair", "role": "user"}, "parts": []})
    assert select_response_message(continued_turn) == {}
    assert select_response_message(continued_turn, "repair") == {}


def test_compaction_and_summary_are_not_answers(continued_turn):
    assert select_response_message(continued_turn[2:4]) == {}
    assert select_response_message(continued_turn[:4]) is continued_turn[1]


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
