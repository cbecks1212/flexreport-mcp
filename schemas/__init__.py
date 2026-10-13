"""Output schemas for the Flexreport MCP tools.

One module per `tools/` module. A tool's return type is a model from here, so
FastMCP advertises it as the tool's `outputSchema` and validates each result
against it (see `core._ok` for how backend errors bypass that validation).
"""
