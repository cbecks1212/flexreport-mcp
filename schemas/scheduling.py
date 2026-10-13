"""Output schemas for tools/scheduling.py (backend src/routers/scheduler.py)."""

from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class ScheduleWorkflowResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: Optional[int] = Field(None, description="201 when the workflow was created, 200 when an existing one was updated.")
    workflow_id: Optional[Union[int, str]] = Field(None, description="Id of the saved workflow definition.")
    name: Optional[str] = Field(None, description="The workflow name (upsert key).")
    scheduled: Optional[bool] = None
    cron: Optional[str] = Field(None, description="The cron expression attached.")
    task_name: str = Field(description="\"wf:{email}:{name}\" — the key list_scheduled_tasks shows and delete_scheduled_task takes.")


class ScheduledTask(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(description="Task key; pass to delete_scheduled_task. Workflow schedules are \"wf:{email}:{workflow-name}\".")
    active: Optional[bool] = None
    schedule: Optional[str] = Field(None, description="The schedule as a cron string.")
    args: Optional[list[Any]] = None
    kwargs: Optional[dict[str, Any]] = Field(None, description="Stored task parameters (kwargs.params holds workflow_id, delivery, ...).")
    enabled: Optional[bool] = None
    last_run_at: Optional[str] = Field(None, description="ISO-8601 time of the last fire; null if it never ran.")
    total_run_count: Optional[int] = None


class ScheduledTasksResult(BaseModel):
    tasks: list[ScheduledTask] = Field(description="The caller's scheduled tasks; empty when there are none.")


class DeleteScheduledTaskResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    msg: str = Field(description="\"<task_name> deleted\".")
