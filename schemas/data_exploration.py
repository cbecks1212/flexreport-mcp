"""Output schemas for tools/data_exploration.py.

Each model is a tool's return type, so FastMCP advertises it as the tool's
`outputSchema` and validates every result against it. Models use extra="allow"
so a new backend key passes through instead of failing validation. Query-result
rows have arbitrary columns, so they stay `dict[str, Any]`: only the envelope
around them is typed.
"""

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class TaskDispatch(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_id: str = Field(description="Poll this with get_task_status until SUCCESS.")
    status: str = Field(description="PENDING on dispatch.")
    queue: Optional[str] = Field(None, description="Worker queue the job was routed to.")


class SourceRelation(BaseModel):
    model_config = ConfigDict(extra="allow")

    relation: str = Field(description="Pass to trace_data_sources(relation=...).")
    traceable: Optional[str] = Field(
        None, description="\"row\" = each row maps to one document; \"cohort\" = cite only as a SET of filings."
    )
    grain: Optional[list[Any]] = Field(None, description="Columns identifying a row; pass them (plus its date) as trace keys.")
    document: Optional[str] = Field(None, description="Label of the source document type.")


class SourcesBlock(BaseModel):
    """Provenance of a result: which relations its numbers come from and how to cite them."""

    model_config = ConfigDict(extra="allow")

    mode: Optional[str] = None
    relations: list[SourceRelation] = Field(default_factory=list)
    trace_token: Optional[str] = Field(None, description="Opaque token for trace_data_sources(encrypted_query_token=...).")
    reason: Optional[str] = None


class QueryRowsResult(BaseModel):
    """Rows behind a signed query token, never the SQL."""

    model_config = ConfigDict(extra="allow")

    format: Optional[str] = Field(None, description="\"table\".")
    count: Optional[int] = Field(None, description="Rows returned in `rows`.")
    rows: Optional[list[dict[str, Any]]] = Field(
        None,
        description="Result rows (cell values are strings or null). May carry _source_* citation columns. "
                    "Null on a replay entry whose token failed (see `error`).",
    )
    sources: Optional[SourcesBlock] = Field(None, description="Provenance block; present when the numbers trace to documents.")
    row_count: Optional[int] = Field(None, description="Full row count when `truncated` (max_rows sliced the result).")
    truncated: Optional[bool] = None
    parameters: Optional[Any] = Field(None, description="Values substituted into a placeholder twin; echo them to the user.")
    defaulted_parameters: Optional[Any] = Field(None, description="Slots that kept the saved example value.")
    ignored_values: Optional[Any] = Field(None, description="Keys in `values` this token does not use.")


class SavedQueryReplay(QueryRowsResult):
    index: int = Field(description="Position in query_tokens this entry answers.")
    error: Optional[str] = Field(None, description="Set instead of rows when this token failed (others still ran).")
    detail: Optional[Any] = Field(None, description="Backend detail for `error`.")


class SavedQueryReplayResult(BaseModel):
    queries_run: int
    results: list[SavedQueryReplay] = Field(description="One entry per query token, in the same order.")


class TraceSourcesResult(BaseModel):
    """Polymorphic by relation: documents, citations (finest), or a cohort."""

    model_config = ConfigDict(extra="allow")

    mode: Optional[str] = Field(None, description="\"documents\" when resolved; \"recipe\"/\"none\" otherwise.")
    relation: Optional[str] = None
    traceable: Optional[str] = Field(None, description="\"row\", \"cohort\" or \"none\" — read before writing.")
    reason: Optional[str] = Field(None, description="Why nothing resolved, e.g. \"no_keys\" (a join column, usually the date, was missing).")
    documents: Optional[list[dict[str, Any]]] = Field(None, description="Whole source documents (SEC filings, transcripts, IR PDFs).")
    citations: Optional[list[dict[str, Any]]] = Field(None, description="Finer citations: verified quotes or transcript speaker turns.")
    cohort: Optional[list[dict[str, Any]]] = Field(None, description="The SET of filings a cohort number sums over.")
    providers: Optional[list[str]] = Field(None, description="Citation providers that answered.")
    trace_token: Optional[str] = None


class TranscriptSection(BaseModel):
    """One speaker turn; every value arrives as a string."""

    model_config = ConfigDict(extra="allow")

    symbol: Optional[str] = None
    year: Optional[str] = None
    quarter: Optional[str] = None
    call_date: Optional[str] = None
    section_no: Optional[str] = None
    speaker: Optional[str] = Field(None, description="Who said it — attribute quotes to this speaker.")
    content: Optional[str] = Field(None, description="The turn's text.")


class DocumentSectionResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    symbol: str
    year: int = Field(description="Fiscal year of the call.")
    quarter: int = Field(description="Fiscal quarter of the call.")
    n: int = Field(description="1-based section number.")
    format: Optional[str] = None
    count: Optional[int] = None
    rows: list[TranscriptSection]
    highlights: list[Any] = Field(default_factory=list, description="Quotes the platform verified against this section.")
    total_sections: Optional[int] = Field(None, description="Upper bound for n; read n-1 / n+1 for context.")
    transcript_url: Optional[str] = Field(None, description="Full call link; frequently null.")


class SaveQueryResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    msg: str
    id: int = Field(description="Saved row id — the delete_saved_query key.")
    placeholder_status: Optional[str] = Field(None, description="PENDING (twins + metadata being authored) or NOT_DISPATCHED.")
    placeholder_id_to_poll: Optional[str] = Field(None, description="Task id of the placeholder-authoring job.")


class SavedQuery(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int = Field(description="Row id for delete_saved_query.")
    user_request: Optional[str] = Field(None, description="The user's label; do not match on it.")
    queries: Optional[list[str]] = Field(None, description="Opaque tokens for an exact replay.")
    query_placeholder: Optional[list[str]] = Field(
        None, description="Placeholder-twin tokens, index-aligned with queries; null until authored."
    )
    metadata: Optional[dict[str, Any]] = Field(
        None, description="summary / parameters / entities / datasets / topics / result_schema / stale; null until authored."
    )
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SavedQueriesResult(BaseModel):
    count: int
    saved_queries: list[SavedQuery] = Field(description="The calling user's saved rows, newest first.")


class DeleteSavedQueryResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    msg: str
