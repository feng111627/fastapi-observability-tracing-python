"""事后可回溯的请求时间线存储（trace timeline store）。

在实时日志/指标之外，把每个请求从进来到结束（含中途转出的后台工作）
产生的事件按关联 ID 归档，请求响应完之后仍可整条取回。

设计要点（实现逐步填充）：
- 事件来源与实时 JSON 日志同一条管道，保证时间线里看到的就是当时输出的；
- 每条事件带全局单调序号，并发/后台交错时按真实发生顺序排列；
- 存储有上限（时间线条数 / 单条时间线事件数 / 保留时长均可调），
  超限按时间先后淘汰最旧的，内存不随请求量无限增长；
- 全部读写淘汰在单把锁内完成，高并发下不读到半截数据、不串号。
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

# 容量默认值：本地长时间高频跑的合理上限，均可通过环境变量覆盖
DEFAULT_MAX_TRACES = 1000
DEFAULT_MAX_EVENTS_PER_TRACE = 200
DEFAULT_TTL_SECONDS = 3600.0


@dataclass
class TraceRecord:
    """一条关联 ID 名下的完整时间线。"""

    correlation_id: str
    started_at: float
    method: str = ""
    path: str = ""
    ended_at: float | None = None
    duration_ms: float | None = None
    status_code: int | None = None
    error_type: str | None = None
    truncated: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)
    last_activity: float = 0.0


class TraceStore:
    """线程安全、容量有界的关联 ID 时间线存储。"""

    def __init__(
        self,
        max_traces: int = DEFAULT_MAX_TRACES,
        max_events_per_trace: int = DEFAULT_MAX_EVENTS_PER_TRACE,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._lock = threading.RLock()
        self._traces: OrderedDict[str, TraceRecord] = OrderedDict()
        self._seq = 0
        self.configure(
            max_traces=max_traces,
            max_events_per_trace=max_events_per_trace,
            ttl_seconds=ttl_seconds,
        )

    def configure(
        self,
        max_traces: int | None = None,
        max_events_per_trace: int | None = None,
        ttl_seconds: float | None = None,
    ) -> None:
        """调整容量参数（None 表示保持原值）；调小后立即按新上限淘汰。"""
        with self._lock:
            if max_traces is not None:
                self._max_traces = max(1, max_traces)
            if max_events_per_trace is not None:
                self._max_events = max(1, max_events_per_trace)
            if ttl_seconds is not None:
                self._ttl = max(0.0, ttl_seconds)
            self._evict_overflow()

    def reset(self) -> None:
        """清空全部时间线（主要用于测试隔离）。"""
        with self._lock:
            self._traces.clear()
            self._seq = 0

    # ---- 写入侧（中间件 / 日志捕获调用） ----

    def begin(self, correlation_id: str, method: str, path: str) -> None:
        """请求进入时开立一条时间线。

        同一关联 ID 再次进入（如调用方重试复用同一 ID）视为新请求，
        重置为一条新的时间线并视为最新；超出容量时淘汰最旧的。
        """
        now = time.time()
        with self._lock:
            self._purge_expired(now)
            record = TraceRecord(
                correlation_id=correlation_id,
                started_at=now,
                method=method,
                path=path,
                last_activity=now,
            )
            self._traces[correlation_id] = record
            self._traces.move_to_end(correlation_id)
            self._evict_overflow()

    def add_event(self, correlation_id: str, event: dict[str, Any]) -> None:
        """向既有时间线追加一条事件；时间线不存在（未开立或已淘汰）时丢弃。

        每条事件在锁内领取全局单调序号 ``seq`` 并计算相对开端的
        ``offset_ms``，因此并发/后台交错时事件顺序即真实发生顺序。
        单条时间线事件数超上限后停止追加并置 ``truncated``，
        防止个别请求无限刷事件撑爆内存。
        """
        now = time.time()
        with self._lock:
            record = self._traces.get(correlation_id)
            if record is None:
                return
            if len(record.events) >= self._max_events:
                record.truncated = True
                return
            self._seq += 1
            entry = dict(event)
            entry["seq"] = self._seq
            entry["offset_ms"] = round((now - record.started_at) * 1000, 3)
            record.events.append(entry)
            record.last_activity = now

    def finish(
        self,
        correlation_id: str,
        status_code: int,
        error_type: str | None,
        duration: float,
    ) -> None:
        """请求结束时落定状态码/错误类型/耗时。

        只落定请求级汇总字段，不关闭时间线：响应发出后转出的后台工作
        产生的事件仍可继续追加。时间线已被淘汰时静默忽略。
        """
        now = time.time()
        with self._lock:
            record = self._traces.get(correlation_id)
            if record is None:
                return
            record.ended_at = now
            record.duration_ms = round(duration * 1000, 3)
            record.status_code = status_code
            record.error_type = error_type
            record.last_activity = now

    # ---- 查询侧 ----

    def get(self, correlation_id: str) -> dict[str, Any] | None:
        """按关联 ID 取回时间线快照；不存在（从未出现或已淘汰）返回 None。

        返回的是普通 dict/list 深拷贝快照，调用方读到的是一致的一个瞬间，
        不会因并发写入看到半截数据。
        """
        now = time.time()
        with self._lock:
            self._purge_expired(now)
            record = self._traces.get(correlation_id)
            if record is None:
                return None
            return {
                "correlation_id": record.correlation_id,
                "method": record.method,
                "path": record.path,
                "started_at": _iso(record.started_at),
                "ended_at": _iso(record.ended_at) if record.ended_at else None,
                "duration_ms": record.duration_ms,
                "status_code": record.status_code,
                "error_type": record.error_type,
                "finished": record.ended_at is not None,
                "truncated": record.truncated,
                "event_count": len(record.events),
                "events": [dict(e) for e in record.events],
            }

    # ---- 内部：容量与过期维护（调用时必须已持有锁） ----

    def _evict_overflow(self) -> None:
        """超出条数上限时，按开立先后淘汰最旧的时间线。"""
        while len(self._traces) > self._max_traces:
            self._traces.popitem(last=False)

    def _purge_expired(self, now: float) -> None:
        """淘汰超过保留时长（按最近活动时间计）的时间线。"""
        if self._ttl <= 0:
            return
        expired = [
            cid
            for cid, record in self._traces.items()
            if now - record.last_activity > self._ttl
        ]
        for cid in expired:
            del self._traces[cid]


def _iso(epoch: float) -> str:
    """把 epoch 秒格式化为带毫秒的本地 ISO 时间串。"""
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch))
    return f"{base}.{int((epoch % 1) * 1000):03d}"


def _from_env() -> "TraceStore":
    """按环境变量构造全局单例（缺省用默认值）。"""
    return TraceStore(
        max_traces=int(os.environ.get("TRACE_STORE_MAX_TRACES", DEFAULT_MAX_TRACES)),
        max_events_per_trace=int(
            os.environ.get(
                "TRACE_STORE_MAX_EVENTS_PER_TRACE", DEFAULT_MAX_EVENTS_PER_TRACE
            )
        ),
        ttl_seconds=float(
            os.environ.get("TRACE_STORE_TTL_SECONDS", DEFAULT_TTL_SECONDS)
        ),
    )


# 全局单例，中间件 / 日志捕获 / 查询路由共用
trace_store = _from_env()
