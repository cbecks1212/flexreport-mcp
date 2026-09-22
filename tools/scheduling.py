"""Scheduling tools: recurring workflows the backend runs on the user's behalf."""

from typing import Any, Literal, Optional

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations

from core import mcp, _send


@mcp.tool(annotations=ToolAnnotations(title="Schedule Recurring Workflow", readOnlyHint=False, destructiveHint=False))
async def schedule_task(
    ctx: Context,
    name: str,
    steps: list[dict],
    regular_cron: str,
    delivery: Literal["email", "dashboard"] = "email",
    description: Optional[str] = None,
) -> Any:
    """Save a multi-step WORKFLOW (upsert by `name`) and attach a recurring cron
    schedule for its delivery.

    Use this when the user wants a sequence of platform calls delivered ON A
    SCHEDULE / repeatedly (e.g. "every weekday morning pull 8-K releases, screen
    for cheap large-caps, and email me the overlaps as a table"). For a ONE-OFF
    request, call the corresponding tools directly instead.

    `name` is a lowercase slug (pattern ^[a-z0-9][a-z0-9_-]{0,63} — no spaces,
    colons, or uppercase) and the upsert key: re-saving the same name updates
    the definition and re-attaches the schedule from `regular_cron`.

    `steps` is an ordered list of 1-8 dicts of two kinds:

    - fetch     -> {"kind": "fetch", "endpoint": "...", "params": {...},
                    "label": "..."}: call an allowlisted backend endpoint
                    (kebab-case path, no leading slash).
    - transform -> {"kind": "transform", "instruction": "...", "label": "..."}:
                    apply a natural-language instruction (<=2000 chars) to the
                    accumulated results of ALL prior steps (e.g. "intersect the
                    two event sets by symbol and build a table").

    The FIRST step must be a fetch. `label` is optional (unique lowercase slug;
    defaults to step_{i}) and keys that step's result. The LAST step's output is
    what gets delivered, so end table/summary workflows with a transform.

    Allowlisted fetch endpoints (anything else is rejected with a 422 at save
    time; `params` takes the same keys as the matching tool):

    - POST, JSON-body params: "get-realtime-events" (`list_realtime_events`),
      "list-upcoming-earnings-announcements" (`list_earnings_announcements`),
      "predict-earnings-announcement-move" ({"symbols": [...]}),
      "data-catalogue-exploration" ({"query": "..."}).
    - GET, scalar query params only (no lists/dicts): "get-company-snapshot"
      (requires {"symbol": "..."}), "get-strategy-performance-summary",
      "get-strategy-track-record", "get-strategy-swaps", "get-stock-picks",
      "list-tickers", "list-realtime-event-options", "get-sectors",
      "get-sub-industries".
    - EXPENSIVE — max 2 per workflow, and the cron must use a literal minute and
      at most 4 literal hours (no sub-hourly / "*" fields): "screen-stocks"
      (`screen_stocks`), "generate-research-report" ({"query": "..."}),
      "create-full-report" (a full ~10 min rebuild every run; {"ticker": "..."} for
      the symbol's standard report;
      add "user_override": true plus the shaping lists `generate_report_for_stock`
      takes — financial_items, ratios, as_reported_financial_items, revenue_segment,
      technical_analysis_items, estimate_items, institutional_ownership CIKs — for a
      custom one; the backend rejects the lists without the switch),
      "optimize-symbols", "optimize-portfolio", "list-optimized-stock-picks".

    `delivery` is "email" (default) or "dashboard".

    `regular_cron` is a raw 5-field cron expression controlling when the
    workflow fires (e.g. "30 6 * * 1-5" = weekdays 06:30), validated
    server-side. Each user may hold at most 10 active schedules.

    Returns 201 (created) or 200 (updated) with `workflow_id` and `task_name`
    ("wf:{email}:{name}"), the key that `list_scheduled_tasks` shows and
    `delete_scheduled_task` takes.
    """

    workflow: dict[str, Any] = {
        "name": name,
        "steps": steps,
        "delivery": delivery,
    }
    if description:
        workflow["description"] = description

    body: dict[str, Any] = {
        "workflow": workflow,
        "schedule": {"regular_cron": regular_cron},
    }

    return await _send(
        ctx, "POST", "/save-user-workflow", json=body
    )


@mcp.tool(annotations=ToolAnnotations(title="List Scheduled Tasks", readOnlyHint=True))
async def list_scheduled_tasks(
    ctx: Context,
) -> Any:
    """List the caller's scheduled tasks (cron jobs created via `schedule_task`).

    Returns only the authenticated user's jobs, each as
    {"name", "active", "schedule" (cron string), "args", "kwargs", "enabled",
     "last_run_at", "total_run_count"}. Workflow schedules are named
    "wf:{email}:{workflow-name}". Use `name` as the key to remove a job with
    `delete_scheduled_task`.
    """
    return await _send(ctx, "GET", "/get-scheduled-tasks")


@mcp.tool(annotations=ToolAnnotations(title="Delete Scheduled Task", readOnlyHint=False, destructiveHint=True, idempotentHint=True))
async def delete_scheduled_task(
    ctx: Context,
    task_name: str,
) -> Any:
    """Delete a scheduled task (cron job) by its name.

    `task_name` is the `name` returned by `list_scheduled_tasks` — for workflow
    schedules that is the "wf:{email}:{workflow-name}" value `schedule_task`
    returns as `task_name`, NOT the bare workflow name. Deleting removes only
    the cron schedule; the saved workflow definition is kept and can be
    re-scheduled by re-saving it via `schedule_task`. Returns
    {"msg": "<task_name> deleted"} on success, or an error if no such task exists.
    """
    return await _send(
        ctx, "DELETE", "/delete-scheduled-task",
        params={"task_name": task_name},
    )
