from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ServiceSettings:
    database_url: str
    exchange_root: Path
    lease_seconds: int = 120
    heartbeat_seconds: int = 30
    poll_seconds: float = 2.0
    max_worker_recoveries: int = 3
    worker_processes: int = 1
    model_profiles_path: Path | None = None

    @property
    def package_root(self) -> Path:
        return self.exchange_root / "packages"

    @property
    def run_root(self) -> Path:
        return self.exchange_root / "runs"

    @classmethod
    def from_env(cls) -> ServiceSettings:
        root = Path(os.environ.get("HE_SERVICE_EXCHANGE_ROOT", "/exchange"))
        if not root.is_absolute():
            raise ValueError("HE_SERVICE_EXCHANGE_ROOT must be absolute")
        profiles = os.environ.get("HE_SERVICE_MODEL_PROFILES")
        worker_processes = int(os.environ.get("HE_SERVICE_WORKER_PROCESSES", "1"))
        if worker_processes != 1:
            raise ValueError(
                "HE_SERVICE_WORKER_PROCESSES must be 1 until distributed "
                "rate-limit-group coordination is configured"
            )
        return cls(
            database_url=os.environ["HE_SERVICE_DATABASE_URL"],
            exchange_root=root,
            lease_seconds=int(os.environ.get("HE_SERVICE_LEASE_SECONDS", "120")),
            heartbeat_seconds=int(os.environ.get("HE_SERVICE_HEARTBEAT_SECONDS", "30")),
            poll_seconds=float(os.environ.get("HE_SERVICE_POLL_SECONDS", "2")),
            max_worker_recoveries=int(
                os.environ.get("HE_SERVICE_MAX_WORKER_RECOVERIES", "3")
            ),
            worker_processes=worker_processes,
            model_profiles_path=Path(profiles) if profiles else None,
        )
