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
from app.tracing import level_name, record_extra_fields, trace_store

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
    只放行 ``app`` 命名空间的业务日志，第三方库日志不进入 JSON 流，
    避免无关联 ID 的噪音（如需也可单独调高第三方 logger 级别）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # 只放行 app 命名空间的业务日志，第三方/服务器框架日志不进入这条
        # JSON 流（uvicorn 的日志在 configure_logging 中单独接 JSON handler）
        if record.name != "app" and not record.name.startswith("app."):
            return False
        record.correlation_id = get_correlation_id()
        record.method = getattr(record, "method", "")
        record.path = getattr(record, "path", "")
        record.status_code = getattr(record, "status_code", None)
        record.duration_ms = getattr(record, "duration_ms", None)
        return True


class _ContextDefaultsFilter(logging.Filter):
    """与 :class:`CorrelationFilter` 相同的默认字段注入，但不限制 logger 名。

    用于挂在 uvicorn 等框架 logger 上，使框架日志同样具备时间线/JSON
    渲染所需的稳定字段（缺失时给默认值，绝不因字段缺失而报错）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = getattr(record, "correlation_id", "") or (
            get_correlation_id()
        )
        record.method = getattr(record, "method", "")
        record.path = getattr(record, "path", "")
        record.status_code = getattr(record, "status_code", None)
        record.duration_ms = getattr(record, "duration_ms", None)
        return True


class TimelineHandler(logging.Handler):
    """旁路 handler：把每条 ``app.*`` 日志实时追加到请求时间线。

    - 归属完全由当前上下文的关联 ID 决定（后台任务线程拷贝了 context，
      因此后台日志自动归到发起请求的时间线下）；
    - 时间线已淘汰或不存在（脱离请求上下文的日志）时静默丢弃，存储是
      旁路，绝不能反过来影响实时日志；
    - 这里只保留轻量字段（级别/消息/来源组件/extra 结构化字段），
      完整异常链 traceback 仍只走实时 JSON 日志，避免时间线膨胀。
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            cid = getattr(record, "correlation_id", "") or get_correlation_id()
            if not cid:
                return
            fields = record_extra_fields(record)
            if record.exc_info:
                exc = record.exc_info[1]
                fields["exc_type"] = (
                    f"{type(exc).__module__}.{type(exc).__qualname__}"
                )
                fields["exc_message"] = str(exc)
            trace_store.add_event(
                cid,
                level_name(record.levelno),
                record.getMessage(),
                record.name,
                fields=fields,
            )
        except Exception:  # noqa: BLE001 - 旁路失败绝不能影响请求
            self.handleError(record)


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
            "correlation_id": getattr(record, "correlation_id", ""),
            "method": getattr(record, "method", ""),
            "path": getattr(record, "path", ""),
            "status_code": getattr(record, "status_code", None),
            "duration_ms": getattr(record, "duration_ms", None),
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
        "processName", "process", "taskName", "getMessage",
        "correlation_id", "method", "path", "status_code", "duration_ms",
    }
)


def configure_logging(level: int = logging.INFO) -> None:
    """配置进程内日志：全部输出为统一的单行 JSON 到 stdout。

    - ``app.*`` 业务日志：经 :class:`CorrelationFilter` 注入关联 ID 等
      字段，同时旁路一份到 :class:`TimelineHandler` 进入请求时间线；
    - ``uvicorn`` / ``uvicorn.error`` / ``uvicorn.access`` 等框架日志：
      同样接管为 JSON 格式，保证本地服务输出里不再混入格式不一致的
      明文行（uvicorn 自带的明文访问日志直接关闭，访问口径以本服务
      自己的结构化 ``request completed`` 为准）；
    - 全进程只配置一次，重复调用仅调整级别。
    """
    root = logging.getLogger()
    if getattr(root, "_obs_configured", False):
        root.setLevel(level)
        return

    json_handler = logging.StreamHandler(sys.stdout)
    json_handler.addFilter(CorrelationFilter())
    json_handler.setFormatter(JsonFormatter())

    timeline_handler = TimelineHandler()
    timeline_handler.addFilter(CorrelationFilter())

    root.addHandler(json_handler)
    root.addHandler(timeline_handler)
    root.setLevel(level)

    # 接管 uvicorn 框架日志：统一 JSON，关闭其自带的明文 handler，
    # 并禁止向 root 传播，避免同一条框架日志输出两遍。
    defaults = _ContextDefaultsFilter()
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        framework_logger = logging.getLogger(name)
        handler = logging.StreamHandler(sys.stdout)
        handler.addFilter(defaults)
        handler.setFormatter(JsonFormatter())
        framework_logger.handlers = [handler]
        framework_logger.propagate = False
        framework_logger.setLevel(level)
    # uvicorn 自带的明文访问行直接丢弃：本服务的结构化访问日志
    # （app.access / request completed）已是统一且更丰富的访问口径。
    logging.getLogger("uvicorn.access").disabled = True

    root._obs_configured = True  # type: ignore[attr-defined]


def get_logger(name: str) -> logging.Logger:
    """获取一个 logger。"""
    return logging.getLogger(name)
