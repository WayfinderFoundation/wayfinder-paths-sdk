"""The worker's OpenCode session bootstrap must survive a slow health probe."""

from __future__ import annotations

import pytest

from wayfinder_paths.jobs import worker


class _FlakyClient:
    def __init__(self, answers: list[bool]) -> None:
        self.answers = list(answers)
        self.created: list[str] = []

    def healthy(self) -> bool:
        return self.answers.pop(0) if self.answers else False

    def find_child_session(self, *, parent_id, title):  # noqa: ANN001
        return None

    def create_session(self, *, parent_id=None, title=None, agent=None):  # noqa: ANN001
        self.created.append(str(title))
        return "ses-created"


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(worker.time, "sleep", lambda s: slept.append(s))
    return slept


def test_worker_session_survives_a_late_health_probe(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    client = _FlakyClient([False, False, True])
    monkeypatch.setattr(worker, "OPENCODE_CLIENT", client)
    assert worker._ensure_worker_session("job-a", "monitor") == "ses-created"
    assert client.created == ["job/job-a/monitor"]
    assert len(no_sleep) == 2


def test_worker_session_gives_up_after_the_last_probe(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    client = _FlakyClient([False, False, False])
    monkeypatch.setattr(worker, "OPENCODE_CLIENT", client)
    assert worker._ensure_worker_session("job-a", "monitor") is None
    assert client.created == []
    assert len(no_sleep) == 2
