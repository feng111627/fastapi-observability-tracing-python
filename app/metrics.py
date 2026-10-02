"""进程内指标：请求总数、错误数（按类型）、耗时分布、进行中请求数。

- 全部状态保存在单个 :class:`~threading.RLock` 保护下，``record_*`` 为
  原子操作；后台任务运行在线程池、服务主循环运行在事件循环线程，混合同步/
  异步高并发下计数不会丢失或串错。
- 错误既能按 HTTP 状态码分类（``errors_by_status``），也能按异常类型分类
  （``errors_by_type``），而不是一律算成一类。
- 耗时分布采用与 Prometheus histogram 相同的边界累计桶（le = +Inf），
  另附总和与样本数，可在无外部服务的情况下计算分位数。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any

# 单位：秒。覆盖本地接口常见的 5ms ~ 10s 区间。
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)


def _histogram(buckets: tuple[float, ...]) -> dict[str, Any]:
    return {
        "buckets": list(buckets),
        "cumulative_counts": [0] * len(buckets),
        "le_inf": 0,
        "sum": 0.0,
        "count": 0,
    }


def _observe(hist: dict[str, Any], value: float) -> None:
    for i, boundary in enumerate(hist["buckets"]):
        if value <= boundary:
            hist["cumulative_counts"][i] += 1
    hist["le_inf"] += 1
    hist["sum"] += value
    hist["count"] += 1


class Metrics:
    """线程安全的内存指标注册表。"""

    def __init__(self, buckets: tuple[float, ...] = DEFAULT_BUCKETS) -> None:
        self._buckets = buckets
        self._lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        """清空全部指标（主要用于测试隔离）。"""
        with self._lock:
            self._requests_total = 0
            self._inflight = 0
            # key: (METHOD, path template)
            self._requests_by_route: dict[tuple[str, str], int] = defaultdict(int)
            self._errors_by_status: dict[int, int] = defaultdict(int)
            self._errors_by_type: dict[str, int] = defaultdict(int)
            self._latency: dict[tuple[str, str], dict[str, Any]] = {}
            self._latency_all = _histogram(self._buckets)

    def _route_hist(self, method: str, path: str) -> dict[str, Any]:
        key = (method, path)
        hist = self._latency.get(key)
        if hist is None:
            hist = _histogram(self._buckets)
            self._latency[key] = hist
        return hist

    def record_request(
        self,
        method: str,
        path: str,
        status_code: int,
        duration: float,
        error_type: str | None = None,
    ) -> None:
        """原子地记录一次已完成请求。

        参数 ``path`` 应使用路由模板（如 ``/items/{id}``）而非原始 URL，
        避免高基数标签。``status_code >= 500`` 计入服务端错误；
        提供 ``error_type`` 时额外按异常类型计数。
        """
        with self._lock:
            self._requests_total += 1
            self._requests_by_route[(method, path)] += 1
            _observe(self._latency_all, duration)
            _observe(self._route_hist(method, path), duration)
            if status_code >= 500:
                self._errors_by_status[status_code] += 1
            if error_type:
                self._errors_by_type[error_type] += 1

    def inc_inflight(self) -> None:
        """进行中请求数 +1。"""
        with self._lock:
            self._inflight += 1

    def dec_inflight(self) -> None:
        """进行中请求数 -1（不会低于 0）。"""
        with self._lock:
            if self._inflight > 0:
                self._inflight -= 1

    @property
    def inflight(self) -> int:
        with self._lock:
            return self._inflight

    def snapshot(self) -> dict[str, Any]:
        """导出当前所有指标的快照（普通 dict/list，可直接 JSON 序列化）。"""
        with self._lock:
            by_route = {
                f"{method} {path}": count
                for (method, path), count in sorted(self._requests_by_route.items())
            }
            latency_by_route = {
                f"{method} {path}": {
                    "buckets": hist["buckets"],
                    "cumulative_counts": list(hist["cumulative_counts"]),
                    "le_inf": hist["le_inf"],
                    "sum": round(hist["sum"], 6),
                    "count": hist["count"],
                }
                for (method, path), hist in sorted(self._latency.items())
            }
            return {
                "requests_total": self._requests_total,
                "requests_by_route": by_route,
                "errors_total_by_status": {
                    str(status): count
                    for status, count in sorted(self._errors_by_status.items())
                },
                "errors_total_by_type": dict(
                    sorted(self._errors_by_type.items())
                ),
                "inflight_requests": self._inflight,
                "latency_seconds_all": {
                    "buckets": list(self._latency_all["buckets"]),
                    "cumulative_counts": list(
                        self._latency_all["cumulative_counts"]
                    ),
                    "le_inf": self._latency_all["le_inf"],
                    "sum": round(self._latency_all["sum"], 6),
                    "count": self._latency_all["count"],
                },
                "latency_seconds_by_route": latency_by_route,
            }


# 全局单例，中间件与 /metrics 路由共用
metrics = Metrics()
