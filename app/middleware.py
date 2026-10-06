"""可观测性中间件：关联 ID、访问日志、指标埋点、异常链记录。

每个请求的处理流程：
1. 从入站 ``X-Correlation-ID`` 头沿用关联 ID，缺失则生成；
2. 绑定到当前 :class:`contextvars.ContextVar`（响应头原样回传，
   后台任务/异步并发自动拷贝上下文，不串号）；
3. 在 :data:`app.tracing.trace_store` 开立本条关联 ID 的时间线
   （``/traces/*`` 查询入口除外，见下），处理期间的日志事件由日志
   管道自动归档进去；开立/结束时固定写入入口/结束骨架事件；
4. 业务请求：进行中请求数 +1，记录开始时间；
5. 调用下游；抛出的未处理异常在此记录**完整异常链**与脱敏请求头，并兜底
   生成带关联 ID 的 JSON 500 响应（新版 Starlette 异常会穿透 call_next）；
6. 无论成功失败都：业务请求做指标与访问日志，随后落定时间线的
   状态码/错误类型/总耗时，最后恢复上下文。

关联 ID 绑定与响应头回传覆盖**所有入口**；时间线记录覆盖除
``/traces/*`` 查询入口外的所有入口（含 ``/healthz``、``/metrics``），
任何请求事后都能按关联 ID 查到自己带首尾记录的时间线。
``/traces/*`` 是纯读取入口：查询绝不能因为自身的关联 ID 而新建、重置
或覆盖任何已有时间线（否则「带着某 ID 去查它自己」会把该 ID 的时间线
清空），因此查询入口自身不入时间线。``/healthz``、``/metrics``、
``/traces/*`` 等平台端点仍不纳入业务指标与访问日志（口径与上一轮一致）。
"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.context import (
    CORRELATION_ID_HEADER,
    bind_correlation_id,
    get_correlation_id,
    reset_correlation_id,
)
from app.logging_setup import flatten_exception_chain, get_logger, mask_headers
from app.metrics import metrics
from app.tracing import trace_store

logger = get_logger("app.access")

# 不纳入业务指标/访问日志的平台路径（时间线仍然记录）
_SKIP_PATHS = frozenset({"/healthz", "/metrics"})


def _is_platform_path(path: str) -> bool:
    return path in _SKIP_PATHS or path.startswith("/traces")


def _is_trace_query_path(path: str) -> bool:
    """时间线查询入口：纯读取，自身不开立/落定时间线。"""
    return path.startswith("/traces")


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """关联 ID + 时间线 + 结构化访问日志 + 指标的一体化中间件。"""

    async def dispatch(self, request: Request, call_next):
        inbound = request.headers.get(CORRELATION_ID_HEADER)
        correlation_id, token = bind_correlation_id(inbound)
        start = time.perf_counter()
        # 查询入口纯读取：绝不开立/重置/覆盖任何时间线
        record_timeline = not _is_trace_query_path(request.url.path)
        if record_timeline:
            trace_store.begin(correlation_id, request.method, request.url.path)

        platform = _is_platform_path(request.url.path)
        if not platform:
            metrics.inc_inflight()
        status_code = 500
        error_type: str | None = None

        # 请求头在本中间件内只做一件事：以脱敏形式进入日志，绝不落原始值
        safe_headers = mask_headers(dict(request.headers))

        try:
            response: Response = await call_next(request)
            status_code = response.status_code
            response.headers[CORRELATION_ID_HEADER] = correlation_id
            return response
        except Exception as exc:
            error_type = f"{type(exc).__module__}.{type(exc).__qualname__}"
            # 关键：exc_info=True 让日志带上完整异常链（含 __cause__/__context__）
            logger.error(
                "unhandled exception while processing request",
                exc_info=True,
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "request_headers": safe_headers,
                },
            )
            # 新版 Starlette 中异常会穿透 call_next，由本中间件兜底，
            # 直接构造带关联 ID 的 JSON 500 响应。
            chain = flatten_exception_chain(exc)
            response = JSONResponse(
                status_code=500,
                content={
                    "error": "internal_server_error",
                    "correlation_id": get_correlation_id(),
                    "detail": chain[0]["message"] if chain else "internal error",
                },
            )
            response.headers[CORRELATION_ID_HEADER] = correlation_id
            return response
        finally:
            duration = time.perf_counter() - start
            if not platform:
                metrics.dec_inflight()
                # 路由模板（/items/{id}）优先，取不到时退回原始路径
                route = request.scope.get("route")
                route_path = getattr(route, "path", request.url.path)
                metrics.record_request(
                    method=request.method,
                    path=route_path,
                    status_code=status_code,
                    duration=duration,
                    error_type=error_type,
                )
                logger.info(
                    "request completed",
                    extra={
                        "method": request.method,
                        "path": route_path,
                        "status_code": status_code,
                        "duration_ms": round(duration * 1000, 3),
                        "error_type": error_type,
                    },
                )
            # 时间线对所有非查询入口落定汇总；访问日志/指标仍只覆盖业务入口
            if record_timeline:
                trace_store.finish(
                    correlation_id,
                    status_code=status_code,
                    error_type=error_type,
                    duration=duration,
                )
            reset_correlation_id(token)
