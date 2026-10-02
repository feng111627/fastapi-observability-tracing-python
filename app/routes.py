"""HTTP 路由：示例端点、健康检查、指标端点。

除了健康检查与指标端点外，还提供若干演示端点，用于在本地验证
关联 ID 贯穿、异常链记录、后台任务上下文与并发场景。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Header, Request

from app.context import get_correlation_id
from app.metrics import registry

router = APIRouter()
logger = logging.getLogger("app.demo")


@router.get("/")
async def read_root() -> dict[str, str]:
    """根路径示例端点，返回当前请求的关联 ID。"""
    return {"hello": "world", "correlation_id": get_correlation_id()}


@router.get("/health", tags=["observability"])
async def health() -> dict[str, Any]:
    """健康检查入口（liveness）。"""
    return {"status": "ok"}


@router.get("/metrics", tags=["observability"])
async def metrics() -> dict[str, Any]:
    """指标输出入口（JSON）。"""
    return registry.snapshot()


@router.get("/whoami", tags=["demo"])
async def whoami(request: Request) -> dict[str, Any]:
    """回显当前请求的关联 ID，并演示日志中自动携带该 ID。"""
    correlation_id = get_correlation_id()
    logger.info("whoami called", extra={"from_scope": request.state.correlation_id})
    return {
        "correlation_id": correlation_id,
        "from_scope": request.state.correlation_id,
        "header_echo": request.headers.get("x-correlation-id"),
    }


@router.get("/boom", tags=["demo"])
async def boom() -> None:
    """抛出带完整因果链的异常，用于验证异常链路记录。

    最外层是 :class:`RuntimeError`，其 ``__cause__`` 为
    :class:`ValueError`（显式 ``raise ... from ...``）。
    """

    def _load_config() -> None:
        raise ValueError("配置项 timeout_ms 不是数字: 'abc'")

    try:
        _load_config()
    except ValueError as exc:
        raise RuntimeError("服务初始化失败") from exc


@router.get("/flaky", tags=["demo"])
async def flaky(fail: bool = False) -> dict[str, str]:
    """按查询参数决定是否抛错，便于构造不同类型的错误指标。"""
    if fail:
        raise ValueError("flaky 被要求失败")
    return {"correlation_id": get_correlation_id()}


@router.get("/background", tags=["demo"])
async def background(request: Request) -> dict[str, Any]:
    """把工作转成 Starlette 后台任务，验证关联 ID 不丢失、不串号。"""
    from starlette.background import BackgroundTask
    from starlette.responses import JSONResponse

    correlation_id = get_correlation_id()

    def _background_job() -> None:
        # 后台任务执行点：此处读到的关联 ID 必须与发起请求一致
        bg_cid = get_correlation_id()
        logger.info(
            "background task running",
            extra={"bg_correlation_id": bg_cid, "expected": correlation_id},
        )
        assert bg_cid == correlation_id, "后台任务关联 ID 丢失或串号"

    return JSONResponse(
        {"correlation_id": correlation_id, "scheduled": True},
        background=BackgroundTask(_background_job),
    )


@router.get("/concurrent", tags=["demo"])
async def concurrent(delay_ms: int = 20) -> dict[str, Any]:
    """模拟一个有耗时的端点，用于并发请求下的指标与关联 ID 测试。"""
    cid = get_correlation_id()
    await asyncio.sleep(delay_ms / 1000.0)
    # 醒来后关联 ID 必须仍然是自己的
    assert get_correlation_id() == cid
    return {"correlation_id": cid}


@router.get("/sensitive", tags=["demo"])
async def sensitive(
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """演示鉴权头在访问日志中被脱敏（不在响应里回显其值）。"""
    logger.info("sensitive endpoint hit")
    return {"correlation_id": get_correlation_id(), "auth_received": bool(authorization)}
