"""Chaos demo: hard-kill the worker mid-hike and show the alert still fires.

  python scripts/kill_test.py                     # 1 trip-minute = 1 s
  python scripts/kill_test.py --minute-seconds 2  # slower, easier to film

Needs `temporal server start-dev` running. Uses Ollama if it's up, otherwise
the planner retries and falls back to rules (that's part of the show too).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from temporalio.client import Client  # noqa: E402

from trailwatch import config  # noqa: E402
from trailwatch.models import CheckIn, TripRequest  # noqa: E402
from trailwatch.workflows import HikeWorkflow  # noqa: E402

T0 = time.monotonic()
LIVE = ROOT / ".data" / "kill_live.txt"  # the /demo screen tails this


def say(msg: str) -> None:
    line = f"[{time.monotonic() - T0:6.1f}s] {msg}"
    print(line, flush=True)
    with LIVE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def spawn_worker() -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, "-m", "trailwatch.worker"], cwd=ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=os.environ.copy())
    say(f"worker started (pid {p.pid})")
    return p


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minute-seconds", type=float, default=1.0)
    ap.add_argument("--trip", default="Quick solo loop up the ridge behind campus, back in 40 minutes")
    ap.add_argument("--hiker-topic", default="")
    ap.add_argument("--contact-topic", default="")
    ap.add_argument("--lead", type=float, default=0, help="seconds to wait before starting (for recording)")
    args = ap.parse_args()
    LIVE.parent.mkdir(exist_ok=True)
    LIVE.write_text("", encoding="utf-8")
    await asyncio.sleep(args.lead)

    client = await Client.connect(config.TEMPORAL_ADDRESS, namespace=config.TEMPORAL_NAMESPACE)
    worker = spawn_worker()
    trip_id = f"hike-killtest-{uuid.uuid4().hex[:6]}"
    tz = int(datetime.now().astimezone().utcoffset().total_seconds() // 60)
    h = await client.start_workflow(
        HikeWorkflow.run,
        TripRequest(description=args.trip, hiker_name="Ali", contact_name="Sam",
                    hiker_topic=args.hiker_topic, contact_topic=args.contact_topic,
                    tz_offset_min=tz, minute_seconds=args.minute_seconds),
        id=trip_id, task_queue=config.TASK_QUEUE)
    say(f"trip started: {trip_id}")
    say(f"watch it live: {config.TEMPORAL_UI}/namespaces/{config.TEMPORAL_NAMESPACE}/workflows/{trip_id}")

    while True:
        s = await h.query(HikeWorkflow.status)
        if s["state"] != "planning":
            break
        await asyncio.sleep(0.5)
    p = s["plan"]
    say(f"armed by {p['source']}: back by {s['return_local']}, check in every {p['checkin_every_min']} min, "
        f"grace {p['grace_min']} min")

    due = datetime.fromisoformat(s["next_checkin_at"])
    deadline_min = (due - datetime.fromisoformat(s["started_at"])).total_seconds() / args.minute_seconds
    alert_at = datetime.fromisoformat(s["started_at"]).timestamp() + (deadline_min + p["grace_min"]) * args.minute_seconds
    say(f"no check-ins will be sent. Contact alert is due in {alert_at - time.time():.0f}s")

    await asyncio.sleep(2)
    worker.kill()  # TerminateProcess on Windows, SIGKILL elsewhere: no cleanup, no goodbye
    worker.wait()
    say("KILLED the worker. Nothing is running TrailWatch code now.")

    while time.time() < alert_at + 5:
        left = alert_at - time.time()
        say(f"  worker dead... alert {'due in %.0fs' % left if left > 0 else 'is %.0fs OVERDUE' % -left}")
        await asyncio.sleep(5)

    desc = await h.describe()
    say(f"workflow status while worker is dead: {desc.status.name} (timers keep running on the server)")
    worker = spawn_worker()

    while True:
        s = await h.query(HikeWorkflow.status)
        if s["state"] == "escalated":
            break
        await asyncio.sleep(0.5)
    late = time.time() - alert_at
    say(f"contact ALERTED {late:.1f}s after the scheduled time (that's the restart gap, not lost state)")
    for n in s["notices"]:
        t = datetime.fromisoformat(n["at"]).astimezone().strftime("%H:%M:%S")
        say(f"  {t} -> {n['audience']:7} | {n['title']}")

    await asyncio.sleep(6)
    say("hiker gets signal back and taps I'm OK")
    await h.signal(HikeWorkflow.checkin, CheckIn(kind="ok", source="ntfy"))
    while (await h.query(HikeWorkflow.status))["state"] != "out":
        await asyncio.sleep(0.5)
    say("contact got the all-clear")
    await asyncio.sleep(5)
    say("hiker taps I'm home")
    await h.signal(HikeWorkflow.checkin, CheckIn(kind="finished", source="ntfy"))
    result = await h.result()
    say(f"trip closed: {result['state']}, alerts sent: {result['alerts_sent']}")
    worker.kill()


if __name__ == "__main__":
    asyncio.run(main())
