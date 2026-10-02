"""请求关联 ID（correlation ID）的上下文管理。

基于 :class:`contextvars.ContextVar`：
- 每个进入的请求绑定一个关联 ID，沿用调用方在 ``X-Correlation-ID`` 头中
  自带的值，否则自动生成；
- ``asyncio`` 任务、``BackgroundTasks``（线程池执行）在创建时都会拷贝
  当前 context，因此请求中途转后台任务时 ID 不会丢失；
- 不同请求/任务持有各自的 context 副本，高并发下互不串号。
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token

CORRELATION_ID_HEADER = "X-Correlation-ID"

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


def get_correlation_id() -> str:
    """返回当前上下文的关联 ID；未设置时返回空字符串。"""
    return _correlation_id.get()


def set_correlation_id(value: str) -> Token[str]:
    """把关联 ID 绑定到当前上下文，返回用于恢复的 Token。"""
    return _correlation_id.set(value)


def reset_correlation_id(token: Token[str]) -> None:
    """根据 Token 恢复关联 ID 上下文。"""
    _correlation_id.reset(token)


def new_correlation_id() -> str:
    """生成一个新的关联 ID（UUID4 的 32 位十六进制形式）。"""
    return uuid.uuid4().hex


def resolve_correlation_id(inbound: str | None) -> str:
    """沿用入站 ID（去空白后非空即有效），否则生成新 ID。"""
    if inbound is not None:
        value = inbound.strip()
        if value:
            return value
    return new_correlation_id()


def bind_correlation_id(inbound: str | None) -> tuple[str, Token[str]]:
    """沿用入站 ID 或生成新 ID，并绑定到当前上下文。

    返回 ``(correlation_id, token)``；调用方在处理结束后应使用
    :func:`reset_correlation_id` 恢复。
    """
    value = resolve_correlation_id(inbound)
    token = set_correlation_id(value)
    return value, token
