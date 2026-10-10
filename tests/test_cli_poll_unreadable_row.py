"""The CLI's multi-claim poll: an answer this release cannot read fails that
row only, as a 410 does; the other rows keep polling."""

from __future__ import annotations

from typing import Any

from lenz_io.cli.verify import _poll_all
from lenz_io.errors import LenzInvalidResponseError
from lenz_io.models import TaskStatus


class _Client:
    def get_status(self, task_id: str) -> TaskStatus:
        if task_id == "bad":
            raise LenzInvalidResponseError(message="The API answered HTTP 200 with status completed and no result.")
        return TaskStatus(task_id=task_id, status="completed")


def test_an_unreadable_row_fails_and_the_rest_complete() -> None:
    statuses: dict[str, Any] = {}
    _poll_all(_Client(), None, [("bad", "A."), ("good", "B.")], 30, statuses, on_update=None)  # type: ignore[arg-type]
    assert statuses["bad"].status == "failed"
    assert "no result" in statuses["bad"].error
    assert statuses["good"].status == "completed"
