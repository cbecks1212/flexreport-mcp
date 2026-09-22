"""Tool modules for the Flexreport MCP server.

Importing this package registers every `@mcp.tool` on the shared `core.mcp`
instance. Each module wraps one group of backend endpoints; add a new tool to the
module its endpoint family belongs to (or a new module, imported here).

The import order below is the order tools are advertised to MCP clients: situate
and the realtime tools first, since they route everything else.
"""

from tools import (  # noqa: F401  (imported for their registration side effects)
    realtime_events,
    event_subscriptions,
    pdf_reports,
    data_exploration,
    utility,
    market_data,
    strategies,
    scheduling,
)

__all__ = [
    "realtime_events",
    "event_subscriptions",
    "pdf_reports",
    "data_exploration",
    "utility",
    "market_data",
    "strategies",
    "scheduling",
]
