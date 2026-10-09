"""Web app + API. Run with: uvicorn trailwatch.app:app --host 0.0.0.0 --port 8000

Stateless: every trip lives in Temporal. Restart this freely.
"""

from __future__ import annotations

import hmac
from datetime import timedelta
import re
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.service import RPCError

from . import config
from .models import CheckIn, TripRequest
from .workflows import HikeWorkflow

app = FastAPI(title="TrailWatch")
STATIC = Path(__file__).parent / "static"
_client: Optional[Client] = None
DATA = Path(__file__).resolve().parent.parent / ".data"


@app.middleware("http")
async def require_key(request: Request, call_next):
    """If TRAILWATCH_ACCESS_KEY is set (do this before exposing the app through a
    tunnel), every API call needs it in the X-TrailWatch-Key header. The page
    picks it up from the URL fragment, which browsers never send to servers."""
    if config.ACCESS_KEY and request.url.path.startswith("/api/") and request.url.path != "/api/config":
        if not hmac.compare_digest(request.headers.get("x-trailwatch-key", ""), config.ACCESS_KEY):
            return JSONResponse({"detail": "Missing or wrong access key."}, status_code=401)
    return await call_next(request)


async def client() -> Client:
    global _client
    if _client is None:
        _client = await Client.connect(config.TEMPORAL_ADDRESS, namespace=config.TEMPORAL_NAMESPACE)
    return _client


class StartBody(BaseModel):
    description: str = Field(min_length=3, max_length=500)
    hiker_name: str = Field(min_length=1, max_length=40)
    contact_name: str = Field(min_length=1, max_length=40)
    hiker_topic: str = ""
    contact_topic: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    tz_offset_min: int = 0
    minute_seconds: float = Field(default=60.0, ge=0.5, le=60.0)


class CheckInBody(BaseModel):
    kind: str = Field(pattern="^(ok|late|sos|finished|cancel|location)$")
    minutes: int = 0
    note: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    source: str = "web"


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
async def get_config() -> dict:
    return {"temporal_ui": config.TEMPORAL_UI, "namespace": config.TEMPORAL_NAMESPACE,
            "ntfy_url": config.NTFY_URL, "model": config.LLM_LABEL, "needs_key": bool(config.ACCESS_KEY)}


@app.get("/demo")
async def demo() -> FileResponse:
    return FileResponse(STATIC / "demo.html")


@app.get("/api/demo/console")
async def demo_console() -> PlainTextResponse:
    """Live output of scripts/kill_test.py, for the demo screen."""
    f = DATA / "kill_live.txt"
    return PlainTextResponse(f.read_text(encoding="utf-8") if f.exists() else "")


@app.get("/api/trips/{trip_id}/history")
async def trip_history(trip_id: str) -> list[dict]:
    """The workflow's event history, straight from Temporal."""
    c = await client()
    out = []
    try:
        async for e in c.get_workflow_handle(trip_id).fetch_history_events():
            name = e.WhichOneof("attributes") or ""
            attrs = getattr(e, name, None)
            detail = ""
            if attrs is not None and hasattr(attrs, "activity_type"):
                detail = attrs.activity_type.name
            elif attrs is not None and hasattr(attrs, "signal_name"):
                detail = attrs.signal_name
            elif attrs is not None and hasattr(attrs, "start_to_fire_timeout"):
                detail = f"{attrs.start_to_fire_timeout.ToTimedelta().total_seconds():.0f}s"
            out.append({"id": e.event_id, "type": EventType.Name(e.event_type).removeprefix("EVENT_TYPE_"),
                        "time": e.event_time.ToDatetime().isoformat() + "Z", "detail": detail})
    except RPCError as e:
        raise HTTPException(404, f"No history: {e.message}")
    return out


@app.post("/api/trips")
async def start_trip(b: StartBody) -> dict:
    slug = re.sub(r"[^a-z0-9]+", "-", b.hiker_name.lower()).strip("-")[:20] or "hiker"
    trip_id = f"hike-{slug}-{uuid.uuid4().hex[:6]}"
    for t in (b.hiker_topic, b.contact_topic):
        if t and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", t):
            raise HTTPException(400, "ntfy topics may only use letters, numbers, - and _")
    c = await client()
    await c.start_workflow(HikeWorkflow.run, TripRequest(**b.model_dump()), id=trip_id, task_queue=config.TASK_QUEUE)
    return {"trip_id": trip_id}


@app.get("/api/trips")
async def list_trips() -> list[dict]:
    c = await client()
    out = []
    async for w in c.list_workflows("WorkflowType = 'HikeWorkflow'", limit=15):
        out.append({"trip_id": w.id, "status": w.status.name if w.status else "?",
                    "started": w.start_time.isoformat() if w.start_time else None})
    return out


@app.get("/api/trips/{trip_id}")
async def trip_status(trip_id: str) -> dict:
    c = await client()
    h = c.get_workflow_handle(trip_id)
    try:
        desc = await h.describe()
    except RPCError as e:
        raise HTTPException(404, f"Trip not found: {e.message}")
    try:
        s = await h.query(HikeWorkflow.status, rpc_timeout=timedelta(seconds=4))
    except RPCError:
        # No worker to answer the query. The trip itself is safe on the server.
        return {"worker_offline": True, "workflow_status": desc.status.name if desc.status else "?"}
    s["workflow_status"] = desc.status.name if desc.status else "?"
    s["temporal_url"] = f"{config.TEMPORAL_UI}/namespaces/{config.TEMPORAL_NAMESPACE}/workflows/{trip_id}"
    return s


@app.post("/api/trips/{trip_id}/checkin")
async def checkin(trip_id: str, b: CheckInBody) -> dict:
    c = await client()
    h = c.get_workflow_handle(trip_id)
    try:
        if b.kind == "late":
            # An Update, not a Signal: the phone gets the new return time back,
            # or a clear rejection from the workflow's validator.
            new_return = await h.execute_update(HikeWorkflow.extend_return, b.minutes or 30)
            return {"ok": True, "return_local": new_return}
        await h.signal(HikeWorkflow.checkin, CheckIn(**b.model_dump()))
    except WorkflowUpdateFailedError as e:
        raise HTTPException(409, str(e.cause.message if e.cause else e))
    except RPCError as e:
        raise HTTPException(404, f"Trip not running: {e.message}")
    return {"ok": True}
