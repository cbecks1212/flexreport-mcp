"""Output schemas for tools/event_subscriptions.py.

The two stream tools build their result locally in `_read_sse` (a bounded SSE read),
so `StreamReadResult` models that envelope; each `events` item is the backend's
`Event` frame (src/models/subscription_events.py). Payloads differ per event_type, so
`payload` stays an open object. Models carrying backend data use extra="allow".
"""

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class StreamEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: Optional[str] = Field(None, description="Stream cursor of this event.")
    event_type: Optional[str] = Field(None, description="Realtime event type, or \"report_plan\" for a saved report plan.")
    symbol: Optional[str] = Field(None, description="Ticker (13f_* filer events carry the filer's CIK).")
    payload: Optional[dict[str, Any]] = Field(
        None, description="The same object list_realtime_events returns for that type; report_plan frames "
                          "carry {symbol, event_type, planned_at, queued_at, plan_key}."
    )
    published_at: Optional[str] = None


class StreamReadResult(BaseModel):
    events: list[StreamEvent] = Field(
        description="Matched events in publish order; empty on a quiet tape. A frame whose data was not "
                    "JSON comes through as {\"raw\": <text>}."
    )
    cursor: Optional[str] = Field(
        None, description="Last stream id seen (advances on heartbeats too). Pass it back on the next call; "
                          "null only when no frame carried an id and none was passed in."
    )
    closed: Optional[str] = Field(None, description="Set when the server ended the stream (e.g. \"subscription deleted\"); do not reconnect.")
    elapsed_seconds: float = Field(description="Wall time this read held the stream open.")


class EventFilter(BaseModel):
    model_config = ConfigDict(extra="allow")

    topics: Optional[list[str]] = Field(None, description="Normalised topics (lower-cased, de-duplicated); null = every topic.")
    symbols: Optional[list[str]] = Field(None, description="Normalised symbols (upper-cased, de-duplicated); null = every symbol.")


class RegisterSubscriptionResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    subscription_id: str = Field(description="Keep it: stream_event_subscription and delete_event_subscription take it.")
    filter: EventFilter = Field(description="The normalised filter that was saved.")
    cursor: Optional[str] = Field(None, description="Stream position the subscription starts from (events after registration).")
    created_at: Optional[str] = None


class DeleteSubscriptionResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str = Field(description="\"DELETED\".")
    subscription_id: str
