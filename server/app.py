"""Flask webhook receiver for Tavus tool_call events.

Run locally:

    flask --app server.app run --port 8080

Then point the conversation's `callback_url` at this server (use ngrok or
similar for a public URL during dev).
"""

from __future__ import annotations

import os
import pathlib
from typing import Any

from flask import Flask, jsonify, request

from server.tools import ToolError, dispatch


def _load_env_file(path: pathlib.Path) -> None:
    """Minimal .env loader so we don't need python-dotenv as a hard dep."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value


_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_load_env_file(_REPO_ROOT / ".env")


app = Flask(__name__)


def _extract_tool_call(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Pull `(name, arguments)` out of Tavus's tool_call payload shape.

    Tavus's exact field names have shifted over time; accept the common
    variants rather than locking to one.
    """
    if not isinstance(payload, dict):
        raise ToolError("payload must be a JSON object")

    tool_call = payload.get("tool_call") or payload.get("data") or payload
    if not isinstance(tool_call, dict):
        raise ToolError("tool_call must be an object")

    name = (
        tool_call.get("name")
        or tool_call.get("tool_name")
        or (tool_call.get("function") or {}).get("name")
    )
    if not name:
        raise ToolError("missing tool name")

    arguments = (
        tool_call.get("arguments")
        or tool_call.get("parameters")
        or (tool_call.get("function") or {}).get("arguments")
        or {}
    )
    if isinstance(arguments, str):
        import json

        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ToolError(f"arguments is not valid JSON: {exc}") from exc

    if not isinstance(arguments, dict):
        raise ToolError("arguments must be an object")

    return name, arguments


@app.post("/tavus/webhook")
def tavus_webhook():
    auth_enabled = os.environ.get("WEBHOOK_AUTH_ENABLED", "true").lower() != "false"
    if auth_enabled:
        expected_secret = os.environ.get("TAVUS_WEBHOOK_SECRET")
        if not expected_secret:
            return jsonify({"error": "server not configured: TAVUS_WEBHOOK_SECRET unset"}), 500

        presented = request.headers.get("X-Webhook-Secret", "")
        if presented != expected_secret:
            return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"error": "request body must be JSON"}), 400

    try:
        name, arguments = _extract_tool_call(payload)
        result = dispatch(name, arguments)
    except ToolError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({"result": result}), 200


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True}), 200
