from __future__ import annotations

from pathlib import Path

from hyperextract.documents.checkpoint import fingerprint
from hyperextract.documents.course_pipeline import (
    PipelineControl,
    PipelineOptions,
    run_course_document,
)
from hyperextract.methods.rag.course_knowledge_graph import CourseKnowledgeGraph
from hyperextract.providers.artifacts import ModelArtifactStore
from hyperextract.providers.contracts import ProfileConfigurationError
from hyperextract.providers.gateway import ModelExecutionGateway
from hyperextract.providers.langchain import AdapterEmbeddings
from hyperextract.providers.probe import ensure_probe_eligibility
from hyperextract.providers.scheduling import (
    PROCESS_CIRCUIT_BREAKERS,
    PROCESS_SCHEDULERS,
)
from hyperextract.utils.client import create_llm

from .model_profiles import ModelProfileRegistry


class CourseRunExecutor:
    def __init__(
        self, settings, repository, registry: ModelProfileRegistry | None = None
    ):
        self.settings = settings
        self.repository = repository
        self.registry = registry or ModelProfileRegistry(settings.model_profiles_path)

    def pipeline_options(self, record, *, recovery=None) -> PipelineOptions:
        return PipelineOptions(
            max_workers=2,
            retry_attempts=(
                recovery.transient_retry_attempts + 1 if recovery is not None else 4
            ),
            recovery=recovery,
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
        expected_profile_fingerprint = (request.get("resolved_config") or {}).get(
            "model_profile_fingerprint"
        )
        actual_profile_fingerprint = profile.profile.public_fingerprint()
        if (
            expected_profile_fingerprint
            and expected_profile_fingerprint != actual_profile_fingerprint
        ):
            raise ProfileConfigurationError(
                "The model profile changed after the run was accepted; submit a new "
                "run with the current profile configuration",
                code="MODEL_PROFILE_FINGERPRINT_MISMATCH",
            )
        probe_result = ensure_probe_eligibility(profile.profile)
        package = Path(str(request["resolved_package_path"]))
        work = self.settings.run_root / record.run_id / "work"
        artifact_store = ModelArtifactStore(work / ".he-run")
        llm_kwargs = {"timeout": profile.request_timeout, "max_retries": 0}
        if profile.max_tokens is not None:
            llm_kwargs["max_tokens"] = profile.max_tokens
        llm = create_llm(profile.llm, api_key=profile.llm_api_key, **llm_kwargs)
        options = self.pipeline_options(record, recovery=profile.profile.recovery)
        embedding_capabilities = profile.profile.embedding_capabilities
        if embedding_capabilities is None:
            raise ProfileConfigurationError(
                f"Profile {profile.name!r} does not configure an embedder",
                code="EMBEDDER_MISSING",
            )
        embedding_scheduler = PROCESS_SCHEDULERS.get(
            profile.profile.embedder_rate_limit_group or f"{profile.name}:embedding",
            max_concurrency=options.max_workers,
            recommended_concurrency=(embedding_capabilities.recommended_concurrency),
            requests_per_minute=embedding_capabilities.requests_per_minute,
            tokens_per_minute=embedding_capabilities.tokens_per_minute,
        )
        embedding_adapter = self.registry.providers.create_embedding_adapter(
            profile.name,
            scheduler=embedding_scheduler,
            api_key=profile.embedder_api_key,
        )
        embedder = AdapterEmbeddings(
            embedding_adapter,
            response_sink=artifact_store.save_embedding_response,
        )
        adapter = self.registry.providers.create_generation_adapter(
            profile.name,
            api_key=profile.llm_api_key,
        )
        capabilities = profile.profile.capabilities
        rate_limit_group = profile.profile.llm_rate_limit_group or profile.name
        scheduler = PROCESS_SCHEDULERS.get(
            rate_limit_group,
            max_concurrency=options.max_workers,
            recommended_concurrency=capabilities.recommended_concurrency,
            requests_per_minute=capabilities.requests_per_minute,
            tokens_per_minute=capabilities.tokens_per_minute,
        )
        generation_gateway = ModelExecutionGateway(
            adapter,
            profile.profile,
            scheduler=scheduler,
            circuit_breaker=PROCESS_CIRCUIT_BREAKERS.get(rate_limit_group),
            event_sink=artifact_store.save_gateway_event,
        )
        graph = CourseKnowledgeGraph(
            llm,
            embedder,
            max_workers=1,
            structured_output_mode=profile.structured_output_mode,
            output_repair_attempts=profile.output_repair_attempts,
            validation_retry_attempts=(
                profile.profile.recovery.validation_retry_attempts
            ),
            invalid_item_policy=profile.profile.recovery.invalid_list_item_policy,
            invalid_item_ratio_threshold=(
                profile.profile.recovery.invalid_item_ratio_threshold
            ),
            generation_gateway=generation_gateway,
        )
        set_embedding_usage_sink = getattr(embedding_adapter, "set_usage_sink", None)
        usage_tracker = getattr(graph, "usage_tracker", None)
        if callable(set_embedding_usage_sink) and usage_tracker is not None:
            set_embedding_usage_sink(usage_tracker.record_embedding_event)
        graph.model_profile_fingerprint = actual_profile_fingerprint
        graph.model_fingerprint = fingerprint(
            {
                "profile_fingerprint": actual_profile_fingerprint,
                "model": profile.profile.llm,
                "transport": profile.profile.transport,
            }
        )
        graph.capability_fingerprint = actual_profile_fingerprint
        graph.adapter_name = profile.profile.transport
        graph.adapter_version = "1"
        graph.probe_evidence = (
            probe_result.model_dump(mode="json") if probe_result is not None else None
        )

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
            options=options,
            input_format="document-package",
            resume=True,
            force=False,
            control=PipelineControl(
                run_id=record.run_id,
                event_sink=event_sink,
                should_cancel=should_cancel,
            ),
        )
