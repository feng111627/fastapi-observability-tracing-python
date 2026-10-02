"""指标测试：计数准确、错误按类型/状态分类、耗时分布、进行中请求数、并发准确。"""

from __future__ import annotations

import concurrent.futures
import threading

from app.metrics import DEFAULT_BUCKETS, Metrics


def test_healthz(client) -> None:
    """健康检查入口可用，且不计入业务指标。"""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    snap = client.get("/metrics").json()
    assert snap["requests_total"] == 0


def test_metrics_endpoint_shape(client) -> None:
    """/metrics 输出包含题目要求的四类指标且结构合法。"""
    client.get("/")
    snap = client.get("/metrics").json()
    assert snap["requests_total"] == 1
    assert "errors_total_by_status" in snap
    assert "errors_total_by_type" in snap
    assert "inflight_requests" in snap
    hist = snap["latency_seconds_all"]
    assert hist["count"] == 1
    assert hist["le_inf"] == 1
    assert hist["buckets"] == list(DEFAULT_BUCKETS)
    assert len(hist["cumulative_counts"]) == len(DEFAULT_BUCKETS)
    assert hist["sum"] > 0


def test_errors_classified_by_status_and_type(client) -> None:
    """500 同时按状态码和异常类型分类；不同异常不被混成一类。"""
    client.get("/fail")  # ServiceError 500
    client.get("/fail-handled")  # 400，不计服务端错误
    snap = client.get("/metrics").json()

    assert snap["requests_total"] == 2
    assert snap["errors_total_by_status"] == {"500": 1}
    assert snap["errors_total_by_type"] == {"app.routes.ServiceError": 1}
    assert snap["inflight_requests"] == 0


def test_route_template_labels(client) -> None:
    """指标按路由模板聚合，而不是按每个具体 URL 产生高基数标签。"""
    for item in ("a", "b", "c"):
        client.get(f"/items/{item}")
    snap = client.get("/metrics").json()
    assert snap["requests_by_route"] == {"GET /items/{item_id}": 3}
    assert "GET /items/a" not in str(snap["requests_by_route"])
    assert "GET /items/{item_id}" in snap["latency_seconds_by_route"]


def test_latency_histogram_buckets(client) -> None:
    """耗时样本进入正确的累计桶。"""
    client.get("/slow?ms=20")  # 0.02s -> 落入 0.025 桶
    snap = client.get("/metrics").json()
    hist = snap["latency_seconds_all"]
    assert hist["count"] == 1
    # 0.02 <= 0.025，从 0.025 桶开始累计为 1
    idx_0025 = hist["buckets"].index(0.025)
    idx_001 = hist["buckets"].index(0.01)
    assert hist["cumulative_counts"][idx_001] == 0
    assert hist["cumulative_counts"][idx_0025] == 1
    assert hist["cumulative_counts"][-1] == 1  # 最大桶
    assert hist["le_inf"] == 1


def test_inflight_counter_returns_to_zero(client) -> None:
    """请求结束后进行中计数归零。"""
    for _ in range(5):
        client.get("/slow?ms=5")
    snap = client.get("/metrics").json()
    assert snap["inflight_requests"] == 0


def test_inflight_observed_during_request() -> None:
    """用 Metrics 原语直接验证 inc/dec 在重叠调用期间的进行中计数。"""
    m = Metrics()
    m.inc_inflight()
    m.inc_inflight()
    assert m.inflight == 2
    m.dec_inflight()
    assert m.inflight == 1
    m.dec_inflight()
    assert m.inflight == 0
    # 不会减成负数
    m.dec_inflight()
    assert m.inflight == 0


def test_concurrent_counter_accuracy() -> None:
    """多线程高并发下请求/错误计数不丢失、不串错（核心并发场景）。

    模拟「事件循环线程 + 后台任务线程池」同时写指标的真实情况：
    直接用多线程对 Metrics 原语做高频混合写入。
    """
    m = Metrics()
    threads = 8
    per_thread = 500

    def worker(tid: int) -> None:
        for i in range(per_thread):
            m.inc_inflight()
            is_error = i % 5 == 0
            m.record_request(
                method="POST" if tid % 2 else "GET",
                path="/work" if is_error else "/items/{id}",
                status_code=500 if is_error else 200,
                duration=0.001 * (i % 10),
                error_type="RuntimeError" if is_error else None,
            )
            m.dec_inflight()

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(worker, range(threads)))

    total = threads * per_thread
    errors = threads * sum(1 for i in range(per_thread) if i % 5 == 0)
    snap = m.snapshot()

    assert snap["requests_total"] == total
    assert sum(snap["requests_by_route"].values()) == total
    assert snap["inflight_requests"] == 0
    assert snap["latency_seconds_all"]["count"] == total
    assert snap["latency_seconds_all"]["le_inf"] == total
    assert sum(snap["errors_total_by_status"].values()) == errors
    assert snap["errors_total_by_status"]["500"] == errors
    assert snap["errors_total_by_type"]["RuntimeError"] == errors


def test_concurrent_requests_via_http_counter_accuracy(client) -> None:
    """通过真实 HTTP 接口并发请求，验证端到端计数（成功 + 失败混合）。"""
    n_ok = 30
    n_fail = 15

    def ok_request(_: int) -> int:
        return client.get("/slow?ms=10").status_code

    def fail_request(_: int) -> int:
        return client.get("/fail").status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        oks = list(pool.map(ok_request, range(n_ok)))
        fails = list(pool.map(fail_request, range(n_fail)))

    assert oks == [200] * n_ok
    assert fails == [500] * n_fail

    snap = client.get("/metrics").json()
    assert snap["requests_total"] == n_ok + n_fail
    assert snap["errors_total_by_status"]["500"] == n_fail
    assert snap["errors_total_by_type"]["app.routes.ServiceError"] == n_fail
    assert snap["inflight_requests"] == 0
    assert snap["latency_seconds_all"]["count"] == n_ok + n_fail


def test_inflight_during_overlapping_http_requests(client) -> None:
    """多个慢请求重叠期间，独立采样线程应观察到进行中计数 >= 2，最终归零。"""
    observed: list[int] = []
    start = threading.Event()
    stop = threading.Event()

    def slow_call() -> None:
        start.wait()
        client.get("/slow?ms=150")

    def sampler() -> None:
        start.wait()
        while not stop.is_set():
            observed.append(client.get("/metrics").json()["inflight_requests"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        slow_futs = [pool.submit(slow_call) for _ in range(4)]
        sample_fut = pool.submit(sampler)
        start.set()
        for f in slow_futs:
            f.result()
        stop.set()
        sample_fut.result()

    assert max(observed) >= 2, f"重叠期间未观察到并发进行中计数: {observed}"
    assert client.get("/metrics").json()["inflight_requests"] == 0
