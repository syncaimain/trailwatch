"""Model client. Raises on any failure so Temporal can retry the activity.

Two backends, same open-weight model family:
  - Ollama on this machine (default): works offline, nothing leaves the laptop.
  - Any OpenAI-compatible API (set LLM_API_BASE): Google AI Studio's Gemma,
    OpenRouter, Groq, DigitalOcean, a vLLM box, LM Studio...
"""

from __future__ import annotations

import json
import re

import httpx

from . import config


class ModelError(Exception):
    pass


async def chat(system: str, user: str, *, json_mode: bool, timeout: float = 170.0) -> str:
    if config.LLM_API_BASE:
        return await _chat_api(system, user, json_mode=json_mode, timeout=timeout)
    return await _chat_ollama(system, user, json_mode=json_mode, timeout=timeout)


async def _chat_ollama(system: str, user: str, *, json_mode: bool, timeout: float) -> str:
    body = {
        "model": config.MODEL,
        "stream": False,
        "keep_alive": "30m",
        "options": {"temperature": 0.2},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if json_mode:
        body["format"] = "json"
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            r = await client.post(f"{config.OLLAMA_URL}/api/chat", json=body)
        except httpx.HTTPError as e:
            raise ModelError(f"Ollama unreachable at {config.OLLAMA_URL}: {e!r}") from e
    if r.status_code != 200:
        raise ModelError(f"Ollama returned {r.status_code}: {r.text[:200]}")
    return _nonempty(r.json().get("message", {}).get("content", ""))


async def _chat_api(system: str, user: str, *, json_mode: bool, timeout: float) -> str:
    # Some hosts (e.g. Gemma on Google AI Studio) reject system messages, so the
    # instructions ride in the user turn. Works on every OpenAI-compatible host.
    body = {
        "model": config.MODEL,
        "temperature": 0.2,
        "messages": [{"role": "user", "content": f"{system}\n\n---\n\n{user}"}],
    }
    headers = {"Authorization": f"Bearer {config.LLM_API_KEY}"} if config.LLM_API_KEY else {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            r = await client.post(f"{config.LLM_API_BASE}/chat/completions", json=body, headers=headers)
        except httpx.HTTPError as e:
            raise ModelError(f"LLM API unreachable at {config.LLM_API_BASE}: {e!r}") from e
    if r.status_code != 200:
        # 429/5xx are worth retrying; Temporal does that for us.
        raise ModelError(f"LLM API returned {r.status_code}: {r.text[:200]}")
    try:
        content = r.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as e:
        raise ModelError(f"Unexpected LLM API response: {r.text[:200]}") from e
    return _nonempty(content)


def _nonempty(content: str | None) -> str:
    if not content or not content.strip():
        raise ModelError("Model returned an empty reply")
    return content


async def chat_json(system: str, user: str) -> dict:
    text = await chat(system, user, json_mode=True)
    # Hosted models without a JSON mode often wrap the object in ```json fences.
    m = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(m.group(0) if m else text)
    except json.JSONDecodeError as e:
        raise ModelError(f"Model returned invalid JSON: {text[:200]}") from e
    if not isinstance(data, dict):
        raise ModelError(f"Model returned JSON that isn't an object: {text[:200]}")
    return data
