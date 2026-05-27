"""One-time helper: run Google's OAuth installed-app flow and print the refresh token.

Usage:
    export GOOGLE_OAUTH_CLIENT_ID=...
    export GOOGLE_OAUTH_CLIENT_SECRET=...
    python scripts/google_oauth_refresh_token.py

Prerequisites:
    1. In Google Cloud Console, enable the **Google Calendar API**.
    2. Create an OAuth client of type **Desktop app** (or Web app with a
       loopback redirect URI) under "APIs & Services -> Credentials".
    3. Add yourself as a Test user under "OAuth consent screen" while the
       app is in Testing.

The script opens your default browser, you consent, Google redirects back
to a one-shot local HTTP server on 127.0.0.1, and the script prints the
``refresh_token`` you should paste into ``.env`` as
``GOOGLE_OAUTH_REFRESH_TOKEN``.
"""

from __future__ import annotations

import http.server
import os
import pathlib
import secrets
import sys
import urllib.parse
import webbrowser
from typing import Any

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/calendar.events"


def load_env_file(path: pathlib.Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures the OAuth ``?code=...`` query param off a single GET."""

    server: "_CallbackServer"

    def do_GET(self) -> None:  # noqa: N802 - http.server protocol name
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        self.server.received_params = {k: v[0] for k, v in params.items() if v}

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        message = (
            "Authorization received. You can close this tab and return to the terminal."
            if "code" in self.server.received_params
            else "Authorization failed. Check the terminal for details."
        )
        self.wfile.write(f"<html><body><p>{message}</p></body></html>".encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature override
        return


class _CallbackServer(http.server.HTTPServer):
    received_params: dict[str, str] = {}


def _run_local_callback(host: str = "127.0.0.1", port: int = 8765) -> dict[str, str]:
    server = _CallbackServer((host, port), _CallbackHandler)
    server.received_params = {}
    server.handle_request()
    return server.received_params


def main() -> int:
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    load_env_file(repo_root / ".env")

    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(
            "error: GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET must be set "
            "in the environment or .env",
            file=sys.stderr,
        )
        return 1

    try:
        import requests
    except ImportError:
        print(
            "error: `requests` is not installed. Run `pip install -r requirements.txt`.",
            file=sys.stderr,
        )
        return 1

    port = 8765
    redirect_uri = f"http://127.0.0.1:{port}"
    state = secrets.token_urlsafe(24)

    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    auth_url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(auth_params)}"

    print(f"Opening browser for Google consent... If it does not open, visit:\n{auth_url}\n")
    try:
        webbrowser.open(auth_url, new=1, autoraise=True)
    except Exception:
        pass

    print(f"Waiting for redirect on {redirect_uri} ...")
    params = _run_local_callback(port=port)

    if params.get("state") != state:
        print(f"error: state mismatch (expected {state!r}, got {params.get('state')!r})", file=sys.stderr)
        return 1
    if "error" in params:
        print(f"error: OAuth flow returned {params['error']!r}", file=sys.stderr)
        return 1
    code = params.get("code")
    if not code:
        print("error: OAuth flow did not return an authorization code", file=sys.stderr)
        return 1

    response = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if not response.ok:
        print(
            f"error: token exchange failed: HTTP {response.status_code}: {response.text}",
            file=sys.stderr,
        )
        return 1

    body = response.json()
    refresh_token = body.get("refresh_token")
    if not refresh_token:
        print(
            "error: response did not include a refresh_token. "
            "Revoke prior consent at https://myaccount.google.com/permissions "
            "and re-run this script (prompt=consent should force a new refresh token).",
            file=sys.stderr,
        )
        return 1

    print("\nSuccess. Paste this into .env as GOOGLE_OAUTH_REFRESH_TOKEN:\n")
    print(refresh_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
