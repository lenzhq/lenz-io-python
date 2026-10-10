"""Every public call form, with scripted answers, for the request freeze.

Each case is ``(name, client, call, answers)``: ``client`` names a factory in
``freeze_harness.CLIENTS``, ``call`` runs the public method, ``answers`` maps
``(method, path)`` to the scripted answers. ``test_request_freeze.py`` records
what each case sends (URL, raw body, ordered headers, the four timeout
components, sleeps) and compares it with ``fixtures/freeze/requests.json``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from freeze_harness import PINNED, Answer, Raw, Slow

from lenz_io import Lenz

_CONTRACT = Path(__file__).parent / "fixtures" / "contract"


def _load(name: str) -> Any:
    return json.loads((_CONTRACT / name).read_text())


TASK = "t1"
ACCEPTED = (200, {"task_id": TASK, "claim_text": "A."})
DONE = (200, {"status": "completed", "task_id": TASK, "result": {"verification_id": "v1", "claim": "A."}})
DONE2 = (200, {"status": "completed", "task_id": "t2", "result": {"verification_id": "v2", "claim": "B."}})
RUNNING = (200, {"status": "processing", "task_id": TASK, "progress": {"step": "research", "poll_after_seconds": 3}})
RUNNING_NO_HINT = (200, {"status": "processing", "task_id": TASK, "progress": {"step": "research"}})
BATCH = (200, {"batch_id": "b1", "items": [{"task_id": TASK, "claim": "A."}, {"task_id": "t2", "claim": "B."}]})
EXTRACTED = (200, {"claims": [], "status": "ok"})
ASSESSED = (200, {"claims": []})
SELECTED = (200, {"items": []})
REVIEW_DONE = _load("review_completed.json")
REVIEW_RUNNING = _load("review_assessing.json")
RID = REVIEW_DONE["review_id"]
CHECK_DONE = _load("citecheck_completed.json")
CID = CHECK_DONE["citecheck_id"]
CANCEL_TASK = _load("cancel_verify_cancelled.json")
CANCEL_REVIEW = _load("cancel_review_cancelled.json")
CANCEL_CHECK = _load("cancel_citecheck_cancelled.json")
_ITEM = _load("verifications_list.json")["items"][0]
PAGE_1 = (200, {"items": [_ITEM], "total": 2, "page": 1, "page_size": 1})
PAGE_2 = (200, {"items": [_ITEM], "total": 2, "page": 2, "page_size": 1})
LIB_ITEM = {"verification_id": "lib1", "claim": "A."}
LIB_1 = (200, {"items": [LIB_ITEM], "total": 2, "page": 1, "page_size": 1})
LIB_2 = (200, {"items": [LIB_ITEM], "total": 2, "page": 2, "page_size": 1})
DETAIL = (200, _load("verifications_detail.json"))
CERT = (200, _load("certificate.json"))
USAGE = (200, _load("usage.json"))
ASK_REPLY = (200, {"role": "expert", "content": "Because."})
ASK_HISTORY = (200, {"messages": []})
RELATED = (200, {"items": []})
DRAFT = "Draft text."

S503 = (503, {"detail": "busy"})
CONFLICT = (409, {"code": "idempotency_conflict", "detail": "in flight"}, {"Retry-After": "2"})
LIMITED = (429, {"code": "rate_limited", "detail": "slow down"}, {"Retry-After": "2"})
NOT_FOUND = (404, {"code": "not_found", "detail": "nope"})


def _drop() -> Exception:
    return httpx.ConnectError("connection refused")


Case = tuple[str, str, Callable[[Lenz], Any], dict[tuple[str, str], list[Answer]]]

CASES: list[Case] = [
    # ── submits ──
    ("verify_pinned", "default", lambda c: c.verify("A.", idempotency_key=PINNED), {("POST", "/verify"): [ACCEPTED]}),
    ("verify_random_key", "default", lambda c: c.verify(claim="A."), {("POST", "/verify"): [ACCEPTED]}),
    ("verify_no_key", "default", lambda c: c.verify("A.", idempotency=False), {("POST", "/verify"): [ACCEPTED]}),
    (
        "verify_options",
        "default",
        lambda c: c.verify(
            "A.",
            language="es",
            visibility="unlisted",
            depth="low",
            source_url="https://e.x/a",
            webhook_url="https://e.x/h",
        ),
        {("POST", "/verify"): [ACCEPTED]},
    ),
    (
        "verify_batch",
        "default",
        lambda c: c.verify_batch(claims=[{"claim": "A."}, {"text": "B."}], idempotency_key=PINNED, language="de"),
        {("POST", "/verify/batch"): [BATCH]},
    ),
    ("extract", "default", lambda c: c.extract(text="Doc."), {("POST", "/extract"): [EXTRACTED]}),
    (
        "extract_options",
        "default",
        lambda c: c.extract(text="Doc.", language="it", focus="figures", locate=True, idempotency_key=PINNED),
        {("POST", "/extract"): [EXTRACTED]},
    ),
    ("extract_timeout_5", "default", lambda c: c.extract(text="Doc.", timeout=5), {("POST", "/extract"): [EXTRACTED]}),
    (
        "extract_timeout_obj",
        "default",
        lambda c: c.extract(text="Doc.", timeout=httpx.Timeout(7, read=300)),  # type: ignore[arg-type]
        {("POST", "/extract"): [EXTRACTED]},
    ),
    ("assess_claim", "default", lambda c: c.assess("A."), {("POST", "/assess"): [ASSESSED]}),
    (
        "assess_claims",
        "default",
        lambda c: c.assess(claims=["A.", "B."], language="auto", suggest_rewrite=True, idempotency_key=PINNED),
        {("POST", "/assess"): [ASSESSED]},
    ),
    ("assess_timeout_20", "default", lambda c: c.assess("A.", timeout=20), {("POST", "/assess"): [ASSESSED]}),
    (
        "select",
        "default",
        lambda c: c.select(TASK, claims=["A.", "B."]),
        {("POST", f"/verify/{TASK}/select"): [SELECTED]},
    ),
    ("get_status", "default", lambda c: c.get_status(TASK), {("GET", f"/verify/status/{TASK}"): [DONE]}),
    (
        "cancel",
        "default",
        lambda c: c.cancel(CANCEL_TASK["task_id"]),
        {("POST", f"/verify/{CANCEL_TASK['task_id']}/cancel"): [(200, CANCEL_TASK)]},
    ),
    (
        "review",
        "default",
        lambda c: c.review(DRAFT),
        {("POST", "/review"): [(202, {"review_id": RID, "status": "queued"})]},
    ),
    (
        "review_options",
        "default",
        lambda c: c.review(DRAFT, verdicts=["False"], max_citations=3, suggest_edits=True, idempotency_key=PINNED),
        {("POST", "/review"): [(202, {"review_id": RID, "status": "queued"})]},
    ),
    ("get_review", "default", lambda c: c.get_review(RID), {("GET", f"/reviews/{RID}"): [(200, REVIEW_DONE)]}),
    (
        "get_review_issues",
        "default",
        lambda c: c.get_review(RID, view="issues"),
        {("GET", f"/reviews/{RID}"): [(200, _load("review_completed_issues.json"))]},
    ),
    (
        "cancel_review",
        "default",
        lambda c: c.cancel_review(CANCEL_REVIEW["review_id"]),
        {("POST", f"/reviews/{CANCEL_REVIEW['review_id']}/cancel"): [(200, CANCEL_REVIEW)]},
    ),
    (
        "citecheck_text",
        "default",
        lambda c: c.citecheck(DRAFT, max_citations=2),
        {("POST", "/citecheck"): [(202, {"citecheck_id": CID, "status": "queued"})]},
    ),
    (
        "citecheck_pairs",
        "default",
        lambda c: c.citecheck(pairs=[{"statement": "S.", "url": "https://e.x/s"}], idempotency_key=PINNED),
        {("POST", "/citecheck"): [(202, {"citecheck_id": CID, "status": "queued"})]},
    ),
    ("get_citecheck", "default", lambda c: c.get_citecheck(CID), {("GET", f"/citechecks/{CID}"): [(200, CHECK_DONE)]}),
    (
        "cancel_citecheck",
        "default",
        lambda c: c.cancel_citecheck(CID),
        {("POST", f"/citechecks/{CID}/cancel"): [(200, CANCEL_CHECK)]},
    ),
    ("usage", "default", lambda c: c.usage(), {("GET", "/me/usage"): [USAGE]}),
    # ── namespaces ──
    ("verifications_list", "default", lambda c: c.verifications.list(), {("GET", "/verifications"): [PAGE_1]}),
    ("verifications_list_p2", "default", lambda c: c.verifications.list(page=2), {("GET", "/verifications"): [PAGE_2]}),
    ("verifications_iter", "default", lambda c: c.verifications.iter(), {("GET", "/verifications"): [PAGE_1, PAGE_2]}),
    ("verifications_get", "default", lambda c: c.verifications.get("v1"), {("GET", "/verifications/v1"): [DETAIL]}),
    (
        "verifications_get_keyless",
        "keyless",
        lambda c: c.verifications.get("v1"),
        {("GET", "/verifications/v1"): [DETAIL]},
    ),
    (
        "verifications_get_certificate",
        "default",
        lambda c: c.verifications.get_certificate("v1"),
        {("GET", "/verifications/v1/certificate"): [CERT]},
    ),
    (
        "verifications_delete",
        "default",
        lambda c: c.verifications.delete("v1"),
        {("DELETE", "/verifications/v1"): [(204, None)]},
    ),
    (
        "verifications_delete_404",
        "default",
        lambda c: c.verifications.delete("v1"),
        {("DELETE", "/verifications/v1"): [NOT_FOUND]},
    ),
    (
        "verifications_related",
        "default",
        lambda c: c.verifications.related("v1", limit=3),
        {("GET", "/verifications/v1/related"): [RELATED]},
    ),
    ("ask_history", "default", lambda c: c.ask.history("v1"), {("GET", "/ask/v1"): [ASK_HISTORY]}),
    (
        "ask_send",
        "default",
        lambda c: c.ask.send("v1", message="Why?", language="auto"),
        {("POST", "/ask/v1"): [ASK_REPLY]},
    ),
    ("ask_reset", "default", lambda c: c.ask.reset("v1"), {("DELETE", "/ask/v1"): [(204, None)]}),
    ("library_list", "default", lambda c: c.library.list(), {("GET", "/library"): [LIB_1]}),
    (
        "library_list_filters",
        "keyless",
        lambda c: c.library.list(page=2, sort="most_true", search="x", curated=["trivia"], verdict="True"),
        {("GET", "/library"): [LIB_2]},
    ),
    ("library_iter", "default", lambda c: c.library.iter(search="x"), {("GET", "/library"): [LIB_1, LIB_2]}),
    # ── waits ──
    (
        "verify_and_wait",
        "default",
        lambda c: c.verify_and_wait("A.", idempotency_key=PINNED),
        {("POST", "/verify"): [ACCEPTED], ("GET", f"/verify/status/{TASK}"): [RUNNING, RUNNING_NO_HINT, DONE]},
    ),
    (
        "verify_and_wait_times_out",
        "default",
        lambda c: c.verify_and_wait("A.", timeout=5),
        {("POST", "/verify"): [ACCEPTED], ("GET", f"/verify/status/{TASK}"): [RUNNING_NO_HINT]},
    ),
    ("wait_zero", "default", lambda c: c.wait(TASK, timeout=0), {("GET", f"/verify/status/{TASK}"): [RUNNING]}),
    ("wait_negative", "default", lambda c: c.wait(TASK, timeout=-1), {("GET", f"/verify/status/{TASK}"): [DONE]}),
    (
        "wait_poll_errors",
        "default",
        lambda c: c.wait(TASK, timeout=60),
        {("GET", f"/verify/status/{TASK}"): [S503, LIMITED, _drop(), DONE]},
    ),
    (
        "verify_batch_and_wait",
        "default",
        lambda c: c.verify_batch_and_wait(claims=[{"claim": "A."}, {"claim": "B."}], idempotency_key=PINNED),
        {
            ("POST", "/verify/batch"): [BATCH],
            ("GET", f"/verify/status/{TASK}"): [RUNNING, DONE],
            ("GET", "/verify/status/t2"): [NOT_FOUND],
        },
    ),
    (
        "review_and_wait",
        "default",
        lambda c: c.review_and_wait(DRAFT, idempotency_key=PINNED),
        {
            ("POST", "/review"): [(202, {"review_id": RID, "status": "queued"})],
            ("GET", f"/reviews/{RID}"): [(200, REVIEW_RUNNING), S503, (200, REVIEW_DONE)],
        },
    ),
    (
        "review_and_wait_zero",
        "default",
        lambda c: c.review_and_wait(DRAFT, timeout=0),
        {
            ("POST", "/review"): [(202, {"review_id": RID, "status": "queued"})],
            ("GET", f"/reviews/{RID}"): [(200, REVIEW_RUNNING)],
        },
    ),
    (
        "review_and_wait_times_out",
        "default",
        lambda c: c.review_and_wait(DRAFT, timeout=25),
        {
            ("POST", "/review"): [(202, {"review_id": RID, "status": "queued"})],
            ("GET", f"/reviews/{RID}"): [(200, REVIEW_RUNNING)],
        },
    ),
    (
        "citecheck_and_wait",
        "default",
        lambda c: c.citecheck_and_wait(DRAFT),
        {
            ("POST", "/citecheck"): [(202, {"citecheck_id": CID, "status": "queued"})],
            ("GET", f"/citechecks/{CID}"): [(200, CHECK_DONE)],
        },
    ),
    # ── retries and errors ──
    ("retry_503", "default", lambda c: c.assess("A.", idempotency_key=PINNED), {("POST", "/assess"): [S503, ASSESSED]}),
    ("retry_429_stated", "default", lambda c: c.usage(), {("GET", "/me/usage"): [LIMITED, USAGE]}),
    (
        "retry_409_conflict",
        "default",
        lambda c: c.verify("A.", idempotency_key=PINNED),
        {("POST", "/verify"): [CONFLICT, ACCEPTED]},
    ),
    ("retry_transport", "default", lambda c: c.get_status(TASK), {("GET", f"/verify/status/{TASK}"): [_drop(), DONE]}),
    ("retries_exhausted_503", "default", lambda c: c.usage(), {("GET", "/me/usage"): [S503]}),
    ("retries_exhausted_transport", "default", lambda c: c.ask.history("v1"), {("GET", "/ask/v1"): [_drop()]}),
    ("max_retries_0_503", "max_retries_0", lambda c: c.usage(), {("GET", "/me/usage"): [S503]}),
    ("max_retries_1_503", "max_retries_1", lambda c: c.assess("A."), {("POST", "/assess"): [S503]}),
    ("error_404", "default", lambda c: c.get_status(TASK), {("GET", f"/verify/status/{TASK}"): [NOT_FOUND]}),
    ("keyless_refused", "keyless", lambda c: c.usage(), {}),
]

# The per-attempt timeout under every client configuration: the floored calls,
# a plain call, and a wait's polls (capped by what is left of the wait).
_TIMEOUT_CLIENTS = [
    "timeout_none",
    "timeout_5_read_200",
    "timeout_200_read_5",
    "timeout_30_read_none",
    "timeout_120",
    "borrowed_10",
    "borrowed_300",
]
for _client in _TIMEOUT_CLIENTS:
    CASES += [
        (f"{_client}:extract", _client, lambda c: c.extract(text="Doc."), {("POST", "/extract"): [EXTRACTED]}),
        (f"{_client}:assess", _client, lambda c: c.assess("A."), {("POST", "/assess"): [ASSESSED]}),
        (
            f"{_client}:extract_timeout_9",
            _client,
            lambda c: c.extract(text="Doc.", timeout=9),
            {("POST", "/extract"): [EXTRACTED]},
        ),
        (f"{_client}:usage", _client, lambda c: c.usage(), {("GET", "/me/usage"): [USAGE]}),
        (
            f"{_client}:wait",
            _client,
            lambda c: c.wait(TASK, timeout=12),
            {("GET", f"/verify/status/{TASK}"): [RUNNING_NO_HINT, RUNNING_NO_HINT, RUNNING_NO_HINT, DONE]},
        ),
        (
            f"{_client}:review_and_wait",
            _client,
            lambda c: c.review_and_wait(DRAFT, timeout=12, idempotency_key=PINNED),
            {
                ("POST", "/review"): [(202, {"review_id": RID, "status": "queued"})],
                ("GET", f"/reviews/{RID}"): [(200, REVIEW_RUNNING), (200, REVIEW_DONE)],
            },
        ),
    ]

# ── more waits, retries and recoveries (added with the async client: these
# pin the sync client's timing, errors and results before its internals moved
# to the shared core) ──

_OLD_VERSION = {"X-Lenz-API-Version": "2026-05-13"}
UNAUTHORIZED = (401, {"detail": "Invalid API key."})
TYPED_503 = (503, {"code": "upstream_unavailable", "detail": "busy"}, {"Retry-After": "120"})
UNTYPED_503_LONG = (503, {"detail": "maintenance"}, {"Retry-After": "3600"})
REVIEW_IN_FLIGHT = (429, {"code": "review_in_flight", "detail": "one at a time"}, {"Retry-After": "5"})
LIMITED_LONG = (429, {"code": "rate_limited", "detail": "tomorrow"}, {"Retry-After": "40000"})
GARBAGE_HINT = (200, {"status": "processing", "task_id": TASK, "progress": {"step": "x", "poll_after_seconds": 0.1}})
HUGE_HINT = (200, {"status": "processing", "task_id": TASK, "progress": {"step": "x", "poll_after_seconds": 500}})
NEEDS_INPUT = (
    200,
    {
        "status": "needs_input",
        "task_id": TASK,
        "reason": "multi_claim",
        "hint": "Pick one.",
        "claims": [{"claim": "A."}, {"claim": "B."}],
    },
)
FAILED = (
    200,
    {
        "status": "failed",
        "task_id": TASK,
        "failure": {"failure_reason": "no_sources", "failure_class": "other", "retryable": False, "hint": "Rephrase."},
    },
)
TASK_CANCELLED = (200, {"status": "cancelled", "task_id": TASK})
DONE_EMPTY = (200, {"status": "completed", "task_id": TASK})
REVIEW_FAILED = _load("review_failed_no_claim.json")
CHECK_RUNNING = {**CHECK_DONE, "status": "checking", "outcome": None, "poll_after_seconds": 1}
REVIEW_CONFLICT = (409, {"code": "idempotency_conflict", "detail": "in flight", "review_id": RID})
CHECK_CONFLICT = (409, {"code": "idempotency_conflict", "detail": "in flight", "citecheck_id": CID})
BATCH3 = (
    200,
    {
        "batch_id": "b1",
        "items": [{"task_id": TASK, "claim": "A."}, {"task_id": "t2", "claim": "B."}, {"task_id": "t3", "claim": "C."}],
    },
)


def _progress_log(c: Lenz) -> list[Any]:
    """``wait`` with an ``on_progress`` that records what it was given, a
    second one that raises, and the log as the call's result."""
    seen: list[Any] = []

    def record(task_id: str, progress: Any) -> None:
        seen.append([task_id, progress.model_dump(mode="json")])
        progress.step = "mutated"  # a copy: changing it changes nothing

    c.wait(TASK, timeout=60, on_progress=record)
    c.wait(TASK, timeout=60, on_progress=lambda t, p: 1 / 0)
    return seen


