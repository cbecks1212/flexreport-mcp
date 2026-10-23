"""Output schemas for tools/data_feeds.py.

Each model is a tool's return type, so FastMCP advertises it as the tool's
`outputSchema`. Feed rows are passed through as plain dicts: each dataset has its
own columns (documented on the tool) and new backend columns must not fail
validation.
"""

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class DownloadDatasetResult(BaseModel):
    dataset: str = Field(description="The feed that was read.")
    start_date: Optional[str] = Field(None, description="First date requested (inclusive, YYYY-MM-DD); null with bulk_download.")
    end_date: Optional[str] = Field(None, description="Last date requested (inclusive, YYYY-MM-DD); null with bulk_download.")
    bulk_download: bool = Field(description="True when every published date was requested.")
    latest_file_version: bool = Field(description="True = newest file version per date only; false = every version.")
    shape: Literal["rows", "documents", "pages", "download_links"] = Field(
        description="What `rows` holds: `rows` (the feed's rows as sent), `documents` (a document feed "
                    "collapsed to one row per document, no page text), `pages` (one row per document page, "
                    "with `content`), or `download_links` (presigned URLs valid 12 hours)."
    )
    rows: list[dict[str, Any]] = Field(description="The feed rows after symbol filtering, collapsing and the max_rows cap.")
    row_count: int = Field(description="Rows before the max_rows cap (after symbol filtering and collapsing).")
    truncated: bool = Field(description="True when max_rows sliced `rows`; row_count is the full count.")
    published_at: Optional[str] = Field(None, description="Newest `published_at` among the returned rows (UTC) — quote it as the data's as-of.")
    notes: list[str] = Field(description="One line per thing left out or worth telling the user (pages collapsed, symbols filtered, truncation, empty feed).")


# File formats a user dataset can be saved in (backend DatasetFormat).
DatasetFormat = Literal["parquet", "csv", "tsv", "json", "jsonl", "xlsx", "xml", "rss", "atom"]


class DraftCustomDatasetDispatch(BaseModel):
    model_config = ConfigDict(extra="allow")

    draft_id: str = Field(description="The draft's id: pass to save_user_dataset, or as previous_draft_id to refine it.")
    task_id: str = Field(description="Poll this with get_task_status until SUCCESS (same value as draft_id).")
    status: str = Field(description="PENDING on dispatch.")
    queue: Optional[str] = Field(None, description="Worker queue the draft was routed to.")


class DatasetColumn(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    pg_type: Optional[str] = Field(None, description="Postgres type of the column.")


class SaveUserDatasetResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    dataset_id: Optional[Any] = Field(None, description="Id of the saved dataset row.")
    dataset_name: str = Field(description="The dataset's name (upsert key).")
    saved_query_id: Optional[Any] = Field(None, description="The saved query the builds run.")
    task_name: str = Field(description="\"ds:{email}:{name}\" — the schedule's key in list_scheduled_tasks / delete_scheduled_task.")
    cron: str = Field(description="When builds fire (America/New_York).")
    format: DatasetFormat
    date_mode: Literal["rolling", "snapshot"]
    schema_: list[DatasetColumn] = Field(alias="schema", description="Columns every build must produce.")
    feed_fields: Optional[dict[str, Optional[str]]] = Field(
        None, description="rss/atom only: the column filling each item field {title, link, description, pub_date, "
                          "guid}; null = fallback (description lists the other columns, pub_date = build time, "
                          "guid = link or a row hash)."
    )
    latest_url: Optional[str] = Field(
        None, description="Fixed, sign-in-free link to the newest file (the link is the key) — for an RSS reader, "
                          "Excel / Google Sheets web import or a platform's poller. 404 until the first build."
    )
    placeholder_id_to_poll: Optional[str] = Field(None, description="Task authoring the rolling-date placeholder twin; no need to poll.")
    build_task_id: Optional[str] = Field(None, description="The first build's task (build_now); null when not dispatched.")


class UserDatasetFile(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_date: Optional[str] = Field(None, description="Build date (ET) the file belongs to.")
    published_at: Optional[str] = Field(None, description="UTC time the file version was published.")
    url: str = Field(description="Presigned download link, valid 12 hours — hand to the user, never fetch or store.")


class UserDataset(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    user_request: Optional[str] = Field(None, description="The plain-English request the dataset was drafted from.")
    output_schema: Optional[list[dict[str, Any]]] = Field(None, description="[{name, pg_type}] every build must match.")
    file_format: Optional[str] = None
    date_mode: Optional[str] = None
    cron: Optional[str] = None
    task_name: Optional[str] = Field(None, description="\"ds:{email}:{name}\" schedule key.")
    last_built_at: Optional[str] = None
    last_build_status: Optional[str] = Field(
        None, description="written | unchanged (same rows, no new file) | empty | too_large (>500k rows) | "
                          "schema_drift (columns no longer match) | failed; null before the first build."
    )
    last_row_count: Optional[int] = None
    latest_url: Optional[str] = Field(None, description="Fixed, sign-in-free link to the newest file; 404 until the first build.")
    files: list[UserDatasetFile] = Field(default_factory=list)


class ListUserDatasetsResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    count: int
    datasets: list[UserDataset]


class UserDatasetFileResult(BaseModel):
    dataset_name: str
    format: str = Field(description="The file's format: json, jsonl, csv, tsv, xml, rss or atom.")
    version: str = Field(description="\"latest\", or the run_date / published_at of the file that was read.")
    published_at: Optional[str] = Field(None, description="When the file was published (UTC) — quote it as the data's as-of.")
    row_count: int
    rows: list[dict[str, Any]] = Field(
        description="The file's rows as published. rss/atom: one row per item {title, link, description, pub_date, "
                    "guid, categories, company_symbols}."
    )


class DeleteUserDataFeedResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    msg: str = Field(description="\"Deleted dataset <name>\".")
    dataset_id: Optional[Any] = None
    schedule_deleted: Optional[bool] = Field(None, description="False when the schedule was already gone.")
    files_deleted: Optional[int] = Field(None, description="Published files removed.")
    saved_query_deleted: Optional[Any] = Field(None, description="Id of the saved query removed with it.")
