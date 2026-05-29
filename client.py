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
    """Extract the inbound Authorization header to forward to the backend.

    `request` is the Starlette Request from `ctx.request_context.request` (or None
    if unavailable). When `required` is True and no token is present, raises
    MissingAuthError; when False, returns an empty dict (header simply omitted).
    """
    token = request.headers.get("authorization") if request is not None else None
    if not token:
        if required:
            raise MissingAuthError(
                "No Authorization header found. Configure your MCP client with "
                "'Authorization: Bearer <JWT>' — obtain a JWT from the backend's "
                "POST /token (OAuth2 password flow)."
            )
        return {}
    return {"Authorization": token}
