"""
A pace of Unfret's own for an outside service that blocks clients who ask too often (the web archives).

Per process: at most so many requests a minute (and, if set, a day), and none for a while after the service refused
us. A request that doesn't fit is not sent, and the caller skips what it needed it for instead of waiting.
"""

import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, date, datetime


class Pace:
    """
    How often this process asks one service.

    The limits are read at every request (callables), so a changed setting applies at once.
    """

    def __init__(
        self,
        *,
        per_minute: Callable[[], int],
        pause_s: Callable[[], float],
        daily_cap: Callable[[], int] | None = None,
    ) -> None:
        """
        Start with nothing sent.

        :param per_minute: Requests a minute at most.
        :param pause_s: How long :meth:`pause` stops all requests, in seconds.
        :param daily_cap: Requests a UTC day at most (``None``: no daily cap; ``0``: none at all).
        """
        self.per_minute, self.pause_s, self.daily_cap = per_minute, pause_s, daily_cap
        self.sent: deque[float] = deque()
        self.paused_until = 0.0
        self.day: date | None = None
        self.today_sent = 0

    def take(self) -> bool:
        """
        Take one request from the pace.

        :return: ``True`` when the request may be sent now.
        """
        now = time.monotonic()
        while self.sent and self.sent[0] <= now - 60:
            self.sent.popleft()
        today = datetime.now(tz=UTC).date()
        if today != self.day:
            self.day, self.today_sent = today, 0
        if now < self.paused_until or len(self.sent) >= self.per_minute():
            return False
        if self.daily_cap is not None and self.today_sent >= self.daily_cap():
            return False
        self.sent.append(now)
        self.today_sent += 1
        return True

    def pause(self) -> None:
        """Stop asking for ``pause_s``."""
        self.paused_until = time.monotonic() + self.pause_s()
