"""Utility tools: async task polling, parameter-option catalogues, symbol onboarding, billing."""

from typing import Any, Literal, Optional, get_args

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations

from core import mcp, _send


_OPTION_ENDPOINTS = {
    "event_types": "/list-realtime-event-options",
    "financial_items": "/list-financial-items",
    "financial_ratios": "/list-financial-ratios",
    "sectors": "/get-sectors",
    "institutional_investor_types": "/list-institutional-investor-types",
    "countries": "/list-countries",
    "fiscal_quarter": "/get-fiscal-quarter",
    "market_cap": "/list-marketcap-options",
    "intraday_frequency": "/list-intraday-chart-options",
    "technical_indicators": "/list-technical-indicators",
    "tickers": "/list-tickers",
    "tickers_with_names": "/list-symbols-with-names",
    # Report vocabularies scoped by a query parameter (see _OPTION_QUERY_PARAMS).
    "as_reported_items": "/list-as-reported-items",
    "revenue_segments": "/list-revenue-segments",
    "institutional_managers": "/list-institutional-managers",
}

# Catalogues that take a query parameter: a filer's own vocabularies (its tagged XBRL
# concepts, its revenue segments) need a `ticker`; the manager lookup needs a name
# fragment `q` and/or a SEC `cik`. `list_options` forwards whichever were passed and
# refuses the call when none of the kind's parameters is present.
_OPTION_QUERY_PARAMS: dict[str, tuple[str, ...]] = {
    "as_reported_items": ("ticker",),
    "revenue_segments": ("ticker",),
    "institutional_managers": ("q", "cik"),
}

# The `kind` schema advertised to MCP clients. Members must stay in lockstep
# with the dict keys above (guarded by the assert).
_OptionKind = Literal[
    "event_types",
    "financial_items",
    "financial_ratios",
    "sectors",
    "institutional_investor_types",
    "countries",
    "fiscal_quarter",
    "market_cap",
    "intraday_frequency",
    "technical_indicators",
    "tickers",
    "tickers_with_names",
    "as_reported_items",
    "revenue_segments",
    "institutional_managers",
]
assert set(get_args(_OptionKind)) == set(_OPTION_ENDPOINTS)
assert set(_OPTION_QUERY_PARAMS) <= set(_OPTION_ENDPOINTS)


@mcp.tool(annotations=ToolAnnotations(title="Get Async Task Status", readOnlyHint=True))
async def get_task_status(ctx: Context, task_id: str) -> Any:
    """Poll the status of an async job (a get_latest_report `rendering` task, generate_report_for_stock, generate_research_report, explore_data_catalogue, explore_data_coverage, screen_stocks, ...).

    Returns {"task_id": ..., "status": ..., "result": ...}. `status` is one of
    PENDING, SUCCESS, FAILURE, RETRY. `result` is populated once status is SUCCESS.
    """
    return await _send(
        ctx, "GET", "/task-status",
        params={"task_id": task_id}, require_auth=False,
    )


