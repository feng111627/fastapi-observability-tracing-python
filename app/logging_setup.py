"""结构化 JSON 日志配置。

特性：
- 每条日志输出为**单行 JSON**（便于机器解析），统一写入 stdout；
- 自动注入当前上下文的关联 ID（``correlation_id``）以及请求方法/路径
  （``method``/``path``，由中间件通过 ``extra=`` 提供）；
- 记录异常时输出完整异常链 ``exc_chain``：从最外层异常一直到被
  ``raise ... from ...`` 包装的根因，每一环都带类型、消息和 traceback；
- 鉴权头等敏感字段通过 :func:`mask_headers` 脱敏。

注意：本模块命名为 logging_setup，避免与标准库 logging 冲突。
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from collections.abc import Mapping
from typing import Any

from app.context import get_correlation_id

# 命中即需要脱敏的请求头（小写匹配；authorization 同时覆盖 bearer token）
SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
    }
)
MASKED = "***"


def mask_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """复制一份请求头并把敏感头的值替换为 ``***``。"""
    masked: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in SENSITIVE_HEADERS:
            masked[key] = MASKED
        else:
            masked[key] = value
    return masked


def flatten_exception_chain(exc: BaseException) -> list[dict[str, str]]:
    """把异常及其 ``__cause__`` / ``__context__`` 链展开为有序列表。

    顺序为「最外层 → 最内层根因」，与异常发生的因果方向相反排列，
    方便直接看到完整包装关系。每一环包含：
    ``type``（异常类型全名）、``message``（str(e)）、
    ``traceback``（该层 traceback 文本）。
    """
    chain: list[dict[str, str]] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        tb_text = "".join(
            traceback.format_exception(type(current), current, current.__traceback__)
        )
        chain.append(
            {
                "type": f"{type(current).__module__}.{type(current).__qualname__}",
                "message": str(current),
                "traceback": tb_text,
            }
        )
        # raise X from Y -> __cause__ 优先；隐式链 -> __context__
        current = current.__cause__ or current.__context__
    return chain


class CorrelationFilter(logging.Filter):
    """为每条日志注入当前上下文的关联 ID 与请求信息。

    请求维度字段（method/path/status_code/duration_ms）由中间件在
    ``extra=`` 中提供；缺失时给出稳定的默认值，保证字段始终存在。
    只放行 ``app`` 命名空间的业务日志与 ``uvicorn`` 命名空间的服务器
    日志，其余第三方库日志不进入 JSON 流，避免无关联 ID 的噪音
    （如需也可单独调高第三方 logger 级别）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not (
            record.name == "app"
            or record.name.startswith("app.")
            or record.name == "uvicorn"
            or record.name.startswith("uvicorn.")
        ):
            return False
        record.correlation_id = get_correlation_id()
        record.method = getattr(record, "method", "")
        record.path = getattr(record, "path", "")
        record.status_code = getattr(record, "status_code", None)
        record.duration_ms = getattr(record, "duration_ms", None)
        return True


class JsonFormatter(logging.Formatter):
    """把 LogRecord 渲染为单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": logging.Formatter.formatTime(
                self, record, "%Y-%m-%dT%H:%M:%S"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": record.correlation_id,
            "method": record.method,
            "path": record.path,
            "status_code": record.status_code,
            "duration_ms": record.duration_ms,
        }
        if record.exc_info:
            payload["exc_chain"] = flatten_exception_chain(record.exc_info[1])
        # 中间件/业务侧通过 extra 传入的其他结构化字段
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_KEYS:
                continue
            payload.setdefault(key, value)
        return json.dumps(payload, ensure_ascii=False, default=str)


_RESERVED_RECORD_KEYS = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "getMessage", "color_message",
        "correlation_id", "method", "path", "status_code", "duration_ms",
    }
)


class TraceCaptureHandler(logging.Handler):
    """把每条业务日志同步归档进 :data:`app.tracing.trace_store`。

    与 stdout 的 JSON 流共用同一套 filter/formatter，因此时间线里的事件
    与实时日志内容完全一致（同样脱敏、同样带异常链）；只归档当前上下文
    有关联 ID 的记录，无 ID 的（如启动日志）不进时间线。
    """

    def __init__(self) -> None:
        super().__init__()
        self.addFilter(CorrelationFilter())
        self.setFormatter(JsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            correlation_id = getattr(record, "correlation_id", "")
            if not correlation_id:
                return
            # 延迟导入，避免 logging_setup <-> tracing 循环依赖
            from app.tracing import trace_store

            payload = json.loads(self.format(record))
            trace_store.add_event(correlation_id, payload)
        except Exception:  # 归档失败绝不能影响业务日志本身
            self.handleError(record)


def configure_logging(level: int = logging.INFO) -> None:
    """配置根 logger：JSON 输出到 stdout，全进程只配置一次。"""
    root = logging.getLogger()
    if getattr(root, "_obs_configured", False):
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(CorrelationFilter())
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.addHandler(TraceCaptureHandler())
    root.setLevel(level)
    root._obs_configured = True  # type: ignore[attr-defined]
    # uvicorn 的 logging 配置先于应用导入生效，导入时即统一一次；
    # 之后若被 uvicorn.run 再次覆盖，由应用启动钩子再统一一次。
    unify_uvicorn_logging()


def unify_uvicorn_logging() -> None:
    """把 uvicorn 自身日志并入 JSON 管道，保证服务输出只有一种格式。

    - ``uvicorn`` / ``uvicorn.error``（启动、关停、异常）：摘掉自带的明文
      handler，改为传播到根 logger，走统一的 JSON 输出；
    - ``uvicorn.access``：访问日志已由中间件以结构化形式输出（且带关联
      ID），uvicorn 的明文访问行属于重复且格式不一致，直接关闭。

    需要在 uvicorn 应用完自己的 logging 配置之后再调用（如应用启动
    钩子），幂等，可重复调用。
    """
    for name in ("uvicorn", "uvicorn.error"):
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.setLevel(logging.CRITICAL + 1)


def get_logger(name: str) -> logging.Logger:
    """获取一个 logger。"""
    return logging.getLogger(name)
