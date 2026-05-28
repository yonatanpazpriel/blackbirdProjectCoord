"""Linear GraphQL client.

We call Linear's public GraphQL endpoint directly with a Personal API Key
rather than shelling out to the `linear` CLI, which uses an interactive
browser auth flow that's not suitable for a headless server.
"""

from __future__ import annotations

import re
from typing import Any

import requests

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
_REQUEST_TIMEOUT = 30


class LinearError(Exception):
    """Generic Linear API error."""


class AssigneeNotFound(LinearError):
    """No active Linear user matched the supplied assignee string."""


class AssigneeAmbiguous(LinearError):
    """More than one active Linear user matched the assignee string."""


_team_id_cache: dict[str, str] = {}


def normalize_issue_identifier(
    ticket_id_or_url: str,
    *,
    default_team_key: str | None = None,
) -> str:
    """Extract a Linear issue identifier from an identifier or issue URL.

    Accepts (in order of priority):
      - ``ENT-123`` / ``e-1`` / a URL containing one — returned uppercased.
      - ``ENT 123`` / ``ENT123`` (whitespace-flexible) — joined with a hyphen.
      - Bare digits or ``#123`` — when ``default_team_key`` is supplied,
        returned as ``{TEAM}-123``. The LLM frequently drops the team
        prefix when the user says "ticket 542" verbally; this rescues that
        case.

    Anything else is returned untouched — Linear will produce a clean
    "Entity not found" that we surface back to the caller.
    """
    value = ticket_id_or_url.strip()
    match = re.search(r"\b[A-Z][A-Z0-9]*-\d+\b", value, flags=re.IGNORECASE)
    if match:
        return match.group(0).upper()

    spoken_match = re.search(r"\b([A-Z][A-Z0-9]*?)\s*(\d+)\b", value, flags=re.IGNORECASE)
    if spoken_match:
        return f"{spoken_match.group(1).upper()}-{spoken_match.group(2)}"

    if default_team_key:
        digits_only = re.fullmatch(r"\s*#?\s*(\d+)\s*", value)
        if digits_only:
            return f"{default_team_key.upper()}-{digits_only.group(1)}"

    return value


def _graphql(query: str, variables: dict[str, Any], api_key: str) -> dict[str, Any]:
    response = requests.post(
        LINEAR_GRAPHQL_URL,
        headers={
            "Authorization": api_key,
            "Content-Type": "application/json",
        },
        json={"query": query, "variables": variables},
        timeout=_REQUEST_TIMEOUT,
    )
    if not response.ok:
        raise LinearError(f"HTTP {response.status_code}: {response.text}")
    body = response.json()
    if body.get("errors"):
        raise LinearError(str(body["errors"]))
    return body.get("data") or {}


def resolve_team_id(team_key: str, *, api_key: str) -> str:
    """Map a team key (e.g. 'ENT') to its Linear team UUID. Cached."""
    cached = _team_id_cache.get(team_key)
    if cached:
        return cached

    query = """
    query($key: String!) {
        teams(filter: { key: { eq: $key } }) {
            nodes { id key name }
        }
    }
    """
    data = _graphql(query, {"key": team_key}, api_key)
    nodes = ((data.get("teams") or {}).get("nodes")) or []
    if not nodes:
        raise LinearError(f"no Linear team with key {team_key!r}")
    team_id = nodes[0]["id"]
    _team_id_cache[team_key] = team_id
    return team_id


def resolve_assignee(query_str: str, *, api_key: str) -> dict[str, Any]:
    """Look up a Linear user by email, displayName, or full name.

    Returns the matched user node: ``{id, displayName, name, email, active}``.
    Raises AssigneeNotFound on 0 active matches, AssigneeAmbiguous on >1.
    """
    query = """
    query($q: String!) {
        users(filter: {
            or: [
                { email: { eq: $q } },
                { displayName: { containsIgnoreCase: $q } },
                { name: { containsIgnoreCase: $q } }
            ]
        }) {
            nodes { id displayName name email active }
        }
    }
    """
    data = _graphql(query, {"q": query_str}, api_key)
    nodes = ((data.get("users") or {}).get("nodes")) or []
    active = [n for n in nodes if n.get("active")]

    if not active:
        raise AssigneeNotFound(
            f"no active Linear user matches {query_str!r} — "
            "ask the user for the assignee's exact name or email"
        )

    exact = [
        n
        for n in active
        if query_str.lower() in {(n.get("email") or "").lower(), (n.get("displayName") or "").lower()}
    ]
    if len(exact) == 1:
        return exact[0]

    if len(active) > 1:
        candidates = ", ".join(
            f"{n.get('displayName')} <{n.get('email')}>" for n in active[:5]
        )
        raise AssigneeAmbiguous(
            f"multiple Linear users match {query_str!r}: {candidates} — "
            "ask the user to specify by email or full display name"
        )

    return active[0]


def resolve_assignee_id(query_str: str, *, api_key: str) -> str:
    """Look up a Linear user by email/displayName/name and return their UUID."""
    return resolve_assignee(query_str, api_key=api_key)["id"]


def create_issue(
    *,
    team_id: str,
    title: str,
    description: str,
    priority: int,
    assignee_id: str,
    api_key: str,
) -> dict[str, Any]:
    """Create a Linear issue and return `{ id, identifier, url, title }`."""
    mutation = """
    mutation($input: IssueCreateInput!) {
        issueCreate(input: $input) {
            success
            issue { id identifier url title }
        }
    }
    """
    variables = {
        "input": {
            "teamId": team_id,
            "title": title,
            "description": description,
            "priority": priority,
            "assigneeId": assignee_id,
        }
    }
    data = _graphql(mutation, variables, api_key)
    result = data.get("issueCreate") or {}
    if not result.get("success") or not result.get("issue"):
        raise LinearError(f"issueCreate did not return an issue: {result!r}")
    return result["issue"]


def get_issue_context(
    ticket_id_or_url: str,
    *,
    api_key: str,
    default_team_key: str | None = None,
) -> dict[str, Any]:
    """Fetch issue details needed by the meeting coordinator persona.

    ``default_team_key`` is forwarded to :func:`normalize_issue_identifier`
    so bare numeric IDs (the LLM-drops-the-prefix case) resolve against the
    configured default team.
    """
    identifier = normalize_issue_identifier(
        ticket_id_or_url, default_team_key=default_team_key
    )
    query = """
    query($id: String!) {
        issue(id: $id) {
            id
            identifier
            url
            title
            description
            priority
            dueDate
            createdAt
            updatedAt
            creator { id name displayName email }
            assignee { id name displayName email }
            state { id name type }
            team { id key name }
            labels { nodes { id name } }
            comments(first: 10) {
                nodes {
                    id
                    body
                    createdAt
                    user { id name displayName email }
                }
            }
        }
    }
    """
    data = _graphql(query, {"id": identifier}, api_key)
    issue = data.get("issue")
    if not issue:
        raise LinearError(f"no Linear issue found for {ticket_id_or_url!r}")
    return issue
