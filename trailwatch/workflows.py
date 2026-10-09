"""HikeWorkflow: one durable workflow per trip.

The whole safety promise lives here. Every wait is a Temporal durable timer, so
if the worker, the web app or the Temporal server restarts mid-hike, the timer
still fires at the right moment once things come back. Nothing is held in a
process's memory that a crash could lose.

Time is measured in "trip minutes". In real use one trip minute is 60 s; for
demos `minute_seconds` compresses a 3-hour hike into a few minutes without
changing any logic.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from . import rules
    from .activities import fetch_conditions, plan_trip, send_notice, write_briefing
    from .models import BriefingInput, CheckIn, Conditions, ConditionsQuery, Notice, PlanInput, TripPlan, TripRequest

# How often the contact is re-alerted while a trip stays escalated.
REALERT_EVERY_MIN = 30
MAX_TOTAL_EXTENSION_MIN = 6 * 60


@workflow.defn
class HikeWorkflow:
    def __init__(self) -> None:
        self.req: Optional[TripRequest] = None
        self.plan: Optional[TripPlan] = None
        self.cond = Conditions()
        self.state = "planning"  # planning | out | missed_checkin | overdue | escalated | finished | cancelled
        self.inbox: list[CheckIn] = []
        self.log: list[dict] = []
        self.notices: list[dict] = []
        self.started: Optional[datetime] = None
        self.next_checkin_min = 0.0
        self.return_min = 0.0
        self.extended_min = 0
        self.last_contact_min = 0.0
        self.last_location: Optional[str] = None
        self.alerts_sent = 0
        self.seq = 0

    # ---------- time helpers ----------

    def _elapsed(self) -> float:
        assert self.started and self.req
        return (workflow.now() - self.started).total_seconds() / self.req.minute_seconds

    def _wall(self, trip_min: float) -> datetime:
        assert self.started and self.req
        return self.started + timedelta(seconds=trip_min * self.req.minute_seconds)

    def _local(self, trip_min: float) -> str:
        """Trip-clock local time: start time in the hiker's zone + trip minutes."""
        assert self.started and self.req
        return rules.fmt_local(self.started + timedelta(minutes=self.req.tz_offset_min + trip_min))

    def _note(self, kind: str, text: str) -> None:
        self.log.append({"at": workflow.now().isoformat(), "trip_min": round(self._elapsed(), 1),
                         "local": self._local(self._elapsed()), "kind": kind, "text": text})

    # ---------- signals, updates, queries ----------

    @workflow.signal
    def checkin(self, c: CheckIn) -> None:
        self.inbox.append(c)

    @workflow.update
    def extend_return(self, minutes: int) -> str:
        """Synchronous "I'm running late": the caller gets the new return time back."""
        self.inbox.append(CheckIn(kind="late", minutes=minutes, source="update"))
        return self._local(self.return_min + minutes)

    @extend_return.validator
    def _validate_extend(self, minutes: int) -> None:
        if self.state in ("planning", "finished", "cancelled"):
            raise ValueError(f"Trip is {self.state}; nothing to extend.")
        if not 5 <= minutes <= 180:
            raise ValueError("Extend by 5-180 minutes at a time.")
        if self.extended_min + minutes > MAX_TOTAL_EXTENSION_MIN:
            raise ValueError("That would push the trip more than 6h past the original plan. Start a new trip instead.")

    @workflow.query
    def status(self) -> dict:
        s: dict = {"state": self.state, "log": self.log, "notices": self.notices,
                   "conditions": self.cond, "plan": self.plan, "alerts_sent": self.alerts_sent,
                   "last_location": self.last_location}
        if self.req and self.started:
            s.update(
                hiker=self.req.hiker_name,
                contact=self.req.contact_name,
                description=self.req.description,
                minute_seconds=self.req.minute_seconds,
                started_at=self.started.isoformat(),
                started_local=self._local(0),
                elapsed_min=round(self._elapsed(), 1),
            )
        if self.plan:
            s.update(
                next_checkin_at=self._wall(self.next_checkin_min).isoformat(),
                next_checkin_local=self._local(self.next_checkin_min),
                return_at=self._wall(self.return_min).isoformat(),
                return_local=self._local(self.return_min),
                turnaround_local=self._local(self.plan.turnaround_in_min),
                last_contact_local=self._local(self.last_contact_min),
            )
        return s

    # ---------- main ----------

    @workflow.run
    async def run(self, req: TripRequest) -> dict:
        self.req = req
        self.started = workflow.now()
        self._note("start", f"Trip started: \"{req.description}\"")

        try:
            self.cond = await workflow.execute_activity(
                fetch_conditions,
                ConditionsQuery(req.lat, req.lon, self.started.isoformat()),
                start_to_close_timeout=timedelta(seconds=20),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError:
            self._note("warn", "Weather lookup failed; planning without it.")

        try:
            # Gemma can be cold, out of memory, or return junk JSON. Retry with
            # backoff, but don't make someone wait at the trailhead forever.
            self.plan = await workflow.execute_activity(
                plan_trip,
                PlanInput(req, self.cond, self.started.isoformat()),
                # A cold Gemma load on a 6 GB laptop GPU takes ~70-130 s.
                start_to_close_timeout=timedelta(seconds=180),
                schedule_to_close_timeout=timedelta(minutes=6),
                retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2,
                                         maximum_interval=timedelta(seconds=30), maximum_attempts=6),
            )
        except ActivityError as e:
            self._note("warn", f"Planner unavailable ({e.cause or e}); using the rule-based fallback.")
            now_local = (self.started + timedelta(minutes=req.tz_offset_min)).replace(tzinfo=None)
            self.plan = rules.fallback_plan(req.description, now_local, self.cond)

        p = self.plan
        self.return_min = p.return_in_min
        self.next_checkin_min = min(p.checkin_every_min, self.return_min)
        self.state = "out"
        self._note("plan", f"Armed ({p.source}): back by {self._local(self.return_min)}, "
                           f"check in every {p.checkin_every_min} min, risk {p.risk}.")

        tips = "\n".join(f"- {t}" for t in (p.warnings + p.tips)[:4])
        await self._notify("hiker", "TrailWatch is armed",
                           f"Back by {self._local(self.return_min)}. Turn around by {self._local(p.turnaround_in_min)}.\n"
                           f"First check-in due {self._local(self.next_checkin_min)}. Put your phone away.\n{tips}".strip(),
                           priority=3, tags=["evergreen_tree"], actions=True)
        await self._notify("contact", f"{req.hiker_name} is heading out",
                           f"{req.hiker_name} started: {p.route}. Expected back {self._local(self.return_min)}.\n"
                           f"You'll only hear from TrailWatch again if they miss a check-in, or when they're home.",
                           priority=2, tags=["information_source"])

        await self._watch()

        self._note("end", f"Trip {self.state}.")
        return {"state": self.state, "alerts_sent": self.alerts_sent, "log": self.log}

    async def _watch(self) -> None:
        assert self.plan
        while self.state not in ("finished", "cancelled"):
            if self.state == "escalated":
                if await self._wait_until(self.next_checkin_min):
                    await self._drain()
                else:
                    await self._escalate("Still no word since the last alert.")
                continue

            overdue = self.return_min <= self.next_checkin_min
            deadline = min(self.next_checkin_min, self.return_min)
            if await self._wait_until(deadline):
                await self._drain()
                continue

            # A deadline passed with no word. Nudge the hiker, then give them a grace period.
            self.state = "overdue" if overdue else "missed_checkin"
            what = "You're past your return time" if overdue else "Missed check-in"
            self._note("missed", f"{what}; nudging {self.req.hiker_name}, grace {self.plan.grace_min} min.")
            await self._notify("hiker", f"{what}. Are you OK?",
                               f"Tap I'm OK within {self.plan.grace_min} min or {self.req.contact_name} gets alerted.",
                               priority=5, tags=["warning"], actions=True)
            # Grace is counted from the missed deadline, not from when the nudge
            # went out, so worker downtime can never push the alert later.
            if await self._wait_until(deadline + self.plan.grace_min):
                await self._drain()
                continue
            await self._escalate("Past their return time." if overdue else "Missed a check-in and the grace period.")

    async def _wait_until(self, trip_min: float) -> bool:
        """Durable timer racing the inbox. True if a check-in arrived first."""
        if self.inbox:
            return True
        seconds = (trip_min - self._elapsed()) * self.req.minute_seconds
        if seconds <= 0:
            return False
        try:
            await workflow.wait_condition(lambda: bool(self.inbox), timeout=timedelta(seconds=seconds))
            return True
        except asyncio.TimeoutError:
            return False

    async def _drain(self) -> None:
        assert self.plan and self.req
        while self.inbox:
            c = self.inbox.pop(0)
            now = self._elapsed()
            if c.lat is not None and c.lon is not None:
                self.last_location = f"https://www.openstreetmap.org/?mlat={c.lat:.5f}&mlon={c.lon:.5f}#map=15/{c.lat:.5f}/{c.lon:.5f}"
            was_alerted = self.state == "escalated"

            if c.kind == "sos":
                self._note("sos", f"SOS from {c.source}." + (f" \"{c.note}\"" if c.note else ""))
                await self._escalate("They pressed SOS.", sos=True)
                continue
            if c.kind == "cancel":
                self.state = "cancelled"
                self._note("cancel", "Trip cancelled by hiker.")
                if was_alerted:
                    await self._all_clear("cancelled the trip and is OK")
                return
            if c.kind == "finished":
                self.state = "finished"
                self._note("home", f"{self.req.hiker_name} is back.")
                if was_alerted:
                    await self._all_clear("is back safe")
                else:
                    await self._notify("contact", f"{self.req.hiker_name} is back",
                                       f"{self.req.hiker_name} finished {self.plan.route} at {self._local(now)}. No action needed.",
                                       priority=2, tags=["white_check_mark"])
                return
            if c.kind == "location":
                self._note("location", "Location updated.")
                continue

            # ok / late both count as contact.
            self.last_contact_min = now
            if c.kind == "late":
                m = max(5, min(180, c.minutes or 30))
                m = min(m, MAX_TOTAL_EXTENSION_MIN - self.extended_min)
                self.extended_min += m
                self.return_min = max(self.return_min, now) + m
                self._note("late", f"Running late: return moved to {self._local(self.return_min)}.")
            else:
                self._note("ok", f"Check-in OK via {c.source}.")
            if self.return_min <= now:
                # Past return time but they say they're fine: give them one more interval.
                self.return_min = now + self.plan.checkin_every_min
            self.next_checkin_min = now + self.plan.checkin_every_min
            self.state = "out"
            if was_alerted:
                await self._all_clear("checked in and says they're OK")

    async def _escalate(self, reason: str, sos: bool = False) -> None:
        assert self.plan and self.req
        self.state = "escalated"
        self.alerts_sent += 1
        now = self._elapsed()
        self._note("alert", f"Alerting {self.req.contact_name}: {reason}")

        # 1) The alert itself is a template. It never waits on a model.
        text = rules.alert_text(self.req.hiker_name, self.req.contact_name, self.plan, reason,
                                self._local(0), self._local(self.last_contact_min),
                                self._local(self.return_min), self.last_location)
        title = f"SOS from {self.req.hiker_name}" if sos else f"{self.req.hiker_name} hasn't checked in"
        await self._notify("contact", title, text, priority=5, tags=["rotating_light"])

        # 2) Then, best effort, a Gemma-written brief with the full context.
        if self.alerts_sent == 1 or sos:
            try:
                brief = await workflow.execute_activity(
                    write_briefing,
                    BriefingInput(self.req, self.plan, self.cond, reason, self._local(0),
                                  self._local(self.last_contact_min), self._local(self.return_min),
                                  self.last_location, [f"{e['local']} {e['text']}" for e in self.log]),
                    start_to_close_timeout=timedelta(seconds=180),
                    schedule_to_close_timeout=timedelta(minutes=5),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                await self._notify("contact", "Situation brief (written by Gemma)", brief,
                                   priority=4, tags=["memo"])
            except ActivityError:
                self._note("warn", "Brief unavailable; the template alert already went out.")

        # Keep watching from here: re-alert unless they check in.
        self.next_checkin_min = now + REALERT_EVERY_MIN

    async def _all_clear(self, what: str) -> None:
        assert self.req
        self._note("clear", f"All-clear sent: {self.req.hiker_name} {what}.")
        await self._notify("contact", f"All clear: {self.req.hiker_name} {what}",
                           f"{self.req.hiker_name} {what} at {self._local(self._elapsed())}. You can stand down.",
                           priority=4, tags=["white_check_mark"])

    async def _notify(self, audience: str, title: str, message: str, *, priority: int,
                      tags: list[str], actions: bool = False) -> None:
        assert self.req
        self.seq += 1
        n = Notice(id=f"{workflow.info().workflow_id}-n{self.seq}", trip_id=workflow.info().workflow_id,
                   audience=audience, topic=self.req.hiker_topic if audience == "hiker" else self.req.contact_topic,
                   title=title, message=message, priority=priority, tags=tags, with_actions=actions)
        # Pushes retry until they land (phones and ntfy can both be unreachable), for up to an hour.
        try:
            result = await workflow.execute_activity(
                send_notice, n,
                start_to_close_timeout=timedelta(seconds=20),
                schedule_to_close_timeout=timedelta(hours=1),
                retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), maximum_interval=timedelta(minutes=1)),
            )
        except ActivityError as e:
            # Give up on this one push, but never let it end the watch.
            result = f"FAILED after retries: {e.cause or e}"
            self._note("warn", f"Could not deliver '{title}' to {audience}.")
        self.notices.append({"at": workflow.now().isoformat(), "local": self._local(self._elapsed()),
                             "audience": audience, "title": title, "message": message,
                             "priority": priority, "result": result})
