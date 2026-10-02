"""请求关联 ID（correlation ID）的上下文管理。

基于 :mod:`contextvars`，每个请求在自己的异步任务上下文中设置关联 ID，
天然支持 asyncio 并发：不同请求（不同 Task）之间互不干扰、不会串号；
请求处理过程中派生出的后台任务 / 线程池任务会自动复制当前上下文。
"""

from __future__ import annotations

import contextvars
import uuid

#: 没有关联 ID 时的占位值，避免日志字段为 null。
DEFAULT_CORRELATION_ID = "-"

#: 存放当前执行上下文关联 ID 的 ContextVar。
_correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=DEFAULT_CORRELATION_ID
)


def generate_correlation_id() -> str:
    """生成一个新的关联 ID（UUID4 的十六进制形式）。"""
    return uuid.uuid4().hex


def get_correlation_id() -> str:
    """获取当前上下文的关联 ID；未设置时返回占位值。"""
    return _correlation_id_var.get()


def set_correlation_id(value: str) -> contextvars.Token[str]:
    """把关联 ID 设置到当前上下文，返回用于恢复现场的 Token。"""
    return _correlation_id_var.set(value)


def reset_correlation_id(token: contextvars.Token[str]) -> None:
    """根据 Token 把关联 ID 恢复到设置前的值。

    中间件在请求结束（含异常）后必须调用，防止上下文被复用（例如线程池
    工作线程）时把上一个请求的关联 ID 泄漏给下一个请求。
    """
    _correlation_id_var.reset(token)
