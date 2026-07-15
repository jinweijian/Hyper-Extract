from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PublicResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class RunLinksResponse(PublicResponse):
    self_url: str = Field(alias="self")
    result: str
    result_metadata: str
    artifacts: str
    errors: str
    cancel: str | None = None
    resume: str | None = None


class ProgressResponse(PublicResponse):
    current: int | None = None
    total: int | None = None
    percent: float | None = None


class TimelineStepResponse(PublicResponse):
    activity: Literal[
        "DOCUMENT_INGESTING",
        "CHUNK_PLANNING",
        "EXTRACTING_CHUNK",
        "DEDUPLICATING",
        "BUILDING_GLOBAL_EDGES",
        "QUALITY_CHECKING",
        "BUILDING_COMMUNITIES",
        "FINALIZING",
        "ARTIFACT_PUBLISHING",
    ]
    label: str
    status: Literal["pending", "running", "completed", "failed", "skipped"]
    message: str = ""
    message_seq: int = 0
    progress: ProgressResponse | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    attempt: int | None = None


class RunResponse(PublicResponse):
    run_id: str
    status: str
    stage: str
    stage_status: str
    attempt: int
    activity: str | None = None
    message: str | None = None
    message_seq: int = 0
    progress: ProgressResponse | None = None
    timeline_schema_version: Literal["1.0"]
    timeline: list[TimelineStepResponse] = Field(min_length=9, max_length=9)
    error_summary: dict[str, object] | None = None
    resumable: bool
    cancel_requested: bool
    updated_at: datetime | None = None
    links: RunLinksResponse


class ResultProfileResponse(PublicResponse):
    name: str
    version: str
    content_hash: str
    prompt_hash: str


class ResultExtractionBriefResponse(PublicResponse):
    id: str
    version: str
    content_hash: str


class ResultArtifactResponse(PublicResponse):
    media_type: Literal["application/json"]
    schema_name: Literal["HyperExtractCourseGraph"]
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ResultPerformanceResponse(PublicResponse):
    elapsed_seconds: float = Field(ge=0)
    chunk_count: int = Field(ge=0)


class ResultRelationDistributionResponse(PublicResponse):
    prerequisite: int = Field(ge=0)
    derivative: int = Field(ge=0)
    related: int = Field(ge=0)
    confusable: int = Field(ge=0)


class ResultQualityResponse(PublicResponse):
    outline_sections: int = Field(ge=0)
    extractable_sections: int = Field(ge=0)
    covered_sections: int = Field(ge=0)
    directly_covered_sections: int = Field(ge=0)
    hierarchically_covered_sections: int = Field(ge=0)
    outline_coverage: float = Field(ge=0, le=1)
    uncovered_section_ids: list[str]
    knowledge_points: int = Field(ge=0)
    relations: int = Field(ge=0)
    relation_distribution: ResultRelationDistributionResponse
    dangling_edge_count: int = Field(ge=0)
    passed: bool


class ResultMetadataResponse(PublicResponse):
    schema_name: Literal["HyperExtractResultMetadata"] = "HyperExtractResultMetadata"
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    completed_at: datetime
    profile: ResultProfileResponse
    extraction_brief: ResultExtractionBriefResponse | None
    artifact: ResultArtifactResponse
    performance: ResultPerformanceResponse
    quality: ResultQualityResponse


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
