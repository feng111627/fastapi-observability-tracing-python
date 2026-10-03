"""事后可回溯的请求时间线存储。

为每个关联 ID 保存一条**请求时间线**：入口、业务处理、中途转出去的
后台/异步工作、异常、最终结果都以事件形式追加进来，请求早已响应完成
之后仍可按关联 ID 取回复盘。

设计要点：
- **全局有序**：所有事件共享一个单调递增的 ``seq``，并附带事件发生
  时刻的墙钟时间与相对请求开始的 ``elapsed_ms``。同一关联 ID 下即使
  事件由事件循环线程和后台任务线程池交错产生，真实发生顺序也不会乱。
- **归属隔离**：事件按关联 ID 分桶，追加时按当前上下文的 ID 归位，
  不会把别的请求的事件混进来；存储只做追加，不做跨桶合并。
- **有界内存**：同时受 ``max_traces``（保留条数）与 ``ttl_seconds``
  （保留时长）约束，超限按开始时间**从最旧开始淘汰**；被淘汰的 ID
  会在一个有界的“墓碑”集合里短暂留痕，使查询可以把「从未出现」与
  「曾存在但已淘汰」明确区分开。
- **并发安全**：共享结构全部在单个 :class:`~threading.RLock` 的临界
  区内访问；查询返回深拷贝快照，调用方读到的永远是一致的整条数据，
  不会读到追加一半的事件列表。
- **淘汰不影响实时链路**：存储是旁路。被淘汰只影响事后查询，请求
  本身的结构化日志与指标照常输出，互不影响。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

# 默认容量：本地单机长时间高频跑也不至于把内存吃光，同时足够复盘近期请求
DEFAULT_MAX_TRACES = 1000
# 默认保留时长：1 小时
DEFAULT_TTL_SECONDS = 3600.0
# “已淘汰”墓碑最多保留多少个 ID，防止墓碑集合本身无限增长
DEFAULT_MAX_EVICTED = 4096

# finish 事件（结构化字段）保留在日志 payload 顶层的键，不重复塞进 fields
_RESERVED_EVENT_KEYS = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "getMessage",
        "correlation_id", "method", "path", "status_code", "duration_ms",
        # trace_store 自身占位，防止日志记录把存储实例带进时间线
        "trace_store",
    }
)

# 各数字级别到稳定级别名的映射
_LEVEL_NAMES = {
    0: "NOTSET",
    10: "DEBUG",
    20: "INFO",
    30: "WARNING",
    40: "ERROR",
    50: "CRITICAL",
}


def level_name(level: int) -> str:
    """把 logging 的数字级别转换为稳定的级别名字符串。"""
    return _LEVEL_NAMES.get(level, f"L{level}")


def utc_now() -> datetime:
    """当前 UTC 时间（注入点，便于需要时替换时钟）。"""
    return datetime.now(timezone.utc)


def isoformat_ms(dt: datetime) -> str:
    """把时间渲染成毫秒精度、带时区偏移的 ISO 8601 字符串。"""
    return dt.isoformat(timespec="milliseconds")


def record_extra_fields(record: Any) -> dict[str, Any]:
    """从一条 LogRecord 中提取业务方通过 ``extra=`` 传入的结构化字段。"""
    fields: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key in _RESERVED_EVENT_KEYS or key.startswith("_"):
            continue
        fields[key] = value
    return fields


class _Trace:
    """一条关联 ID 的内部累积态。"""

    __slots__ = (
        "correlation_id", "method", "path", "started_at", "started_perf",
        "events", "finished", "finished_at", "status_code", "duration_ms",
        "error_type",
    )

    def __init__(
        self, correlation_id: str, method: str, path: str,
        started_at: datetime, started_perf: float,
    ) -> None:
        self.correlation_id = correlation_id
        self.method = method
        self.path = path
        self.started_at = started_at
        self.started_perf = started_perf
        self.events: list[dict[str, Any]] = []
        self.finished = False
        self.finished_at: datetime | None = None
        self.status_code: int | None = None
        self.duration_ms: float | None = None
        self.error_type: str | None = None


class TraceStore:
    """有容量上限、线程安全的请求时间线存储。

    参数
    ----
    max_traces:
        最多保留多少条请求时间线；超出后从开始时间最早的开始淘汰。
    ttl_seconds:
        单条时间线最长保留秒数；惰性淘汰（写入/查询时顺带清理）。
    max_evicted:
        “已淘汰 ID”墓碑集合的容量上限。
    """

    def __init__(
        self,
        max_traces: int = DEFAULT_MAX_TRACES,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_evicted: int = DEFAULT_MAX_EVICTED,
    ) -> None:
        self._lock = threading.RLock()
        self._configure(max_traces, ttl_seconds, max_evicted)
        self.reset()

    def _configure(
        self, max_traces: int, ttl_seconds: float, max_evicted: int,
    ) -> None:
        self.max_traces = max(1, int(max_traces))
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self._max_evicted = max(0, int(max_evicted))

    def configure(
        self,
        max_traces: int | None = None,
        ttl_seconds: float | None = None,
        max_evicted: int | None = None,
    ) -> None:
        """运行时调整容量/保留时长（主要用于测试），立即触发一次淘汰。"""
        with self._lock:
            self._configure(
                self.max_traces if max_traces is None else max_traces,
                self.ttl_seconds if ttl_seconds is None else ttl_seconds,
                self._max_evicted if max_evicted is None else max_evicted,
            )
            self._evict_expired()
            self._enforce_capacity()

    def reset(self) -> None:
        """清空全部时间线与墓碑（主要用于测试隔离）。"""
        with self._lock:
            # OrderedDict 同时充当 FIFO 队列：最旧在头部
            self._traces: OrderedDict[str, _Trace] = OrderedDict()
            self._evicted: OrderedDict[str, float] = OrderedDict()
            self._seq = 0

    # ------------------------------------------------------------------ #
    # 内部：事件构造与淘汰
    # ------------------------------------------------------------------ #
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _make_event(
        self,
        trace: _Trace,
        level: str,
        message: str,
        component: str,
        kind: str,
        fields: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        now_perf = time.perf_counter()
        return {
            "seq": self._next_seq(),
            "timestamp": isoformat_ms(utc_now()),
            "elapsed_ms": round((now_perf - trace.started_perf) * 1000, 3),
            "level": level,
            "message": message,
            "component": component,
            "kind": kind,
            "fields": dict(fields) if fields else {},
        }

    def _remember_evicted(self, cid: str) -> None:
        self._evicted[cid] = time.time()
        self._evicted.move_to_end(cid)
        while len(self._evicted) > self._max_evicted:
            self._evicted.popitem(last=False)

    def _evict_expired(self) -> None:
        """按 TTL 淘汰开始时间早于截止点的时间线（调用方需持锁）。"""
        if self.ttl_seconds <= 0:
            return
        deadline = time.time() - self.ttl_seconds
        # 墓碑也按时间清理
        stale = [cid for cid, ts in self._evicted.items() if ts < deadline]
        for cid in stale:
            self._evicted.pop(cid, None)
        expired: list[str] = []
        for cid, trace in self._traces.items():
            if trace.started_at.timestamp() < deadline:
                expired.append(cid)
            else:
                break  # OrderedDict 按开始先后排列，遇到未过期即可停止
        for cid in expired:
            self._traces.pop(cid, None)
            self._remember_evicted(cid)

    def _enforce_capacity(self) -> None:
        """超出条数上限时从最旧开始淘汰（调用方需持锁）。"""
        while len(self._traces) > self.max_traces:
            cid, _trace = self._traces.popitem(last=False)
            self._remember_evicted(cid)

    # ------------------------------------------------------------------ #
    # 写入 API（中间件 / 日志 handler 使用）
    # ------------------------------------------------------------------ #
    def begin(self, correlation_id: str, method: str, path: str) -> None:
        """记录请求入口并开启一条新时间线。

        极端情况下同一关联 ID 被复用（上游重用头），视为同一时间线的
        新一轮活动：保留既有事件，刷新开始信息，不丢历史事件。
        """
        with self._lock:
            self._evict_expired()
            # 复用 ID 若已被标记淘汰，则“复活”为一条新时间线
            self._evicted.pop(correlation_id, None)
            started_at = utc_now()
            started_perf = time.perf_counter()
            trace = self._traces.get(correlation_id)
            if trace is None:
                trace = _Trace(correlation_id, method, path, started_at, started_perf)
                self._traces[correlation_id] = trace
                trace.events.append(
                    self._make_event(
                        trace, "INFO", "request started", "middleware",
                        "request_start",
                        {"method": method, "path": path},
                    )
                )
                self._enforce_capacity()
                return
            # 复用已有时间线：刷新入口元数据并追加一轮入口事件
            trace.method = method
            trace.path = path
            trace.started_at = started_at
            trace.started_perf = started_perf
            trace.finished = False
            trace.finished_at = None
            trace.status_code = None
            trace.duration_ms = None
            trace.error_type = None
            self._traces.move_to_end(correlation_id)
            trace.events.append(
                self._make_event(
                    trace, "INFO", "request started", "middleware",
                    "request_start",
                    {"method": method, "path": path},
                )
            )
            self._enforce_capacity()

    def add_event(
        self,
        correlation_id: str,
        level: str,
        message: str,
        component: str,
        *,
        kind: str = "log",
        fields: Mapping[str, Any] | None = None,
    ) -> None:
        """向某条时间线追加一条事件。

        关联 ID 对应的时间线不存在（从未 begin）或已被淘汰时静默忽略：
        存储是旁路，绝不能因为回溯能力反过来影响请求处理与实时日志。
        """
        if not correlation_id:
            return
        with self._lock:
            trace = self._traces.get(correlation_id)
            if trace is None:
                return
            trace.events.append(
                self._make_event(trace, level, message, component, kind, fields)
            )

    def finish(
        self,
        correlation_id: str,
        status_code: int,
        duration_ms: float,
        error_type: str | None,
    ) -> None:
        """记录请求结束事件并冻结摘要（状态码/耗时/错误类型）。

        后台任务可能在响应完成后继续写入，因此 finish 只冻结摘要、
        不阻止后续事件追加；查询时按全局 seq 仍能看到真实先后顺序。
        """
        with self._lock:
            trace = self._traces.get(correlation_id)
            if trace is None:
                return
            trace.finished = True
            trace.finished_at = utc_now()
            trace.status_code = status_code
            trace.duration_ms = round(float(duration_ms), 3)
            trace.error_type = error_type
            level = "ERROR" if status_code >= 500 else (
                "WARNING" if status_code >= 400 else "INFO"
            )
            fields: dict[str, Any] = {
                "method": trace.method,
                "path": trace.path,
                "status_code": status_code,
                "duration_ms": trace.duration_ms,
            }
            if error_type:
                fields["error_type"] = error_type
            trace.events.append(
                self._make_event(
                    trace, level, "request finished", "middleware",
                    "request_end", fields,
                )
            )

    # ------------------------------------------------------------------ #
    # 查询 API（/traces 路由使用）
    # ------------------------------------------------------------------ #
    def get_timeline(self, correlation_id: str) -> tuple[str, dict[str, Any] | None]:
        """按关联 ID 查询时间线。

        返回 ``(status, payload)``：
        - ``("found", payload)``：命中，payload 为完整时间线快照；
        - ``("not_found", None)``：该 ID 从未出现过；
        - ``("evicted", None)``：该 ID 曾经存在，但因容量/TTL 已被淘汰。

        返回的是深拷贝快照，调用方遍历期间不会看到半截数据，
        也不会因为后续写入而改变。
        """
        with self._lock:
            self._evict_expired()
            trace = self._traces.get(correlation_id)
            if trace is None:
                if correlation_id in self._evicted:
                    return "evicted", None
                return "not_found", None
            payload: dict[str, Any] = {
                "correlation_id": trace.correlation_id,
                "method": trace.method,
                "path": trace.path,
                "started_at": isoformat_ms(trace.started_at),
                "finished_at": (
                    isoformat_ms(trace.finished_at)
                    if trace.finished_at is not None
                    else None
                ),
                "status": "in_flight" if not trace.finished else (
                    "error" if (trace.status_code or 0) >= 500 else "completed"
                ),
                "status_code": trace.status_code,
                "duration_ms": trace.duration_ms,
                "error_type": trace.error_type,
                "event_count": len(trace.events),
                "events": [dict(event, fields=dict(event["fields"]))
                           for event in trace.events],
            }
            return "found", payload

    def stats(self) -> dict[str, Any]:
        """存储自身的运行状态（容量/保留条数/当前占用），便于本地确认。"""
        with self._lock:
            return {
                "max_traces": self.max_traces,
                "ttl_seconds": self.ttl_seconds,
                "stored_traces": len(self._traces),
                "remembered_evicted": len(self._evicted),
            }


# 全局单例（与 metrics 一致，中间件 / 路由 / 日志 handler 共用）
trace_store = TraceStore()
