from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from hyperextract.service.errors import ServiceError
from hyperextract.service.runtime import ServiceRuntime, create_runtime

from .routes import contracts, health, runs


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def service_error_handler(_request, error: ServiceError):
        return JSONResponse(
            status_code=error.status_code,
            content=error.body(),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, _error: RequestValidationError
    ):
        if request.method == "POST" and request.url.path == "/v1/runs":
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "INVALID_MULTIPART_REQUEST",
                        "message": "multipart request fields are invalid or missing",
                        "details": [],
                    }
                },
            )
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "REQUEST_VALIDATION_FAILED",
                    "message": "request validation failed",
                    "details": [],
                }
            },
        )


def create_app(runtime: ServiceRuntime | None = None) -> FastAPI:
    resolved_runtime = runtime or create_runtime()
    owns_runtime = runtime is None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        resolved_runtime.prepare()
        yield
        if owns_runtime:
            resolved_runtime.close()

    app = FastAPI(
        title="Hyper-Extract Internal Service",
        version="1.0",
        lifespan=lifespan,
    )
    app.state.runtime = resolved_runtime
    app.include_router(health.router)
    app.include_router(contracts.router)
    app.include_router(runs.router)
    register_exception_handlers(app)
    return app
