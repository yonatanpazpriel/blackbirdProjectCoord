"""Small Slack Web API client for sending creator summaries."""

from __future__ import annotations

from typing import Any

import requests

SLACK_API_URL = "https://slack.com/api"
_REQUEST_TIMEOUT = 30


class SlackError(Exception):
    """Generic Slack API error."""


def _post(method: str, payload: dict[str, Any], *, bot_token: str) -> dict[str, Any]:
    response = requests.post(
        f"{SLACK_API_URL}/{method}",
        headers={
            "Authorization": f"Bearer {bot_token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=payload,
        timeout=_REQUEST_TIMEOUT,
    )
    if not response.ok:
        raise SlackError(f"HTTP {response.status_code}: {response.text}")

    body = response.json()
    if not body.get("ok"):
        raise SlackError(body.get("error") or f"{method} failed")
    return body


def lookup_user_id_by_email(email: str, *, bot_token: str) -> str:
    result = _post("users.lookupByEmail", {"email": email}, bot_token=bot_token)
    user = result.get("user") or {}
    user_id = user.get("id")
    if not user_id:
        raise SlackError(f"Slack user lookup returned no user id for {email!r}")
    return user_id


def open_dm(user_id: str, *, bot_token: str) -> str:
    result = _post("conversations.open", {"users": user_id}, bot_token=bot_token)
    channel = result.get("channel") or {}
    channel_id = channel.get("id")
    if not channel_id:
        raise SlackError(f"Slack conversations.open returned no channel for {user_id!r}")
    return channel_id


def post_message(channel_id: str, text: str, *, bot_token: str) -> dict[str, Any]:
    result = _post(
        "chat.postMessage",
        {"channel": channel_id, "text": text},
        bot_token=bot_token,
    )
    return {"channel": result.get("channel"), "ts": result.get("ts")}


def send_dm_by_email(email: str, text: str, *, bot_token: str) -> dict[str, Any]:
    user_id = lookup_user_id_by_email(email, bot_token=bot_token)
    channel_id = open_dm(user_id, bot_token=bot_token)
    result = post_message(channel_id, text, bot_token=bot_token)
    return {"user_id": user_id, **result}
