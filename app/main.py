"""Qwen Max Slack bot backend: FastAPI service exposing /chat and /slack/events."""
import hmac
import logging
import os
import time
from collections import defaultdict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import llm, slackapp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = FastAPI(title="qwen-slack-bot")

# --- /chat access gate (Shorts Lab AI clipping) -----------------------------
# The Slack bot calls llm.chat in-process and is unaffected by this gate.
CLIP_TOKEN = os.environ.get("CLIP_ACCESS_TOKEN") or ""
ALLOWED_ORIGINS = {
    o.strip()
    for o in os.environ.get(
        "CLIP_ALLOWED_ORIGINS",
        "https://azjhf-dev.github.io,https://shorts-lab.surge.sh",
    ).split(",")
    if o.strip()
}
RATE_PER_MIN = int(os.environ.get("CLIP_RATE_PER_MIN", "10"))
RATE_PER_DAY = int(os.environ.get("CLIP_RATE_PER_DAY", "200"))
MAX_TOKENS_CAP = int(os.environ.get("CLIP_MAX_TOKENS", "4096"))
_minute_hits: dict[str, list[float]] = defaultdict(list)
_day = {"date": "", "count": 0}


def _cors(origin: str) -> dict:
    if origin in ALLOWED_ORIGINS:
        return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
    return {}


def _rate_limited(ip: str) -> bool:
    now = time.time()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if _day["date"] != today:
        _day.update(date=today, count=0)
    hits = [t for t in _minute_hits[ip] if now - t < 60]
    _minute_hits[ip] = hits
    if len(hits) >= RATE_PER_MIN or _day["count"] >= RATE_PER_DAY:
        return True
    hits.append(now)
    _day["count"] += 1
    return False


@app.on_event("startup")
async def startup():
    await llm.init_from_cache()


@app.get("/health")
async def health():
    return {"ok": True, **llm.status()}


class ChatIn(BaseModel):
    message: str | None = None
    messages: list[dict] | None = None
    model: str | None = None
    max_tokens: int | None = None
    system: str | None = None


@app.options("/chat")
async def chat_options(request: Request):
    h = _cors(request.headers.get("origin", ""))
    h.update({
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
        "Access-Control-Max-Age": "600",
    })
    return JSONResponse({}, headers=h)


@app.post("/chat")
async def chat_endpoint(inp: ChatIn, request: Request):
    h = _cors(request.headers.get("origin", ""))
    if not CLIP_TOKEN:
        # fail closed: no token configured, nobody gets in
        return JSONResponse({"error": "chat endpoint disabled"}, status_code=503, headers=h)
    auth = request.headers.get("authorization", "")
    expected = f"Bearer {CLIP_TOKEN}"
    if not hmac.compare_digest(auth, expected):
        return JSONResponse({"error": "unauthorized"}, status_code=401, headers=h)
    ip = request.client.host if request.client else "unknown"
    if _rate_limited(ip):
        return JSONResponse({"error": "rate limited, try again later"}, status_code=429, headers=h)
    model = inp.model or None
    if model and not model.endswith(":free"):
        return JSONResponse({"error": "only free-tier models are allowed on this endpoint"}, status_code=400, headers=h)
    message = inp.message
    if not message and inp.messages:
        parts = [str(m.get("content", "")) for m in inp.messages if m.get("role") != "system"]
        message = "\n".join(p for p in parts if p)
        if not inp.system:
            sys_parts = [str(m.get("content", "")) for m in inp.messages if m.get("role") == "system"]
            inp.system = "\n".join(p for p in sys_parts if p) or None
    if not message:
        return JSONResponse({"error": "message or messages required"}, status_code=400, headers=h)
    max_tokens = min(inp.max_tokens or 1024, MAX_TOKENS_CAP)
    try:
        result = await llm.chat(message, model, max_tokens, inp.system)
        return JSONResponse(result, headers=h)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=502, headers=h)


@app.post("/slack/events")
async def slack_events(request: Request):
    return await slackapp.slack_events(request)