def _update_log(c: Lenz) -> list[Any]:
    seen: list[Any] = []

    def record(review: Any) -> None:
        seen.append(review.status)
        review.status = "mutated"

    c.review_and_wait(DRAFT, idempotency_key=PINNED, on_update=record)
    return seen


CASES += [
    (
        "batch_401_mid_round",
        "default",
        lambda c: c.verify_batch_and_wait(claims=[{"claim": "A."}, {"claim": "B."}, {"claim": "C."}]),
        {
            ("POST", "/verify/batch"): [BATCH3],
            ("GET", f"/verify/status/{TASK}"): [RUNNING],
            ("GET", "/verify/status/t2"): [UNAUTHORIZED],
            ("GET", "/verify/status/t3"): [DONE],
        },
    ),
    (
        "batch_mixed_outcomes",
        "default",
        lambda c: c.verify_batch_and_wait(claims=[{"claim": "A."}, {"claim": "B."}, {"claim": "C."}], timeout=30),
        {
            ("POST", "/verify/batch"): [BATCH3],
            ("GET", f"/verify/status/{TASK}"): [NEEDS_INPUT],
            ("GET", "/verify/status/t2"): [(200, {**DONE2[1], "result": None}), FAILED],
            ("GET", "/verify/status/t3"): [RUNNING_NO_HINT, (200, DONE[1], _OLD_VERSION)],
        },
    ),
    (
        "batch_times_out",
        "default",
        lambda c: c.verify_batch_and_wait(claims=[{"claim": "A."}, {"claim": "B."}], timeout=7),
        {
            ("POST", "/verify/batch"): [BATCH],
            ("GET", f"/verify/status/{TASK}"): [RUNNING],
            ("GET", "/verify/status/t2"): [S503, LIMITED, RUNNING_NO_HINT],
        },
    ),
    (
        "wait_slow_poll_eats_the_deadline",
        "default",
        lambda c: c.verify_batch_and_wait(claims=[{"claim": "A."}, {"claim": "B."}], timeout=10),
        {
            ("POST", "/verify/batch"): [BATCH],
            ("GET", f"/verify/status/{TASK}"): [Slow(12, RUNNING)],
            ("GET", "/verify/status/t2"): [DONE2],
        },
    ),
    (
        "wait_garbage_hints",
        "default",
        lambda c: c.wait(TASK, timeout=60),
        {("GET", f"/verify/status/{TASK}"): [GARBAGE_HINT, HUGE_HINT, RUNNING, DONE]},
    ),
    (
        "wait_stated_wait_beats_hint",
        "default",
        lambda c: c.wait(TASK, timeout=120),
        {("GET", f"/verify/status/{TASK}"): [(429, {"detail": "x"}, {"Retry-After": "90"}), RUNNING, DONE]},
    ),
    ("wait_needs_input", "default", lambda c: c.wait(TASK), {("GET", f"/verify/status/{TASK}"): [NEEDS_INPUT]}),
    ("wait_failed", "default", lambda c: c.wait(TASK), {("GET", f"/verify/status/{TASK}"): [FAILED]}),
    ("wait_cancelled", "default", lambda c: c.wait(TASK), {("GET", f"/verify/status/{TASK}"): [TASK_CANCELLED]}),
    ("wait_completed_empty", "default", lambda c: c.wait(TASK), {("GET", f"/verify/status/{TASK}"): [DONE_EMPTY]}),
    ("wait_401", "default", lambda c: c.wait(TASK), {("GET", f"/verify/status/{TASK}"): [RUNNING, UNAUTHORIZED]}),
    (
        "wait_version",
        "default",
        lambda c: c.wait(TASK),
        {("GET", f"/verify/status/{TASK}"): [(200, DONE[1], _OLD_VERSION)]},
    ),
    (
        "wait_unreadable_poll",
        "default",
        lambda c: c.wait(TASK, timeout=30),
        {("GET", f"/verify/status/{TASK}"): [(200, {"status": 5})]},
    ),
    ("wait_empty_id", "default", lambda c: c.wait(""), {}),
    (
        "wait_progress_callbacks",
        "default",
        _progress_log,
        {("GET", f"/verify/status/{TASK}"): [RUNNING, RUNNING_NO_HINT, DONE, RUNNING, DONE]},
    ),
    (
        "verify_and_wait_submit_fails",
        "default",
        lambda c: c.verify_and_wait("A.", idempotency_key=PINNED),
        {("POST", "/verify"): [(422, {"code": "invalid_request", "detail": "bad"})]},
    ),
    (
        "verify_and_wait_poll_404",
        "default",
        lambda c: c.verify_and_wait("A."),
        {("POST", "/verify"): [ACCEPTED], ("GET", f"/verify/status/{TASK}"): [NOT_FOUND]},
    ),
    (
        "review_and_wait_failed",
        "default",
        lambda c: c.review_and_wait(DRAFT, idempotency_key=PINNED),
        {
            ("POST", "/review"): [(202, {"review_id": RID, "status": "queued"})],
            ("GET", f"/reviews/{RID}"): [(200, {**REVIEW_FAILED, "review_id": RID})],
        },
    ),
    (
        "review_and_wait_cancelled",
        "default",
        lambda c: c.review_and_wait(DRAFT),
        {
            ("POST", "/review"): [(202, {"review_id": RID, "status": "queued"})],
            ("GET", f"/reviews/{RID}"): [(200, {**REVIEW_RUNNING, "status": "cancelled"})],
        },
    ),
    (
        "review_and_wait_odd_bodies",
        "default",
        lambda c: c.review_and_wait(DRAFT, timeout=100),
        {
            ("POST", "/review"): [(202, {"review_id": RID, "status": "queued"})],
            ("GET", f"/reviews/{RID}"): [
                (200, {"status": "completed"}),
                Raw(200, b"<html>proxy</html>"),
                (200, {**REVIEW_RUNNING, "poll_after_seconds": 1}),
                (503, {"detail": "x"}, {"Retry-After": "400"}),
                (200, {**REVIEW_RUNNING, "poll_after_seconds": 20}),
                (200, REVIEW_DONE),
            ],
        },
    ),
    (
        "review_and_wait_404",
        "default",
        lambda c: c.review_and_wait(DRAFT),
        {("POST", "/review"): [(202, {"review_id": RID, "status": "queued"})], ("GET", f"/reviews/{RID}"): [NOT_FOUND]},
    ),
    (
        "review_and_wait_updates",
        "default",
        _update_log,
        {
            ("POST", "/review"): [(202, {"review_id": RID, "status": "queued"})],
            ("GET", f"/reviews/{RID}"): [(200, REVIEW_RUNNING), (200, REVIEW_RUNNING), (200, REVIEW_DONE)],
        },
    ),
    (
        "citecheck_and_wait_stated_wait_and_floor",
        "default",
        lambda c: c.citecheck_and_wait(DRAFT, timeout=100),
        {
            ("POST", "/citecheck"): [(202, {"citecheck_id": CID, "status": "queued"})],
            ("GET", f"/citechecks/{CID}"): [
                (200, CHECK_RUNNING),
                (503, {"detail": "x"}, {"Retry-After": "20"}),
                (429, {"detail": "x"}, {"Retry-After": "2"}),
                (200, CHECK_DONE),
            ],
        },
    ),
    (
        "citecheck_and_wait_failed",
        "default",
        lambda c: c.citecheck_and_wait(DRAFT, idempotency_key=PINNED),
        {
            ("POST", "/citecheck"): [(202, {"citecheck_id": CID, "status": "queued"})],
            ("GET", f"/citechecks/{CID}"): [
                (200, {**CHECK_DONE, "status": "failed", "failure": {"failure_reason": "x", "retryable": True}})
            ],
        },
    ),
    (
        "citecheck_and_wait_times_out",
        "default",
        lambda c: c.citecheck_and_wait(DRAFT, timeout=8),
        {
            ("POST", "/citecheck"): [(202, {"citecheck_id": CID, "status": "queued"})],
            ("GET", f"/citechecks/{CID}"): [_drop()],
        },
    ),
    ("retry_untyped_503_long", "default", lambda c: c.usage(), {("GET", "/me/usage"): [UNTYPED_503_LONG, USAGE]}),
    ("retry_untyped_503_exhausted", "default", lambda c: c.usage(), {("GET", "/me/usage"): [UNTYPED_503_LONG]}),
    ("typed_503_raises", "default", lambda c: c.assess("A."), {("POST", "/assess"): [TYPED_503]}),
    (
        "typed_503_short_wait",
        "default",
        lambda c: c.usage(),
        {("GET", "/me/usage"): [(503, {"code": "capacity"}, {"Retry-After": "30"}), USAGE]},
    ),
    ("no_retry_429_code", "default", lambda c: c.usage(), {("GET", "/me/usage"): [REVIEW_IN_FLIGHT]}),
    ("retry_429_long_raises", "default", lambda c: c.extract(text="Doc."), {("POST", "/extract"): [LIMITED_LONG]}),
    (
        "retry_429_body_wait",
        "default",
        lambda c: c.usage(),
        {("GET", "/me/usage"): [(429, {"code": "rate_limited", "reset_in_seconds": 3}), USAGE]},
    ),
    (
        "retry_409_exhausted",
        "default",
        lambda c: c.assess("A.", idempotency_key=PINNED),
        {("POST", "/assess"): [CONFLICT]},
    ),
    (
        "retry_409_no_key",
        "default",
        lambda c: c.assess("A.", idempotency=False),
        {("POST", "/assess"): [CONFLICT]},
    ),
    (
        "retry_409_long_wait",
        "default",
        lambda c: c.ask.send("v1", message="Why?"),
        {("POST", "/ask/v1"): [(409, {"code": "idempotency_conflict"}, {"Retry-After": "90"}), ASK_REPLY]},
    ),
    ("review_409_names_the_job", "default", lambda c: c.review(DRAFT), {("POST", "/review"): [REVIEW_CONFLICT]}),
    (
        "review_409_then_accepted",
        "default",
        lambda c: c.review(DRAFT, idempotency_key=PINNED),
        {("POST", "/review"): [CONFLICT, (202, {"review_id": RID, "status": "queued"})]},
    ),
    (
        "review_and_wait_409_names_the_job",
        "default",
        lambda c: c.review_and_wait(DRAFT, idempotency_key=PINNED),
        {("POST", "/review"): [REVIEW_CONFLICT], ("GET", f"/reviews/{RID}"): [(200, REVIEW_DONE)]},
    ),
    (
        "citecheck_409_names_the_job",
        "default",
        lambda c: c.citecheck(DRAFT),
        {("POST", "/citecheck"): [CHECK_CONFLICT]},
    ),
    (
        "citecheck_409_other_code",
        "max_retries_0",
        lambda c: c.citecheck(DRAFT, idempotency_key=PINNED),
        {("POST", "/citecheck"): [(409, {"code": "conflict", "citecheck_id": CID})]},
    ),
    (
        "verifications_delete_version_404",
        "default",
        lambda c: c.verifications.delete("v1"),
        {("DELETE", "/verifications/v1"): [(404, {"code": "not_found"}, _OLD_VERSION)]},
    ),
    (
        "verifications_delete_500",
        "max_retries_0",
        lambda c: c.verifications.delete("v1"),
        {("DELETE", "/verifications/v1"): [(500, {"detail": "boom"})]},
    ),
    ("unreadable_200", "default", lambda c: c.assess("A."), {("POST", "/assess"): [Raw(200, b"<html>")]}),
    ("unreadable_200_no_key", "default", lambda c: c.usage(), {("GET", "/me/usage"): [Raw(200, b"<html>")]}),
    ("empty_200", "default", lambda c: c.ask.reset("v1"), {("DELETE", "/ask/v1"): [Raw(200, b"")]}),
    (
        "cancel_unexpected_body",
        "default",
        lambda c: c.cancel(TASK),
        {("POST", f"/verify/{TASK}/cancel"): [(200, {"task_id": "other", "cancelled": True, "status": "cancelled"})]},
    ),
    (
        "cancel_review_unexpected_body",
        "default",
        lambda c: c.cancel_review(RID),
        {("POST", f"/reviews/{RID}/cancel"): [(200, {"status": "cancelled"})]},
    ),
    (
        "cancel_citecheck_unexpected_body",
        "default",
        lambda c: c.cancel_citecheck(CID),
        {("POST", f"/citechecks/{CID}/cancel"): [(200, {"status": "cancelled"})]},
    ),
    (
        "path_ids_encoded",
        "default",
        lambda c: c.get_status("a/b?c#d%e"),
        {("GET", "/verify/status/a%2Fb%3Fc%23d%25e"): [DONE]},
    ),
    ("path_id_dots", "default", lambda c: c.verifications.get(".."), {}),
    ("get_review_bad_view", "default", lambda c: c.get_review(RID, view="nope"), {}),  # type: ignore[call-overload]
    ("review_empty", "default", lambda c: c.review("  "), {}),
    ("review_selector_str", "default", lambda c: c.review(DRAFT, verdicts="False"), {}),  # type: ignore[arg-type]
    ("citecheck_both", "default", lambda c: c.citecheck(DRAFT, pairs=[]), {}),
    ("citecheck_pairs_max", "default", lambda c: c.citecheck(pairs=[], max_citations=2), {}),
    ("assess_both_forms", "default", lambda c: c.assess("A.", claims=["B."]), {}),
    ("select_empty", "default", lambda c: c.select(TASK, claims=[]), {}),
    ("library_iter_random", "default", lambda c: c.library.iter(sort="random"), {}),
    ("verifications_iter_page_0", "default", lambda c: c.verifications.iter(page=0), {}),
    (
        "verifications_iter_clamped",
        "default",
        lambda c: c.verifications.iter(page=2),
        {("GET", "/verifications"): [(200, {"items": [_ITEM], "total": 9, "page": 1, "page_size": 1})]},
    ),
    (
        "library_iter_short_page",
        "keyless",
        lambda c: c.library.iter(curated=["trivia"]),
        {("GET", "/library"): [(200, {"items": [LIB_ITEM], "total": 5, "page": 1, "page_size": 2})]},
    ),
    ("bad_timeout_option", "default", lambda c: c.usage(timeout=0), {}),
    ("bad_header_option", "default", lambda c: c.usage(extra_headers={"Authorization": "x"}), {}),
    (
        "options_everywhere",
        "default",
        lambda c: c.with_options(timeout=11, max_retries=1, extra_headers={"X-A": "1", "X-B": "2"}).verify_and_wait(
            "A.", idempotency_key=PINNED, extra_headers={"x-b": None, "X-C": "3"}, max_retries=1
        ),
        {("POST", "/verify"): [S503, ACCEPTED], ("GET", f"/verify/status/{TASK}"): [RUNNING_NO_HINT, DONE]},
    ),
    (
        "options_on_review_and_wait",
        "timeout_120",
        lambda c: c.review_and_wait(DRAFT, idempotency_key=PINNED, extra_headers={"X-T": "1"}, max_retries=2),
        {
            ("POST", "/review"): [S503, (202, {"review_id": RID, "status": "queued"})],
            ("GET", f"/reviews/{RID}"): [(200, REVIEW_DONE)],
        },
    ),
    (
        "options_on_iter",
        "default",
        lambda c: c.with_options(timeout=httpx.Timeout(3, read=9)).library.iter(
            max_retries=0, extra_headers={"X-P": "1"}
        ),
        {("GET", "/library"): [LIB_1, LIB_2]},
    ),
    (
        "option_ua_override",
        "default",
        lambda c: c.usage(extra_headers={"User-Agent": "mine/1.0"}),
        {("GET", "/me/usage"): [USAGE]},
    ),
]
