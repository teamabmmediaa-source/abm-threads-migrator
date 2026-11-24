"""Publishing slots (10:00 / 14:00 / 20:00 Morocco time) and queue projection.

All times are computed in the IANA zone ``Africa/Casablanca`` so the schedule
follows Morocco's clock changes (UTC+1 most of the year, UTC+0 during Ramadan)
without touching the cron expression.
"""
from __future__ import annotations

from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo


def parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00").replace("+0000", "+00:00"))


def slot_key(dt: datetime) -> str:
    return dt.isoformat(timespec="minutes")


class Slots:
    def __init__(self, cfg: dict):
        self.tz = ZoneInfo(cfg["timezone"])
        self.times = sorted(dtime.fromisoformat(t) for t in cfg["daily_slots"])
        self.grace = timedelta(minutes=cfg["slot_grace_minutes"])
        # One-off extra slots, e.g. "2026-11-24T16:25" (local time), on top of the daily ones.
        self.extra = sorted(datetime.fromisoformat(x).replace(tzinfo=self.tz) for x in cfg.get("extra_slots", []))

    def iter_from(self, start: datetime):
        """Yield slots >= start, in order, as aware datetimes in the local zone."""
        local = start.astimezone(self.tz)
        day = local.date()
        floor = local.replace(second=0, microsecond=0)
        while True:
            todays = {datetime.combine(day, t, tzinfo=self.tz) for t in self.times}
            todays |= {e for e in self.extra if e.date() == day}
            for s in sorted(todays):
                if s >= floor:
                    yield s
            day += timedelta(days=1)

    def due_slot(self, now: datetime, used: set[str], not_before: datetime | None = None) -> datetime | None:
        """The most recent slot that has started, is within the grace window, is unused,
        and is not earlier than ``not_before`` (the approval time)."""
        found = None
        start = now - self.grace
        if not_before is not None and not_before > start:
            start = not_before
        for s in self.iter_from(start):
            if s > now:
                break
            if slot_key(s) not in used:
                found = s
        return found

    def next_after(self, now: datetime) -> datetime:
        return next(s for s in self.iter_from(now) if s > now)


def approved_since(state: dict) -> datetime | None:
    return parse(state["approval"].get("approved_at")) if state["approval"].get("approved") else None


def eligible(item: dict, at: datetime) -> bool:
    nra = parse(item.get("next_retry_at"))
    return nra is None or nra <= at


def queue(state: dict) -> list[dict]:
    """Items waiting to be published, oldest Instagram post first."""
    items = [i for i in state["items"].values() if i["status"] == "queued"]
    return sorted(items, key=lambda i: (i["timestamp"], i["ig_id"]))


def project(state: dict, cfg: dict, now: datetime, limit: int | None = None) -> list[tuple[dict, datetime]]:
    """Predict which item goes out in which slot, using the same rule the runner uses:
    at each slot, the oldest queued item whose retry back-off has expired."""
    slots = Slots(cfg)
    used = set(state["used_slots"])
    pending = queue(state)
    since = approved_since(state)
    first = slots.due_slot(now, used, since) if since else None
    start = first if first is not None else now
    out: list[tuple[dict, datetime]] = []
    for s in slots.iter_from(start):
        if not pending or (limit is not None and len(out) >= limit):
            break
        if slot_key(s) in used:
            continue
        for idx, item in enumerate(pending):
            if eligible(item, s):
                out.append((pending.pop(idx), s))
                break
        if len(out) > 10000:  # safety
            break
    return out
