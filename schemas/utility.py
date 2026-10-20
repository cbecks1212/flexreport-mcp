"""Output schemas for tools/utility.py.

Each model is a tool's return type, so FastMCP advertises it as the tool's
`outputSchema` and validates every result against it. Models carrying backend
data use extra="allow" so a new backend field passes through instead of failing
validation; fields the backend can omit or null are Optional.
"""

from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class TaskStatusResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_id: str
    status: str = Field(description="PENDING, STARTED, RETRY, SUCCESS or FAILURE.")
    result: Optional[Any] = Field(
        None, description="The job's output once status is SUCCESS (shape depends on the job); "
                          "error info on FAILURE; null while pending."
    )


class ListOptionsResult(BaseModel):
    kind: str = Field(description="The catalog that was listed.")
    options: Union[list[Any], dict[str, Any], int, float, str, None] = Field(
        description="The valid values. Usually a list — bare strings for most kinds, records for "
                    "event_types, tickers, tickers_with_names, as_reported_items, revenue_segments, "
                    "institutional_managers and data_feeds. fiscal_quarter is a single quarter number."
    )


class SubIndustriesResult(BaseModel):
    industries: list[Optional[str]] = Field(
        description="Distinct industries within the requested sectors, for list_realtime_events(industry=[...])."
    )


class OnboardAccepted(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_id: str = Field(description="Onboarding request id; the user is emailed when the first report is ready.")
    status: str = Field(description="PENDING on submission.")


class PlanUsage(BaseModel):
    model_config = ConfigDict(extra="allow")

    plan: Optional[str] = None
    used: Optional[int] = None
    limit: Optional[int] = Field(None, description="Request limit for the period; null when unlimited.")
    period: Optional[str] = None


class BillingLinkResult(BaseModel):
    message: str = Field(description="Ready-to-show markdown for the user, including the link when there is one.")
    url: Optional[str] = Field(None, description="The browser link; null when it could not be generated.")
    usage: Optional[PlanUsage] = Field(None, description="Current plan usage (upgrade_plan only, when known).")
