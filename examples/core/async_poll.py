"""Async submit + poll — get a verification result without webhooks.

Run:
    export LENZ_API_KEY=lenz_...
    python examples/core/async_poll.py

`verify()` returns immediately with a task_id; the pipeline runs async
(~60-90s for a cold claim). `wait()` blocks on that task until it lands —
the polling counterpart to a webhook. Use this in scripts, notebooks, and
request/response handlers where blocking is fine; use webhooks for
production async flows.

For full control you can drive the loop yourself with
`client.get_status(task_id)` (a single non-blocking poll) — `wait()` just
does that loop for you with sensible backoff.
"""

from __future__ import annotations

import os

from lenz_io import Lenz


def main() -> None:
    client = Lenz(api_key=os.environ.get("LENZ_API_KEY"))

    # 1. Submit — returns a task_id immediately, pipeline runs async.
    task = client.verify(claim="Sharks don't get cancer")
    print(f"Submitted: {task.task_id}")

    # 2. Block on the task until it terminates. Accepts the TaskAccepted
    #    object directly, or you could pass task.task_id.
    verification = client.wait(task, timeout=180)

    print(f"Verdict: {verification.verdict} (lenz_score {verification.lenz_score})")
    print(f"Summary: {verification.executive_summary}")


if __name__ == "__main__":
    main()
