"""Tool-name dispatcher for Tavus tool_call events.

Each handler takes the `arguments` dict from the tool call and returns a
JSON-serializable dict that gets relayed back to the LLM via the webhook
HTTP response body.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable

from server import google_calendar, linear_client, meeting_registry, slack_client
from server.priority import to_linear_priority

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python <3.9, shouldn't hit on this project but keep us safe.
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment,misc]


_EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CALENDAR_DESCRIPTION_MAX_CHARS = 8000

# Defaults used when Charlie's Action Mode call omits these fields.
_DEFAULT_PRIORITY = "medium"
_DEFAULT_DURATION_MINUTES = 30
_DEFAULT_START_HOUR = 10  # 10am local
_DEFAULT_TIMEZONE_NAME = "America/Los_Angeles"


def _default_timezone() -> Any:
    """Resolve DEFAULT_TIMEZONE from env, falling back to America/Los_Angeles,
    then UTC if zoneinfo is unavailable or the name is invalid."""
    name = os.environ.get("DEFAULT_TIMEZONE") or _DEFAULT_TIMEZONE_NAME
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return timezone.utc


def _next_business_day_at(hour: int, *, tz: Any) -> datetime:
    """Return the next weekday (Mon–Fri) at ``hour:00`` in ``tz``.

    "Next" is always tomorrow or later; we never schedule for today even if
    it's still morning, because Charlie's defaults are for the *upcoming*
    business day from the user's POV.
    """
    today = datetime.now(tz).date()
    candidate = today + timedelta(days=1)
    while candidate.weekday() >= 5:  # 5 = Sat, 6 = Sun
        candidate += timedelta(days=1)
    return datetime.combine(candidate, time(hour=hour), tzinfo=tz)


class ToolError(Exception):
    """Raised when a tool call can't be fulfilled. The message is surfaced
    back to the LLM as the `error` field in the webhook response."""


def _create_linear_ticket(arguments: dict[str, Any]) -> dict[str, Any]:
    # priority is now optional — server defaults to "medium" when Charlie
    # is in Action Mode and didn't get one from the user.
    required = ("assignee", "title", "description")
    missing = [k for k in required if not arguments.get(k)]
    if missing:
        raise ToolError(f"missing required argument(s): {', '.join(missing)}")

    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        raise ToolError("server not configured: LINEAR_API_KEY unset")

    team_key = os.environ.get("LINEAR_DEFAULT_TEAM_KEY")
    if not team_key:
        raise ToolError("server not configured: LINEAR_DEFAULT_TEAM_KEY unset")

    priority_value = arguments.get("priority") or _DEFAULT_PRIORITY
    try:
        priority = to_linear_priority(priority_value)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc

    try:
        team_id = linear_client.resolve_team_id(team_key, api_key=api_key)
    except linear_client.LinearError as exc:
        raise ToolError(f"linear team lookup failed: {exc}") from exc

    try:
        assignee_id = linear_client.resolve_assignee_id(
            arguments["assignee"], api_key=api_key
        )
    except linear_client.AssigneeNotFound as exc:
        raise ToolError(str(exc)) from exc
    except linear_client.AssigneeAmbiguous as exc:
        raise ToolError(str(exc)) from exc
    except linear_client.LinearError as exc:
        raise ToolError(f"linear assignee lookup failed: {exc}") from exc

    description = arguments["description"]
    file_links = arguments.get("file_links") or []
    if file_links:
        bullets = "\n".join(f"- {link}" for link in file_links if link)
        if bullets:
            description = f"{description}\n\n**Links**\n{bullets}"

    try:
        issue = linear_client.create_issue(
            team_id=team_id,
            title=arguments["title"],
            description=description,
            priority=priority,
            assignee_id=assignee_id,
            api_key=api_key,
        )
    except linear_client.LinearError as exc:
        raise ToolError(f"linear issueCreate failed: {exc}") from exc

    return {
        "issue_id": issue["id"],
        "identifier": issue["identifier"],
        "url": issue["url"],
        "defaults_applied": {"priority": priority_value} if not arguments.get("priority") else {},
    }


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ToolError(f"server not configured: {name} unset")
    return value


def _display_user(user: dict[str, Any] | None) -> dict[str, Any] | None:
    if not user:
        return None
    return {
        "id": user.get("id"),
        "name": user.get("displayName") or user.get("name"),
        "email": user.get("email"),
    }


def _ticket_dump(issue: dict[str, Any]) -> dict[str, Any]:
    """Project a Linear `get_issue_context` response into the shape we
    surface to charlie-meet via tools and persist in the meeting registry.

    Keep this in sync with the persona-side schema so
    ``get_linear_ticket_context`` and ``get_meeting_context`` return
    interchangeable ticket shapes.
    """
    return {
        "issue_id": issue.get("id"),
        "identifier": issue.get("identifier"),
        "url": issue.get("url"),
        "title": issue.get("title"),
        "description": issue.get("description"),
        "priority": issue.get("priority"),
        "due_date": issue.get("dueDate"),
        "created_at": issue.get("createdAt"),
        "updated_at": issue.get("updatedAt"),
        "creator": _display_user(issue.get("creator")),
        "assignee": _display_user(issue.get("assignee")),
        "status": (issue.get("state") or {}).get("name"),
        "team": issue.get("team"),
        "labels": [
            label.get("name")
            for label in ((issue.get("labels") or {}).get("nodes") or [])
            if label.get("name")
        ],
        "comments": [
            {
                "body": comment.get("body"),
                "created_at": comment.get("createdAt"),
                "user": _display_user(comment.get("user")),
            }
            for comment in ((issue.get("comments") or {}).get("nodes") or [])
        ],
    }


def _get_linear_ticket_context(arguments: dict[str, Any]) -> dict[str, Any]:
    ticket_id_or_url = arguments.get("ticket_id_or_url")
    if not ticket_id_or_url:
        raise ToolError("missing required argument(s): ticket_id_or_url")

    api_key = _require_env("LINEAR_API_KEY")
    default_team_key = os.environ.get("LINEAR_DEFAULT_TEAM_KEY")
    try:
        issue = linear_client.get_issue_context(
            ticket_id_or_url, api_key=api_key, default_team_key=default_team_key
        )
    except linear_client.LinearError as exc:
        raise ToolError(f"linear issue lookup failed: {exc}") from exc

    return _ticket_dump(issue)


def _get_meeting_context(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return the ticket charlie-meet should discuss in this Google Meet.

    Resolution order:
      1. If ``meet_link`` is supplied, exact-match the registry.
      2. Else, claim the most recently scheduled meeting that hasn't been
         claimed yet (within a 6-hour freshness window).

    Single-user demo concurrency model — if two meets start within seconds
    and neither passes ``meet_link``, the first ``get_meeting_context``
    call wins the newest record. Pass ``meet_link`` when you can.
    """
    meet_link = (arguments.get("meet_link") or "").strip()
    if meet_link:
        record = meeting_registry.lookup_by_meet_link(meet_link)
        if not record:
            raise ToolError(
                f"no scheduled meeting found in registry for meet_link {meet_link!r}; "
                "fall back to asking the attendee for the Linear ticket ID"
            )
    else:
        record = meeting_registry.claim_most_recent_unclaimed()
        if not record:
            raise ToolError(
                "no recently scheduled meeting available; ask the attendee "
                "for the Linear ticket ID and use get_linear_ticket_context instead"
            )

    return {
        "meet_link": record.get("meet_link"),
        "ticket": record.get("ticket"),
        "scheduled": {
            "start": record.get("start"),
            "end": record.get("end"),
            "event_id": record.get("event_id"),
        },
    }


