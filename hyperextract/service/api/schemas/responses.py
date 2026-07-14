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
