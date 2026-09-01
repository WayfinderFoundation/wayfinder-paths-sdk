from __future__ import annotations

import json
import sqlite3

from wayfinder_paths.jobs.benchmarks.agent_adapter import (
    meter_session_ids,
    resolve_session_db,
    session_diagnostic_summary,
)


def test_session_db_path_honors_explicit_override(tmp_path, monkeypatch) -> None:
    expected = tmp_path / "opencode.db"
    monkeypatch.setenv("OPENCODE_DB_PATH", str(expected))

    assert resolve_session_db() == expected


def test_session_meter_includes_cache_tokens_and_tool_payload_bytes(tmp_path) -> None:
    session_db = tmp_path / "opencode.db"
    connection = sqlite3.connect(session_db)
    connection.executescript(
        """
        CREATE TABLE message (
          id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT
        );
        CREATE TABLE part (
          id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
          time_created INTEGER, data TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO message VALUES (?,?,?,?)",
        (
            "message-1",
            "session-1",
            200,
            json.dumps(
                {
                    "tokens": {
                        "input": 10,
                        "output": 3,
                        "reasoning": 2,
                        "cache": {"read": 90, "write": 4},
                    }
                }
            ),
        ),
    )
    tool_data = json.dumps(
        {"type": "tool", "tool": "read", "state": {"output": "x" * 50}}
    )
    connection.execute(
        "INSERT INTO part VALUES (?,?,?,?,?)",
        ("part-1", "message-1", "session-1", 200, tool_data),
    )
    connection.commit()
    connection.close()

    totals = meter_session_ids(["session-1"], since_ms=100, session_db=session_db)

    assert totals["sessions"] == 1
    assert totals["tokens_in"] == 10
    assert totals["tokens_out"] == 3
    assert totals["tokens_reasoning"] == 2
    assert totals["tokens_cache_read"] == 90
    assert totals["tokens_cache_write"] == 4
    assert totals["tool_calls"] == 1
    assert totals["tool_result_bytes"] == len(tool_data)
    assert totals["tool_result_bytes_by_tool"] == {"read": len(tool_data)}
    assert totals["tool_output_bytes"] == 50
    assert totals["tool_output_bytes_by_tool"] == {"read": 50}
    assert totals["wall_seconds"] == 0.0


def test_session_meter_splits_model_tool_and_other_wall_time(tmp_path) -> None:
    session_db = tmp_path / "opencode.db"
    connection = sqlite3.connect(session_db)
    connection.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY);
        CREATE TABLE message (
          id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT
        );
        CREATE TABLE part (
          id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
          time_created INTEGER, data TEXT
        );
        """
    )
    connection.execute("INSERT INTO session VALUES (?)", ("session-1",))
    connection.execute(
        "INSERT INTO message VALUES (?,?,?,?)",
        (
            "message-1",
            "session-1",
            1_000,
            json.dumps(
                {
                    "role": "assistant",
                    "time": {"created": 1_000, "completed": 2_750},
                    "tokens": {},
                }
            ),
        ),
    )
    connection.execute(
        "INSERT INTO part VALUES (?,?,?,?,?)",
        (
            "tool-1",
            "message-1",
            "session-1",
            2_250,
            json.dumps(
                {
                    "type": "tool",
                    "tool": "read",
                    "state": {
                        "output": "hello",
                        "time": {"start": 2_250, "end": 2_750},
                    },
                }
            ),
        ),
    )
    connection.commit()
    connection.close()

    totals = meter_session_ids(["session-1"], session_db=session_db)

    assert totals["wall_seconds"] == 1.75
    # OpenCode assistant intervals contain their tool calls. The split removes
    # that overlap rather than billing the same 500ms as both model and tool.
    assert totals["model_seconds"] == 1.25
    assert totals["tool_seconds"] == 0.5
    assert totals["other_seconds"] == 0.0
    assert totals["tool_output_bytes"] == 5


def test_session_meter_does_not_count_unknown_session(tmp_path) -> None:
    session_db = tmp_path / "opencode.db"
    connection = sqlite3.connect(session_db)
    connection.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY);
        CREATE TABLE message (
          id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT
        );
        CREATE TABLE part (
          id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
          time_created INTEGER, data TEXT
        );
        """
    )
    connection.commit()
    connection.close()

    totals = meter_session_ids(["missing"], session_db=session_db)

    assert totals["sessions"] == 0


def test_session_diagnostics_are_bounded_and_exclude_tool_payloads(tmp_path) -> None:
    session_db = tmp_path / "opencode.db"
    connection = sqlite3.connect(session_db)
    connection.executescript(
        """
        CREATE TABLE message (
          id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT
        );
        CREATE TABLE part (
          id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
          time_created INTEGER, data TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO message VALUES (?,?,?,?)",
        ("assistant-1", "session-1", 100, json.dumps({"role": "assistant"})),
    )
    for index in range(30):
        connection.execute(
            "INSERT INTO part VALUES (?,?,?,?,?)",
            (
                f"tool-{index}",
                "assistant-1",
                "session-1",
                100 + index,
                json.dumps(
                    {
                        "type": "tool",
                        "tool": "wayfinder_core_jobs",
                        "state": {
                            "status": "completed",
                            "input": {
                                "action": "evolution_prepare",
                                "secret": "must-not-survive",
                            },
                            "output": "x" * 10_000,
                            "error": "e" * 500 if index == 29 else None,
                        },
                    }
                ),
            ),
        )
    connection.execute(
        "INSERT INTO part VALUES (?,?,?,?,?)",
        (
            "text-1",
            "assistant-1",
            "session-1",
            200,
            json.dumps({"type": "text", "text": "z" * 2_000}),
        ),
    )
    connection.commit()
    connection.close()

    summary = session_diagnostic_summary("session-1", session_db=session_db)

    assert len(summary["tool_calls"]) == 25
    assert summary["omitted_tool_calls"] == 5
    assert summary["tool_calls"][-1] == {
        "tool": "wayfinder_core_jobs",
        "status": "completed",
        "action": "evolution_prepare",
        "error": "e" * 300,
    }
    assert summary["final_assistant_text"] == "z" * 1_500
    assert "must-not-survive" not in json.dumps(summary)
    assert "x" * 100 not in json.dumps(summary)
