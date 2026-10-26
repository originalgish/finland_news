"""healthchecks.io pings. A failed ping is printed, never raised, so it cannot fail a run.

The ping URL carries a secret, so only the error's type is printed, never its text.
"""

import httpx

PING_TIMEOUT_SECONDS = 10


def ping(url: str | None) -> None:
    if not url:
        return
    try:
        httpx.get(url, timeout=PING_TIMEOUT_SECONDS)
    except httpx.HTTPError as error:
        print(f"healthcheck ping failed: {type(error).__name__}")
