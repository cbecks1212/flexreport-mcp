"""Output schemas for tools/pdf_reports.py.

Each model is a tool's return type, so FastMCP advertises it as the tool's
`outputSchema` and validates every result against it. Models carrying backend
data use extra="allow" so a new backend field passes through instead of failing
validation; fields the backend can omit or null are Optional.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ReportTask(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_id: str = Field(description="Celery task id; poll it with get_task_status until SUCCESS.")
    status: str = Field(description="Task state at dispatch, normally \"PENDING\".")
    symbol: Optional[str] = Field(None, description="Ticker the report task renders.")


class StockReportResult(BaseModel):
    """Keyed by the uppercased ticker: {"<TICKER>": {"task_id", "status", "symbol"}}."""

    model_config = ConfigDict(extra="allow")

    __pydantic_extra__: dict[str, ReportTask] = Field(init=False)


class TaskDispatch(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_id: str = Field(description="Celery task id; poll it with get_task_status until SUCCESS.")
    status: str = Field(description="Task state at dispatch, normally \"PENDING\".")


class CachedReport(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    url: Optional[str] = Field(None, description="Presigned PDF download link, valid ~6h.")
    report: Optional[str] = Field(None, description="The same PDF inline as base64.")
    generated_at: Optional[str] = Field(None, description="When the cached PDF was built (ISO UTC).")
    age_hours: Optional[float] = Field(None, description="Hours since generated_at.")
    latest_event_at: Optional[str] = Field(None, description="Timestamp of the symbol's latest report inputs (the event).")
    stale: Optional[bool] = Field(
        None, description="True: the PDF predates the latest event. False: it reflects it. Null: freshness unknown."
    )


class CachedReportsResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    result: list[CachedReport] = Field(default_factory=list, description="Symbols served from the cached PDF.")
    missing: list[str] = Field(default_factory=list, description="Symbols with no plan to render and no cached PDF.")
    rendering: dict[str, ReportTask] = Field(
        default_factory=dict,
        description="Symbols whose fresh saved plan is rendering into the user's template, keyed by ticker.",
    )


class ReportPlan(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    event_type: Optional[str] = Field(None, description="Realtime event type that triggered the plan.")
    planned_at: Optional[str] = Field(None, description="When the plan was built (ISO).")
    queued_at: Optional[str] = Field(None, description="Timestamp of the report inputs (the event) the plan was built from.")
    fresh: Optional[bool] = Field(
        None, description="True: get_latest_report renders it in ~10-20 s. False: a newer event landed; only a rebuild refreshes it."
    )
    source: Optional[str] = Field(None, description="\"redis\" (built within 3 days) or \"s3\" (older durable copy).")


class ReportPlansResult(BaseModel):
    plans: list[ReportPlan] = Field(
        description="Saved report plans in the window, newest first. Empty means nothing was planned in the window."
    )


class DownloadedPdf(BaseModel):
    file_name: str = Field(description="The filename passed in.")
    media_type: str = Field(description="Always \"application/pdf\".")
    report: str = Field(description="The PDF as base64.")


class TemplateTask(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_id: str = Field(description="Celery task id; poll it with get_task_status for the result.")
    status: str = Field(description="Task state at dispatch, normally \"PENDING\".")
    next_step: Optional[str] = Field(None, description="What the finished task returns.")
    changed: Optional[list[str]] = Field(None, description="update_user_template only: the fields that were updated.")


class UserTemplateRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    template_type: Optional[str] = Field(None, description="\"add_on\" or \"bespoke\".")
    template_format: Optional[str] = Field(None, description="\"html\".")
    anchor: Optional[str] = Field(None, description="Section an add_on applies to; null for bespoke.")
    template: Optional[str] = Field(None, description="The sanitized markup as submitted (up to 200 KB).")
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class DeleteTemplateResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    msg: str = Field(description="Confirmation message.")
