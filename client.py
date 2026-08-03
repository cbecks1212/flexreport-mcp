"""Shared httpx client + inbound-bearer-header forwarding for the FlexReport MCP server.

This service is a stateless, credential-free proxy: it never holds API keys. Each
tool forwards the caller's inbound `Authorization: Bearer <JWT>` header to the
backend so the backend authenticates and meters the request as that user.
"""

import os

import httpx

API_BASE_URL = os.environ.get("API_BASE_URL", "https://flexreportfinapi.com")
HTTP_TIMEOUT = float(os.environ.get("API_HTTP_TIMEOUT", "60"))

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    """Return a lazily-created, shared AsyncClient bound to the backend base URL."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=API_BASE_URL, timeout=HTTP_TIMEOUT)
    return _client


class MissingAuthError(Exception):
    """Raised when a JWT-protected tool is called without an inbound bearer token."""


def auth_headers(request, *, required: bool = True) -> dict:
    """Build the Authorization header to forward to the backend.

    Forwards the inbound `Authorization` header from `request` — the OAuth bearer
    already validated at the transport. When it is absent and `required` is True,
    raises MissingAuthError; when False, returns an empty dict (header omitted).
    """
    inbound = request.headers.get("authorization") if request is not None else None
    if not inbound:
        if required:
            raise MissingAuthError(
                "Not authenticated. Your MCP client should run the OAuth sign-in "
                "flow (it is triggered by the 401 challenge) and retry."
            )
        return {}
    return {"Authorization": inbound}
