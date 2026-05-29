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


# In-memory, per-session JWT cache (session key -> access token). Populated by the
# `login` tool, consulted by auth_headers, cleared by `logout` / on a 401. Never
# persisted — the service still holds no credentials at rest, and the cache is
# lost on restart.
_session_tokens: dict[str, str] = {}


def cache_token(session_key: str, token: str) -> None:
    """Store a JWT for a session (called by the `login` tool)."""
    _session_tokens[session_key] = token


def clear_token(session_key: str) -> None:
    """Drop a session's cached JWT (called by `logout` / on a 401)."""
    _session_tokens.pop(session_key, None)


def _bearer(value: str) -> str:
    """Normalize a raw JWT (or pre-prefixed value) to a `Bearer <jwt>` header value."""
    return value if value.lower().startswith("bearer ") else f"Bearer {value}"


def auth_headers(
    request,
    *,
    required: bool = True,
    token: str | None = None,
    session_key: str | None = None,
) -> dict:
    """Build the Authorization header to forward to the backend.

    Resolution order:
      1. An explicit `token` (threaded through a tool's `bearer_token` arg).
      2. The session's cached JWT (set by the `login` tool), keyed by `session_key`.
      3. The inbound `Authorization` header from `request` (the Starlette Request
         from `ctx.request_context.request`, or None if unavailable).

    When none are present and `required` is True, raises MissingAuthError; when
    False, returns an empty dict (header simply omitted). All values normalized to
    `Bearer <jwt>`.
    """
    if token:
        return {"Authorization": _bearer(token)}

    cached = _session_tokens.get(session_key) if session_key else None
    if cached:
        return {"Authorization": _bearer(cached)}

    inbound = request.headers.get("authorization") if request is not None else None
    if not inbound:
        if required:
            raise MissingAuthError(
                "Not authenticated. Call the `login` tool (or pass `bearer_token` "
                "from `get_token`), or configure your MCP client with "
                "'Authorization: Bearer <JWT>'."
            )
        return {}
    return {"Authorization": inbound}
