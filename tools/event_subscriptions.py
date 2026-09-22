"""Event subscription tools: push delivery of realtime events over the backend's
Server-Sent Events streams (`/events`, `/event-subscription/{id}`).

A tool call returns once, so the two stream tools are BOUNDED READS: open the SSE
stream with the caller's bearer, collect up to `max_events` matched events or wait
up to `max_wait_seconds`, close the connection, and return the events plus the last
cursor seen. The MCP server keeps no state — subscriptions, filters and saved
cursors all live in the API.
"""

import asyncio
import json
import time
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations

from client import MissingAuthError, auth_headers, get_client
from core import mcp, _inbound_request, _send

# The backend emits a heartbeat every <= 8 s on an idle stream and the ALB idle timeout
# is 60 s; a read timeout comfortably above the heartbeat keeps a quiet stream open
# while a stalled one still fails fast.
_SSE_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)

# One MCP call is itself one HTTP request through the same ALB, so a bounded read
# must return before the 60 s idle timeout cuts it off.
_MAX_WAIT_CAP_S = 55
_MAX_EVENTS_CAP = 200


def _parse_frame(lines: list[str]) -> dict[str, Any]:
    """Assemble one SSE frame from its field lines (the text between blank lines)."""
    frame: dict[str, Any] = {"id": None, "event": None, "data": []}
    for line in lines:
        if not line or line.startswith(":"):
            continue  # comment / keep-alive
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            frame["data"].append(value)
        elif field in ("id", "event"):
            frame[field] = value
    frame["data"] = "\n".join(frame["data"])
    return frame


async def _read_sse(
    ctx: Context,
    path: str,
    params: dict[str, Any],
    *,
    cursor: Optional[str],
    max_events: int,
    max_wait_seconds: int,
) -> dict[str, Any]:
    """Bounded read of a backend SSE stream.

    Returns {"events": [...], "cursor": <last id seen, matched or not>, "closed": None | reason,
    "elapsed_seconds": n}. `cursor` falls back to the one passed in when no frame carried an id,
    so the agent can always feed it straight back into the next call. Errors come back as
    {"error": ...} like every other tool.
    """
    max_events = max(1, min(int(max_events), _MAX_EVENTS_CAP))
    max_wait_seconds = max(1, min(int(max_wait_seconds), _MAX_WAIT_CAP_S))

    try:
        headers = auth_headers(_inbound_request(ctx), required=True)
    except MissingAuthError as e:
        return {"error": str(e)}
    headers["Accept"] = "text/event-stream"

    events: list[Any] = []
    last_id: Optional[str] = cursor
    closed: Optional[str] = None
    started = time.monotonic()

    try:
        async with asyncio.timeout(max_wait_seconds):
            async with get_client().stream(
                "GET", path, params=params, headers=headers, timeout=_SSE_TIMEOUT
            ) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    try:
                        detail: Any = json.loads(body)
                    except Exception:
                        detail = body.decode(errors="replace")
                    msg = f"Backend returned HTTP {resp.status_code}"
                    if resp.status_code == 401:
                        msg += " — not authenticated / token expired. Your MCP client should re-run the OAuth sign-in flow."
                    return {"error": msg, "detail": detail}

                pending: list[str] = []
                async for line in resp.aiter_lines():
                    if line.strip():
                        pending.append(line.rstrip("\r"))
                        continue
                    if not pending:
                        continue
                    frame = _parse_frame(pending)
                    pending = []
                    if frame["id"]:
                        last_id = frame["id"]
                    kind = frame["event"]
                    if kind == "event":
                        try:
                            events.append(json.loads(frame["data"]))
                        except Exception:
                            events.append({"raw": frame["data"]})
                        if len(events) >= max_events:
                            break
                    elif kind == "closed":
                        try:
                            closed = json.loads(frame["data"]).get("reason") or "closed"
                        except Exception:
                            closed = frame["data"] or "closed"
                        break
                    # heartbeat (and anything unknown): the id above is all we keep
    except TimeoutError:
        pass  # max_wait_seconds elapsed — return what was collected
    except httpx.HTTPError as e:
        if not events and last_id == cursor:
            return {"error": f"Request to backend failed: {e}"}
        # a transport hiccup after some frames: return them, the cursor resumes the rest

    return {
        "events": events,
        "cursor": last_id,
        "closed": closed,
        "elapsed_seconds": round(time.monotonic() - started, 1),
    }


