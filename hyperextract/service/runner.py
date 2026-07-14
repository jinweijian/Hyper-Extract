from __future__ import annotations

from pathlib import Path

from hyperextract.documents.course_pipeline import (
    PipelineControl,
    PipelineOptions,
    run_course_document,
)
from hyperextract.methods.rag.course_knowledge_graph import CourseKnowledgeGraph
from hyperextract.utils.client import create_embedder, create_llm

from .model_profiles import ModelProfileRegistry


class CourseRunExecutor:
    def __init__(
        self, settings, repository, registry: ModelProfileRegistry | None = None
    ):
        self.settings = settings
        self.repository = repository
        self.registry = registry or ModelProfileRegistry(settings.model_profiles_path)

    def pipeline_options(self, record) -> PipelineOptions:
        return PipelineOptions(
            max_workers=2,
            retry_attempts=4,
            heartbeat_interval=self.settings.heartbeat_seconds,
            build_index=False,
            community_reports=False,
        )

    def execute(self, record) -> dict[str, object]:
        request = record.request_json
        execution = request.get("execution") or {}
        profile = self.registry.resolve_runtime(
            str(execution.get("model_profile", "minimax-course-default"))
        )
        llm_kwargs = {"timeout": profile.request_timeout, "max_retries": 0}
        if profile.max_tokens is not None:
            llm_kwargs["max_tokens"] = profile.max_tokens
        llm = create_llm(profile.llm, api_key=profile.llm_api_key, **llm_kwargs)
        embedder = create_embedder(
            profile.embedder,
            api_key=profile.embedder_api_key,
        )
        graph = CourseKnowledgeGraph(
            llm,
            embedder,
            max_workers=1,
            structured_output_mode=profile.structured_output_mode,
            output_repair_attempts=profile.output_repair_attempts,
        )
        package = Path(str(request["resolved_package_path"]))
        work = self.settings.run_root / record.run_id / "work"

        def event_sink(event):
            self.repository.update_progress(
                record.run_id,
                stage=event.stage,
                progress={
                    "status": event.status,
                    "message": event.message,
                    "current": event.current,
                    "total": event.total,
                    "chunk_id": event.chunk_id,
                    "details": event.details,
                },
            )

        return run_course_document(
            package,
            work,
            graph,
            options=self.pipeline_options(record),
            input_format="document-package",
            resume=True,
            force=False,
            control=PipelineControl(
                run_id=record.run_id,
                event_sink=event_sink,
                should_cancel=lambda: bool(
                    self.repository.get(record.run_id).cancel_requested
                ),
            ),
        )
