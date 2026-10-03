"""FastAPI 应用工厂。

把所有可观测性组件装配到一起：
- 进程启动时配置一次结构化 JSON 日志；
- 注册 :class:`~app.middleware.ObservabilityMiddleware`（关联 ID、
  访问日志、指标、异常链与 500 兜底都在该中间件内完成）；
- 挂载健康检查、指标导出与演示路由。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.logging_setup import configure_logging, get_logger, unify_uvicorn_logging
from app.middleware import ObservabilityMiddleware
from app.routes import router

logger = get_logger("app.startup")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # uvicorn 在启动时才应用自己的 logging 配置（明文 handler），
    # 这里在其之后把服务器日志统一并入 JSON 管道。
    unify_uvicorn_logging()
    yield


def create_app() -> FastAPI:
    """创建并返回配置完整的 FastAPI 应用实例。"""
    configure_logging(logging.INFO)

    app = FastAPI(
        title="fastapi-observability-tracing-python",
        version="0.1.0",
        description="本地可观测性（关联 ID / 结构化日志 / 指标 / 事后时间线）演示服务",
        lifespan=_lifespan,
    )
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(router)

    logger.info("application configured", extra={"routes": len(app.routes)})
    return app


app = create_app()