@mcp.tool(annotations=ToolAnnotations(title="Stream Realtime Events (Ad-Hoc)", readOnlyHint=True))
async def stream_events(
    ctx: Context,
    topics: Optional[list[str]] = None,
    symbols: Optional[list[str]] = None,
    cursor: Optional[str] = None,
    max_events: int = 20,
    max_wait_seconds: int = 30,
) -> Any:
    """Listen for realtime events as they publish — a bounded read of the backend's
    ad-hoc SSE stream (`GET /events`), filter passed inline, nothing saved server-side.

    Returns {"events": [...], "cursor": "<last id seen>", "closed": null | reason}.
    Each item in `events` is the same object `list_realtime_events` returns for that
    type ({event_type, symbol, payload, published_at, id}) — summary, thesis impact,
    significant changes, citations — the moment it publishes, no polling of the 12h
    cache. There is NO PDF link on an event: for the research, POST the symbol to
    get_latest_report (a fresh plan renders in ~10-20 s; stale/missing -> a
    generate_report_for_stock rebuild, ticker only).

    THE LOOP: a call returns after `max_events` matched events or `max_wait_seconds`
    (cap 55 s), whichever comes first — call it again to keep listening, and ALWAYS
    pass the returned `cursor` back in: this stream keeps no state, so without it
    the next call starts at "now" and drops whatever published in between. The
    cursor advances on heartbeats too (events the filter dropped), so an empty
    `events` list with a moved cursor is normal on a quiet tape. Omit `cursor` on
    the first call to start at now. For a subscription that resumes by itself, use
    register_event_subscription + stream_event_subscription instead.

    `topics` (optional, any mix; case-insensitive):
      - an event type from list_options("event_types") — eps_update, 8k_release,
        transcript_update, ir_publication, biggest_mover, 13f_new, strategy_update, ...
      - a family from the same listing — earnings, news, market_movement,
        institutional_ownership, analyst_activity, strategy, prediction — which
        matches every type in it, including types added later;
      - `report_plan` (stream-only): a frame each time a report plan is SAVED
        ({symbol, event_type, planned_at, queued_at, plan_key}); the next step is
        get_latest_report([symbol]), which finds the plan fresh and renders it.
    `symbols` (optional): tickers; 13f_* filer events carry the filer's CIK in the
    symbol slot. No topics and no symbols = everything. Unknown topic -> 422 with the
    allowed lists in `detail`.

    `closed` set means the server ended the stream — do not reconnect with that id.
    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    params: dict[str, Any] = {}
    if topics:
        params["topics"] = [t.strip() for t in topics if t and t.strip()]
    if symbols:
        params["symbols"] = [s.strip() for s in symbols if s and s.strip()]
    if cursor:
        params["cursor"] = cursor
    return await _read_sse(
        ctx, "/events", params,
        cursor=cursor, max_events=max_events, max_wait_seconds=max_wait_seconds,
    )


@mcp.tool(annotations=ToolAnnotations(title="Register Event Subscription", readOnlyHint=False, destructiveHint=False))
async def register_event_subscription(
    ctx: Context,
    topics: Optional[list[str]] = None,
    symbols: Optional[list[str]] = None,
) -> Any:
    """Save a realtime-event filter server-side and get a `subscription_id` whose
    stream resumes where the last read stopped (`POST /register-subscription`).

    Returns {subscription_id, filter, cursor, created_at}. `filter` echoes the
    normalised filter (topics lower-cased, symbols upper-cased, duplicates dropped);
    `cursor` is the position the subscription starts from — only events published
    AFTER registration are ever delivered. KEEP the subscription_id: there is no list
    endpoint yet, and it is what stream_event_subscription and
    delete_event_subscription take.

    `topics` / `symbols` as in stream_events: event types and families from
    list_options("event_types") plus the stream-only `report_plan` topic; an empty
    body listens to everything. Same vocabulary rules — `report_plan` for "stream
    full research as it is minted", a family for "everything earnings-related",
    symbols for a watchlist. 30 registrations/hour per user; delete what you no
    longer need. The stream keeps ~2,000 entries (several days at current volume): a
    subscription idle longer than that resumes at the oldest entry and may have
    missed events.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    body: dict[str, Any] = {}
    if topics:
        body["topics"] = [t.strip() for t in topics if t and t.strip()]
    if symbols:
        body["symbols"] = [s.strip() for s in symbols if s and s.strip()]
    return await _send(ctx, "POST", "/register-subscription", json=body)


