"""Same-origin browser controls, including the local cloudflared connector.

Only loopback peers may supply a single forwarded scheme. Never trust an
arbitrary LAN client's forwarded headers, nor rewrite Host from such headers.
This is CSRF protection, not user authentication.
"""
from urllib.parse import urlsplit

from flask import request


def same_origin_request():
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        return False
    origin = request.headers.get("Origin")
    if not origin:
        return True  # JSON-only command-line clients remain supported.
    scheme = request.scheme
    forwarded = request.headers.get("X-Forwarded-Proto", "")
    if request.remote_addr in {"127.0.0.1", "::1"} and forwarded in {"http", "https"}:
        scheme = forwarded
    try:
        source = urlsplit(origin)
        target = urlsplit(f"{scheme}://{request.host}")
        return (source.scheme in {"http", "https"}
                and not source.username and not source.password
                and not source.path and not source.query and not source.fragment
                and source.scheme == target.scheme
                and source.hostname == target.hostname
                and (source.port or (443 if source.scheme == "https" else 80))
                == (target.port or (443 if target.scheme == "https" else 80)))
    except ValueError:
        return False
