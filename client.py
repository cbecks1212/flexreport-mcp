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


def _bearer(value: str) -> str:
    """Normalize a raw JWT (or pre-prefixed value) to a `Bearer <jwt>` header value."""
    return value if value.lower().startswith("bearer ") else f"Bearer {value}"


def auth_headers(request, *, required: bool = True, token: str | None = None) -> dict:
    """Build the Authorization header to forward to the backend.

    Resolution order:
      1. An explicit `token` — a JWT from `get_token`, threaded through a tool's
         `bearer_token` arg. This is the primary path for the agent-managed flow.
      2. The inbound `Authorization` header from `request` (set by the MCP client
         config, if the deployment uses one).

    When neither is present and `required` is True, raises MissingAuthError; when
    False, returns an empty dict (header omitted). Values normalized to `Bearer <jwt>`.
    """
    if token:
        return {"Authorization": _bearer(token)}

    inbound = request.headers.get("authorization") if request is not None else None
    if not inbound:
        if required:
            raise MissingAuthError(
                "Not authenticated. Get a JWT with `get_token(username, password)` "
                "(register first via `register_user` if you have no account), then "
                "pass it as the `bearer_token` argument."
            )
        return {}
    return {"Authorization": inbound}
