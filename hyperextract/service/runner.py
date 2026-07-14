from __future__ import annotations

from pathlib import Path
import os

from hyperextract.documents.course_pipeline import (
    PipelineControl,
    PipelineOptions,
    run_course_document,
)
from hyperextract.methods.rag.course_knowledge_graph import CourseKnowledgeGraph
from hyperextract.utils.client import create_embedder, create_llm
from hyperextract.providers.gateway import ModelExecutionGateway
from hyperextract.providers.scheduling import PROCESS_SCHEDULERS, CircuitBreaker
from hyperextract.providers.probe import ensure_probe_eligibility

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

    def execute(self, record, *, lease_lost=None) -> dict[str, object]:
        request = record.request_json
        execution = request.get("execution") or {}
        profile = self.registry.resolve_runtime(
            str(execution.get("model_profile", "openai-compatible-default"))
        )
        probe_result = ensure_probe_eligibility(profile.profile)
        llm_kwargs = {"timeout": profile.request_timeout, "max_retries": 0}
        if profile.max_tokens is not None:
            llm_kwargs["max_tokens"] = profile.max_tokens
        llm = create_llm(profile.llm, api_key=profile.llm_api_key, **llm_kwargs)
        embedder = create_embedder(
            profile.embedder,
            api_key=profile.embedder_api_key,
        )
        generation_gateway = None
        if os.environ.get("HYPER_EXTRACT_PROVIDER_GATEWAY", "").strip() == "1":
            adapter = self.registry.providers.create_generation_adapter(profile.name)
            capabilities = profile.profile.capabilities
            scheduler = PROCESS_SCHEDULERS.get(
                profile.profile.llm_rate_limit_group or profile.name,
                max_concurrency=self.pipeline_options(record).max_workers,
                recommended_concurrency=capabilities.recommended_concurrency,
                requests_per_minute=capabilities.requests_per_minute,
                tokens_per_minute=capabilities.tokens_per_minute,
            )
            generation_gateway = ModelExecutionGateway(
                adapter,
                profile.profile,
                scheduler=scheduler,
                circuit_breaker=CircuitBreaker(),
            )
        graph = CourseKnowledgeGraph(
            llm,
            embedder,
            max_workers=1,
            structured_output_mode=profile.structured_output_mode,
            output_repair_attempts=profile.output_repair_attempts,
            generation_gateway=generation_gateway,
        )
        graph.model_profile_fingerprint = profile.profile.public_fingerprint()
        graph.capability_fingerprint = profile.profile.public_fingerprint()
        graph.adapter_name = profile.profile.transport
        graph.adapter_version = "1"
        graph.probe_evidence = (
            probe_result.model_dump(mode="json") if probe_result is not None else None
        )
        package = Path(str(request["resolved_package_path"]))
        work = self.settings.run_root / record.run_id / "work"

        def event_sink(event):
            self.repository.update_progress(
                record.run_id,
                record.lease_owner,
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

        def should_cancel():
            if lease_lost is not None and lease_lost.is_set():
                return True
            return bool(self.repository.get(record.run_id).cancel_requested)

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
                should_cancel=should_cancel,
            ),
        )
