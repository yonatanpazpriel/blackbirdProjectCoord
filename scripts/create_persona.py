"""Create a Tavus persona from a JSON payload.

Usage:
    python scripts/create_persona.py personas/charlie.json
    python scripts/create_persona.py personas/charlie.json --env stg

Reads TAVUS_API_KEY from the environment (or a sibling `.env` file). Posts
the payload to `POST /v2/personas` and prints the response.

Tavus API hosts:
    prod  https://tavusapi.com
    stg   https://tavusapi-stg.tavus.io
    test  https://tavusapi-test.tavus.io
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from typing import Any

API_HOSTS = {
    "prod": "https://tavusapi.com",
    "stg": "https://tavusapi-stg.tavus.io",
    "test": "https://tavusapi-test.tavus.io",
}


def load_env_file(path: pathlib.Path) -> None:
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
        os.environ.setdefault(key, value)


def post_persona(payload: dict[str, Any], host: str, api_key: str) -> Any:
    try:
        import requests
    except ImportError:
        print(
            "error: `requests` is not installed. Run `pip install -r requirements.txt`.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return requests.post(
        f"{host}/v2/personas",
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", help="Path to persona JSON payload")
    parser.add_argument(
        "--env",
        choices=API_HOSTS.keys(),
        default="prod",
        help="Tavus API environment (default: prod)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the payload that would be sent and exit without calling the API",
    )
    args = parser.parse_args()

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    load_env_file(repo_root / ".env")

    payload_path = pathlib.Path(args.payload)
    if not payload_path.is_absolute():
        payload_path = (repo_root / payload_path).resolve()
    if not payload_path.exists():
        print(f"error: payload file not found: {payload_path}", file=sys.stderr)
        return 1

    payload = json.loads(payload_path.read_text())

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    api_key = os.environ.get("TAVUS_API_KEY")
    if not api_key:
        print(
            "error: TAVUS_API_KEY not set. Export it or add it to .env "
            "(see .env.example).",
            file=sys.stderr,
        )
        return 1

    host = API_HOSTS[args.env]
    response = post_persona(payload, host, api_key)

    print(f"POST {host}/v2/personas -> {response.status_code}")
    try:
        print(json.dumps(response.json(), indent=2))
    except ValueError:
        print(response.text)

    return 0 if response.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
