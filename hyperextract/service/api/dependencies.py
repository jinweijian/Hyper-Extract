from fastapi import Request

from hyperextract.service.runtime import ServiceRuntime


def get_runtime(request: Request) -> ServiceRuntime:
    return request.app.state.runtime
