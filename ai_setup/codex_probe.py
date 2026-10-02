"""A tool-free Codex test using the form's model and account credentials."""

import asyncio
from datetime import datetime
import json
import time
from urllib.error import HTTPError
from urllib.request import Request
import uuid

from .codex_auth import AuthError, CLIENT_ID, CLIENT_VERSION, USER_AGENT, credentials, token_claims, token_request
from .responses_probe import request_test_response


RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"


def needs_refresh(auth):
    if not auth.get("access_token"):
        return True
    expiry = token_claims(auth["access_token"]).get("exp")
    if isinstance(expiry, (int, float)) and expiry <= time.time() + 300:
        return True
    try:
        return datetime.fromisoformat(auth.get("last_refresh", "").replace("Z", "+00:00")).timestamp() <= time.time() - 3300
    except ValueError:
        return False


def test_codex(settings, prompt, cancelled, auth, on_refresh):
    started = time.monotonic()
    deadline = started + 120

    def check():
        if cancelled.is_set():
            raise asyncio.CancelledError
        if time.monotonic() >= deadline:
            raise TimeoutError("The model test timed out")

    def refresh():
        nonlocal auth
        check()
        value = credentials(token_request({"client_id": CLIENT_ID, "grant_type": "refresh_token",
                                           "refresh_token": auth["refresh_token"]}), auth)
        # Persist rotation even if the inference fails or is cancelled afterwards.
        auth = on_refresh(auth, value) or value

    check()
    if not auth.get("refresh_token"):
        raise AuthError("auth_required")
    if needs_refresh(auth):
        refresh()
    body = {"model": settings["model_id"], "instructions": "Answer briefly in the user's language.",
            "input": [{"role": "user", "content": prompt}], "stream": True, "store": False,
            "reasoning": {"effort": settings["reasoning_effort"]}, "prompt_cache_key": str(uuid.uuid4())}
    if settings["reasoning_effort"] != "none":
        body["reasoning"]["summary"] = "auto"
    for attempt in range(2):
        check()
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream",
                   "Authorization": "Bearer " + auth.get("access_token", ""),
                   "originator": "codex_cli_rs", "User-Agent": USER_AGENT, "Version": CLIENT_VERSION,
                   "OpenAI-Beta": "responses=experimental",
                   "session-id": body["prompt_cache_key"]}
        if auth.get("account_id"):
            headers["ChatGPT-Account-ID"] = auth["account_id"]
        try:
            text = request_test_response(Request(RESPONSES_URL, data=json.dumps(body).encode(), headers=headers), check, deadline)
            break
        except HTTPError as error:
            status = error.code
            error.close()
            if status == 401 and attempt == 0:
                refresh()
                continue
            raise RuntimeError(f"Codex request failed (HTTP {status})") from None
    return {"text": text, "seconds": time.monotonic() - started}
