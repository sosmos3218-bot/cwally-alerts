"""HTTP helpers. EDGAR is capped below 10 requests per second."""

from __future__ import annotations

import os
import threading
import time
import urllib.error
import urllib.request

# EDGAR rejects a User-Agent that has no contact token.
_FALLBACK_UA = "cwally-alerts research bot admin@cwally-alerts.local"


def user_agent() -> str:
    raw = (os.environ.get("SEC_USER_AGENT") or "").strip()
    return raw or _FALLBACK_UA


class RateLimiter:
    def __init__(self, per_sec: float) -> None:
        self.gap = 1.0 / per_sec if per_sec else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if self.gap <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            if delay < 0:
                delay = 0.0
                self._next = now + self.gap
            else:
                self._next += self.gap
        if delay:
            time.sleep(delay)


class HttpClient:
    def __init__(self, per_sec: float, ua: str | None = None) -> None:
        self.ua = ua or user_agent()
        self.limiter = RateLimiter(per_sec)

    def get(self, url: str, timeout: int = 60, missing_ok: bool = False) -> bytes | None:
        """Return the body. None when the resource is absent and missing_ok is set."""
        headers = {"User-Agent": self.ua, "Accept-Encoding": "identity", "Accept": "*/*"}
        last: Exception | None = None
        for attempt in range(4):
            self.limiter.wait()
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code in (404, 400):
                    return None
                if exc.code in (429, 500, 502, 503, 403) and attempt < 3:
                    time.sleep(1.2 * (attempt + 1))
                    continue
                if missing_ok:
                    return None
                raise
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < 3:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                if missing_ok:
                    return None
                raise
        if last:
            raise last
        return None


def yahoo_get(url: str, timeout: int = 30) -> bytes | None:
    """Chart prices are not on the EDGAR budget. A browser-like agent is enough."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return None
