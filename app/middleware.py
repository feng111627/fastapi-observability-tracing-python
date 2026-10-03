"""可观测性中间件：关联 ID、访问日志、指标埋点、异常链记录、时间线录制。

每个请求的处理流程：
1. 从入站 ``X-Correlation-ID`` 头沿用关联 ID，缺失则生成；
2. 绑定到当前 :class:`contextvars.ContextVar`（响应头原样回传，
   后台任务/异步并发自动拷贝上下文，不串号）；
3. 在事后时间线中开启一条记录（``request_start``），进行中请求数 +1，
   记录开始时间；
4. 调用下游；抛出的未处理异常在此记录**完整异常链**与脱敏请求头，并兜底
   生成带关联 ID 的 JSON 500 响应（新版 Starlette 异常会穿透 call_next）；
5. 无论成功失败都：进行中请求数 -1、按状态码/异常类型记录指标、
   冻结时间线摘要（``request_end``：状态码/耗时/错误类型）、输出一条带
   method/path/status_code/duration_ms 的结构化访问日志，最后恢复上下文。

**所有入口一视同仁**：``/healthz``、``/metrics`` 等平台端点同样绑定/回传
关联 ID 并可在事后查询到自己的时间线（本轮之前它们被整体排除）；只是它们
仍不纳入业务指标与业务访问日志，避免探针污染既有指标口径。
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

# 不纳入业务指标/业务访问日志的平台路径（但仍有完整关联 ID 与时间线）
_SKIP_METRICS_EXACT = frozenset({"/healthz", "/metrics"})
_SKIP_METRICS_PREFIXES = ("/traces",)


def _is_platform_path(path: str) -> bool:
    """平台端点：健康检查、指标与时间线查询本身。

    它们不纳入业务指标/业务访问日志（避免观测端点自我污染既有指标口径），
    但关联 ID 绑定、响应头回传与时间线录制对它们同样生效。
    """
    if path in _SKIP_METRICS_EXACT:
        return True
    return any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in _SKIP_METRICS_PREFIXES
    )


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """关联 ID + 结构化访问日志 + 指标 + 事后时间线的一体化中间件。"""

    async def dispatch(self, request: Request, call_next):
        # 注意：不再对任何入口整体提前返回。平台端点也走完关联 ID 绑定、
        # 响应头回传与时间线录制，确保任何入口的请求都能事后查到。
        skip_metrics = _is_platform_path(request.url.path)

        inbound = request.headers.get(CORRELATION_ID_HEADER)
        correlation_id, token = bind_correlation_id(inbound)
        start = time.perf_counter()
        # 时间线入口事件（全部入口都记录）
        trace_store.begin(correlation_id, request.method, request.url.path)
        if not skip_metrics:
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
            if not skip_metrics:
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
            # 时间线对所有入口冻结摘要；平台端点没有路由模板，用原始路径
            trace_store.finish(
                correlation_id,
                status_code=status_code,
                duration_ms=duration * 1000,
                error_type=error_type,
            )
            reset_correlation_id(token)
