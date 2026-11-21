"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api.routes import router
from .config import Settings
from .db import Database
from .service import ServiceError, recover_on_startup


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = Database(settings.db_path)
        database.init()
        recover_on_startup(database)
        app.state.db = database
        app.state.settings = settings
        yield

    app = FastAPI(
        title="采购验收分批核对 API",
        version="1.0.0",
        description=(
            "按装箱单与收货点扫描记录核对实收情况。\n\n"
            "## 演示要点\n"
            "1. `POST /batches` 登记装箱单。\n"
            "2. `POST /batches/{batch_id}/scans` 上报扫描：整箱需封签完好，"
            "单件直接给序列号。\n"
            "3. 同一件在**同一收货点**重复扫描只算一次；由**多个收货点**上报则进入冲突清单。\n"
            "4. `POST /batches/{batch_id}/packing-lists/corrections` 更正单件所属的箱，"
            "以新版本重算；重算期间查询仍返回原版本结果。\n"
            "5. `GET /batches/{batch_id}/result` 查询已保存的核对结果（实收、冲突、"
            "无凭证、扫描明细）。"
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    @app.exception_handler(ServiceError)
    async def service_error_handler(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    app.include_router(router)

    @app.get("/", include_in_schema=False)
    def root():
        return {"service": "采购验收分批核对 API", "docs": "/docs"}

    return app


app = create_app()
