"""A polite, caching HTTP fetcher.

* Every page is saved under ``data/raw/`` the first time it is downloaded.
  Completed events, fights and fighter profiles don't change, so later runs
  read from disk instead of hitting the site again.
* There's a minimum gap between live requests, so we don't hammer anyone's server.
* Transient failures (timeouts, 5xx, 429) are retried with backoff.
  Anything else fails loudly.
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..errors import UfcEdgeError

USER_AGENT = "ufc-edge-research/0.2 (personal statistics project; polite crawler)"


class FetchError(UfcEdgeError):
    """A page could not be downloaded."""


def _looks_like_bot_challenge(body: str) -> bool:
    head = body[:4000].lower()
    return "checking your browser" in head or ("<noscript>" in head and "requires javascript" in head
                                               and len(body) < 20000)


class Fetcher:
    def __init__(
        self,
        cache_dir: str | Path,
        min_interval: float = 1.5,
        max_retries: int = 3,
        timeout: float = 30.0,
        offline: bool = False,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.timeout = timeout
        self.offline = offline
        self._last_request = 0.0
        self.live_requests = 0
        self.cache_hits = 0

    def cache_path(self, cache_key: str) -> Path:
        return self.cache_dir / cache_key

    def get(self, url: str, cache_key: str | None = None, refresh: bool = False) -> str:
        """Return the page body, from the cache if present (unless ``refresh``)."""
        path = self.cache_path(cache_key) if cache_key else None
        if path is not None and path.exists() and (not refresh or self.offline):
            self.cache_hits += 1
            return path.read_text(encoding="utf-8")
        if self.offline:
            raise FetchError(f"Offline mode and no cached copy of {url}")
        body = self._download(url)
        if _looks_like_bot_challenge(body):
            # Never cache it, and never try to solve it: that would be bypassing an access control.
            raise FetchError(f"{url} returned a browser-verification page instead of data.")
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, path)  # atomic: no half-written cache files
        return body

    def _download(self, url: str) -> str:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            self.live_requests += 1
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    charset = resp.headers.get_content_charset() or "utf-8"
                    return resp.read().decode(charset, errors="replace")
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504):
                    last_error = exc
                else:
                    raise FetchError(f"HTTP {exc.code} for {url}") from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last_error = exc
            time.sleep(min(30.0, 2.0 ** attempt))
        raise FetchError(f"Gave up on {url} after {self.max_retries} attempts: {last_error}")
