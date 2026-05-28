"""Flask webhook receiver for Tavus tool_call events.

Run locally:

    flask --app server.app run --port 8080

Then point the conversation's `callback_url` at this server (use ngrok or
similar for a public URL during dev).
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any
from urllib.parse import urljoin

import requests
from flask import Flask, jsonify, request, send_from_directory

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
_STATIC_DIR = _REPO_ROOT / "static"
_load_env_file(_REPO_ROOT / ".env")


app = Flask(__name__)


_TAVUS_API_HOSTS = {
    "prod": "https://tavusapi.com",
    "stg": "https://tavusapi-stg.tavus.io",
    "test": "https://tavusapi-test.tavus.io",
}


_LOG_LEVEL = os.environ.get("WEBHOOK_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
app.logger.setLevel(getattr(logging, _LOG_LEVEL, logging.INFO))


def _short(value: Any, limit: int = 1200) -> str:
    """Compact JSON repr truncated to ``limit`` chars for log lines."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _is_tool_call(payload: dict[str, Any]) -> bool:
    """True if the payload posted to ``/tavus/webhook`` (no tool in URL) looks like a tool call.

    Tavus sends three categories of POSTs to a conversation's ``callback_url``:
    - ``message_type: "system"`` — replica join/leave, conversation lifecycle.
    - ``message_type: "application"`` — perception analysis, etc.
    - Tool calls — when ``delivery.api.url`` is set per-tool, Tavus POSTs the
      *arguments object directly* as the request body (no wrapper, no
      ``message_type``). The tool name comes from the URL path, NOT the body.

    The per-tool routes (``/tavus/webhook/<tool_name>``) are the canonical
    delivery path. This helper only matters for the legacy combined route,
    where we use ``message_type`` to filter out system events.
    """
    if not isinstance(payload, dict):
        return False

    message_type = (payload.get("message_type") or "").lower()
    if message_type == "tool_call":
        return True
    if message_type:
        return False

    return bool(
        payload.get("tool_call")
        or (payload.get("function") or {}).get("name")
        or _infer_tool_name(payload)
    )


def _check_webhook_auth() -> tuple[bool, Any]:
    """Returns ``(ok, response_tuple_or_none)``.  Response_tuple is set when auth fails."""
    auth_enabled = os.environ.get("WEBHOOK_AUTH_ENABLED", "true").lower() != "false"
    if not auth_enabled:
        return True, None
    expected_secret = os.environ.get("TAVUS_WEBHOOK_SECRET")
    if not expected_secret:
        return False, (jsonify({"error": "server not configured: TAVUS_WEBHOOK_SECRET unset"}), 500)
    if request.headers.get("X-Webhook-Secret", "") != expected_secret:
        return False, (jsonify({"error": "unauthorized"}), 401)
    return True, None


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

    arguments = (
        tool_call.get("arguments")
        or tool_call.get("parameters")
        or (tool_call.get("function") or {}).get("arguments")
        or tool_call
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

    name = (
        tool_call.get("name")
        or tool_call.get("tool_name")
        or (tool_call.get("function") or {}).get("name")
        or _infer_tool_name(arguments)
    )
    if not name:
        raise ToolError("missing tool name")

    return name, arguments


def _run_tool(name: str, arguments: dict[str, Any]):
    """Shared dispatch + error handling used by both webhook routes."""
    try:
        app.logger.info("Dispatching tool %s args=%s", name, _short(arguments))
        result = dispatch(name, arguments)
    except ToolError as exc:
        app.logger.warning("Tool %s rejected: %s", name, exc)
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 - want the traceback in dev logs
        app.logger.exception("Tool %s crashed: %s", name, exc)
        return jsonify({"error": f"internal error: {exc}"}), 500

    app.logger.info("Tool %s succeeded: %s", name, _short(result))
    return jsonify({"result": result}), 200


def _infer_tool_name(arguments: dict[str, Any]) -> str | None:
    """Infer Tavus API-delivered tool calls that send arguments as the body."""
    if {"summary", "blockers", "questions", "next_steps"} & set(arguments):
        return "send_linear_creator_summary"
    if "ticket_id_or_url" in arguments:
        return "get_linear_ticket_context"
    return None


@app.post("/tavus/webhook/<tool_name>")
def tavus_tool_webhook(tool_name: str):
    """Per-tool delivery target. Tavus POSTs the arguments object directly."""
    ok, err = _check_webhook_auth()
    if not ok:
        return err

    payload = request.get_json(silent=True)
    if payload is None:
        app.logger.warning(
            "Tool %s body was not valid JSON (raw=%s)",
            tool_name,
            _short(request.get_data(as_text=True), 400),
        )
        return jsonify({"error": "request body must be JSON"}), 400

    if not isinstance(payload, dict):
        return jsonify({"error": "arguments must be a JSON object"}), 400

    app.logger.info("Tool %s payload: %s", tool_name, _short(payload))
    return _run_tool(tool_name, payload)


@app.post("/tavus/webhook")
def tavus_webhook():
    """Legacy combined route. Tavus delivers system/lifecycle events here, plus
    any tool whose ``delivery.api.url`` still points at the un-suffixed URL.
    """
    ok, err = _check_webhook_auth()
    if not ok:
        return err

    payload = request.get_json(silent=True)
    if payload is None:
        app.logger.warning("Webhook body was not valid JSON (raw=%s)", _short(request.get_data(as_text=True), 400))
        return jsonify({"error": "request body must be JSON"}), 400

    app.logger.info("Webhook payload: %s", _short(payload))

    if not _is_tool_call(payload):
        event_label = (
            payload.get("event_type")
            or payload.get("message_type")
            or "non-tool_call event"
        )
        app.logger.info("Ignoring %s webhook (no tool_call payload)", event_label)
        return jsonify({"ok": True, "ignored": event_label}), 200

    try:
        name, arguments = _extract_tool_call(payload)
    except ToolError as exc:
        app.logger.warning("Tool call rejected: %s", exc)
        return jsonify({"error": str(exc)}), 400

    return _run_tool(name, arguments)


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True}), 200


