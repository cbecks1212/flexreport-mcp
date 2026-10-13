"""Scheduling tools: recurring workflows the backend runs on the user's behalf."""

from typing import Annotated, Any, Literal, Optional

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from pydantic import Field

from core import mcp, _send, _ok
from schemas.scheduling import (
    DeleteScheduledTaskResult,
    ScheduledTasksResult,
    ScheduleWorkflowResult,
)


@mcp.tool(annotations=ToolAnnotations(title="Schedule Recurring Workflow", readOnlyHint=False, destructiveHint=False))
async def schedule_task(
    ctx: Context,
    name: Annotated[
        str,
        Field(description="Lowercase slug and upsert key (no spaces, colons, or uppercase); re-saving the "
                          "same name updates the workflow and re-attaches its schedule.",
              pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$"),
    ],
    steps: Annotated[
        list[dict],
        Field(description="Ordered 1-8 steps; the first must be a fetch. Fetch: {\"kind\": \"fetch\", "
                          "\"endpoint\": \"<allowlisted kebab-case path, no leading slash>\", \"params\": {...}, "
                          "\"label\": \"...\"}. Transform: {\"kind\": \"transform\", \"instruction\": "
                          "\"<=2000 chars, applied to all prior results>\", \"label\": \"...\"}. `label` is an "
                          "optional unique lowercase slug (default step_{i}). The last step's output is delivered.",
              min_length=1, max_length=8),
    ],
    regular_cron: Annotated[
        str,
        Field(description="Raw 5-field cron expression for when the workflow fires, e.g. \"30 6 * * 1-5\" "
                          "(weekdays 06:30); validated server-side."),
    ],
    delivery: Annotated[
        Literal["email", "dashboard"],
        Field(description="Where each run's output goes."),
    ] = "email",
    description: Annotated[Optional[str], Field(description="Optional human-readable note saved with the workflow.", max_length=500)] = None,
) -> ScheduleWorkflowResult:
    """Save a multi-step WORKFLOW (upsert by `name`) and attach a recurring cron
    schedule for its delivery.

    Use this when the user wants a sequence of platform calls delivered ON A
    SCHEDULE / repeatedly (e.g. "every weekday morning pull 8-K releases, screen
    for cheap large-caps, and email me the overlaps as a table"). For a ONE-OFF
    request, call the corresponding tools directly instead.

    `steps` (shape in the parameter description): a transform applies its
    instruction to the accumulated results of ALL prior steps (e.g. "intersect the
    two event sets by symbol and build a table"), and each step's `label` keys its
    result. The LAST step's output is what gets delivered, so end table/summary
    workflows with a transform.

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

    Each user may hold at most 10 active schedules.

    Returns `status` 201 (created) or 200 (updated) with `workflow_id` and
    `task_name` ("wf:{email}:{name}"), the key that `list_scheduled_tasks` shows
    and `delete_scheduled_task` takes.
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

    return _ok(await _send(
        ctx, "POST", "/save-user-workflow", json=body
    ))


@mcp.tool(annotations=ToolAnnotations(title="List Scheduled Tasks", readOnlyHint=True))
async def list_scheduled_tasks(
    ctx: Context,
) -> ScheduledTasksResult:
    """List the caller's scheduled tasks (cron jobs created via `schedule_task`).

    Returns `tasks`, only the authenticated user's jobs, each as
    {"name", "active", "schedule" (cron string), "args", "kwargs", "enabled",
     "last_run_at", "total_run_count"}. Workflow schedules are named
    "wf:{email}:{workflow-name}". Use `name` as the key to remove a job with
    `delete_scheduled_task`.
    """
    tasks = _ok(await _send(ctx, "GET", "/get-scheduled-tasks"))
    return ScheduledTasksResult(tasks=tasks)


@mcp.tool(annotations=ToolAnnotations(title="Delete Scheduled Task", readOnlyHint=False, destructiveHint=True, idempotentHint=True))
async def delete_scheduled_task(
    ctx: Context,
    task_name: Annotated[
        str,
        Field(description="The task `name` from list_scheduled_tasks — for workflows the full "
                          "\"wf:{email}:{workflow-name}\", NOT the bare workflow name."),
    ],
) -> DeleteScheduledTaskResult:
    """Delete a scheduled task (cron job) by its name.

    For workflow schedules `task_name` is the value `schedule_task` returned as
    `task_name`. Deleting removes only
    the cron schedule; the saved workflow definition is kept and can be
    re-scheduled by re-saving it via `schedule_task`. Returns
    {"msg": "<task_name> deleted"} on success, or an error if no such task exists.
    """
    return _ok(await _send(
        ctx, "DELETE", "/delete-scheduled-task",
        params={"task_name": task_name},
    ))
