from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MAX_UPLOAD_BYTES = 500_000_000
DEFAULT_PIPELINE_MAX_WORKERS = 2


@dataclass(frozen=True)
class ServiceSettings:
    database_url: str
    exchange_root: Path
    lease_seconds: int = 120
    heartbeat_seconds: int = 30
    poll_seconds: float = 2.0
    max_worker_recoveries: int = 3
    worker_processes: int = 1
    pipeline_max_workers: int = DEFAULT_PIPELINE_MAX_WORKERS
    model_profiles_path: Path | None = None
    # Upload / unpack limits for multipart POST /v1/runs.
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES  # 500 MB tarball (configurable)
    max_expanded_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GiB extracted
    max_archive_members: int = 20_000
    upload_read_block: int = 1024 * 1024  # 1 MiB streaming chunk
    progress_seconds: float = 5.0

    @property
    def package_root(self) -> Path:
        return self.exchange_root / "packages"

    @property
    def run_root(self) -> Path:
        return self.exchange_root / "runs"

    @property
    def upload_root(self) -> Path:
        return self.exchange_root / "uploads"

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

        def _env_int(name: str, default: int) -> int:
            raw = os.environ.get(name)
            if raw is None or raw == "":
                return default
            try:
                value = int(raw)
            except ValueError as error:
                raise ValueError(f"{name} must be an integer") from error
            if value <= 0:
                raise ValueError(f"{name} must be positive")
            return value

        def _env_float(name: str, default: float) -> float:
            raw = os.environ.get(name)
            if raw is None or raw == "":
                return default
            try:
                value = float(raw)
            except ValueError as error:
                raise ValueError(f"{name} must be a number") from error
            if value <= 0:
                raise ValueError(f"{name} must be positive")
            return value

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
            pipeline_max_workers=_env_int(
                "HE_SERVICE_PIPELINE_MAX_WORKERS", DEFAULT_PIPELINE_MAX_WORKERS
            ),
            model_profiles_path=Path(profiles) if profiles else None,
            max_upload_bytes=_env_int(
                "HE_SERVICE_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES
            ),
            max_expanded_bytes=_env_int(
                "HE_SERVICE_MAX_EXPANDED_BYTES", 2 * 1024 * 1024 * 1024
            ),
            max_archive_members=_env_int("HE_SERVICE_MAX_ARCHIVE_MEMBERS", 20_000),
            upload_read_block=_env_int("HE_SERVICE_UPLOAD_READ_BLOCK", 1024 * 1024),
            progress_seconds=_env_float("HE_SERVICE_PROGRESS_SECONDS", 5.0),
        )
