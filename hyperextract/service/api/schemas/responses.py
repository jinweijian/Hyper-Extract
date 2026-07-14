from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PublicResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class OutputResponse(PublicResponse):
    run_uri: str
    artifacts_uri: str
    manifest_uri: str
    success_marker_uri: str


class RunLinksResponse(PublicResponse):
    self_url: str = Field(alias="self")
    cancel: str
    resume: str
    errors: str
    artifacts: str


class RunResponse(PublicResponse):
    run_id: str
    status: str
    stage: str
    stage_status: str
    attempt: int
    progress: dict[str, object]
    error_summary: dict[str, object] | None
    resumable: bool
    cancel_requested: bool
    output: OutputResponse
    links: RunLinksResponse


class ErrorEntryResponse(PublicResponse):
    """Public projection of a row in ``he_run_errors``.

    The schema deliberately excludes ``details_json`` so the API can never
    leak exception repr, request headers, provider response bodies, keys, or
    full Prompt content. Only operator-safe fields are exposed.
    """

    attempt: int
    code: str
    source: str
    message: str
    occurred_at: datetime


class RunErrorsResponse(PublicResponse):
    run_id: str
    errors: list[ErrorEntryResponse]
