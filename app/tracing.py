"""事后可回溯的请求时间线存储（trace timeline store）。

在实时日志/指标之外，把每个请求从进来到结束（含中途转出的后台工作）
产生的事件按关联 ID 归档，请求响应完之后仍可整条取回。

设计要点（实现逐步填充）：
- 每条时间线固定有「请求进入 / 请求结束」两条结构事件（由中间件在开立与
  落定时写入），即使全程没有任何业务日志，事后也能看出请求的首尾、
  时间点与最终结果；结构事件不占单条时间线的业务事件上限；
- 业务事件来源与实时 JSON 日志同一条管道，保证时间线里看到的就是当时输出的；
- 每条事件带全局单调序号，并发/后台交错时按真实发生顺序排列；
- 存储有上限（时间线条数 / 单条时间线事件数 / 保留时长均可调），
  超限按时间先后淘汰最旧的，内存不随请求量无限增长；
- 被淘汰的关联 ID 会留下有界的「淘汰标记」（容量挤出 / 过期清理），
  查询侧据此把「已淘汰」与「从未出现」区分开；
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

# 淘汰标记（cid -> 淘汰原因）的保底留存条数，实际下限取
# max(max_traces, 该值)，保证小容量配置下仍能区分「已淘汰」与「从未出现」
MIN_EVICTED_MARKS = 128

# 结构事件的来源标识与种类（区别于日志管道归档的业务事件）
TIMELINE_LOGGER = "app.timeline"
EVENT_KIND_STARTED = "request_started"
EVENT_KIND_FINISHED = "request_finished"

# 淘汰原因
EVICTED_BY_CAPACITY = "capacity"
EVICTED_BY_TTL = "ttl"


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
    business_event_count: int = 0
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
        # 近期被淘汰的关联 ID -> 淘汰原因（capacity/ttl），有界 FIFO，
        # 仅用于查询侧区分「已淘汰」与「从未出现」，不参与时间线内容
        self._evicted: OrderedDict[str, str] = OrderedDict()
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
        """清空全部时间线与淘汰标记（主要用于测试隔离）。"""
        with self._lock:
            self._traces.clear()
            self._evicted.clear()
            self._seq = 0

    # ---- 写入侧（中间件 / 日志捕获调用） ----

    def begin(self, correlation_id: str, method: str, path: str) -> None:
        """请求进入时开立一条时间线，并写入固定的「请求进入」结构事件。

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
            # 该 ID 若曾被淘汰，如今重新出现，淘汰标记随之失效
            self._evicted.pop(correlation_id, None)
            self._append_structural(
                record,
                now,
                {
                    "timestamp": _iso(now),
                    "level": "INFO",
                    "logger": TIMELINE_LOGGER,
                    "message": "request started",
                    "correlation_id": correlation_id,
                    "method": method,
                    "path": path,
                    "event_kind": EVENT_KIND_STARTED,
                },
            )
            self._evict_overflow()

    def add_event(self, correlation_id: str, event: dict[str, Any]) -> None:
        """向既有时间线追加一条业务事件；时间线不存在（未开立或已淘汰）时丢弃。

        每条事件在锁内领取全局单调序号 ``seq`` 并计算相对开端的
        ``offset_ms``，因此并发/后台交错时事件顺序即真实发生顺序。
        业务事件数超上限后停止追加并置 ``truncated``，
        防止个别请求无限刷事件撑爆内存（首/尾结构事件不占该上限，
        保证任何请求都能看出完整的入口与结束）。
        """
        now = time.time()
        with self._lock:
            record = self._traces.get(correlation_id)
            if record is None:
                return
            if record.business_event_count >= self._max_events:
                record.truncated = True
                return
            record.business_event_count += 1
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
        """请求结束时落定状态码/错误类型/耗时，并写入「请求结束」结构事件。

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
            self._append_structural(
                record,
                now,
                {
                    "timestamp": _iso(now),
                    "level": "ERROR" if error_type else "INFO",
                    "logger": TIMELINE_LOGGER,
                    "message": "request finished",
                    "correlation_id": correlation_id,
                    "method": record.method,
                    "path": record.path,
                    "status_code": status_code,
                    "duration_ms": record.duration_ms,
                    "error_type": error_type,
                    "event_kind": EVENT_KIND_FINISHED,
                },
            )

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

    def eviction_reason(self, correlation_id: str) -> str | None:
        """该 ID 的时间线若近期被淘汰，返回淘汰原因（``capacity``/``ttl``）。

        返回 ``None`` 表示没有淘汰记录（从未出现，或标记本身也已被挤出）。
        与 :meth:`get` 配合，让查询侧能把「已淘汰」与「从未出现」区分开。
        """
        with self._lock:
            return self._evicted.get(correlation_id)

    # ---- 内部：容量与过期维护（调用时必须已持有锁） ----

    def _append_structural(
        self, record: TraceRecord, now: float, event: dict[str, Any]
    ) -> None:
        """追加首/尾结构事件：照常领取序号，但不占业务事件上限、不置 truncated。"""
        self._seq += 1
        entry = dict(event)
        entry["seq"] = self._seq
        entry["offset_ms"] = round((now - record.started_at) * 1000, 3)
        record.events.append(entry)

    def _mark_evicted(self, correlation_id: str, reason: str) -> None:
        """记录一条淘汰标记，并保持标记总量有界（FIFO）。"""
        self._evicted[correlation_id] = reason
        self._evicted.move_to_end(correlation_id)
        limit = max(self._max_traces, MIN_EVICTED_MARKS)
        while len(self._evicted) > limit:
            self._evicted.popitem(last=False)

    def _evict_overflow(self) -> None:
        """超出条数上限时，按开立先后淘汰最旧的时间线。"""
        while len(self._traces) > self._max_traces:
            cid, _ = self._traces.popitem(last=False)
            self._mark_evicted(cid, EVICTED_BY_CAPACITY)

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
            self._mark_evicted(cid, EVICTED_BY_TTL)


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
