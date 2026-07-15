from __future__ import annotations

import logging
import threading

from hyperextract.documents.checkpoint import fingerprint
from hyperextract.documents import document_package_fingerprint
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

from . import progress, timeline
from .model_profiles import ModelProfileRegistry
from .storage import SharedVolumeStore

logger = logging.getLogger(__name__)


class CourseRunExecutor:
    def __init__(
        self, settings, repository, registry: ModelProfileRegistry | None = None
    ):
        self.settings = settings
        self.repository = repository
        self.registry = registry or ModelProfileRegistry(settings.model_profiles_path)

    def pipeline_options(self, record, *, recovery=None) -> PipelineOptions:
        return PipelineOptions(
            max_workers=self.settings.pipeline_max_workers,
            retry_attempts=(
                recovery.transient_retry_attempts + 1 if recovery is not None else 4
            ),
            recovery=recovery,
            heartbeat_interval=self.settings.heartbeat_seconds,
            build_index=False,
            community_reports=False,
        )

    def execute(self, record, *, lease_lost=None) -> dict[str, object]:
        timeline_path = (
            self.settings.run_root / record.run_id / "state" / "timeline.json"
        )
        timeline_state = [
            timeline.prepare_timeline(
                timeline.read_timeline(timeline_path),
                run_id=record.run_id,
                worker_id=record.lease_owner,
                attempt=getattr(record, "attempt", 1),
            )
        ]
        try:
            timeline.write_timeline(timeline_path, timeline_state[0])
        except OSError as error:
            logger.warning("Failed to initialize timeline: %s", error)

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
        package_ref = request.get("resolved_package_ref")
        if not package_ref or not isinstance(package_ref, str):
            raise ProfileConfigurationError(
                "Run request is missing a resolved package reference",
                code="PACKAGE_REF_MISSING",
            )
        store = SharedVolumeStore(self.settings.exchange_root)
        try:
            package = store.resolve_package_ref(package_ref)
        except ValueError as error:
            raise ProfileConfigurationError(
                f"Referenced package could not be resolved: {error}",
                code="PACKAGE_REF_INVALID",
            ) from error
        try:
            actual_package_ref = document_package_fingerprint(package)
        except ValueError as error:
            raise ProfileConfigurationError(
                f"Referenced package failed validation: {error}",
                code="DOCUMENT_PACKAGE_INVALID",
            ) from error
        if actual_package_ref != package_ref:
            raise ProfileConfigurationError(
                "Referenced package fingerprint no longer matches its content",
                code="DOCUMENT_PACKAGE_HASH_MISMATCH",
            )
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

        sequence_counter = [0]
        last_stage = [None]
        progress_state = {
            "stage": None,
            "activity": None,
            "current": None,
            "total": None,
        }
        progress_lock = threading.Lock()
        progress_stop = threading.Event()
        progress_seconds = getattr(self.settings, "progress_seconds", 5.0)
        progress_path = (
            self.settings.run_root / record.run_id / "state" / "progress.json"
        )

        def write_progress(*, stage, activity, message, current, total):
            with progress_lock:
                snapshot = progress.build_snapshot(
                    run_id=record.run_id,
                    attempt=record.attempt,
                    worker_id=record.lease_owner,
                    sequence=sequence_counter[0],
                    stage=stage,
                    activity=activity,
                    message=message,
                    current=current,
                    total=total,
                )
                sequence_counter[0] += 1
                progress_state.update(
                    stage=stage,
                    activity=activity,
                    current=current,
                    total=total,
                )
                try:
                    progress.write_snapshot(progress_path, snapshot)
                except OSError as error:
                    logger.warning("Failed to write progress snapshot: %s", error)

        def event_sink(event):
            activity = progress.activity_for_event(
                event.stage,
                event.status,
                recovering=False,
            )
            # ``run/failed`` is a terminal audit event, not one of the fixed
            # public activities. Keep the last real activity in the current
            # snapshot; the lifecycle reducer below marks it failed.
            if event.stage == "run" and event.status == "failed":
                with progress_lock:
                    activity = progress_state["activity"] or activity
                    display_stage = progress_state["stage"] or event.stage
            else:
                display_stage = event.stage
            write_progress(
                stage=display_stage,
                activity=activity,
                message=event.message or None,
                current=event.current,
                total=event.total,
            )
            if event.status in {"started", "completed", "failed", "skipped"}:
                lifecycle_activity = timeline.PIPELINE_STAGE_ACTIVITY.get(event.stage)
                if event.stage == "run" and event.status == "failed":
                    lifecycle_activity = None
                with progress_lock:
                    timeline_state[0] = timeline.transition(
                        timeline_state[0],
                        activity=lifecycle_activity,
                        status=event.status,
                        message=event.message or "",
                        current=event.current,
                        total=event.total,
                        timestamp=getattr(event, "timestamp", None),
                    )
                    try:
                        timeline.write_timeline(timeline_path, timeline_state[0])
                    except OSError as error:
                        logger.warning("Failed to write timeline: %s", error)
            # Low-frequency DB update: only when the high-level stage changes,
            # so PostgreSQL is not written on every chunk event.
            if event.stage != last_stage[0]:
                last_stage[0] = event.stage
                try:
                    self.repository.update_progress(
                        record.run_id,
                        record.lease_owner,
                        stage=event.stage,
                        progress={},
                    )
                except Exception:
                    pass

        def progress_ticker():
            while not progress_stop.wait(progress_seconds):
                if lease_lost is not None and lease_lost.is_set():
                    return
                with progress_lock:
                    stage = progress_state["stage"]
                    activity = progress_state["activity"]
                    current = progress_state["current"]
                    total = progress_state["total"]
                if not stage or not activity:
                    continue
                write_progress(
                    stage=stage,
                    activity=activity,
                    message=progress.rotating_message(activity, sequence_counter[0]),
                    current=current,
                    total=total,
                )

        def should_cancel():
            if lease_lost is not None and lease_lost.is_set():
                return True
            return bool(self.repository.get(record.run_id).cancel_requested)

        ticker = threading.Thread(
            target=progress_ticker,
            name=f"he-progress-{record.run_id}",
            daemon=True,
        )
        ticker.start()
        try:
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
        finally:
            progress_stop.set()
            ticker.join(timeout=max(1.0, progress_seconds + 1.0))
