"""Fixed, drift-free schedule: runs start on wall-clock multiples of the interval (:00, :05, :10 ...).
A run that overruns the next slot simply skips it (no catch-up burst)."""
from __future__ import annotations

import math


def next_slot(now: float, interval: float) -> float:
    """First slot strictly after `now`. Slots are aligned to the Unix epoch, i.e. to whole minutes/hours
    (Warsaw's UTC offset is a whole number of hours, so 300 s -> :00, :05, :10 local too)."""
    return (math.floor(now / interval) + 1) * interval


def current_or_next_slot(now: float, interval: float, grace: float = 2.0) -> float:
    """If we are within `grace` seconds after a slot boundary, that slot; otherwise the next one."""
    base = math.floor(now / interval) * interval
    return base if now - base <= grace else base + interval


def skipped_slots(started_slot: float, finished_at: float, interval: float) -> int:
    """How many slots were missed because the run that started at `started_slot` ended at `finished_at`."""
    return max(0, int((finished_at - started_slot) // interval))
