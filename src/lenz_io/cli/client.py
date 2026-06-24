"""Client factory — builds a ``lenz_io.Lenz`` whose outbound requests carry a
distinct ``User-Agent: lenz-cli/<version>`` so CLI traffic is attributable
(separate from the raw ``lenz-io-python/<version>`` SDK UA).

The SDK builds its own ``httpx.Client`` with its own UA, but it accepts an
injected ``http_client=`` — the public extension point. We pass one that
mirrors the SDK's default headers (``X-Lenz-API-Version`` + ``Accept``) and
swaps the UA. The full key / Authorization header is never logged.
"""

from __future__ import annotations

import httpx

from lenz_io import Lenz, __version__
from lenz_io.client import API_VERSION, DEFAULT_TIMEOUT


def cli_user_agent() -> str:
    return f"lenz-cli/{__version__} (httpx {httpx.__version__})"


def build_client(*, api_key: str, base_url: str) -> Lenz:
    http = httpx.Client(
        timeout=httpx.Timeout(DEFAULT_TIMEOUT),
        headers={
            "User-Agent": cli_user_agent(),
            "X-Lenz-API-Version": API_VERSION,
            "Accept": "application/json",
        },
    )
    # api_key="" → SDK would fall back to env; we pass the already-resolved key
    # (or None to let auth-required calls raise LenzAuthError cleanly).
    return Lenz(api_key=api_key or None, base_url=base_url or None, http_client=http)
