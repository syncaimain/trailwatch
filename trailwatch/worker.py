"""Run with: python -m trailwatch.worker

Kill it whenever you like. Running trips keep their timers on the Temporal
server and carry on as soon as a worker is back.
"""

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from . import config
from .activities import fetch_conditions, plan_trip, send_notice, write_briefing
from .workflows import HikeWorkflow


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = await Client.connect(config.TEMPORAL_ADDRESS, namespace=config.TEMPORAL_NAMESPACE)
    worker = Worker(
        client,
        task_queue=config.TASK_QUEUE,
        workflows=[HikeWorkflow],
        activities=[fetch_conditions, plan_trip, send_notice, write_briefing],
    )
    logging.info("TrailWatch worker up (queue=%s, model=%s, ntfy=%s)", config.TASK_QUEUE, config.LLM_LABEL, config.NTFY_URL)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
