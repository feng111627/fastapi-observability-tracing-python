"""进程内指标收集（线程安全，无外部依赖）。

暴露四类指标，快照见 :meth:`MetricsRegistry.snapshot`：

1. ``requests_total``：请求总数（含按 method/path/status_code 维度）；
2. ``errors_total``：错误请求数（status>=400），按异常类型与状态码区分；
3. ``latency_ms``：耗时分布直方图（累积桶）+ count/sum；
4. ``in_progress``：当前正在处理的请求数（gauge）。

计数全部在 :class:`threading.RLock` 保护下更新，多线程 / asyncio
线程池并发调用也不会丢计数。
"""

from __future__ import annotations

import threading
from typing import Any

#: 直方图上界（毫秒），+Inf 桶单独记录。
DEFAULT_BUCKETS: tuple[float, ...] = (
    5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0, 5000.0,
)


class MetricsRegistry:
    """保存请求总数、错误数、耗时分布、进行中请求数。"""

    def __init__(self, buckets: tuple[float, ...] = DEFAULT_BUCKETS) -> None:
        self._buckets = tuple(buckets)
        self._lock = threading.RLock()
        self.reset()

    def record_request(
        self,
        method: str,
        path: str,
        status_code: int,
        duration_ms: float,
        error_type: str | None = None,
    ) -> None:
        """记录一次完成的请求。

        :param method: HTTP 方法
        :param path: 路由模板（拿不到时用原始 path）
        :param status_code: HTTP 状态码
        :param duration_ms: 处理耗时（毫秒）
        :param error_type: 异常类型全限定名（仅出错时提供）
        """
        with self._lock:
            self._total += 1
            self._by_status[status_code] = self._by_status.get(status_code, 0) + 1
            self._by_method[method] = self._by_method.get(method, 0) + 1
            self._by_path[path] = self._by_path.get(path, 0) + 1

            self._latency_count += 1
            self._latency_sum += duration_ms
            hit = False  # 是否已落入某个有限上界桶
            for i, upper in enumerate(self._buckets):
                if not hit and duration_ms <= upper:
                    self._bucket_counts[i] += 1
                    hit = True
            if not hit:
                self._bucket_counts[len(self._buckets)] += 1  # +Inf

            if status_code >= 400:
                self._errors_total += 1
                self._errors_by_status[status_code] = (
                    self._errors_by_status.get(status_code, 0) + 1
                )
                key = error_type or f"HTTP {status_code}"
                self._errors_by_type[key] = self._errors_by_type.get(key, 0) + 1

    def track_in_progress(self, delta: int) -> None:
        """调整进行中请求计数（开始 +1，结束 -1）。"""
        with self._lock:
            self._in_progress += delta

    def snapshot(self) -> dict[str, Any]:
        """导出当前所有指标的快照（用于 ``/metrics``）。"""
        with self._lock:
            # 累积直方图：把每个独立桶转成 le<=upper 的累积计数
            cumulative: list[int] = []
            running = 0
            for count in self._bucket_counts:
                running += count
                cumulative.append(running)

            buckets = [
                {"le": upper, "count": cumulative[i]}
                for i, upper in enumerate(self._buckets)
            ]
            buckets.append({"le": "+Inf", "count": cumulative[-1]})

            return {
                "requests_total": self._total,
                "in_progress": self._in_progress,
                "requests_by_status": dict(self._by_status),
                "requests_by_method": dict(self._by_method),
                "requests_by_path": dict(self._by_path),
                "errors_total": self._errors_total,
                "errors_by_status": dict(self._errors_by_status),
                "errors_by_type": dict(self._errors_by_type),
                "latency_ms": {
                    "count": self._latency_count,
                    "sum": round(self._latency_sum, 3),
                    "buckets": buckets,
                },
            }

    def reset(self) -> None:
        """清空所有指标（主要用于测试隔离）。"""
        with self._lock:
            self._total = 0
            self._in_progress = 0
            self._by_status: dict[int, int] = {}
            self._by_method: dict[str, int] = {}
            self._by_path: dict[str, int] = {}
            self._errors_total = 0
            self._errors_by_status: dict[int, int] = {}
            self._errors_by_type: dict[str, int] = {}
            self._latency_count = 0
            self._latency_sum = 0.0
            # 最后一个元素是 +Inf 桶
            self._bucket_counts = [0] * (len(self._buckets) + 1)


#: 全局单例。
registry = MetricsRegistry()


def get_metrics() -> MetricsRegistry:
    """返回全局指标注册表。"""
    return registry
