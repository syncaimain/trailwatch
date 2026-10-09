"""Activities: everything that touches the outside world.

Each one may fail (no network, Ollama out of memory, bad JSON) and is retried
by Temporal according to the policy the workflow sets. None of them sleep or
keep state - all waiting happens in the workflow as durable timers.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx
from temporalio import activity

from . import config, llm, rules
from .models import BriefingInput, Conditions, ConditionsQuery, Notice, PlanInput, TripPlan


@activity.defn
async def fetch_conditions(q: ConditionsQuery) -> Conditions:
    """Sunset + next-8h weather from Open-Meteo (open data, no API key)."""
    if q.lat is None or q.lon is None:
        return Conditions()
    params = {
        "latitude": q.lat,
        "longitude": q.lon,
        "timezone": "auto",
        "forecast_days": 2,
        "daily": "sunset",
        "hourly": "temperature_2m,precipitation_probability,wind_gusts_10m",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
        r.raise_for_status()
        d = r.json()

    offset = timedelta(seconds=d.get("utc_offset_seconds", 0))
    now_local = (datetime.fromisoformat(q.started_at) + offset).replace(tzinfo=None)

    sunset_local, minutes_to_sunset = "", None
    for s in d["daily"]["sunset"]:
        t = datetime.fromisoformat(s)
        if t > now_local:
            sunset_local = t.strftime("%H:%M")
            minutes_to_sunset = int((t - now_local).total_seconds() // 60)
            break

    h = d["hourly"]
    window = [
        i for i, t in enumerate(h["time"])
        if now_local - timedelta(hours=1) <= datetime.fromisoformat(t) <= now_local + timedelta(hours=8)
    ]
    temps = [h["temperature_2m"][i] for i in window if h["temperature_2m"][i] is not None]
    precip = [h["precipitation_probability"][i] or 0 for i in window]
    gusts = [h["wind_gusts_10m"][i] or 0 for i in window]

    c = Conditions(
        available=True,
        sunset_local=sunset_local,
        minutes_to_sunset=minutes_to_sunset,
        temp_c=temps[0] if temps else None,
        max_precip_prob=int(max(precip, default=0)),
        max_wind_kmh=float(max(gusts, default=0)),
    )
    parts = []
    if c.temp_c is not None:
        parts.append(f"{c.temp_c:.0f}°C now")
    parts.append(f"rain chance up to {c.max_precip_prob}%")
    parts.append(f"gusts up to {c.max_wind_kmh:.0f} km/h")
    if sunset_local:
        parts.append(f"sunset {sunset_local}")
    c.summary = ", ".join(parts) + " (next 8h)."
    return c


PLAN_SYSTEM = """You turn a person's casual note about an outdoor trip into a JSON plan for a safety check-in timer.
Reply with ONLY a JSON object with these keys:
  "route": short name of where they are going, using their words (string)
  "activity": one of "hike","run","walk","bike","climb","paddle","other"
  "solo": true if they are going alone or don't mention companions
  "expected_return_local": the time they said they'd be back, as 24h "HH:MM"; "" if they gave no time
  "duration_min": your estimate of total minutes out, as an integer
  "turnaround_in_min": minutes from now at which they should turn back to make their return time
  "checkin_every_min": how often (minutes) they should tap "I'm OK"; shorter for solo, remote or risky trips (15-120)
  "risk": "low", "medium" or "high"
  "reasons": up to 4 short reasons for the risk level
  "tips": up to 3 short, specific safety tips for THIS trip and weather
Rules: Never invent places or facts. If they say "back by 5" in the afternoon they mean 17:00. Consider sunset and weather."""


@activity.defn
async def plan_trip(inp: PlanInput) -> TripPlan:
    """Ask local Gemma for a plan, then let the deterministic rules clamp it."""
    attempt = activity.info().attempt
    if attempt <= config.CHAOS_PLAN_FAILS:
        raise llm.ModelError(f"[chaos] injected planner failure on attempt {attempt}")

    req, cond = inp.request, inp.conditions
    now_local = (datetime.fromisoformat(inp.started_at) + timedelta(minutes=req.tz_offset_min)).replace(tzinfo=None)
    user = (
        f"Current local time: {now_local:%H:%M} ({now_local:%A}).\n"
        f"Conditions: {cond.summary}\n"
        f"Their note: \"{req.description}\""
    )
    activity.logger.info("Planning with %s (attempt %d)", config.LLM_LABEL, attempt)
    raw = await llm.chat_json(PLAN_SYSTEM, user)
    return rules.finalize_plan(raw, now_local, cond, source=config.LLM_LABEL)


BRIEF_SYSTEM = """You write a short situation brief for the emergency contact of someone who is overdue from an outdoor trip.
Use ONLY the facts given. Do not speculate about injuries or what happened. Plain text, no markdown, under 90 words.
Structure: one line on the situation; what is known (route, times, last location, conditions); then 2-3 concrete next steps,
starting with trying to call them, and when to contact local emergency services."""


@activity.defn
async def write_briefing(b: BriefingInput) -> str:
    facts = {
        "person": b.request.hiker_name,
        "contact": b.request.contact_name,
        "why_alerted": b.reason,
        "trip_note": b.request.description,
        "route": b.plan.route,
        "activity": b.plan.activity,
        "solo": b.plan.solo,
        "started": b.started_local,
        "expected_back": b.expected_return_local,
        "last_heard_from": b.last_contact_local,
        "last_location": b.last_location or "unknown",
        "conditions": b.conditions.summary,
        "trip_log": b.events[-8:],
    }
    text = await llm.chat(BRIEF_SYSTEM, json.dumps(facts, indent=1), json_mode=False)
    return text.strip()[:900]


# Notice ids already pushed by this worker process. Activities are at-least-once,
# so a retry after a successful push could double-send; this narrows that window.
_sent: set[str] = set()


@activity.defn
async def send_notice(n: Notice) -> str:
    activity.logger.info("NOTICE -> %s [%s] %s: %s", n.audience, n.topic or "no topic", n.title, n.message)
    if not n.topic:
        return "recorded (no ntfy topic set)"
    if n.id in _sent:
        return "duplicate suppressed"

    payload: dict = {
        "topic": n.topic,
        "title": n.title,
        "message": n.message,
        "priority": n.priority,
        "tags": n.tags,
        # The key rides in the fragment, which is never sent to a server.
        "click": f"{config.PUBLIC_BASE_URL}/?trip={n.trip_id}" + (f"#key={config.ACCESS_KEY}" if config.ACCESS_KEY else ""),
    }
    if n.with_actions:
        url = f"{config.PUBLIC_BASE_URL}/api/trips/{n.trip_id}/checkin"
        payload["actions"] = [
            {"action": "http", "label": label, "url": url, "method": "POST",
             "headers": {"Content-Type": "application/json",
                         **({"X-TrailWatch-Key": config.ACCESS_KEY} if config.ACCESS_KEY else {})},
             "body": json.dumps({"source": "ntfy", **body}), "clear": True}
            for label, body in (
                ("I'm OK", {"kind": "ok"}),
                ("+30 min", {"kind": "late", "minutes": 30}),
                ("SOS", {"kind": "sos"}),
            )
        ]
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(config.NTFY_URL, json=payload)
        r.raise_for_status()
    _sent.add(n.id)
    return f"pushed to {config.NTFY_URL}/{n.topic}"
