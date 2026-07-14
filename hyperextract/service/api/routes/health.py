from fastapi import APIRouter, Depends

from hyperextract.service.errors import ServiceError
from hyperextract.service.runtime import ServiceRuntime

from ..dependencies import get_runtime

router = APIRouter()


@router.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
def ready(runtime: ServiceRuntime = Depends(get_runtime)) -> dict:
    checks = {
        "package_root_readable": runtime.settings.package_root.is_dir(),
        "run_root_writable": runtime.settings.run_root.is_dir(),
        "repository": True,
    }
    if not all(checks.values()):
        raise ServiceError(503, "SERVICE_NOT_READY", "Service checks failed")
    return {"status": "ready", "checks": checks}
