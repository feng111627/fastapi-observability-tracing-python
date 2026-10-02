"""FastAPI 应用组装入口。"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app import logging_setup
from app.middleware import ObservabilityMiddleware
from app.routes import router


def create_app() -> FastAPI:
    """构造并返回 FastAPI 应用实例。"""
    logging_setup.setup_logging(level=logging.INFO)
    app = FastAPI(
        title="fastapi-observability-tracing-python",
        description="本地可观测性（关联 ID / 结构化日志 / 指标 / 健康检查）示例",
    )
    # 纯 ASGI 中间件，最外层执行
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(router)
    return app


app = create_app()
