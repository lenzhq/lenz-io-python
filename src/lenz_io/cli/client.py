"""Client factory — builds a ``lenz_io.Lenz`` whose outbound requests carry a
distinct ``User-Agent: lenz-cli/<version>`` so CLI traffic is attributable
(separate from the raw ``lenz-io-python/<version>`` SDK UA).

We pass ``user_agent=`` to the SDK so it builds its own client with all of its
default headers intact and only the UA overridden — rather than hand-copying
the SDK's header set (which would silently drop any header the SDK later adds).
The full key / Authorization header is never logged.
"""

from __future__ import annotations

import httpx

from lenz_io import Lenz, __version__


def cli_user_agent() -> str:
    return f"lenz-cli/{__version__} (httpx {httpx.__version__})"


def build_client(*, api_key: str, base_url: str) -> Lenz:
    # api_key="" → SDK would fall back to env; we pass the already-resolved key
    # (or None to let auth-required calls raise LenzAuthError cleanly).
    return Lenz(api_key=api_key or None, base_url=base_url or None, user_agent=cli_user_agent())
