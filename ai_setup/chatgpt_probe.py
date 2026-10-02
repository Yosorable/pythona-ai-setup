"""Public model discovery and a tool-free ChatGPT-plan setup test."""

import asyncio
import json
import re
import time
from urllib.error import HTTPError
from urllib.request import Request

from .chatgpt_auth import AuthError, RESOURCE, USER_AGENT, needs_refresh, plan_enabled, read_json_response, refresh_credentials, without_tokens
from .responses_probe import request_test_response


def request_error(value, status=None):
    response = value.get("response")
    error = (response.get("error") if isinstance(response, dict) else None) or value.get("error") or value
    code = error.get("code") if isinstance(error, dict) else None
    keys = {"subscription_sharing_usage_limit_exceeded": "chatgpt_usage_limit",
            "subscription_sharing_user_not_eligible": "chatgpt_not_eligible",
            "subscription_sharing_usage_unavailable": "chatgpt_unavailable",
            "subscription_sharing_user_unavailable": "chatgpt_unavailable",
            "subscription_sharing_unsupported_capability": "chatgpt_unsupported",
            "chatpass_v2_scope_not_authorized": "chatgpt_plan_required",
            "chatpass_v2_invalid_authorization_context": "chatgpt_plan_required"}
    if code in keys:
        return AuthError(keys[code])
    detail = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_]{1,100}", code) else "incomplete_response"
    message = error.get("message") if isinstance(error, dict) else None
    message = message or value.get("detail")
    suffix = ": " + message[:1000] if isinstance(message, str) else ""
    return RuntimeError(f"ChatGPT request failed ({'HTTP ' + str(status) + '; ' if status else ''}{detail}){suffix}")


def error_response(error):
    try:
        data = json.loads(error.read(131072))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}
    finally:
        error.close()


def headers(auth):
    return {"Authorization": "Bearer " + auth.get("access_token", ""), "User-Agent": USER_AGENT}


def renew_credentials(auth, on_refresh):
    try:
        value = refresh_credentials(auth)
    except AuthError as error:
        if str(error) == "chatgpt_refresh_expired":
            current = on_refresh(auth, without_tokens(auth))
            if current and current.get("refresh_token") and current["refresh_token"] != auth.get("refresh_token"):
                return current
        raise
    return on_refresh(auth, value) or value


def list_models(auth, on_refresh=lambda previous, value: value):
    if needs_refresh(auth):
        auth = renew_credentials(auth, on_refresh)
    if not plan_enabled(auth):
        raise AuthError("chatgpt_plan_required")
    for attempt in range(2):
        try:
            value = read_json_response(Request(RESOURCE + "/models", headers=headers(auth)), limit=1048576)
            break
        except HTTPError as error:
            status, data = error.code, error_response(error)
            if status == 401 and attempt == 0:
                auth = renew_credentials(auth, on_refresh)
                if not plan_enabled(auth):
                    raise AuthError("chatgpt_plan_required")
                continue
            raise request_error(data, status) from None
    if not isinstance(value.get("models"), list):
        raise ValueError("Invalid ChatGPT model catalog")
    result, seen = [], set()
    for model in value["models"]:
        if not isinstance(model, dict) or model.get("visibility") != "list":
            continue
        slug = model.get("slug")
        if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", slug) or slug in seen:
            continue
        seen.add(slug)
        name = model.get("display_name")
        result.append({"id": slug, "name": name if isinstance(name, str) and name else slug})
    return result


def test_chatgpt(settings, prompt, cancelled, auth, on_refresh):
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
        auth = renew_credentials(auth, on_refresh)

    check()
    if not auth.get("refresh_token") or not auth.get("client_id"):
        raise AuthError("chatgpt_auth_required")
    if needs_refresh(auth):
        refresh()
    if not plan_enabled(auth):
        raise AuthError("chatgpt_plan_required")
    body = {"model": settings["model_id"], "instructions": "Answer briefly in the user's language.",
            "input": [{"role": "user", "content": prompt}], "stream": True, "store": False,
            "reasoning": {"effort": settings["reasoning_effort"]}}
    if settings["reasoning_effort"] != "none":
        body["reasoning"]["summary"] = "auto"
    for attempt in range(2):
        check()
        try:
            text = request_test_response(Request(RESOURCE + "/responses", data=json.dumps(body).encode(),
                                                 headers={**headers(auth), "Content-Type": "application/json", "Accept": "text/event-stream"}),
                                         check, deadline, request_error)
            break
        except HTTPError as error:
            status, data = error.code, error_response(error)
            if status == 401 and attempt == 0:
                refresh()
                if not plan_enabled(auth):
                    raise AuthError("chatgpt_plan_required")
                continue
            raise request_error(data, status) from None
    return {"text": text, "seconds": time.monotonic() - started}