@mcp.tool(annotations=ToolAnnotations(title="List Valid Parameter Options", readOnlyHint=True))
async def list_options(
    ctx: Context,
    kind: _OptionKind,
    ticker: Optional[str] = None,
    q: Optional[str] = None,
    cik: Optional[str] = None,
) -> Any:
    """Enumerate the valid values for a parameter, straight from the backend.

    Call this BEFORE guessing a parameter value. `kind` selects which catalog; three
    kinds are scoped by a query parameter (`ticker`, or `q` / `cik`) and refuse the call
    without it:

    - "event_types"                  -> valid `event_type` for `list_realtime_events`
                                        (eps_update, company_update, biggest_mover, ...)
    - "financial_items"              -> standardized EDGAR line items ("revenue",
                                        "operatingIncome") for a CUSTOM report's
                                        `financial_items` (`generate_report_for_stock`,
                                        or a scheduled "create-full-report" step)
    - "financial_ratios"             -> EDGAR ratios ("grossProfitMargin") for that
                                        report's `ratios`
    - "as_reported_items"            -> NEEDS `ticker`. The filer's OWN tagged XBRL
                                        concepts from its 10-Q / 10-K filings — {concept,
                                        statement_type, label, standard_concept,
                                        latest_period, facts} — for a custom report's
                                        `as_reported_financial_items`
    - "revenue_segments"             -> NEEDS `ticker`. The filer's revenue segments as
                                        tagged in its filings — {product_label,
                                        product_member, product_axis, concept,
                                        latest_period, facts} — for a custom report's
                                        `revenue_segment` (pass the product_label).
                                        A name lookup only: never sum members across it
    - "institutional_managers"       -> NEEDS `q` (name fragment, e.g. "berkshire")
                                        and/or `cik` (SEC CIK, any width). Managers as
                                        {cik, name, category, aum}, largest AUM first —
                                        pass the `cik` values as a custom report's
                                        `institutional_ownership`, never the names
    - "sectors"                      -> valid `sector` filter values
    - "institutional_investor_types" -> investor CATEGORIES (not managers) — the keys
                                        `screen_stocks(institutional_ownership=...)`
                                        filters on
    - "countries"                    -> covered countries
    - "fiscal_quarter"               -> the most recent fiscal quarter being reported
    - "market_cap"                   -> valid `market_cap` buckets (Small-cap,
                                        Medium-cap, Large-cap, Mega-cap)
    - "intraday_frequency"           -> supported intraday chart frequencies
    - "technical_indicators"         -> indicator names accepted by
                                        `get_technical_indicator_data` (rsi, macd,
                                        sma_50, ...); an unknown indicator there
                                        returns a 400 listing this set
    - "tickers"                      -> the full covered symbol universe as bare
                                        symbols — equities PLUS ~420 market indices
                                        as caret-prefixed symbols (^GSPC, ^VIX, ...).
                                        NOTE: thousands of names — a large payload
    - "tickers_with_names"           -> {symbol, company_name} pairs for the company
                                        universe ONLY — market indices are NOT in
                                        this list, only in "tickers" (an even larger
                                        payload)

    Authoritative and never stale: it reads the backend's live config, not a
    hardcoded list. Public — no auth required.
    """
    wanted = _OPTION_QUERY_PARAMS.get(kind, ())
    given = {"ticker": ticker, "q": q, "cik": cik}
    params = {name: given[name] for name in wanted if given[name]}
    if wanted and not params:
        return {"error": f'list_options("{kind}") needs {" and/or ".join(wanted)} — '
                         "pass it as a keyword argument."}
    return await _send(
        ctx, "GET", _OPTION_ENDPOINTS[kind],
        params=params or None, require_auth=False,
    )


@mcp.tool(annotations=ToolAnnotations(title="List Sub-Industries", readOnlyHint=True))
async def list_sub_industries(ctx: Context, sectors: list[str]) -> Any:
    """List the sub-industries within one or more sectors.

    `sectors` must be values from `list_options("sectors")`. Returns the
    distinct industries used to narrow `list_realtime_events(industry=[...])`.
    """
    return await _send(
        ctx, "GET", "/get-sub-industries",
        params={"sector": sectors}, require_auth=False,
    )


# --- User PDF templates ----------------------------------------------------
# THE way a user gets reports in their own format. A saved template is a
# BLUEPRINT — the author's stylesheet and page chrome plus one markup pattern
# per content kind (title, lead, sections, bullets, KPI cards, tables, charts,
# figure, source line, footnote) — which the backend applies automatically to
# every PDF it renders for that user (`get_latest_report` renders of fresh plans,
# `generate_report_for_stock` rebuilds, scheduled reports). Nobody fills template
# markup: a template is structure and style;
# the document supplies the content. A template is a visual choice, so the
# flow is draft (rendered previews) -> the user looks and picks -> save the
# treatment they approved -> `update_user_template` for later changes.
#
# There is no agent-driven PDF builder here any more: the build_pdf_* tools
# over /create-pdf and /create-pdf-sidebar (and the pdf_options catalogue that
# described their tag DSL) were retired in favour of templates. When a user
# wants a document composed from Flexreport data that no backend report
# covers — including a bespoke take on a company report — the agent explores,
# ideates with the user over the results, builds the PDF with its own document
# tooling and, if the user has a saved template (`get_user_template`), lays the
# content out in that format; if none is saved, it offers to draft one. A
# custom `generate_report_for_stock` rebuild is the ~10 min exception.