def _listify(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(value)]


def _format_bullets(values: list[str]) -> str:
    if not values:
        return "- None reported"
    return "\n".join(f"- {value}" for value in values)


def _format_summary_message(arguments: dict[str, Any], issue: dict[str, Any]) -> str:
    identifier = issue.get("identifier") or arguments["ticket_id_or_url"]
    title = issue.get("title") or "Untitled Linear ticket"
    url = issue.get("url")
    attendee = arguments["attendee"]
    summary = arguments["summary"]
    blockers = _listify(arguments.get("blockers"))
    questions = _listify(arguments.get("questions"))
    next_steps = _listify(arguments.get("next_steps"))

    ticket_line = f"*{identifier}: {title}*"
    if url:
        ticket_line = f"<{url}|{ticket_line}>"

    return "\n".join(
        [
            f"Charlie Meet summary for {ticket_line}",
            f"*Attendee:* {attendee}",
            "",
            f"*Summary:*\n{summary}",
            "",
            f"*Blockers:*\n{_format_bullets(blockers)}",
            "",
            f"*Questions for you:*\n{_format_bullets(questions)}",
            "",
            f"*Next steps:*\n{_format_bullets(next_steps)}",
        ]
    )


def _email_from_creator_arg(creator: str) -> str | None:
    creator = creator.strip()
    if "@" in creator and " " not in creator:
        return creator
    return None


