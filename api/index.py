"""Vercel entrypoint.

Vercel's Python runtime serves the `app` symbol exported from this file
as a WSGI application. We re-export the Flask app defined in
`server/app.py` so all routing/logic stays in one place; this file is
purely a Vercel shim.
"""

from server.app import app  # noqa: F401
