"""Tiny stdlib HTTP client with retry/backoff, shared by all HTTP providers."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from .base import ProviderError

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str], *,
              timeout: float = 600, retries: int = 4, backoff: float = 2.0) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json", **headers}
    delay = backoff
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:2000]
            if e.code in RETRY_STATUS and attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise ProviderError(f"HTTP {e.code} from {url}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise ProviderError(f"cannot reach {url}: {e}") from e
    raise ProviderError("unreachable")