@mcp.tool(annotations=ToolAnnotations(title="Onboard New Symbol", readOnlyHint=False, destructiveHint=False))
async def onboard_symbol(
    ctx: Context,
    symbol: str,
) -> Any:
    """Request onboarding of a NOT-yet-covered ticker (mutating, authenticated).

    Kicks off a 30-60 min backend workflow and emails the authenticated user when
    the first report is ready. Rate-limited to 5/hour per user. Use only when a
    symbol is missing from `list_options("tickers")` / returns no data elsewhere.


    Returns {"task_id": ..., "status": "PENDING"}.
    """
    return await _send(
        ctx, "POST", "/onboard-symbol",
        params={"symbol": symbol},
    )


# --- Billing / plan upgrades ----------------------------------------------
# Both tools return a browser LINK only — the user completes payment/management
# in Stripe. This service never touches card or account details; the caller's
# forwarded bearer identifies the user, so neither tool takes any arguments.

@mcp.tool(annotations=ToolAnnotations(title="Upgrade Plan", readOnlyHint=False, destructiveHint=False))
async def upgrade_plan(ctx: Context) -> str:
    """Get a secure link for the user to upgrade or change their Flexreport Finance plan (e.g. to raise their request limit). Returns a checkout link — the user completes payment in their browser. Call this whenever the user asks to upgrade, subscribe, pay for, or change their plan, or when they've hit a usage/quota limit."""
    # /payment/upgrade-link is quota-exempt, so this is safe even after a 429.
    # _send forwards the caller's inbound bearer token.
    data = await _send(ctx, "GET", "/payment/upgrade-link")
    if not isinstance(data, dict) or data.get("error") or not data.get("url"):
        return ("Sorry, I couldn't generate an upgrade link just now. "
                "Please try again in a moment.")

    url = data["url"]
    u = data.get("usage") or {}
    lines = []
    if u.get("limit") is not None:  # omit the usage line when unlimited / usage null
        lines.append(
            f"You're currently on the **{u.get('plan', 'free')}** plan — "
            f"{u.get('used')}/{u.get('limit')} requests used ({u.get('period')})."
        )
    lines.append(f"👉 [Upgrade your plan]({url})")
    lines.append(
        "Open the link to choose a plan and complete checkout. Your new limit "
        "activates as soon as payment succeeds — no need to re-authenticate here."
    )
    return "\n\n".join(lines)


@mcp.tool(annotations=ToolAnnotations(title="Manage Billing", readOnlyHint=False, destructiveHint=False))
async def manage_billing(ctx: Context) -> str:
    """Get a link to the Stripe billing portal where the user can view invoices, update their card, or cancel their subscription. Use when the user asks to manage, change, or cancel their billing/subscription."""
    # _send forwards the caller's inbound bearer token.
    data = await _send(ctx, "GET", "/payment/portal-link")
    if isinstance(data, dict) and data.get("url") and not data.get("error"):
        return f"👉 [Manage your billing]({data['url']})"

    err = data.get("error", "") if isinstance(data, dict) else ""
    if "HTTP 404" in err:  # no active subscription yet
        return ("You don't have an active paid subscription yet. "
                "Use upgrade_plan to subscribe.")
    return ("Sorry, I couldn't open the billing portal just now. "
            "Please try again in a moment.")
