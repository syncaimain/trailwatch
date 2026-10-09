import os

TEMPORAL_ADDRESS = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
TEMPORAL_NAMESPACE = os.getenv("TEMPORAL_NAMESPACE", "default")
TEMPORAL_UI = os.getenv("TEMPORAL_UI", "http://localhost:8233")
TASK_QUEUE = os.getenv("TRAILWATCH_TASK_QUEUE", "trailwatch")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
# Set LLM_API_BASE to use a hosted OpenAI-compatible API instead of local Ollama, e.g.
#   https://generativelanguage.googleapis.com/v1beta/openai  (TRAILWATCH_MODEL=gemma-3-27b-it)
#   https://openrouter.ai/api/v1                              (TRAILWATCH_MODEL=google/gemma-3-27b-it)
LLM_API_BASE = os.getenv("LLM_API_BASE", "").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
MODEL = os.getenv("TRAILWATCH_MODEL", "gemma3:4b")
LLM_LABEL = f"{MODEL} via {'API' if LLM_API_BASE else 'local Ollama'}"

# ntfy server. Self-host it (it's open source) or use the public ntfy.sh.
# Pushes are only sent when a trip has a topic set.
NTFY_URL = os.getenv("NTFY_URL", "https://ntfy.sh").rstrip("/")
# Where the phone can reach this app, for the I'm OK / Late / SOS buttons.
# e.g. your Tailscale or ngrok URL. localhost only works on this machine.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

# Set before exposing the app through a public tunnel (ngrok, Tailscale Funnel).
ACCESS_KEY = os.getenv("TRAILWATCH_ACCESS_KEY", "")

# Failure injection for demos: make the first N plan attempts fail.
CHAOS_PLAN_FAILS = int(os.getenv("TRAILWATCH_CHAOS_PLAN_FAILS", "0"))
