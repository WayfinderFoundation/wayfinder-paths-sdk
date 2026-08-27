from __future__ import annotations

import json
import sqlite3

from wayfinder_paths.jobs.benchmarks.agent_adapter import meter_session_ids


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

    assert totals["tokens_in"] == 10
    assert totals["tokens_out"] == 3
    assert totals["tokens_reasoning"] == 2
    assert totals["tokens_cache_read"] == 90
    assert totals["tokens_cache_write"] == 4
    assert totals["tool_calls"] == 1
    assert totals["tool_result_bytes"] == len(tool_data)
    assert totals["tool_result_bytes_by_tool"] == {"read": len(tool_data)}
