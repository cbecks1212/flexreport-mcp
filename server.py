"""Flexreport MCP server — exposes the equity backend's live events and report
artifacts as on-demand MCP tools over streamable-http.

Each tool is a thin wrapper around a public backend HTTP endpoint. The caller's
inbound `Authorization: Bearer <JWT>` is forwarded so the backend enforces auth,
plan quota, and rate limits. This service holds no credentials and does not touch
AWS/Redis/DB or import anything from the API repo.

Layout:
  core.py            the FastMCP instance, server instructions and `_send`
  tools/<group>.py   the tools, one module per endpoint family (importing the
                     package registers them all)
  situate.py         pure composition logic behind the `situate` tool
"""

from starlette.responses import PlainTextResponse

import situate as situate_mod
from core import mcp
import tools  # noqa: F401  — registers every @mcp.tool


def _check_plan_registry() -> None:
    """Every tool situate's plan may emit must exist here — a rename fails at import,
    not as a dead hint in an agent's hands."""
    try:
        registered = {t.name for t in mcp._tool_manager.list_tools()}
    except Exception:  # SDK internals moved — do not block startup on the check
        return
    missing = sorted(set(situate_mod.TOOL_REGISTRY) - registered)
    if missing:
        raise RuntimeError(f"situate.TOOL_REGISTRY names tools that are not registered: {missing}")


_check_plan_registry()


@mcp.custom_route("/health", methods=["GET"])
async def health(_request) -> PlainTextResponse:
    """Liveness probe for load balancers (ALB target-group health check).

    Plain 200 outside the MCP protocol — the `/mcp` path speaks MCP and won't
    return 200 to a bare GET, so point the health check here.
    """
    return PlainTextResponse("ok")


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