def _send_linear_creator_summary(arguments: dict[str, Any]) -> dict[str, Any]:
    # In Action Mode charlie-meet may omit creator (server pulls from Linear)
    # and the three follow-up lists (default to empty). attendee + summary
    # stay required: those are the substance of the recap and the server
    # can't invent them.
    required = ("ticket_id_or_url", "attendee", "summary")
    missing = [k for k in required if k not in arguments or arguments.get(k) in (None, "")]
    if missing:
        raise ToolError(f"missing required argument(s): {', '.join(missing)}")

    api_key = _require_env("LINEAR_API_KEY")
    bot_token = _require_env("SLACK_BOT_TOKEN")
    default_team_key = os.environ.get("LINEAR_DEFAULT_TEAM_KEY")

    defaults_applied: dict[str, Any] = {}
    for list_field in ("blockers", "questions", "next_steps"):
        if list_field not in arguments or arguments.get(list_field) is None:
            arguments[list_field] = []
            defaults_applied[list_field] = []

    try:
        issue = linear_client.get_issue_context(
            arguments["ticket_id_or_url"],
            api_key=api_key,
            default_team_key=default_team_key,
        )
    except linear_client.LinearError as exc:
        raise ToolError(f"linear issue lookup failed: {exc}") from exc

    creator = issue.get("creator") or {}
    creator_arg = (arguments.get("creator") or "").strip()
    creator_email = creator.get("email") or _email_from_creator_arg(creator_arg)
    if not creator_email:
        raise ToolError(
            "could not determine the ticket creator's email from Linear or the creator argument"
        )
    if not creator_arg:
        defaults_applied["creator"] = creator.get("email") or creator.get("displayName")

    text = _format_summary_message(arguments, issue)
    try:
        slack_result = slack_client.send_dm_by_email(
            creator_email, text, bot_token=bot_token
        )
    except slack_client.SlackError as exc:
        raise ToolError(f"slack summary delivery failed: {exc}") from exc

    return {
        "sent": True,
        "creator": {
            "name": creator.get("displayName") or creator.get("name") or creator_arg or None,
            "email": creator_email,
        },
        "ticket": {
            "identifier": issue.get("identifier"),
            "url": issue.get("url"),
            "title": issue.get("title"),
        },
        "slack": slack_result,
        "defaults_applied": defaults_applied,
    }


def _resolve_attendee_email(query_str: str, *, api_key: str) -> tuple[str, dict[str, Any] | None]:
    """Return ``(email, linear_user_or_None)`` for a tool-supplied attendee.

    Accepts an RFC-822-ish email directly; otherwise looks the name up in
    Linear and returns the resolved user's email.
    """
    value = (query_str or "").strip()
    if _EMAIL_REGEX.match(value):
        return value, None

    try:
        user = linear_client.resolve_assignee(value, api_key=api_key)
    except linear_client.AssigneeNotFound as exc:
        raise ToolError(str(exc)) from exc
    except linear_client.AssigneeAmbiguous as exc:
        raise ToolError(str(exc)) from exc
    except linear_client.LinearError as exc:
        raise ToolError(f"linear assignee lookup failed: {exc}") from exc

    email = (user.get("email") or "").strip()
    if not email:
        raise ToolError(
            f"Linear user {value!r} has no email on file; "
            "ask the assigner for the attendee's email directly"
        )
    return email, user


