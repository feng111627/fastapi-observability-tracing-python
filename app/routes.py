"""演示路由：正常、异常链、后台任务、健康检查、指标导出。

这些端点用于展示和验证可观测性能力，本身不依赖任何外部服务。
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

from app.context import CORRELATION_ID_HEADER, get_correlation_id
from app.logging_setup import get_logger
from app.metrics import metrics
from app.tracing import trace_store

router = APIRouter()
logger = get_logger("app.routes")


@router.get("/traces/{correlation_id}", tags=["observability"])
async def get_trace(correlation_id: str) -> JSONResponse:
    """按关联 ID 取回该请求的事后时间线（纯读取，不改变任何记录）。

    命中时返回 ``found: true`` 及完整时间线（开始/结束/总耗时/最终状态码/
    错误类型 + 含固定首/尾记录的有序事件列表）。未命中返回 HTTP 200 +
    ``found: false`` 并区分两种原因，均不作为服务错误：
    ``reason=unknown`` 表示该 ID 从未到达过本服务；
    ``reason=evicted`` 表示到达过、但已因容量超限或保留时长过期被淘汰
    （``evicted_by`` 进一步标明是 ``capacity`` 还是 ``ttl_expired``）。
    """
    trace, miss_reason, evicted_by = trace_store.lookup(correlation_id)
    if trace is None:
        body: dict[str, object] = {
            "found": False,
            "correlation_id": correlation_id,
            "reason": miss_reason,
        }
        if evicted_by is not None:
            body["evicted_by"] = evicted_by
        return JSONResponse(body)
    return JSONResponse({"found": True, **trace})


@router.get("/healthz", tags=["observability"])
async def healthz() -> dict[str, str]:
    """存活探针：本地进程能响应即健康，不依赖任何下游。"""
    return {"status": "ok"}


@router.get("/metrics", tags=["observability"])
async def metrics_snapshot() -> JSONResponse:
    """以 JSON 导出请求总数、错误数、耗时分布、进行中请求数。"""
    return JSONResponse(metrics.snapshot())


@router.get("/", tags=["demo"])
async def root() -> dict[str, str]:
    """正常请求示例。"""
    logger.info("handling root request")
    return {"hello": "world"}


@router.get("/echo-correlation-id", tags=["demo"])
async def echo_correlation_id(request: Request) -> dict[str, str]:
    """回显当前上下文里的关联 ID（同时响应头也带 X-Correlation-ID）。"""
    return {
        "correlation_id": get_correlation_id(),
        "header_echo": request.headers.get(CORRELATION_ID_HEADER, ""),
    }


@router.get("/items/{item_id}", tags=["demo"])
async def get_item(item_id: str) -> dict[str, str]:
    """路径参数路由示例，用于验证指标按路由模板聚合。"""
    logger.info("fetching item", extra={"item_id": item_id})
    return {"item_id": item_id}


class DatabaseError(Exception):
    """演示用：模拟的数据访问异常。"""


class ServiceError(Exception):
    """演示用：包装底层异常的服务异常。"""


@router.get("/fail", tags=["demo"])
async def fail() -> dict[str, str]:
    """抛出三层异常链，验证日志是否完整记录根因。

    LookupError -> DatabaseError -> ServiceError（显式 ``from``）。
    该路由最终返回 500。
    """
    try:
        try:
            raise LookupError("row id=42 not found in local demo store")
        except LookupError as inner:
            raise DatabaseError("failed to load item") from inner
    except DatabaseError as middle:
        raise ServiceError("get_item service failed") from middle


@router.get("/fail-handled", tags=["demo"])
async def fail_handled() -> JSONResponse:
    """业务内已处理的异常：返回 400，验证错误按类型/状态分类。"""
    try:
        raise ValueError("invalid query parameter: n must be >= 0")
    except ValueError as exc:
        logger.warning(
            "handled business error",
            exc_info=True,
            extra={"error_type": "ValueError"},
        )
        return JSONResponse(
            status_code=400,
            content={"error": "bad_request", "detail": str(exc)},
        )


@router.post("/background", tags=["demo"])
async def schedule_background(
    background_tasks: BackgroundTasks,
) -> dict[str, object]:
    """把工作转成后台任务。

    FastAPI 在响应发出后于线程池中执行该函数，线程启动时拷贝当前
    context，因此它打出的日志依然带**发起请求**的关联 ID；与其他请求
    并发执行也不会串号。
    """
    captured = get_correlation_id()

    def background_job() -> None:
        job_logger = get_logger("app.background")
        job_logger.info("background job started", extra={"phase": "start"})
        # 模拟本地处理；期间该线程上下文里的 ID 必须仍是发起方的
        time.sleep(0.05)
        observed = get_correlation_id()
        if observed != captured:
            job_logger.error(
                "correlation id lost in background task",
                extra={"expected": captured, "observed": observed},
            )
        job_logger.info(
            "background job finished",
            extra={"phase": "done", "observed_correlation_id": observed},
        )

    background_tasks.add_task(background_job)
    return {"scheduled": True, "correlation_id": captured}


@router.get("/slow", tags=["demo"])
async def slow(ms: int = 50) -> dict[str, object]:
    """等待指定毫秒，用于观察耗时分布和进行中请求数。"""
    await asyncio.sleep(min(ms, 5000) / 1000)
    return {"waited_ms": ms, "correlation_id": get_correlation_id()}
