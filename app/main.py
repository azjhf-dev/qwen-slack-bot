"""Qwen Max Slack bot backend: FastAPI service exposing /chat and /slack/events."""
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import llm, slackapp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = FastAPI(title="qwen-slack-bot")


@app.on_event("startup")
async def startup():
    await llm.init_from_cache()


@app.get("/health")
async def health():
    return {"ok": True, **llm.status()}


class ChatIn(BaseModel):
    message: str
    model: str | None = None
    max_tokens: int | None = None
    system: str | None = None


@app.post("/chat")
async def chat_endpoint(inp: ChatIn):
    try:
        return await llm.chat(inp.message, inp.model, inp.max_tokens or 1024, inp.system)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/slack/events")
async def slack_events(request: Request):
    return await slackapp.slack_events(request)
