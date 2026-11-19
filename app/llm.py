"""LLM provider layer: Nous Portal (OAuth device-flow tokens) with Groq fallback.

Nous refresh tokens rotate on every refresh and reuse revokes the session, so
each rotated refresh token must be persisted immediately. Persistence target is
a private GitHub repo used as a tiny KV store (see tokencache.py).
"""
import os
import time
import asyncio
import logging

import httpx

from . import tokencache

log = logging.getLogger("llm")

PORTAL_BASE = "https://portal.nousresearch.com"
INFERENCE_BASE = os.environ.get("NOUS_INFERENCE_BASE", "https://inference-api.nousresearch.com/v1")
NOUS_CLIENT_ID = os.environ.get("NOUS_CLIENT_ID", "hermes-cli")
DEFAULT_MODEL = os.environ.get("MODEL_NAME", "qwen/qwen4-max:free")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "qwen/qwen3.8-27b")
PROVIDER = os.environ.get("PROVIDER", "auto")  # nous | groq | auto

_state = {
    "access_token": None,
    "access_exp": 0.0,
    "refresh_token": os.environ.get("NOUS_REFRESH_TOKEN") or None,
}
_lock = asyncio.Lock()


def nous_configured() -> bool:
    return bool(_state["refresh_token"])


def groq_configured() -> bool:
    return bool(os.environ.get("GROQ_API_KEY"))


async def init_from_cache():
    """On boot, load the newest rotated refresh token from the shared cache."""
    cached = await tokencache.load()
    if cached and cached.get("refresh_token"):
        if cached["refresh_token"] != _state["refresh_token"]:
            log.info("loaded newer Nous refresh token from cache")
        _state["refresh_token"] = cached["refresh_token"]
        _state["access_token"] = cached.get("access_token")
        _state["access_exp"] = float(cached.get("access_exp") or 0)


async def _refresh_nous(client: httpx.AsyncClient) -> str:
    rt = _state["refresh_token"]
    if not rt:
        raise RuntimeError("nous_not_configured")
    r = await client.post(
        f"{PORTAL_BASE}/api/oauth/token",
        headers={"x-nous-refresh-token": rt},
        data={"grant_type": "refresh_token", "client_id": NOUS_CLIENT_ID, "refresh_token": rt},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"nous_refresh_failed:{r.status_code}")
    payload = r.json()
    at = payload.get("access_token")
    new_rt = payload.get("refresh_token") or rt
    if not at:
        raise RuntimeError("nous_refresh_no_access_token")
    _state["access_token"] = at
    _state["access_exp"] = time.time() + int(payload.get("expires_in", 3600)) - 120
    _state["refresh_token"] = new_rt
    if new_rt != rt:
        # Rotation: persist immediately, reuse of a stale RT revokes the session.
        await tokencache.save({
            "refresh_token": new_rt,
            "access_token": at,
            "access_exp": _state["access_exp"],
        })
    return at


async def _nous_access_token(client: httpx.AsyncClient) -> str:
    async with _lock:
        if _state["access_token"] and time.time() < _state["access_exp"]:
            return _state["access_token"]
        return await _refresh_nous(client)


async def _chat_nous(client: httpx.AsyncClient, messages, model=None, max_tokens: int = 1024) -> dict:
    at = await _nous_access_token(client)
    r = await client.post(
        f"{INFERENCE_BASE}/chat/completions",
        headers={"Authorization": f"Bearer {at}"},
        json={"model": model or DEFAULT_MODEL, "messages": messages, "max_tokens": max_tokens},
        timeout=180,
    )
    if r.status_code == 401:
        # one retry with a forced refresh
        at = await _refresh_nous(client)
        r = await client.post(
            f"{INFERENCE_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {at}"},
            json={"model": model or DEFAULT_MODEL, "messages": messages, "max_tokens": max_tokens},
            timeout=180,
        )
    r.raise_for_status()
    data = r.json()
    return {"reply": data["choices"][0]["message"]["content"],
            "provider": "nous", "model": model or DEFAULT_MODEL,
            "usage": data.get("usage")}


async def _chat_groq(client: httpx.AsyncClient, messages, model=None, max_tokens: int = 1024) -> dict:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise RuntimeError("groq_not_configured")
    m = model or GROQ_MODEL
    r = await client.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": m, "messages": messages, "max_tokens": max_tokens},
        timeout=180,
    )
    r.raise_for_status()
    data = r.json()
    return {"reply": data["choices"][0]["message"]["content"],
            "provider": "groq", "model": m, "usage": data.get("usage")}


SYSTEM_PROMPT = (
    "You are a helpful assistant answering questions in a Slack workspace. "
    "Be concise and direct. Format for Slack: short paragraphs, *bold*, `code`."
)


async def chat(user_text: str, model: str = None, max_tokens: int = 1024, system: str = None) -> dict:
    messages = [
        {"role": "system", "content": system or SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]
    async with httpx.AsyncClient() as client:
        errors = []
        if PROVIDER in ("nous", "auto") and nous_configured():
            try:
                return await _chat_nous(client, messages, model, max_tokens)
            except Exception as e:  # noqa: BLE001
                log.warning("nous failed: %s", e)
                errors.append(f"nous: {e}")
                if PROVIDER == "nous":
                    raise
        if PROVIDER in ("groq", "auto") and groq_configured():
            try:
                return await _chat_groq(client, messages, model, max_tokens)
            except Exception as e:  # noqa: BLE001
                log.warning("groq failed: %s", e)
                errors.append(f"groq: {e}")
                if PROVIDER == "groq":
                    raise
    raise RuntimeError("all providers failed: " + "; ".join(errors))


def status() -> dict:
    return {
        "provider_mode": PROVIDER,
        "nous_configured": nous_configured(),
        "groq_configured": groq_configured(),
        "default_model": DEFAULT_MODEL,
        "groq_model": GROQ_MODEL,
    }
