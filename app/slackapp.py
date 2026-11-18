"""Slack Events API handling: signature verification, event routing, replies."""
import asyncio
import hashlib
import hmac
import logging
import os
import re
import time

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse

from . import llm

log = logging.getLogger("slackapp")

SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
_seen_events: set[str] = set()


def verify_signature(body: bytes, ts: str, sig: str) -> bool:
    if not SIGNING_SECRET or not ts or not sig:
        return False
    if abs(time.time() - int(ts)) > 60 * 5:
        return False
    base = b"v0:" + ts.encode() + b":" + body
    digest = "v0=" + hmac.new(SIGNING_SECRET.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, sig)


async def post_message(channel: str, text: str, thread_ts: str | None = None):
    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {BOT_TOKEN}"},
            json=payload,
            timeout=20,
        )
        data = r.json()
        if not data.get("ok"):
            log.warning("chat.postMessage failed: %s", data.get("error"))
        return data


async def _answer_and_post(channel: str, text: str, thread_ts: str):
    try:
        result = await llm.chat(text)
        reply = result["reply"].strip() or "(empty reply)"
    except Exception as e:  # noqa: BLE001
        log.warning("llm failed: %s", e)
        reply = f":warning: backend error: {e}"
    await post_message(channel, reply, thread_ts)


MENTION_RE = re.compile(r"<@[A-Z0-9]+>")


async def slack_events(request: Request):
    body = await request.body()
    ts = request.headers.get("x-slack-request-timestamp", "")
    sig = request.headers.get("x-slack-signature", "")
    if not verify_signature(body, ts, sig):
        return PlainTextResponse("invalid signature", status_code=401)

    payload = await request.json()
    ptype = payload.get("type")
    if ptype == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge")})
    if ptype != "event_callback":
        return JSONResponse({"ok": True})

    event = payload.get("event") or {}
    event_id = payload.get("event_id")
    if event_id:
        if event_id in _seen_events:
            return JSONResponse({"ok": True})  # Slack retry, already handled
        _seen_events.add(event_id)
        if len(_seen_events) > 5000:
            _seen_events.clear()

    etype = event.get("type")
    if event.get("bot_id") or event.get("subtype") in ("bot_message", "message_changed", "message_deleted"):
        return JSONResponse({"ok": True})

    if etype == "app_mention":
        text = MENTION_RE.sub("", event.get("text", "")).strip()
        if not text:
            text = "Hello! What can I help with?"
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event.get("ts")
        asyncio.create_task(_answer_and_post(channel, text, thread_ts))
    elif etype == "message" and event.get("channel_type") == "im":
        text = (event.get("text") or "").strip()
        if text:
            asyncio.create_task(_answer_and_post(event["channel"], text, event.get("ts")))

    return JSONResponse({"ok": True})
