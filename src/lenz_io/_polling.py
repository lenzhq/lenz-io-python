"""The state of the wait helpers' poll loops, shared by the sync and async
clients. Private, and pure: no request, no sleep, no clock read. The driver
(``Lenz`` or ``AsyncLenz``) reads the clock and passes ``now`` in, makes each
poll request, invokes the callbacks and sleeps for as long as the state says.

``TaskPoll`` is the verification poll (``wait``, ``verify_and_wait``,
``verify_batch_and_wait``); ``JobPoll`` the review and citation-check poll
(``review_and_wait``, ``citecheck_and_wait``). Each driver loop reads, in
order, exactly the clock values and makes exactly the requests and sleeps the
sync client always made.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Generic, TypeVar
from urllib.parse import unquote

from ._core import (
    _TERMINAL_STATUSES,
    MAX_RETRY_AFTER_SLEEP,
    POLL_BACKOFF,
    POLL_BACKOFF_CAP,
    REVIEW_POLL_DEFAULT,
    REVIEW_POLL_FLOOR,
    TimedOut,
    _note_unreadable,
    _poll_hint,
)
from .errors import (
    LenzAPIError,
    LenzApiVersionError,
    LenzAuthError,
    LenzConnectionError,
    LenzError,
    LenzGoneError,
    LenzInvalidResponseError,
    LenzNotFoundError,
    LenzRateLimitError,
)
from .models import Citecheck, Progress, ReviewFull, TaskStatus, _no_result

logger = logging.getLogger("lenz_io")

#: An async job the poll loop waits on: a review or a citation check.
_Job = TypeVar("_Job", ReviewFull, Citecheck)


def _never_sendable(exc: Exception) -> bool:
    """A request that could not be sent at all (a bad ``base_url``, ...): no
    later poll can change it, so the wait raises it at once."""
    return isinstance(exc, LenzConnectionError) and exc.retryable is False


#: The statuses a review or a citation check ends in.
_JOB_TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def _unreadable_end(exc: Exception, terminal: tuple[str, ...], job: tuple[str, str] | None = None) -> bool:
    """Whether ``exc`` is an answer this release could not read whose body
    says the run has ENDED (its ``status`` is terminal): polling again would
    only wait out the deadline and report a run that ended as still going.
    ``job``: ``(id field, id)`` the body must carry too (a review's or a
    citation check's), so another job's body, or one with no id, is polled
    again."""
    if not isinstance(exc, LenzInvalidResponseError):
        return False
    body = exc.body if isinstance(exc.body, dict) else {}
    if job is not None and body.get(job[0]) != job[1]:
        return False
    return body.get("status") in terminal


def _job_of(path: str) -> tuple[str, str] | None:
    """``(id field, id)`` of the job a poll path names (``/reviews/<id>``,
    ``/citechecks/<id>``)."""
    kind, _, segment = path.strip("/").partition("/")
    field = {"reviews": "review_id", "citechecks": "citecheck_id"}.get(kind)
    return (field, unquote(segment)) if field else None


def _progress_copy(status: TaskStatus) -> Progress:
    """What ``on_progress`` is given: a copy, so a caller cannot change the
    poll's state."""
    # ``None`` only when the API sent ``null`` to a ``legacy_aliases=False`` client.
    progress = status.progress if status.progress is not None else Progress()
    return progress.model_copy(deep=True)


class TaskPoll:
    """Round-robin poll of verification tasks until each is terminal or the
    deadline passes (the loop the sync client's ``_poll_to_terminal``
    documents).

    Per round: ``start_round()`` gives the ids to poll, in order; for each,
    ``may_poll(id, now)`` says whether a poll may start (and with what is left
    of the deadline), then the driver records the answer with ``answered`` or
    the error with ``failed``. After the round, ``round_done()`` says whether
    every id is settled, else ``next_sleep(now)`` is the sleep before the next
    round (``None``: the deadline passed, the rest timed out).
    """

    def __init__(self, task_ids: list[str], timeout: float, now: float) -> None:
        self.pending = list(task_ids)
        self.stopped: dict[str, LenzError] = {}
        self.terminal: dict[str, TaskStatus] = {}
        self.timed_out: set[str] = set()
        self.deadline = now + timeout
        self._backoff_idx = 0
        # ``timeout <= 0`` reads every id once, as in 2.x; otherwise no poll
        # starts once the deadline is spent.
        self._one_shot = timeout <= 0
        self._first_round = True
        self._still: list[str] = []
        self._server_hint: float | None = None
        self._stated_wait: float | None = None
        #: Per id, the last poll answer that could not be read, when no
        #: readable one came after it (it becomes the timeout's cause).
        self.unreadable: dict[str, LenzInvalidResponseError] = {}

    def start_round(self) -> list[str]:
        self._still = []
        self._server_hint = None
        self._stated_wait = None
        return list(self.pending)

    def may_poll(self, task_id: str, now: float) -> float | None:
        """What is left of the deadline when a poll of ``task_id`` may start,
        else ``None`` (the deadline is spent: no poll starts past it)."""
        remaining = self.deadline - now
        if remaining <= 0 and not (self._one_shot and self._first_round):
            self._still.append(task_id)
            return None
        return remaining

    def failed(self, task_id: str, exc: Exception) -> bool:
        """Record a failed poll. ``True``: the driver re-raises it.

        A 401 / 403 refuses the key itself: every other id would answer the
        same, so the whole wait ends with it. A 410, a 404 and a version
        error are terminal for this id, and so is an answer this release
        cannot read whose ``status`` says the run ended (completed, failed,
        cancelled, needs input). Any other ``LenzError`` is read again
        next round, after the wait it stated (capped like the retry ladder
        caps it). Anything else is not a poll failure and propagates."""
        if isinstance(exc, LenzAuthError) or _never_sendable(exc):
            return True
        if isinstance(exc, (LenzGoneError, LenzNotFoundError, LenzApiVersionError)) or _unreadable_end(
            exc, _TERMINAL_STATUSES
        ):
            # An unreadable answer whose status says the run ended is final
            # for this id too: no later poll reads differently.
            self.stopped[task_id] = exc  # type: ignore[assignment]
            return False
        if isinstance(exc, LenzInvalidResponseError):
            self.unreadable[task_id] = exc
        if isinstance(exc, LenzError):
            wait = getattr(exc, "retry_after", None)
            if isinstance(wait, int) and not isinstance(wait, bool) and wait > 0:
                wait_s = float(min(wait, MAX_RETRY_AFTER_SLEEP))
                self._stated_wait = wait_s if self._stated_wait is None else max(self._stated_wait, wait_s)
            self._still.append(task_id)
            return False
        return True

    def answered(self, task_id: str, status: TaskStatus) -> bool:
        """Record a poll's status. ``True``: the task is still running, so
        ``on_progress`` fires for it."""
        self.unreadable.pop(task_id, None)
        if status.status == "completed" and status.result is None:
            # Ended, but unreadable: the error ``get_status`` raises for it
            # (``_read_status``). Kept with its status, a batch row's
            # ``status_detail``.
            self.stopped[task_id] = _no_result(status)
            self.terminal[task_id] = status
            return False
        if status.status in _TERMINAL_STATUSES:
            self.terminal[task_id] = status
            return False
        self._still.append(task_id)
        hint = _poll_hint(status.progress)
        # The shortest hint wins: with a batch in flight, waiting the longest
        # one would starve the fastest claim.
        if hint is not None and (self._server_hint is None or hint < self._server_hint):
            self._server_hint = hint
        return True

    def round_done(self) -> bool:
        """End the round. ``True``: no id is left to poll."""
        self.pending = self._still
        self._first_round = False
        return not self.pending

    def next_sleep(self, now: float) -> float | None:
        """The sleep before the next round, or ``None`` when the deadline has
        passed (the ids left are timed out)."""
        remaining = self.deadline - now
        if remaining <= 0:
            self.timed_out.update(self.pending)
            return None
        if self._server_hint is not None:
            sleep_for = self._server_hint
        else:
            sleep_for = min(POLL_BACKOFF[min(self._backoff_idx, len(POLL_BACKOFF) - 1)], POLL_BACKOFF_CAP)
        if self._stated_wait is not None:
            sleep_for = max(sleep_for, self._stated_wait)
        self._backoff_idx += 1
        return min(sleep_for, remaining)

    def live(self) -> list[str]:
        """The ids no poll has settled yet (still running, as far as known)."""
        return [t for t in self.pending if t not in self.terminal and t not in self.stopped]

    def results(self) -> tuple[dict[str, TaskStatus], set[str], dict[str, LenzError]]:
        unreadable = {t: e for t, e in self.unreadable.items() if t in self.timed_out}
        return self.terminal, TimedOut(self.timed_out, unreadable), self.stopped


class JobPoll(Generic[_Job]):
    """The poll of a review or a citation check until it ends (the loop the
    sync client's ``_wait_job`` documents).

    Per poll: ``before_poll(now)`` gives what is left of the deadline (or
    raises the timeout), the driver reads the body with ``read`` (an error
    goes to ``failed``), reports a changed body to ``on_update`` when
    ``changed`` says so, and ends the wait when ``settle`` says so; then it
    sleeps ``next_sleep(now)`` (which raises the timeout when the deadline
    has passed).
    """

    def __init__(
        self,
        path: str,
        timeout: float,
        now: float,
        *,
        parse: Callable[[Any], _Job | None],
        failed: Callable[[_Job], Exception],
        timed_out: Callable[[_Job | None], Exception],
    ) -> None:
        self.path = path
        self.deadline = now + timeout
        self.last: _Job | None = None
        self._last_dump: dict[str, Any] | None = None
        self._first = timeout <= 0  # ``timeout <= 0`` reads once, as in 2.x
        self._parse: Callable[[Any], _Job | None] = parse
        self._failed: Callable[[_Job], Exception] = failed
        self._timed_out: Callable[[_Job | None], Exception] = timed_out
        self._stated_wait: float | None = None
        #: The last poll answer that could not be read, when no readable one
        #: came after it (it becomes the timeout's cause).
        self.unreadable: LenzInvalidResponseError | None = None

    def _timeout(self) -> Exception:
        err = self._timed_out(self.last)
        return _note_unreadable(err, self.unreadable) if isinstance(err, LenzError) else err

    def before_poll(self, now: float) -> float:
        """What is left of the deadline for the next poll. Once it is spent
        no poll starts (``timeout <= 0`` still reads once): the timeout is
        raised, with the last body read."""
        self._stated_wait = None
        remaining = self.deadline - now
        if remaining <= 0 and not self._first:
            raise self._timeout()
        self._first = False
        return remaining

    def read(self, body: Any) -> _Job:
        """The job's model from a 200, or ``ValueError`` for a body that is
        not this job's (a failed poll)."""
        job = self._parse(body)
        if job is None:
            raise ValueError("not this job's body")
        self.unreadable = None
        return job

    def failed(self, exc: Exception) -> bool:
        """Record a failed poll. ``True``: the driver re-raises it.

        A body this release cannot read (pydantic's ValidationError is a
        ValueError) is read again next round, as for a 5xx; a request that
        could not be built (``UnicodeEncodeError``: nothing was sent) is not,
        nor is a body this release cannot read that names this job and whose
        ``status`` says it ended (raised at once: polling on would report it
        as still running).
        A 5xx, a network error or a 429 is read again next round, after the
        wait it stated, capped like the retry ladder caps it (an untyped
        proxy 503 can state an hour). Anything else (404, 403, 410) raises."""
        if (
            isinstance(exc, UnicodeEncodeError)
            or _never_sendable(exc)
            or _unreadable_end(exc, _JOB_TERMINAL_STATUSES, _job_of(self.path))
        ):
            return True
        if isinstance(exc, LenzInvalidResponseError):
            self.unreadable = exc
        if isinstance(exc, ValueError):
            logger.debug("unreadable body for %s", self.path, exc_info=True)
            return False
        if isinstance(exc, (LenzAPIError, LenzRateLimitError)):
            wait = getattr(exc, "retry_after", None)
            self._stated_wait = float(min(wait, MAX_RETRY_AFTER_SLEEP)) if isinstance(wait, int) and wait > 0 else None
            return False
        return True

    def changed(self, job: _Job) -> bool:
        """Whether this body differs from the last one read (``on_update``
        fires only then)."""
        dump = job.model_dump()
        if dump != self._last_dump:
            self._last_dump = dump
            return True
        return False

    def settle(self, job: _Job) -> bool:
        """``True`` when the job completed; raises its failure when it failed
        or was cancelled."""
        self.last = job
        if job.status == "completed":
            return True
        if job.status in ("failed", "cancelled"):
            raise self._failed(job)
        return False

    def next_sleep(self, now: float) -> float:
        """The sleep before the next poll: the wait the server stated, else
        the job's own ``poll_after_seconds``, never under the floor, and never
        past the deadline (which, once passed, raises the timeout)."""
        remaining = self.deadline - now
        if remaining <= 0:
            raise self._timeout()
        if self._stated_wait is not None:
            sleep_for = max(REVIEW_POLL_FLOOR, self._stated_wait)
        else:
            hint = self.last.poll_after_seconds if self.last is not None else None
            sleep_for = REVIEW_POLL_DEFAULT if hint is None else max(REVIEW_POLL_FLOOR, float(hint))
        return min(sleep_for, remaining)