def _schedule_calendar_call(arguments: dict[str, Any]) -> dict[str, Any]:
    # start_time and duration are now optional — server fills defaults in
    # Action Mode (tomorrow business day @ 10am, 30 minutes).
    required = ("attendee_name", "topic", "ticket_id")
    missing = [k for k in required if not arguments.get(k)]
    if missing:
        raise ToolError(f"missing required argument(s): {', '.join(missing)}")

    api_key = _require_env("LINEAR_API_KEY")
    client_id = _require_env("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = _require_env("GOOGLE_OAUTH_CLIENT_SECRET")
    refresh_token = _require_env("GOOGLE_OAUTH_REFRESH_TOKEN")
    charlie_meet_email = _require_env("CHARLIE_MEET_EMAIL")
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID") or "primary"

    defaults_applied: dict[str, Any] = {}

    raw_duration = arguments.get("duration")
    if raw_duration in (None, ""):
        duration = _DEFAULT_DURATION_MINUTES
        defaults_applied["duration"] = duration
    else:
        try:
            duration = int(raw_duration)
        except (TypeError, ValueError) as exc:
            raise ToolError(
                f"invalid duration {raw_duration!r}; expected integer minutes"
            ) from exc
    if duration <= 0:
        raise ToolError(f"invalid duration {duration!r}; must be > 0 minutes")

    raw_start = arguments.get("start_time")
    if raw_start in (None, ""):
        tz = _default_timezone()
        start_dt = _next_business_day_at(_DEFAULT_START_HOUR, tz=tz)
        defaults_applied["start_time"] = start_dt.isoformat()
    else:
        try:
            start_dt = datetime.fromisoformat(str(raw_start))
        except ValueError as exc:
            raise ToolError(
                f"invalid start_time {raw_start!r}; "
                "expected ISO-8601 (e.g. 2026-06-01T15:00:00-07:00)"
            ) from exc
    end_dt = start_dt + timedelta(minutes=duration)

    attendee_email, linear_user = _resolve_attendee_email(
        arguments["attendee_name"], api_key=api_key
    )

    default_team_key = os.environ.get("LINEAR_DEFAULT_TEAM_KEY")
    try:
        issue = linear_client.get_issue_context(
            arguments["ticket_id"],
            api_key=api_key,
            default_team_key=default_team_key,
        )
    except linear_client.LinearError as exc:
        raise ToolError(f"linear issue lookup failed: {exc}") from exc

    identifier = issue.get("identifier") or arguments["ticket_id"]
    title = issue.get("title") or "Untitled Linear ticket"
    summary = f"Check-in: {identifier} — {title}"

    description_parts: list[str] = [str(arguments["topic"]).strip()]
    if issue.get("url"):
        description_parts.append(f"Linear: {issue['url']}")
    issue_description = (issue.get("description") or "").strip()
    if issue_description:
        description_parts.append(issue_description)
    description = "\n\n".join(part for part in description_parts if part)
    if len(description) > _CALENDAR_DESCRIPTION_MAX_CHARS:
        description = description[: _CALENDAR_DESCRIPTION_MAX_CHARS - 1] + "…"

    attendees: list[dict[str, Any]] = [{"email": attendee_email}]
    if charlie_meet_email.lower() != attendee_email.lower():
        attendees.append({"email": charlie_meet_email})

    try:
        event = google_calendar.create_event(
            calendar_id=calendar_id,
            summary=summary,
            description=description,
            start_iso=start_dt.isoformat(),
            end_iso=end_dt.isoformat(),
            attendees=attendees,
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
        )
    except google_calendar.GoogleCalendarError as exc:
        raise ToolError(f"google calendar event creation failed: {exc}") from exc

    ticket_dump = _ticket_dump(issue)

    meet_link = event.get("meet_link")
    if meet_link:
        try:
            meeting_registry.record_meeting(
                {
                    "meet_link": meet_link,
                    "event_id": event["event_id"],
                    "html_link": event.get("html_link"),
                    "start": event.get("start"),
                    "end": event.get("end"),
                    "topic": str(arguments["topic"]).strip(),
                    "ticket": ticket_dump,
                    "attendee": {
                        "email": attendee_email,
                        "name": (
                            (linear_user or {}).get("displayName")
                            or (linear_user or {}).get("name")
                        ),
                    },
                }
            )
        except (OSError, ValueError) as exc:
            # Registry write is best-effort: a failure here shouldn't block
            # the calendar invite that's already been sent.
            import logging

            logging.getLogger(__name__).warning(
                "meeting_registry write failed for %s: %s", meet_link, exc
            )

    return {
        "event_id": event["event_id"],
        "html_link": event["html_link"],
        "meet_link": event["meet_link"],
        "start": event["start"],
        "end": event["end"],
        "attendees": event["attendees"],
        "ticket": {
            "identifier": identifier,
            "url": issue.get("url"),
            "title": title,
        },
        "attendee": {
            "email": attendee_email,
            "name": (
                (linear_user or {}).get("displayName")
                or (linear_user or {}).get("name")
            ),
        },
        "defaults_applied": defaults_applied,
    }


_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "create_linear_ticket": _create_linear_ticket,
    "get_linear_ticket_context": _get_linear_ticket_context,
    "send_linear_creator_summary": _send_linear_creator_summary,
    "schedule_calendar_call": _schedule_calendar_call,
    "get_meeting_context": _get_meeting_context,
}


def dispatch(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    handler = _HANDLERS.get(name)
    if handler is None:
        raise ToolError(f"unknown tool: {name}")
    return handler(arguments)
