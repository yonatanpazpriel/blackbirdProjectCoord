"""Google Calendar v3 client.

Authenticates with the OAuth refresh-token grant (one-time consent dance,
refresh token stored in env) and creates events with auto-generated Google
Meet conferences. We call the REST API directly with ``requests`` rather
than pulling in ``google-api-python-client`` + its transport deps.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import requests

GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_EVENTS_URL = (
    "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
)
_REQUEST_TIMEOUT = 30
# Refresh slightly before expiry to avoid races with in-flight requests.
_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 60


class GoogleCalendarError(Exception):
    """Generic Google Calendar API error."""


_access_token_cache: dict[str, Any] = {"token": None, "expires_at": 0.0}


def _get_access_token(
    *, client_id: str, client_secret: str, refresh_token: str
) -> str:
    """Exchange the refresh token for a short-lived access token.

    Cached in-process until ~60s before expiry.
    """
    now = time.time()
    cached = _access_token_cache.get("token")
    expires_at = _access_token_cache.get("expires_at", 0.0)
    if cached and now < expires_at - _ACCESS_TOKEN_REFRESH_SKEW_SECONDS:
        return cached

    response = requests.post(
        GOOGLE_OAUTH_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=_REQUEST_TIMEOUT,
    )
    if not response.ok:
        raise GoogleCalendarError(
            f"oauth token refresh failed: HTTP {response.status_code}: {response.text}"
        )
    body = response.json()
    token = body.get("access_token")
    if not token:
        raise GoogleCalendarError(f"oauth token response missing access_token: {body!r}")

    expires_in = float(body.get("expires_in") or 0)
    _access_token_cache["token"] = token
    _access_token_cache["expires_at"] = now + expires_in
    return token


def _extract_meet_link(event: dict[str, Any]) -> str | None:
    """Pull the Google Meet URL out of an events.insert response."""
    if event.get("hangoutLink"):
        return event["hangoutLink"]
    conference = event.get("conferenceData") or {}
    for entry in conference.get("entryPoints") or []:
        if entry.get("entryPointType") == "video" and entry.get("uri"):
            return entry["uri"]
    return None


def create_event(
    *,
    calendar_id: str,
    summary: str,
    description: str,
    start_iso: str,
    end_iso: str,
    attendees: list[dict[str, Any]],
    time_zone: str | None = None,
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> dict[str, Any]:
    """Create a calendar event with a Google Meet conference attached.

    ``start_iso`` / ``end_iso`` should be RFC 3339 / ISO-8601 strings. If
    they include a UTC offset, ``time_zone`` may be omitted and Google will
    infer it.
    """
    start_block: dict[str, Any] = {"dateTime": start_iso}
    end_block: dict[str, Any] = {"dateTime": end_iso}
    if time_zone:
        start_block["timeZone"] = time_zone
        end_block["timeZone"] = time_zone

    body: dict[str, Any] = {
        "summary": summary,
        "description": description,
        "start": start_block,
        "end": end_block,
        "attendees": attendees,
        "conferenceData": {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
        "guestsCanSeeOtherGuests": True,
    }

    access_token = _get_access_token(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
    )

    response = requests.post(
        GOOGLE_CALENDAR_EVENTS_URL.format(calendar_id=calendar_id),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        params={"conferenceDataVersion": 1, "sendUpdates": "all"},
        json=body,
        timeout=_REQUEST_TIMEOUT,
    )
    if not response.ok:
        raise GoogleCalendarError(
            f"events.insert failed: HTTP {response.status_code}: {response.text}"
        )

    event = response.json()
    meet_link = _extract_meet_link(event)
    if not meet_link:
        raise GoogleCalendarError(
            "event created but Google Meet link was not provisioned; "
            "check that the OAuth user's domain allows Meet creation"
        )

    return {
        "event_id": event.get("id"),
        "html_link": event.get("htmlLink"),
        "meet_link": meet_link,
        "start": (event.get("start") or {}).get("dateTime") or start_iso,
        "end": (event.get("end") or {}).get("dateTime") or end_iso,
        "attendees": [
            {"email": a.get("email"), "response_status": a.get("responseStatus")}
            for a in (event.get("attendees") or [])
        ],
    }