@mcp.tool(annotations=ToolAnnotations(title="Stream Event Subscription (Resumable)", readOnlyHint=True))
async def stream_event_subscription(
    ctx: Context,
    subscription_id: str,
    cursor: Optional[str] = None,
    max_events: int = 20,
    max_wait_seconds: int = 30,
) -> Any:
    """Read the next events on a saved subscription — a bounded read of
    `GET /event-subscription/{subscription_id}` that resumes from the position saved
    when the previous read closed, so repeated calls see every matching event exactly
    once with no gap and no replay.

    Returns {"events": [...], "cursor": "<last id seen>", "closed": null | reason} —
    same shape and event objects as stream_events. A call returns after `max_events`
    matched events or `max_wait_seconds` (cap 55 s); call again to keep listening. The
    API saves the cursor when this read closes the connection, so the next call resumes
    correctly WITHOUT passing `cursor`. Pass `cursor` only to override the saved
    position for one connection (e.g. re-read from an earlier frame id); it is not
    persisted.

    An empty `events` list means nothing matched in the window, not that the
    subscription is broken. `closed` = "subscription deleted" means it was removed
    mid-stream — do not reconnect. 404 `unknown subscription_id` covers both a missing
    id and another user's id (indistinguishable by design).

    Acting on what arrives: an `event` item is the full realtime event (use it
    directly); to hand over research, get_latest_report([symbol]) first (a fresh plan
    renders in ~10-20 s; batch up to 50 symbols per call), and only if it comes back
    stale/missing a generate_report_for_stock(ticker) rebuild. A `report_plan` item
    means a plan was just saved: get_latest_report([symbol]) renders it fresh.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    sid = (subscription_id or "").strip()
    if not sid:
        return {"error": "`subscription_id` is required — the id returned by register_event_subscription."}
    params: dict[str, Any] = {}
    if cursor:
        params["cursor"] = cursor
    return await _read_sse(
        ctx, f"/event-subscription/{sid}", params,
        cursor=cursor, max_events=max_events, max_wait_seconds=max_wait_seconds,
    )


@mcp.tool(annotations=ToolAnnotations(title="Delete Event Subscription", readOnlyHint=False, destructiveHint=True, idempotentHint=True))
async def delete_event_subscription(ctx: Context, subscription_id: str) -> Any:
    """Remove a saved event subscription (`DELETE /event-subscription/{subscription_id}`).

    Returns {"status": "DELETED", "subscription_id": ...}; any stream still open on it
    ends with a `closed` frame within one read cycle (<= 8 s). 404 `unknown
    subscription_id` for a missing id or another user's id. Registrations are
    rate-limited (30/hour), so delete subscriptions the user is done with rather than
    registering fresh ones for every session.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    sid = (subscription_id or "").strip()
    if not sid:
        return {"error": "`subscription_id` is required — the id returned by register_event_subscription."}
    return await _send(ctx, "DELETE", f"/event-subscription/{sid}")
