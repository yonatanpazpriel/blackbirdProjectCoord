"""Tool-name dispatcher for Tavus tool_call events.

Each handler takes the `arguments` dict from the tool call and returns a
JSON-serializable dict that gets relayed back to the LLM via the webhook
HTTP response body.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from server import linear_client
from server.priority import to_linear_priority


class ToolError(Exception):
    """Raised when a tool call can't be fulfilled. The message is surfaced
    back to the LLM as the `error` field in the webhook response."""


def _create_linear_ticket(arguments: dict[str, Any]) -> dict[str, Any]:
    required = ("assignee", "title", "description", "priority")
    missing = [k for k in required if not arguments.get(k)]
    if missing:
        raise ToolError(f"missing required argument(s): {', '.join(missing)}")

    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        raise ToolError("server not configured: LINEAR_API_KEY unset")

    team_key = os.environ.get("LINEAR_DEFAULT_TEAM_KEY")
    if not team_key:
        raise ToolError("server not configured: LINEAR_DEFAULT_TEAM_KEY unset")

    try:
        priority = to_linear_priority(arguments["priority"])
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
    }


_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "create_linear_ticket": _create_linear_ticket,
}


def dispatch(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    handler = _HANDLERS.get(name)
    if handler is None:
        raise ToolError(f"unknown tool: {name}")
    return handler(arguments)
