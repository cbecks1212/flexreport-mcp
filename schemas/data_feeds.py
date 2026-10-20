"""Output schemas for tools/data_feeds.py.

Each model is a tool's return type, so FastMCP advertises it as the tool's
`outputSchema`. Feed rows are passed through as plain dicts: each dataset has its
own columns (documented on the tool) and new backend columns must not fail
validation.
"""

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


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
