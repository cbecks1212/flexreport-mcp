"""Shared plumbing for every tool module: the FastMCP instance, the server
instructions, and `_send`, the one function that forwards a call to the backend.

Tool modules under `tools/` do `from core import mcp, _send` and register with
`@mcp.tool(...)`; `server.py` imports the package (which registers everything),
adds the health route and runs the transport.
"""

import json
import os
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import Context, FastMCP

from client import MissingAuthError, auth_headers, get_client

# Non-code text (server instructions, auth playbooks) lives in instructions.json
# so the copy can be edited without touching the server logic.
_TEXT = json.loads((Path(__file__).parent / "instructions.json").read_text())

# Auth: OAuth 2.0 Resource Server, always on. The SDK serves
# /.well-known/oauth-protected-resource and returns 401 + WWW-Authenticate challenges
# pointing clients at the backend Authorization Server (issuer_url); only RS256 tokens
# validated against the backend JWKS are accepted.
from mcp.server.auth.settings import AuthSettings

from auth_verifier import (
    MCP_RESOURCE_URL,
    OAUTH_ISSUER,
    FlexReportTokenVerifier,
)

INSTRUCTIONS = _TEXT["instructions"].replace("{auth_block}", _TEXT["auth_block_oauth"])

_token_verifier = FlexReportTokenVerifier()
_auth_settings = AuthSettings(
    issuer_url=OAUTH_ISSUER,
    resource_server_url=MCP_RESOURCE_URL,
    required_scopes=[],  # backend enforces scope/plan; don't gate at the transport
)
# --- Discovery / metadata catalogues ---------------------------------------
# One tool (`list_options`) enumerates valid parameter values (event types,
# report override items, sectors, tickers, indicators, ...) instead
# of a standalone tool per catalogue. All read-only and public.


mcp = FastMCP(
    "flexreport",
    instructions=INSTRUCTIONS,
    host=os.environ.get("MCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("MCP_PORT", "8000")),
    # Behind a load balancer (ALB): make each request self-contained instead of
    # holding a long-lived per-session SSE stream the LB would choke on, and return
    # plain JSON rather than text/event-stream. Stateless mode has no persistent
    # session, so auth is per-call (the validated OAuth bearer), not a cache.
    stateless_http=True,
    json_response=True,
    auth=_auth_settings,
    token_verifier=_token_verifier,
)


def _inbound_request(ctx: Context):
    """Best-effort fetch of the inbound Starlette Request from the MCP context."""
    return getattr(ctx.request_context, "request", None)


async def _send(
    ctx: Context,
    method: str,
    path: str,
    *,
    require_auth: bool = True,
    raw: bool = False,
    **kwargs: Any,
) -> Any:
    """Forward a request to the backend, returning parsed JSON or a structured error.

    Auth (handled by auth_headers): the validated inbound OAuth bearer is
    forwarded to the backend on every call.

    With `raw=True`, a 2xx response's body is returned as bytes (for binary endpoints
    like a PDF download) instead of being parsed as JSON.

    Errors (missing auth, transport failure, non-2xx) are returned as a dict with
    an "error" key rather than raised, so the agent receives a clean, readable message.
    """
    try:
        headers = auth_headers(_inbound_request(ctx), required=require_auth)
    except MissingAuthError as e:
        return {"error": str(e)}

    try:
        resp = await get_client().request(method, path, headers=headers, **kwargs)
    except httpx.HTTPError as e:
        return {"error": f"Request to backend failed: {e}"}

    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        msg = f"Backend returned HTTP {resp.status_code}"
        if resp.status_code == 401:
            msg += " — not authenticated / token expired. Your MCP client should re-run the OAuth sign-in flow."
        return {"error": msg, "detail": detail}

    if raw:
        return resp.content

    try:
        return resp.json()
    except Exception:
        return {"error": "Backend returned a non-JSON response", "detail": resp.text}
