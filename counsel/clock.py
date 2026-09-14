from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Clock(Protocol):
    def now(self) -> datetime: ...


class RealClock:
    def now(self) -> datetime:
        return utcnow()


class SimClock:
    """A clock the demo can move forward. A 3-month case can be walked in minutes."""

    def __init__(self, start: datetime | None = None):
        self._now = start or utcnow()

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> datetime:
        self._now += timedelta(**kwargs)
        return self._now

    def set(self, when: datetime) -> None:
        self._now = when
