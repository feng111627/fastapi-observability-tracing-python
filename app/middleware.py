"""可观测性 ASGI 中间件。

职责：

1. **关联 ID**：从请求头 ``X-Correlation-ID`` 沿用调用方传入的值，
   没有则生成 UUID4；写入 :mod:`contextvars` 供全链路日志使用，
   并通过响应头 ``X-Correlation-ID`` 回传给调用方。
2. **访问日志**：请求结束记录一条 JSON 日志，带关联 ID、method、path、
   状态码、耗时；未被业务处理的异常会连同完整异常链一起记录。
3. **指标**：请求开始/结束更新进行中计数，结束记录总数、错误、耗时。
4. **后台任务**：Starlette ``BackgroundTask`` 在响应发出后才执行，
   中间件会在其运行期间重新绑定关联 ID，保证后台日志不串号、不丢失。

实现为纯 ASGI 中间件（不依赖 BaseHTTPMiddleware），上下文变量在
同一个任务内设置，请求期间业务代码（含线程池依赖）都能读到。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.context import generate_correlation_id, reset_correlation_id, set_correlation_id
from app.logging_setup import redact_headers
from app.metrics import registry

logger = logging.getLogger("app.access")

#: 调用方传入关联 ID 的请求头。
CORRELATION_ID_HEADER = "x-correlation-id"
#: 回传给调用方的响应头。
CORRELATION_ID_RESPONSE_HEADER = b"x-correlation-id"


class ObservabilityMiddleware:
    """为每个 HTTP 请求注入关联 ID、记录访问日志与指标。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        correlation_id = headers.get(CORRELATION_ID_HEADER) or generate_correlation_id()
        # 同时放入 scope，业务端点需要时可直接读取
        scope["state"] = dict(scope.get("state") or {})
        scope["state"]["correlation_id"] = correlation_id

        token = set_correlation_id(correlation_id)
        registry.track_in_progress(1)
        start = time.perf_counter()

        status_code = 500
        raised: BaseException | None = None

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                # 幂等地附带关联 ID 响应头
                msg_headers = message.setdefault("headers", [])
                if not any(
                    name.lower() == CORRELATION_ID_RESPONSE_HEADER
                    for name, _ in msg_headers
                ):
                    msg_headers.append(
                        (CORRELATION_ID_RESPONSE_HEADER, correlation_id.encode())
                    )
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException as exc:  # 未被业务/ExceptionMiddleware 处理的异常
            raised = exc
            raise
        finally:
            duration_ms = (time.perf_counter() - start) * 1000.0
            registry.track_in_progress(-1)
            try:
                self._record(
                    scope, status_code, duration_ms, correlation_id, raised
                )
            finally:
                reset_correlation_id(token)

    @staticmethod
    def _record(
        scope: Scope,
        status_code: int,
        duration_ms: float,
        correlation_id: str,
        raised: BaseException | None,
    ) -> None:
        request = Request(scope)
        route = scope.get("route")
        path = getattr(route, "path", None) or request.url.path
        method = scope.get("method", "-")

        error_type: str | None = None
        if raised is not None:
            error_type = f"{type(raised).__module__}.{type(raised).__qualname__}"

        registry.record_request(
            method=method,
            path=path,
            status_code=status_code,
            duration_ms=duration_ms,
            error_type=error_type,
        )

        safe_headers = redact_headers(dict(request.headers))
        extra: dict[str, Any] = {
            "method": method,
            "path": path,
            "status_code": status_code,
            "duration_ms": round(duration_ms, 3),
            "request_headers": safe_headers,
        }

        if raised is not None:
            extra["error_type"] = error_type
            logger.error(
                "request failed with unhandled exception",
                extra=extra,
                exc_info=(type(raised), raised, raised.__traceback__),
            )
        elif status_code >= 500:
            logger.error("request failed", extra=extra)
        elif status_code >= 400:
            logger.warning("request rejected", extra=extra)
        else:
            logger.info("request completed", extra=extra)
