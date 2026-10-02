"""结构化（JSON）日志配置。

- 每条日志渲染为单行 JSON，便于机器解析（``jq``、日志平台等）。
- 通过 logging Filter 从 :mod:`contextvars` 注入当前请求的关联 ID，
  因此业务代码里任意位置打的日志都会自动带上关联 ID。
- 异常日志（``logger.exception`` / ``exc_info``）会输出完整异常链：
  包括 ``raise ... from ...`` 显式包装（``__cause__``）与隐式上下文
  （``__context__``），每一层都包含异常类型、消息与完整 traceback。
- 鉴权头等敏感请求头统一脱敏。
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from app.context import get_correlation_id

#: 需要脱敏的请求头名称（全部小写比较）。
SENSITIVE_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
    }
)

#: 脱敏后的占位值。
REDACTED = "[REDACTED]"

#: LogRecord 自带属性，不属于业务附加字段，避免被当作 extra 输出。
_RESERVED_ATTRS: frozenset[str] = frozenset(
    {
        "name", "msg", "args", "created", "relativeCreated", "thread",
        "threadName", "msecs", "processName", "process", "levelname",
        "levelno", "pathname", "filename", "module", "exc_info", "exc_text",
        "stack_info", "lineno", "funcName", "taskName", "message", "asctime",
    }
)


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """对敏感请求头做脱敏，返回新字典（不修改入参）。"""
    redacted: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in SENSITIVE_HEADERS:
            redacted[key] = REDACTED
        else:
            redacted[key] = value
    return redacted


def _format_exception_chain(exc_info: tuple[type[BaseException], BaseException,
                                            Any] | None) -> list[dict[str, Any]]:
    """把异常及其完整因果链格式化为结构化列表。

    依次遍历 ``__cause__``（显式 ``raise from``）与 ``__context__``
    （隐式上下文），每层包含异常类型全限定名、消息、关系与完整 traceback。
    """
    if not exc_info:
        return []

    chain: list[dict[str, Any]] = []
    seen: set[int] = set()
    exc_type, exc, tb = exc_info
    relation = "root"  # 最外层异常

    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        # 借助 Formatter 生成该层完整 traceback 字符串
        tb_text = logging.Formatter().formatException(
            (type(exc), exc, exc.__traceback__)
        )
        chain.append(
            {
                "relation": relation,
                "type": f"{type(exc).__module__}.{type(exc).__qualname__}",
                "message": str(exc),
                "traceback": tb_text,
            }
        )
        if exc.__cause__ is not None:
            relation = "cause"  # 显式 raise X from Y
            exc = exc.__cause__
        elif exc.__context__ is not None and not exc.__suppress_context__:
            relation = "context"  # 隐式：处理 Y 时又抛出 X
            exc = exc.__context__
        else:
            exc = None  # type: ignore[assignment]
    return chain


class CorrelationIdFilter(logging.Filter):
    """给每条日志注入当前上下文的关联 ID。"""

    def __init__(self, ctx_var: contextvars.ContextVar[str]) -> None:
        super().__init__()
        self._ctx_var = ctx_var

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = self._ctx_var.get()
        return True


class JsonLogFormatter(logging.Formatter):
    """把 LogRecord 渲染成单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", "-"),
        }

        # 业务/中间件通过 extra= 传入的结构化字段（method/path/status_code 等）
        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = _format_exception_chain(record.exc_info)

        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: int = logging.INFO) -> None:
    """配置根日志记录器：JSON 输出到 stderr，并注入关联 ID。

    同时让 uvicorn 的日志传播到根记录器，保证本地运行时所有日志
    （含框架访问日志）都走同一套 JSON 格式且带关联 ID。
    """
    root = logging.getLogger()
    root.setLevel(level)

    # 幂等：测试或重复调用时避免叠加 handler
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonLogFormatter())
    handler.addFilter(CorrelationIdFilter(_correlation_id_var_ref()))
    root.addHandler(handler)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True


def _correlation_id_var_ref() -> contextvars.ContextVar[str]:
    """延迟导入 ContextVar 本体，避免与 context 模块产生循环导入写法。"""
    from app.context import _correlation_id_var  # noqa: PLC2701

    return _correlation_id_var
