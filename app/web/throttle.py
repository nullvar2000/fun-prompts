"""A per-client submission throttle for the public write routes.

The gallery spends someone else's API budget, so an open form is not neutral:
one determined visitor can burn an afternoon of tokens. This is the blunt
instrument for that — ``FP_RATE_LIMIT_PER_HOUR``, keyed on the submitting
address, counted in memory.

Two things it deliberately is not:

* **Not durable.** One container, one worker, one process: the counter lives in
  memory and resets on restart. A SQLite table for a courtesy throttle would be
  a second source of truth for a number nobody needs to audit.
* **Not a wall.** The key is the peer address, falling back to
  ``X-Forwarded-For`` when the request came through a proxy that set it. That
  header is spoofable by anyone who can reach the app directly, which is fine
  for a throttle and would not be for an authorisation decision. A limit of 0 —
  the default — means no limit.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

WINDOW_SECONDS = 3600


class SubmissionThrottle:
    """``per_hour`` submissions per key, sliding window."""

    def __init__(self, per_hour: int, *, window_seconds: int = WINDOW_SECONDS) -> None:
        self.per_hour = per_hour
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, *, now: float | None = None) -> int | None:
        """Record a submission for ``key``.

        Returns ``None`` when it is allowed, or the whole seconds to wait
        before the oldest hit leaves the window. A refused submission is not
        counted, so hammering the form does not extend the wait.
        """
        if self.per_hour <= 0:
            return None
        moment = time.monotonic() if now is None else now
        hits = self._hits[key]
        while hits and moment - hits[0] >= self.window_seconds:
            hits.popleft()
        if len(hits) >= self.per_hour:
            return int(self.window_seconds - (moment - hits[0])) + 1
        hits.append(moment)
        return None

    def remaining(self, key: str, *, now: float | None = None) -> int:
        """How many submissions this key still has in the current window."""
        if self.per_hour <= 0:
            return 0
        moment = time.monotonic() if now is None else now
        hits = self._hits[key]
        while hits and moment - hits[0] >= self.window_seconds:
            hits.popleft()
        return max(0, self.per_hour - len(hits))

    def reset(self) -> None:
        self._hits.clear()
