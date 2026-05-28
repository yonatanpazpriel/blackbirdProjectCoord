#!/usr/bin/env python3
"""Smoke-test all persona tool handlers via the local Flask webhook server."""

from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

_REPO = pathlib.Path(__file__).resolve().parent.parent


def _load_env() -> None:
    env_path = _REPO / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def post(base: str, tool: str, body: dict, *, secret: str | None) -> tuple[int, dict]:
    url = f"{base.rstrip('/')}/tavus/webhook/{tool}"
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Webhook-Secret"] = secret
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read())
        except json.JSONDecodeError:
            payload = {"error": exc.read().decode(errors="replace")}
        return exc.code, payload


def main() -> int:
    _load_env()
    base = os.environ.get("SMOKE_WEBHOOK_BASE", "http://127.0.0.1:8080")
    auth_enabled = os.environ.get("WEBHOOK_AUTH_ENABLED", "false").lower() == "true"
    secret = os.environ.get("TAVUS_WEBHOOK_SECRET") if auth_enabled else None

    passed = failed = skipped = 0

    def ok(name: str, detail: str = "") -> None:
        nonlocal passed
        passed += 1
        suffix = f" → {detail}" if detail else ""
        print(f"  ✓ {name}{suffix}")

    def bad(name: str, http: int, payload: dict) -> None:
        nonlocal failed
        failed += 1
        err = payload.get("error") or payload
        print(f"  ✗ {name} (HTTP {http}): {err}")

    def skip(name: str, reason: str) -> None:
        nonlocal skipped
        skipped += 1
        print(f"  ⊘ {name}: {reason}")

    print(f"Webhook base: {base}\n")

    # --- Charlie ---
    print("=== CHARLIE: create_linear_ticket ===")
    http, payload = post(
        base,
        "create_linear_ticket",
        {
            "assignee": "yonatan@tavus.io",
            "title": "Smoke test ticket",
            "description": "Automated persona tool smoke test — safe to delete.",
        },
        secret=secret,
    )
    if http == 200 and (payload.get("result") or {}).get("identifier"):
        ticket = payload["result"]["identifier"]
        ok("create_linear_ticket", ticket)
    else:
        bad("create_linear_ticket", http, payload)
        print(f"\n=== SUMMARY: {passed} passed, {failed} failed, {skipped} skipped ===")
        return 1

    print("\n=== CHARLIE: schedule_calendar_call (Action Mode defaults) ===")
    http, payload = post(
        base,
        "schedule_calendar_call",
        {
            "attendee_name": "yonatan@tavus.io",
            "topic": f"Smoke test check-in for {ticket}",
            "ticket_id": ticket,
        },
        secret=secret,
    )
    meet_link = ""
    if http == 200 and (payload.get("result") or {}).get("meet_link"):
        meet_link = payload["result"]["meet_link"]
        ok("schedule_calendar_call", meet_link)
    else:
        bad("schedule_calendar_call", http, payload)

    # --- charlie-meet ---
    print("\n=== CHARLIE-MEET: get_linear_ticket_context ===")
    http, payload = post(
        base, "get_linear_ticket_context", {"ticket_id_or_url": ticket}, secret=secret
    )
    if http == 200 and (payload.get("result") or {}).get("identifier"):
        ok("get_linear_ticket_context", payload["result"]["identifier"])
    else:
        bad("get_linear_ticket_context", http, payload)

    print("\n=== CHARLIE-MEET: send_linear_creator_summary (Action Mode) ===")
    http, payload = post(
        base,
        "send_linear_creator_summary",
        {
            "ticket_id_or_url": ticket,
            "attendee": "yonatan@tavus.io",
            "summary": "Smoke test: attendee confirmed understanding of the ticket scope.",
        },
        secret=secret,
    )
    if http == 200 and payload.get("result") is not None:
        result = payload["result"]
        ok(
            "send_linear_creator_summary",
            f"slack_ok={result.get('slack_ok')} defaults={result.get('defaults_applied')}",
        )
    else:
        bad("send_linear_creator_summary", http, payload)

    print(f"\n=== SUMMARY: {passed} passed, {failed} failed, {skipped} skipped ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
