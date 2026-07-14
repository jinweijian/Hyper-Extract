from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunInput(StrictModel):
    type: Literal["document_package"]
    contract_version: Literal["1.0"]
    package_uri: str
    package_format: Literal["directory"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RunBudget(StrictModel):
    max_model_calls: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)


class RunExecution(StrictModel):
    model_profile: str = "minimax-course-default"
    context_policy: Literal["auto", "preserve", "repack"] = "auto"
    priority: Literal["normal", "low"] = "normal"
    budget: RunBudget = Field(default_factory=RunBudget)


class ProfileSelection(StrictModel):
    name: Literal["course_knowledge_graph"]
    version: Literal["1"]


class PipelineSelection(StrictModel):
    name: Literal["course_graph"]
    profile: ProfileSelection


class ClientContext(StrictModel):
    service: str | None = Field(default=None, max_length=128)
    task_id: str | None = Field(default=None, max_length=128)
    course_id: str | None = Field(default=None, max_length=128)


class RunCreateRequest(StrictModel):
    input: RunInput
    pipeline: PipelineSelection
    execution: RunExecution = Field(default_factory=RunExecution)
    client_context: ClientContext = Field(default_factory=ClientContext)


class ValidatePackageRequest(StrictModel):
    contract_version: Literal["1.0"]
    package_uri: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
