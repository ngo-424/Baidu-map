from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from fastapi.responses import JSONResponse

from .config import Settings, load_settings
from .analysis import router
from .contracts import AnalysisResponse, Issue


class HealthResponse(BaseModel):
    status: str
    baidu_ak_configured: bool


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else load_settings()
    app = FastAPI(title="Life Circle Backend", version="0.1.0", debug=False)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    app.include_router(router)

    def failure_response(status_code: int, code: str, message: str):
        body = AnalysisResponse(status="failed", source="system", errors=[
            Issue(code=code, message=message, scope="request", severity="error")
        ])
        return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Never echo raw input, URLs or credentials from a validation exception.
        return failure_response(422, "INVALID_REQUEST", "请求字段无效，请核对坐标、坐标系、场景和参数。")

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return failure_response(exc.status_code, "HTTP_ERROR", "请求路径或方法不可用。")

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        return failure_response(500, "INTERNAL_ERROR", "分析暂不可用，请稍后重试。")

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok", baidu_ak_configured=config.ak_configured)

    return app


app = create_app()
