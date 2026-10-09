"""Workflow tests on Temporal's time-skipping test server.

Real-time trips (minute_seconds=60): a 3-hour hike with missed check-ins runs in
a couple of seconds because the test server fast-forwards durable timers.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.api.workflowservice.v1 import GetSystemInfoRequest
from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from trailwatch.models import BriefingInput, CheckIn, Conditions, ConditionsQuery, Notice, PlanInput, TripPlan, TripRequest
from trailwatch.workflows import HikeWorkflow

QUEUE = "test-trailwatch"


class Fakes:
    def __init__(self, plan_fails: bool = False) -> None:
        self.sent: list[Notice] = []
        self.plan_calls = 0
        self.plan_fails = plan_fails

    def activities(self):
        @activity.defn(name="fetch_conditions")
        async def fetch_conditions(q: ConditionsQuery) -> Conditions:
            return Conditions(available=True, sunset_local="17:45", minutes_to_sunset=400, summary="Clear.")

        @activity.defn(name="plan_trip")
        async def plan_trip(inp: PlanInput) -> TripPlan:
            self.plan_calls += 1
            if self.plan_fails:
                raise ApplicationError("Ollama is down", non_retryable=self.plan_calls >= 2)
            return TripPlan(route="Monk's Trail", activity="hike", solo=True, return_in_min=180,
                            turnaround_in_min=90, checkin_every_min=60, grace_min=15, risk="medium")

        @activity.defn(name="send_notice")
        async def send_notice(n: Notice) -> str:
            self.sent.append(n)
            return "recorded"

        @activity.defn(name="write_briefing")
        async def write_briefing(b: BriefingInput) -> str:
            return f"BRIEF: {b.request.hiker_name} overdue on {b.plan.route}."

        return [fetch_conditions, plan_trip, send_notice, write_briefing]

    def titles(self, audience: str) -> list[str]:
        return [n.title for n in self.sent if n.audience == audience]


def request(desc: str = "Monk's Trail solo, back by 5") -> TripRequest:
    return TripRequest(description=desc, hiker_name="Ali", contact_name="Sam", minute_seconds=60)


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


async def start(env, fakes, req=None):
    worker = Worker(env.client, task_queue=QUEUE, workflows=[HikeWorkflow], activities=fakes.activities())
    handle = await env.client.start_workflow(HikeWorkflow.run, req or request(),
                                             id=f"hike-{uuid.uuid4().hex[:6]}", task_queue=QUEUE)
    return worker, handle


async def test_happy_path_never_alarms_contact(env):
    f = Fakes()
    worker, h = await start(env, f)
    async with worker:
        for _ in range(2):
            await env.sleep(timedelta(minutes=50))
            await h.signal(HikeWorkflow.checkin, CheckIn(kind="ok", source="test"))
        await env.sleep(timedelta(minutes=30))
        await h.signal(HikeWorkflow.checkin, CheckIn(kind="finished", source="test"))
        result = await h.result()
    assert result["state"] == "finished"
    assert result["alerts_sent"] == 0
    assert f.titles("contact") == ["Ali is heading out", "Ali is back"]


async def test_missed_checkin_nudges_then_escalates_then_all_clear(env):
    f = Fakes()
    worker, h = await start(env, f)
    async with worker:
        # Check-in due at 60 min. Nothing arrives.
        await env.sleep(timedelta(minutes=61))
        s = await h.query(HikeWorkflow.status)
        assert s["state"] == "missed_checkin"
        assert "Missed check-in. Are you OK?" in f.titles("hiker")
        assert s["alerts_sent"] == 0

        # 15 min grace passes -> contact alerted, template first then Gemma brief.
        await env.sleep(timedelta(minutes=15))
        s = await h.query(HikeWorkflow.status)
        assert s["state"] == "escalated"
        assert f.titles("contact")[1:] == ["Ali hasn't checked in", "Situation brief (written by Gemma)"]

        # Hiker had no signal; they surface and tap OK -> contact stands down.
        await h.signal(HikeWorkflow.checkin, CheckIn(kind="ok", lat=18.80, lon=98.92, source="test"))
        await env.sleep(timedelta(minutes=1))
        s = await h.query(HikeWorkflow.status)
        assert s["state"] == "out"
        assert f.titles("contact")[-1].startswith("All clear")
        assert "openstreetmap" in s["last_location"]
        await h.signal(HikeWorkflow.checkin, CheckIn(kind="finished"))
        assert (await h.result())["alerts_sent"] == 1


async def test_worker_dead_through_deadline_alerts_the_moment_it_returns():
    """Integration test against a real dev server (`temporal server start-dev`), real time.

    The time-skipping server won't skip while a task waits for a worker, so
    this runs with a compressed clock instead: 1 trip-minute = 0.1 s.
    """
    try:
        client = await Client.connect("localhost:7233")
        await client.workflow_service.get_system_info(GetSystemInfoRequest(), timeout=timedelta(seconds=2))
    except Exception:
        pytest.skip("no Temporal dev server on localhost:7233")
    f = Fakes()
    queue = f"test-kill-{uuid.uuid4().hex[:6]}"
    req = request()
    req.minute_seconds = 0.1  # check-in due at 6 s, grace ends at 7.5 s
    h = await client.start_workflow(HikeWorkflow.run, req, id=f"hike-kill-{uuid.uuid4().hex[:6]}", task_queue=queue)
    async with Worker(client, task_queue=queue, workflows=[HikeWorkflow], activities=f.activities(),
                      max_cached_workflows=0):
        while len(f.sent) < 2:  # armed
            await asyncio.sleep(0.1)
    # Worker gone. Check-in deadline and grace both pass while nothing is running.
    await asyncio.sleep(10)
    assert f.titles("contact") == ["Ali is heading out"]
    async with Worker(client, task_queue=queue, workflows=[HikeWorkflow], activities=f.activities()):
        for _ in range(50):
            await asyncio.sleep(0.2)
            if "Ali hasn't checked in" in f.titles("contact"):
                break
        # Nudge and alert both go out right after restart: downtime didn't add another grace period.
        assert f.titles("hiker")[-1] == "Missed check-in. Are you OK?"
        assert "Ali hasn't checked in" in f.titles("contact")
        await h.signal(HikeWorkflow.checkin, CheckIn(kind="finished"))
        assert (await h.result())["alerts_sent"] == 1


async def test_whole_silent_hike_realerts_every_30_min(env):
    f = Fakes()
    worker, h = await start(env, f)
    async with worker:
        await env.sleep(timedelta(hours=6))
        s = await h.query(HikeWorkflow.status)
        # First alert at 75 min, then every 30 min: 75, 105, ..., 345 -> 10 alerts in 6h.
        assert s["state"] == "escalated"
        assert s["alerts_sent"] == 10
        # Only the first alert gets a (slow, model-written) brief.
        assert f.titles("contact").count("Situation brief (written by Gemma)") == 1
        await h.signal(HikeWorkflow.checkin, CheckIn(kind="cancel"))
        await h.result()


async def test_late_update_moves_return_and_validator_rejects_nonsense(env):
    f = Fakes()
    worker, h = await start(env, f)
    async with worker:
        await env.sleep(timedelta(minutes=5))
        before = (await h.query(HikeWorkflow.status))["return_local"]
        new_return = await h.execute_update(HikeWorkflow.extend_return, 45)
        assert new_return != before
        with pytest.raises(WorkflowUpdateFailedError):
            await h.execute_update(HikeWorkflow.extend_return, 500)
        await env.sleep(timedelta(minutes=1))
        s = await h.query(HikeWorkflow.status)
        assert s["return_local"] == new_return
        await h.signal(HikeWorkflow.checkin, CheckIn(kind="finished"))
        await h.result()


async def test_sos_alerts_immediately(env):
    f = Fakes()
    worker, h = await start(env, f)
    async with worker:
        await env.sleep(timedelta(minutes=10))
        await h.signal(HikeWorkflow.checkin, CheckIn(kind="sos", note="twisted ankle"))
        await env.sleep(timedelta(seconds=30))
        s = await h.query(HikeWorkflow.status)
        assert s["state"] == "escalated"
        assert "SOS from Ali" in f.titles("contact")
        await h.signal(HikeWorkflow.checkin, CheckIn(kind="finished"))
        await h.result()


async def test_planner_down_falls_back_to_rules(env):
    f = Fakes(plan_fails=True)
    worker, h = await start(env, f, request("Lake loop with a friend, about 2 hours"))
    async with worker:
        await env.sleep(timedelta(minutes=2))
        s = await h.query(HikeWorkflow.status)
        assert s["plan"]["source"] == "fallback"
        assert s["plan"]["return_in_min"] == 120
        assert f.plan_calls == 2  # retried, then gave up on a non-retryable error
        await h.signal(HikeWorkflow.checkin, CheckIn(kind="finished"))
        await h.result()
