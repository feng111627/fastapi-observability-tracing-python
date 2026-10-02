"""指标与健康检查测试：并发计数、进行中请求、错误分类、耗时分布。"""

from __future__ import annotations

import asyncio
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.metrics import MetricsRegistry, registry


def test_health_endpoint(client: httpx.Client) -> None:
    """健康检查返回 200 与 ok。"""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_requests_total_and_status_breakdown(client: httpx.Client) -> None:
    """请求总数与按状态码分布正确。"""
    client.get("/health")
    client.get("/health")
    client.get("/definitely-not-found")

    snap = registry.snapshot()
    assert snap["requests_total"] == 3
    assert snap["requests_by_status"][200] == 2
    assert snap["requests_by_status"][404] == 1
    assert snap["requests_by_method"]["GET"] == 3
    assert snap["requests_by_path"]["/health"] == 2


def test_errors_are_classified_by_type(client: httpx.Client) -> None:
    """错误按异常类型区分，不能一律算成一类。"""
    # ValueError：/boom 里层是 ValueError 但最外层是 RuntimeError
    client.get("/boom")
    client.get("/boom")
    client.get("/flaky", params={"fail": "1"})  # ValueError 最外层
    client.get("/definitely-not-found")  # 404 无异常对象

    snap = registry.snapshot()
    assert snap["errors_total"] == 4
    assert snap["errors_by_type"]["builtins.RuntimeError"] == 2
    assert snap["errors_by_type"]["builtins.ValueError"] == 1
    assert snap["errors_by_type"]["HTTP 404"] == 1
    assert snap["errors_by_status"][500] == 3
    assert snap["errors_by_status"][404] == 1


def test_latency_histogram_and_sum(client: httpx.Client) -> None:
    """耗时直方图为累积分布，count 与请求总数一致，sum 非负。"""
    for _ in range(5):
        client.get("/health")

    latency = registry.snapshot()["latency_ms"]
    assert latency["count"] == 5
    assert latency["sum"] >= 0
    counts = [b["count"] for b in latency["buckets"]]
    # 累积桶单调不减
    assert counts == sorted(counts)
    assert counts[-1] == 5  # +Inf 桶包含全部请求


@pytest.mark.parametrize("threads,per_thread", [(8, 200), (16, 250)])
def test_concurrent_request_metrics_are_accurate(
    threads: int, per_thread: int
) -> None:
    """多线程并发请求后，总数、错误数、进行中计数必须精确无丢失。"""
    registry.reset()

    def worker() -> None:
        local = TestClient(app, raise_server_exceptions=False)
        try:
            for i in range(per_thread):
                if i % 5 == 0:
                    local.get("/flaky", params={"fail": "1"})
                else:
                    local.get("/health")
        finally:
            local.close()

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    total = threads * per_thread
    snap = registry.snapshot()
    assert snap["requests_total"] == total
    assert snap["in_progress"] == 0
    expected_errors = threads * (per_thread // 5)
    assert snap["errors_total"] == expected_errors
    assert snap["latency_ms"]["count"] == total
    assert snap["latency_ms"]["buckets"][-1]["count"] == total
    assert sum(snap["errors_by_type"].values()) == expected_errors


def test_in_progress_gauge_returns_to_zero_under_async_concurrency() -> None:
    """异步并发过程中进行中计数 >=1，结束后归零，且不会变成负数。"""
    registry.reset()

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://local",
        ) as ac:
            async def one() -> None:
                await ac.get("/concurrent", params={"delay_ms": 30})

            await asyncio.gather(*(one() for _ in range(30)))

    asyncio.run(run())
    snap = registry.snapshot()
    assert snap["requests_total"] == 30
    assert snap["in_progress"] == 0


def test_in_progress_gauge_is_positive_during_flight() -> None:
    """请求处理进行中时，in_progress 应反映当前未完成请求数。"""
    registry.reset()

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://local",
        ) as ac:
            async def one() -> None:
                await ac.get("/concurrent", params={"delay_ms": 50})

            task = asyncio.gather(*(one() for _ in range(10)))
            await asyncio.sleep(0.02)  # 等请求都进入处理阶段
            assert registry.snapshot()["in_progress"] == 10
            await task

    asyncio.run(run())
    assert registry.snapshot()["in_progress"] == 0


def test_metrics_endpoint_serves_snapshot(client: httpx.Client) -> None:
    """/metrics 返回与注册表一致的快照。"""
    client.get("/health")
    body = client.get("/metrics").json()
    assert body["requests_total"] >= 1
    assert "latency_ms" in body
    assert "in_progress" in body


def test_registry_is_thread_safe_in_isolation() -> None:
    """直接压测注册表：16 线程 x 1000 次记录不丢计数。"""
    reg = MetricsRegistry()

    def worker() -> None:
        for _ in range(1000):
            reg.track_in_progress(1)
            reg.record_request("GET", "/x", 200, 1.0)
            reg.track_in_progress(-1)

    ts = [threading.Thread(target=worker) for _ in range(16)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    snap = reg.snapshot()
    assert snap["requests_total"] == 16000
    assert snap["in_progress"] == 0
