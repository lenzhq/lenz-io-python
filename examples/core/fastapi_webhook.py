"""Receive Lenz webhook events in a FastAPI app.

Lenz POSTs HMAC-signed payloads to your ``webhook_url`` when the
verification pipeline terminates. This handler verifies the signature,
parses the payload into a typed event, and dispatches per event type.

Run:
    pip install fastapi uvicorn
    export LENZ_WEBHOOK_SECRET=whsec_...
    uvicorn examples.core.fastapi_webhook:app --host 0.0.0.0 --port 8000

Then point your Lenz API key's webhook URL at https://<your-host>/lenz-webhook
on the /api-integration page, or pass `webhook_url=...` on individual
verify() calls.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, HTTPException, Request

from lenz_io import (
    LenzWebhooks,
    LenzWebhookSignatureError,
    VerificationCompleted,
    VerificationFailed,
    VerificationNeedsInput,
)

logger = logging.getLogger("lenz-webhook")
logging.basicConfig(level=logging.INFO)

app = FastAPI()
webhooks = LenzWebhooks(secret=os.environ["LENZ_WEBHOOK_SECRET"])


@app.post("/lenz-webhook")
async def lenz_webhook(request: Request) -> dict[str, str]:
    raw_body = await request.body()  # MUST be raw bytes for signature verification
    try:
        event = webhooks.parse(raw_body, headers=dict(request.headers))
    except LenzWebhookSignatureError as exc:
        logger.warning("Rejected webhook: %s", exc.message)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if isinstance(event, VerificationCompleted):
        verdict = event.result.get("verdict", {})
        logger.info(
            "Completed: %s -> %s (score %s)",
            event.verification_id,
            verdict.get("label"),
            verdict.get("score"),
        )
        # TODO: persist verdict + sources to your DB; ping users; etc.
    elif isinstance(event, VerificationNeedsInput):
        logger.info("Needs input on %s: %s", event.task_id, event.needs_input.get("reason"))
        # TODO: surface the candidate claims to the user, then call
        # client.select(task_id, text=...) to resolve.
    elif isinstance(event, VerificationFailed):
        logger.warning("Pipeline failed: %s (%s)", event.task_id, event.error)
    else:
        logger.info("Unhandled webhook event: %s", event.event)

    # Always return 2xx fast. Lenz expects an ack within 5s; otherwise the
    # delivery retries at 10s / 60s / 600s (4 attempts total).
    return {"received": "ok"}
