"""
What the operator's command-line tools share: a client for the running service's admin API.

Environment: ``UNFRET_URL`` (default https://unfret.net) and ``UNFRET_ADMIN_TOKEN``, or ``UNFRET_ADMIN_TOKEN_FILE``
(default ``~/kroshtan_secrets/unfret-prod-admin-token``).
"""

import os
from pathlib import Path

import httpx

DEFAULT_URL = "https://unfret.net"


def admin_token() -> str:
    """
    The admin token: ``UNFRET_ADMIN_TOKEN``, else the file ``UNFRET_ADMIN_TOKEN_FILE`` names.

    :return: The token.
    """
    if token := os.getenv("UNFRET_ADMIN_TOKEN"):
        return token
    path = Path(os.getenv("UNFRET_ADMIN_TOKEN_FILE", "~/kroshtan_secrets/unfret-prod-admin-token")).expanduser()
    return path.read_text().strip()


def admin_client() -> httpx.Client:
    """
    A client for the service's admin API.

    :return: The client (close it, or use it as a context manager).
    """
    base = os.getenv("UNFRET_URL", DEFAULT_URL).rstrip("/")
    return httpx.Client(base_url=base, headers={"Authorization": f"Bearer {admin_token()}"}, timeout=30)
