# Qwen Max Slack Bot

Slack bot backed by `qwen/qwen4-max:free` on the Nous Portal inference API
(free during the launch promo), with a provider abstraction so the model or
provider can be swapped later (e.g. Groq) without touching the bot code.

## Endpoints

- `GET /health` - service + provider status
- `POST /chat` - `{"message": "...", "model?": "..."}` -> `{"reply", "provider", "model", "usage"}`
- `POST /slack/events` - Slack Events API endpoint (url_verification, app_mention, DM messages)

## How it works

- Slack events are verified with the app signing secret, answered in a thread
  via `chat.postMessage`. Mentions in channels and DMs to the bot both work.
- LLM calls go to Nous Portal's OpenAI-compatible endpoint
  (`https://inference-api.nousresearch.com/v1`). Auth is OAuth device-flow
  (client_id `hermes-cli`): the access token is refreshed with a refresh token.
- **Refresh tokens rotate on every refresh and reuse revokes the session.**
  The backend persists each rotated refresh token to a private GitHub repo
  (`TOKEN_CACHE_REPO`, file `token.json`) and loads it on boot.

## Environment variables

| var | purpose |
| --- | --- |
| `NOUS_REFRESH_TOKEN` | bootstrap Nous refresh token (rotates; cache takes over) |
| `SLACK_SIGNING_SECRET` | Slack app signing secret |
| `SLACK_BOT_TOKEN` | Slack bot OAuth token (xoxb-...) |
| `GITHUB_TOKEN` | token with repo scope, used to persist the rotated Nous RT |
| `TOKEN_CACHE_REPO` | e.g. `azjhf-dev/nous-token-cache` |
| `PROVIDER` | `auto` (default: Nous then Groq), `nous`, or `groq` |
| `MODEL_NAME` | default `qwen/qwen4-max:free` |
| `GROQ_API_KEY` | optional, enables Groq fallback |
| `GROQ_MODEL` | default `llama-3.3-70b-versatile` |

## Switching models/providers

- Another Nous free model: set `MODEL_NAME` (e.g. `upstage/solar-mini4:free`).
- Groq: set `GROQ_API_KEY` and `PROVIDER=groq`, optionally `GROQ_MODEL`.

## Run locally

```
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Deploy (Render free)

Build: `pip install -r requirements.txt`
Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

Render free instances sleep after ~15 min idle. Slack retries events, so the
first mention after an idle period can be delayed while the instance wakes.

Set the Slack app's Event Subscriptions Request URL to
`https://<your-service>.onrender.com/slack/events`.
