"""FastAPI 应用工厂。

把所有可观测性组件装配到一起：
- 进程启动时配置一次结构化 JSON 日志（含 uvicorn 框架日志的统一接管）；
- 注册 :class:`~app.middleware.ObservabilityMiddleware`（关联 ID、
  访问日志、指标、异常链、事后时间线与 500 兜底都在该中间件内完成）；
- 挂载健康检查、指标导出、时间线查询与演示路由。

时间线容量可用环境变量调整（缺省为合理默认）：
- ``TRACE_MAX_TRACES``：最多保留多少条请求时间线（默认 1000）；
- ``TRACE_TTL_SECONDS``：单条时间线最长保留秒数（默认 3600）。
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI

from app.logging_setup import configure_logging, get_logger
from app.middleware import ObservabilityMiddleware
from app.routes import router
from app.tracing import trace_store

logger = get_logger("app.startup")


def _configure_trace_capacity() -> None:
    """从环境变量读取时间线容量/保留时长（缺省沿用 tracing 内默认值）。"""
    kwargs: dict[str, float] = {}
    max_traces = os.environ.get("TRACE_MAX_TRACES")
    if max_traces:
        kwargs["max_traces"] = int(max_traces)
    ttl = os.environ.get("TRACE_TTL_SECONDS")
    if ttl:
        kwargs["ttl_seconds"] = float(ttl)
    if kwargs:
        trace_store.configure(**kwargs)  # type: ignore[arg-type]


def create_app() -> FastAPI:
    """创建并返回配置完整的 FastAPI 应用实例。"""
    configure_logging(logging.INFO)
    _configure_trace_capacity()

    app = FastAPI(
        title="fastapi-observability-tracing-python",
        version="0.2.0",
        description=(
            "本地可观测性（关联 ID / 结构化日志 / 指标 / 事后可回溯时间线）"
            "演示服务"
        ),
    )
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(router)

    logger.info(
        "application configured",
        extra={"routes": len(app.routes), "trace_store": trace_store.stats()},
    )
    return app


app = create_app()
