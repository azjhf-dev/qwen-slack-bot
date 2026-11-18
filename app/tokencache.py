"""Persist the rotated Nous refresh token in a private GitHub repo.

Render free instances have ephemeral disks, so the rotated refresh token is
stored as token.json in a small private repo (TOKEN_CACHE_REPO, e.g.
"owner/nous-token-cache") using the GitHub contents API with GITHUB_TOKEN.
Failing to persist a rotated token would eventually revoke the OAuth session.
"""
import base64
import json
import logging
import os

import httpx

log = logging.getLogger("tokencache")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
REPO = os.environ.get("TOKEN_CACHE_REPO")  # e.g. "azjhf-dev/nous-token-cache"
PATH = "token.json"


def enabled() -> bool:
    return bool(GITHUB_TOKEN and REPO)


def _headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def load() -> dict | None:
    if not enabled():
        return None
    url = f"https://api.github.com/repos/{REPO}/contents/{PATH}"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=_headers(), timeout=20)
            if r.status_code != 200:
                return None
            data = r.json()
            return json.loads(base64.b64decode(data["content"]).decode())
    except Exception as e:  # noqa: BLE001
        log.warning("token cache load failed: %s", e)
        return None


async def save(state: dict) -> bool:
    if not enabled():
        log.warning("token cache not configured; rotated refresh token not persisted")
        return False
    url = f"https://api.github.com/repos/{REPO}/contents/{PATH}"
    body = base64.b64encode(json.dumps(state).encode()).decode()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=_headers(), timeout=20)
            sha = r.json().get("sha") if r.status_code == 200 else None
            payload = {"message": "rotate nous refresh token", "content": body}
            if sha:
                payload["sha"] = sha
            r = await client.put(url, headers=_headers(), json=payload, timeout=20)
            if r.status_code not in (200, 201):
                log.warning("token cache save failed: %s %s", r.status_code, r.text[:200])
                return False
            return True
    except Exception as e:  # noqa: BLE001
        log.warning("token cache save failed: %s", e)
        return False
