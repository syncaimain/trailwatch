"""Deterministic safety rules.

The model proposes a plan; these rules have the final say. Pure functions with
no I/O, so they are safe to call from inside the workflow sandbox (the fallback
planner runs there when Gemma is unavailable).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Optional

from .models import Conditions, TripPlan

MAX_TRIP_MIN = 14 * 60
DEFAULT_TRIP_MIN = 180
ACTIVITIES = {"hike", "run", "walk", "bike", "climb", "paddle", "other"}


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def hhmm_to_minutes_ahead(hhmm: str, now_local: datetime) -> Optional[int]:
    """Minutes from now until the next sensible occurrence of HH:MM.

    "05:00" said at 14:00 almost always means 17:00, so we try +12h before
    rolling over to tomorrow.
    """
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", hhmm or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    target = now_local.replace(hour=h, minute=mi, second=0, microsecond=0)
    if target <= now_local and h < 12:
        target += timedelta(hours=12)
    if target <= now_local:
        target += timedelta(days=1)
    return int((target - now_local).total_seconds() // 60)


def _as_int(v: Any) -> Optional[int]:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def finalize_plan(raw: dict, now_local: datetime, cond: Conditions, source: str) -> TripPlan:
    """Turn a model's (or the regex fallback's) proposal into a safe plan."""
    warnings: list[str] = []

    ret = hhmm_to_minutes_ahead(str(raw.get("expected_return_local") or ""), now_local)
    dur = _as_int(raw.get("duration_min"))
    if ret is None or ret > MAX_TRIP_MIN:
        ret = dur
    elif dur and dur > 0 and abs(ret - dur) > max(60, dur // 2):
        # Gemma once turned "back in 40 minutes" at 23:00 into "12:39" (13h away).
        # When its clock time and its duration disagree, trust the earlier one:
        # a false alarm is better than a late one.
        warnings.append(f"Return time was ambiguous; using the earlier estimate ({min(ret, dur)} min).")
        ret = min(ret, dur)
    if ret is None or ret <= 0:
        ret = DEFAULT_TRIP_MIN
        warnings.append(f"No return time understood - assuming {DEFAULT_TRIP_MIN // 60}h. Send 'Late' if you need longer.")
    if ret > MAX_TRIP_MIN:
        warnings.append("Trips over 14h are capped; start a new trip for multi-day outings.")
    ret = clamp(ret, 20, MAX_TRIP_MIN)

    solo = bool(raw.get("solo", False))
    risk = str(raw.get("risk") or "medium").lower()
    if risk not in ("low", "medium", "high"):
        risk = "medium"

    # Conditions can only raise the risk level, never lower what the model said.
    if cond.minutes_to_sunset is not None and ret > cond.minutes_to_sunset:
        warnings.append(f"You'll still be out after sunset ({cond.sunset_local}). Pack a headlamp.")
        if risk == "low":
            risk = "medium"
    if cond.max_precip_prob >= 60:
        warnings.append(f"{cond.max_precip_prob}% chance of rain while you're out.")
        if risk == "low":
            risk = "medium"
    if cond.max_wind_kmh >= 40:
        warnings.append(f"Gusty: up to {cond.max_wind_kmh:.0f} km/h wind.")
        risk = {"low": "medium", "medium": "high", "high": "high"}[risk]

    every = _as_int(raw.get("checkin_every_min")) or 60
    if solo:
        every = min(every, 60)
    if risk == "high":
        every = min(every, 45)
    every = clamp(every, 15, 120)

    grace = {"low": 20, "medium": 15, "high": 10}[risk]

    # Gemma 3 4B once suggested turning around at minute 1440 of a 3-hour hike.
    # A turnaround must leave time to walk back, so anything past 75% is replaced.
    turnaround = _as_int(raw.get("turnaround_in_min"))
    if turnaround is None or not 5 <= turnaround <= ret * 3 // 4:
        turnaround = ret // 2

    activity = str(raw.get("activity") or "hike").lower()
    if activity not in ACTIVITIES:
        activity = "other"

    def str_list(key: str, n: int) -> list[str]:
        v = raw.get(key) or []
        if not isinstance(v, list):
            v = [v]
        return [str(x)[:140] for x in v if str(x).strip()][:n]

    return TripPlan(
        route=(str(raw.get("route") or "").strip() or "Unnamed outing")[:80],
        activity=activity,
        solo=solo,
        return_in_min=ret,
        turnaround_in_min=turnaround,
        checkin_every_min=every,
        grace_min=grace,
        risk=risk,
        reasons=str_list("reasons", 4),
        tips=str_list("tips", 3),
        warnings=warnings,
        source=source,
    )


_BACK_BY = re.compile(r"back\s*(?:by|at|around|before)?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I)
_FOR_DUR = re.compile(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hours?|m|min|mins|minutes?)\b", re.I)


def fallback_plan(description: str, now_local: datetime, cond: Conditions) -> TripPlan:
    """Regex planner used when the model is down. Boring on purpose."""
    raw: dict[str, Any] = {"route": description.split(",")[0][:60], "risk": "medium"}
    m = _BACK_BY.search(description)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2) or 0)
        ampm = (m.group(3) or "").lower()
        if ampm == "pm" and h < 12:
            h += 12
        if ampm == "am" and h == 12:
            h = 0
        raw["expected_return_local"] = f"{h:02d}:{mi:02d}"
    else:
        d = _FOR_DUR.search(description)
        if d:
            n = float(d.group(1))
            raw["duration_min"] = int(n * 60 if d.group(2).lower().startswith("h") else n)
    raw["solo"] = bool(re.search(r"\b(solo|alone|by myself|on my own)\b", description, re.I))
    for a in ACTIVITIES:
        if re.search(rf"\b{a}", description, re.I):
            raw["activity"] = a
            break
    return finalize_plan(raw, now_local, cond, source="fallback")


def fmt_local(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def alert_text(
    hiker: str,
    contact: str,
    plan: TripPlan,
    reason: str,
    started_local: str,
    last_contact_local: str,
    expected_return_local: str,
    last_location: Optional[str],
) -> str:
    """The first alert to the emergency contact. Template only - it must never
    wait on a model."""
    lines = [
        f"{contact}, {hiker} has not checked in. ({reason})",
        f"Trip: {plan.route} ({plan.activity}{', solo' if plan.solo else ''})",
        f"Started {started_local}, expected back {expected_return_local}.",
        f"Last heard from them at {last_contact_local}.",
    ]
    if last_location:
        lines.append(f"Last known location: {last_location}")
    lines.append("Try calling them first. If you can't reach them, contact local emergency services and share this message.")
    return "\n".join(lines)