@app.get("/")
def index():
    """Serve the type-to-Charlie single-page frontend."""
    return send_from_directory(_STATIC_DIR, "index.html")


@app.get("/favicon.png")
def favicon_png():
    return send_from_directory(_STATIC_DIR, "favicon.png", mimetype="image/png")


@app.get("/favicon-32.png")
def favicon_32_png():
    return send_from_directory(_STATIC_DIR, "favicon-32.png", mimetype="image/png")


@app.get("/favicon.ico")
def favicon_ico():
    """Most browsers still probe /favicon.ico by default; serve the PNG."""
    return send_from_directory(_STATIC_DIR, "favicon.png", mimetype="image/png")


def _webhook_base_url() -> str:
    """Return the public base URL used to build callback_url for Tavus.

    Prefers PUBLIC_WEBHOOK_BASE_URL from env. Falls back to the incoming
    request's host_url (works for ngrok/local dev where the browser is
    hitting the same host that Tavus needs to call back into).
    """
    configured = os.environ.get("PUBLIC_WEBHOOK_BASE_URL")
    if configured:
        return configured.rstrip("/")
    return request.host_url.rstrip("/")


@app.post("/conversation")
def create_conversation():
    """Mint a Tavus conversation for the embedded replica and return its URL.

    Body is optional. Recognized fields: ``persona_id`` (defaults to
    ``CHARLIE_PERSONA_ID`` env), ``conversation_name``.
    """
    api_key = os.environ.get("TAVUS_API_KEY")
    if not api_key:
        return jsonify({"error": "server not configured: TAVUS_API_KEY unset"}), 500

    body = request.get_json(silent=True) or {}
    persona_id = body.get("persona_id") or os.environ.get("CHARLIE_PERSONA_ID")
    if not persona_id:
        return (
            jsonify(
                {
                    "error": "no persona_id: pass one in the request body or set "
                    "CHARLIE_PERSONA_ID in the environment"
                }
            ),
            400,
        )

    env_name = (os.environ.get("TAVUS_ENV") or "prod").lower()
    host = _TAVUS_API_HOSTS.get(env_name, _TAVUS_API_HOSTS["prod"])

    callback_url = urljoin(_webhook_base_url() + "/", "tavus/webhook")
    payload: dict[str, Any] = {
        "persona_id": persona_id,
        "callback_url": callback_url,
    }
    if body.get("conversation_name"):
        payload["conversation_name"] = body["conversation_name"]

    try:
        resp = requests.post(
            f"{host}/v2/conversations",
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
    except requests.RequestException as exc:
        app.logger.exception("Tavus conversation create failed: %s", exc)
        return jsonify({"error": f"tavus request failed: {exc}"}), 502

    if not resp.ok:
        app.logger.warning(
            "Tavus /v2/conversations -> %s %s", resp.status_code, _short(resp.text, 400)
        )
        return (
            jsonify({"error": f"tavus returned {resp.status_code}: {resp.text}"}),
            resp.status_code,
        )

    data = resp.json()
    return (
        jsonify(
            {
                "conversation_id": data.get("conversation_id"),
                "conversation_url": data.get("conversation_url"),
                "status": data.get("status"),
            }
        ),
        200,
    )
