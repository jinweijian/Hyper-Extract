from dataclasses import dataclass

from sqlalchemy.engine import Engine

from hyperextract.service.db import create_engine_and_session
from hyperextract.service.model_profiles import ModelProfileRegistry
from hyperextract.service.repository import RunRepository
from hyperextract.service.settings import ServiceSettings
from hyperextract.service.storage import SharedVolumeStore


@dataclass
class ServiceRuntime:
    settings: ServiceSettings
    repository: RunRepository
    storage: SharedVolumeStore
    model_profiles: ModelProfileRegistry
    owned_engine: Engine | None = None

    def prepare(self) -> None:
        for root in (
            self.settings.upload_root,
            self.settings.package_root,
            self.settings.run_root,
        ):
            root.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        if self.owned_engine is not None:
            self.owned_engine.dispose()


def create_runtime(
    settings: ServiceSettings | None = None,
    repository: RunRepository | None = None,
    model_profiles: ModelProfileRegistry | None = None,
) -> ServiceRuntime:
    resolved = settings or ServiceSettings.from_env()
    owned_engine = None
    if repository is None:
        owned_engine, session_factory = create_engine_and_session(resolved.database_url)
        repository = RunRepository(session_factory)
    return ServiceRuntime(
        settings=resolved,
        repository=repository,
        storage=SharedVolumeStore(resolved.exchange_root),
        model_profiles=model_profiles
        or ModelProfileRegistry(resolved.model_profiles_path),
        owned_engine=owned_engine,
    )
