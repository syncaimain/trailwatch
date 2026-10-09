# TrailWatch

**A hiking check-in timer that can't forget about you.**

Tell it where you're going and when you'll be back, in your own words. A local
Gemma model turns that into a plan: return time, turnaround time, how often to
check in, and safety tips based on sunset and the forecast. Then you put your
phone away.

Each trip is a [Temporal](https://temporal.io) workflow. It sleeps on durable
timers until a check-in is due. If you don't tap **I'm OK**, it nudges you,
waits a grace period, then alerts your emergency contact: first with a template
message (instantly, no AI on the critical path), then with a situation brief
written by local Gemma. When you surface and tap OK, your contact gets an
all-clear.

Kill the worker, restart the web app, reboot the Temporal server mid-hike: the
timer still fires.

> TrailWatch is a prototype. It is not a replacement for a satellite messenger,
> a PLB, or telling a person where you're going.

## How it works

```
phone (web page / ntfy buttons)
   │  start trip · I'm OK (signal) · +30 min (update) · SOS · I'm home
   ▼
FastAPI app (stateless) ──► Temporal server ◄── worker
                             one HikeWorkflow per trip
                             ├─ fetch_conditions  Open-Meteo sunset + weather
                             ├─ plan_trip         Gemma via Ollama → rules clamp it
                             ├─ durable timers    check-in due / grace / re-alert
                             ├─ send_notice       ntfy push, retried until it lands
                             └─ write_briefing    Gemma situation brief for the contact
```

| Temporal feature | What it does here |
|---|---|
| Durable timers | Check-in deadlines, grace periods and re-alerts. They live on the server, not in a process. |
| Signals | I'm OK, SOS, I'm home, cancel: from the web page or straight from ntfy notification buttons. |
| Update + validator | "+30 min" returns the new return time to the phone, or rejects silly extensions (>3h at once, >6h total). |
| Query | The web page reads live trip state from the workflow itself. No database. |
| Activity retries | Gemma cold starts, OOMs and bad JSON retry with backoff, then fall back to a rule-based planner. Pushes retry for up to an hour. |
| Absolute deadlines | Grace is counted from the missed deadline, so worker downtime never delays an alert. |

Safety rules live in plain code (`trailwatch/rules.py`), not in the prompt: the
model can suggest a plan, but it can't stretch check-ins past 60 min on a solo
trip, lower the risk level the weather implies, or delay the first alert.

## Run it

You need Python 3.11+, the [Temporal CLI](https://docs.temporal.io/cli) and
[Ollama](https://ollama.com).

```bash
ollama pull gemma3:4b
temporal server start-dev --db-filename .data/temporal.db
```

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # .venv/bin on macOS/Linux
.venv/Scripts/python -m trailwatch.worker
.venv/Scripts/uvicorn trailwatch.app:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000. Temporal UI: http://localhost:8233.

### Hosted Gemma instead of local Ollama

Any OpenAI-compatible API works. For Gemma on Google AI Studio (free tier),
get a key at https://aistudio.google.com/apikey, then before starting the worker:

```powershell
$env:LLM_API_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
$env:LLM_API_KEY  = "<your key>"
$env:TRAILWATCH_MODEL = "gemma-3-27b-it"
```

The model stays open-weight; only where it runs changes. Local Ollama remains
the default because it works with no internet and nothing leaves the machine.

### Phone alerts

Install the [ntfy](https://ntfy.sh) app, subscribe to two hard-to-guess topics
(one for you, one for your contact's phone) and enter them under *Phone alerts*.
For the I'm OK / +30 / SOS buttons to work from the lock screen, your phone must
reach the app: set `PUBLIC_BASE_URL` to a Tailscale or ngrok URL.

| Env var | Default |
|---|---|
| `TRAILWATCH_MODEL` | `gemma3:4b` |
| `OLLAMA_URL` | `http://localhost:11434` |
| `LLM_API_BASE` / `LLM_API_KEY` | empty = local Ollama; set both to use a hosted OpenAI-compatible API |
| `NTFY_URL` | `https://ntfy.sh` (or your self-hosted ntfy) |
| `PUBLIC_BASE_URL` | `http://localhost:8000` |
| `TEMPORAL_ADDRESS` | `localhost:7233` |
| `TRAILWATCH_ACCESS_KEY` | empty. **Set it before any public tunnel.** API calls then need it; open the page once as `/#key=<key>` and it's remembered. ntfy buttons send it as a header. |
| `TRAILWATCH_CHAOS_PLAN_FAILS` | `0`: make the first N planner attempts fail, to show retries |

## Prove it

```bash
.venv/Scripts/python -m pytest -q             # 11 tests; a 6-hour silent hike runs in seconds
.venv/Scripts/python scripts/kill_test.py     # hard-kills the worker mid-trip, shows the alert still fires
```

To film it: open `http://localhost:8000/demo?topics=<hiker-topic>,<contact-topic>`
in its own window (a split screen of the phone view, the kill-test console, live
Temporal event history and the real ntfy pushes), then run
`scripts/record_demo.ps1` (Windows + ffmpeg) alongside `kill_test.py --lead 4`.
The result is in `media/`.

The workflow tests use Temporal's time-skipping test server. The worker-death
test and `kill_test.py` run against the real dev server with a compressed clock
(1 trip-minute = 1 s), and use the same code path as real time.
