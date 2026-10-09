"""Plain dataclasses passed between the app, the workflow and activities.

Temporal's default data converter serialises these to JSON, so everything here
must stay JSON-friendly (no datetimes - we use ISO strings and minute offsets).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TripRequest:
    description: str  # free text: "Monk's Trail up Doi Suthep, solo, back by 5ish"
    hiker_name: str
    contact_name: str
    hiker_topic: str = ""  # ntfy topic the hiker's phone subscribes to
    contact_topic: str = ""  # ntfy topic the emergency contact subscribes to
    lat: Optional[float] = None
    lon: Optional[float] = None
    tz_offset_min: int = 0  # browser's UTC offset, e.g. +420 for Thailand (ICT)
    # How many real seconds one "trip minute" lasts. 60 = real time.
    # Lower values compress a whole hike into a few minutes for demos.
    minute_seconds: float = 60.0


@dataclass
class ConditionsQuery:
    lat: Optional[float]
    lon: Optional[float]
    started_at: str  # ISO UTC


@dataclass
class Conditions:
    available: bool = False
    sunset_local: str = ""  # "HH:MM" on the trip day, local time
    minutes_to_sunset: Optional[int] = None
    temp_c: Optional[float] = None
    max_precip_prob: int = 0  # next 8h
    max_wind_kmh: float = 0.0  # next 8h
    summary: str = "No forecast available."


@dataclass
class PlanInput:
    request: TripRequest
    conditions: Conditions
    started_at: str  # ISO UTC


@dataclass
class TripPlan:
    route: str
    activity: str
    solo: bool
    return_in_min: int  # trip minutes from start until expected return
    turnaround_in_min: int
    checkin_every_min: int
    grace_min: int  # how long a missed check-in waits before the contact is alerted
    risk: str  # low | medium | high
    reasons: list[str] = field(default_factory=list)
    tips: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # added by deterministic rules, not the model
    source: str = "gemma"  # "gemma" or "fallback"


@dataclass
class CheckIn:
    kind: str  # ok | late | sos | finished | cancel | location
    minutes: int = 0  # for "late"
    note: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    source: str = "web"  # web | ntfy | test


@dataclass
class Notice:
    id: str  # deterministic: <workflow id>-n<seq>, used for de-duplication
    trip_id: str
    audience: str  # hiker | contact
    topic: str
    title: str
    message: str
    priority: int = 3  # ntfy priority 1..5
    tags: list[str] = field(default_factory=list)
    with_actions: bool = False  # attach I'm OK / Late / SOS buttons


@dataclass
class BriefingInput:
    request: TripRequest
    plan: TripPlan
    conditions: Conditions
    reason: str
    started_local: str
    last_contact_local: str
    expected_return_local: str
    last_location: Optional[str]
    events: list[str] = field(default_factory=list)
