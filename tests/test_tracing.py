"""事后时间线测试：查询、后台/并发交错、容量淘汰边界、查不到的情况。"""

from __future__ import annotations

import concurrent.futures
import threading
import time

import pytest

from app.tracing import (
    DEFAULT_MAX_EVENTS_PER_TRACE,
    DEFAULT_MAX_TRACES,
    DEFAULT_TTL_SECONDS,
    TraceStore,
    trace_store,
)

HEADER = "X-Correlation-ID"


@pytest.fixture
def trace_limits():
    """用例内可临时调小容量，结束后恢复默认值并清空。"""
    yield trace_store
    trace_store.configure(
        max_traces=DEFAULT_MAX_TRACES,
        max_events_per_trace=DEFAULT_MAX_EVENTS_PER_TRACE,
        ttl_seconds=DEFAULT_TTL_SECONDS,
    )
    trace_store.reset()


# ---------- 时间线查询 ----------


def test_timeline_query_after_response(client) -> None:
    """请求结束后仍能按关联 ID 取回完整时间线与汇总字段。"""
    resp = client.get("/items/42", headers={HEADER: "tl-basic"})
    assert resp.status_code == 200

    result = client.get("/traces/tl-basic")
    assert result.status_code == 200
    body = result.json()
    assert body["found"] is True
    assert body["correlation_id"] == "tl-basic"
    # 汇总字段：开始/结束/总耗时/最终状态码/错误类型
    assert body["started_at"] and body["ended_at"]
    assert body["finished"] is True
    assert body["duration_ms"] is not None and body["duration_ms"] >= 0
    assert body["status_code"] == 200
    assert body["error_type"] is None
    # 事件：时间点/相对耗时/级别/消息/来源齐全，且按发生顺序排列
    assert body["event_count"] >= 2
    messages = [e["message"] for e in body["events"]]
    assert "fetching item" in messages
    assert "request completed" in messages
    offsets = [e["offset_ms"] for e in body["events"]]
    assert offsets == sorted(offsets)
    for e in body["events"]:
        assert e["timestamp"] and e["level"] and e["logger"]
        assert e["correlation_id"] == "tl-basic"


def test_timeline_records_exception_chain(client) -> None:
    """500 请求的时间线里能看到完整异常链、最终状态码与错误类型。"""
    client.get("/fail", headers={HEADER: "tl-fail"})
    body = client.get("/traces/tl-fail").json()
    assert body["found"] is True
    assert body["status_code"] == 500
    assert body["error_type"] == "app.routes.ServiceError"
    error_events = [e for e in body["events"] if e["level"] == "ERROR"]
    assert error_events, "时间线里必须有异常事件"
    chain = error_events[0]["exc_chain"]
    assert [c["type"].split(".")[-1] for c in chain] == [
        "ServiceError",
        "DatabaseError",
        "LookupError",
    ]


def test_platform_endpoints_are_traceable(client) -> None:
    """/healthz、/metrics 等平台入口同样能查到自己的时间线。"""
    client.get("/healthz", headers={HEADER: "tl-health"})
    body = client.get("/traces/tl-health").json()
    assert body["found"] is True
    assert body["status_code"] == 200
    assert body["path"] == "/healthz"


# ---------- 后台与并发交错 ----------


def test_background_events_join_origin_timeline(client) -> None:
    """响应后执行的后台工作，事件仍归到发起请求的时间线且顺序真实。"""
    client.post("/background", headers={HEADER: "tl-bg"})
    time.sleep(0.3)  # 等后台任务跑完
    body = client.get("/traces/tl-bg").json()
    assert body["found"] is True
    sources = [(e["logger"], e["message"]) for e in body["events"]]
    assert ("app.background", "background job started") in sources
    assert ("app.background", "background job finished") in sources
    # 后台事件发生在响应完成之后，序号必须排在访问日志之后
    access_seq = next(
        e["seq"] for e in body["events"] if e["message"] == "request completed"
    )
    bg_seqs = [
        e["seq"] for e in body["events"] if e["logger"] == "app.background"
    ]
    assert bg_seqs and all(s > access_seq for s in bg_seqs)
    offsets = [e["offset_ms"] for e in body["events"]]
    assert offsets == sorted(offsets)


def test_concurrent_requests_no_cross_contamination(client) -> None:
    """并发请求各自的时间线互不混入，事件关联 ID 全部对得上。"""
    cids = [f"tl-par-{i}" for i in range(20)]

    def hit(cid: str) -> None:
        resp = client.get("/slow", params={"ms": 20}, headers={HEADER: cid})
        assert resp.status_code == 200

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(hit, cids))

    for cid in cids:
        body = client.get(f"/traces/{cid}").json()
        assert body["found"] is True, cid
        assert body["event_count"] >= 1
        for e in body["events"]:
            assert e["correlation_id"] == cid
        offsets = [e["offset_ms"] for e in body["events"]]
        assert offsets == sorted(offsets)


def test_store_event_ordering_under_thread_contention() -> None:
    """多线程向同一时间线写事件：不丢、不重、序号即真实顺序。"""
    store = TraceStore(max_events_per_trace=10_000)
    store.begin("race", "GET", "/race")
    n_threads, n_events = 8, 50

    def writer(tid: int) -> None:
        for i in range(n_events):
            store.add_event("race", {"message": f"t{tid}-e{i}"})

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = store.get("race")
    assert snap is not None
    total = n_threads * n_events
    assert snap["event_count"] == total
    seqs = [e["seq"] for e in snap["events"]]
    assert seqs == list(range(1, total + 1))  # 连续无洞无重
    offsets = [e["offset_ms"] for e in snap["events"]]
    assert offsets == sorted(offsets)


# ---------- 容量与淘汰边界 ----------


def test_eviction_oldest_first(client, trace_limits) -> None:
    """超过条数上限按开立先后淘汰最旧的；被淘汰不影响指标计数。"""
    trace_store.configure(max_traces=2)
    for cid in ("tl-cap-1", "tl-cap-2", "tl-cap-3"):
        client.get("/", headers={HEADER: cid})

    # 直接查存储（HTTP 查询自身也会开立时间线，小容量下会干扰断言）
    assert trace_store.get("tl-cap-1") is None  # 最旧被淘汰
    assert trace_store.get("tl-cap-2") is not None
    assert trace_store.get("tl-cap-3") is not None

    # 淘汰只影响时间线存储，实时指标一条不少
    snap = client.get("/metrics").json()
    assert snap["requests_total"] == 3


def test_per_trace_event_cap(client, trace_limits) -> None:
    """单条时间线事件数超上限后停止追加并标记 truncated，内存有界。"""
    trace_store.configure(max_events_per_trace=1)
    client.get("/items/1", headers={HEADER: "tl-trunc"})
    body = client.get("/traces/tl-trunc").json()
    assert body["found"] is True
    assert body["event_count"] == 1
    assert body["truncated"] is True


def test_ttl_expiry(client, trace_limits) -> None:
    """超过保留时长的时间线被清理，再查返回 found: false。"""
    trace_store.configure(ttl_seconds=0.05)
    client.get("/", headers={HEADER: "tl-ttl"})
    assert client.get("/traces/tl-ttl").json()["found"] is True
    time.sleep(0.1)
    assert client.get("/traces/tl-ttl").json()["found"] is False


def test_events_dropped_after_eviction(client, trace_limits) -> None:
    """时间线被淘汰后，迟到的同名事件不会把它复活成半截数据。"""
    trace_store.configure(max_traces=1)
    client.get("/", headers={HEADER: "tl-gone"})
    client.get("/", headers={HEADER: "tl-other"})  # 挤掉 tl-gone
    trace_store.add_event("tl-gone", {"message": "late event"})
    assert client.get("/traces/tl-gone").json()["found"] is False


def test_reused_id_starts_fresh_timeline(client) -> None:
    """同一关联 ID 再次进入视为新请求，时间线重置而非叠加。"""
    client.get("/items/1", headers={HEADER: "tl-reuse"})
    first = client.get("/traces/tl-reuse").json()
    client.get("/items/2", headers={HEADER: "tl-reuse"})
    second = client.get("/traces/tl-reuse").json()
    assert second["found"] is True
    assert second["started_at"] >= first["started_at"]
    # 第二次只有一条 fetching item（item_id=2），不是两次叠加
    fetches = [e for e in second["events"] if e["message"] == "fetching item"]
    assert len(fetches) == 1
    assert fetches[0]["item_id"] == "2"


# ---------- 查不到的情况 ----------


def test_unknown_id_returns_distinguishable_not_found(client) -> None:
    """从未出现过的 ID：HTTP 200 + found:false，与正常结果明确区分。"""
    resp = client.get("/traces/never-existed")
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is False
    assert body["correlation_id"] == "never-existed"
    assert body["reason"] == "unknown_or_evicted"
    assert "events" not in body  # 不伪装成一条空时间线


def test_evicted_id_returns_not_found(client, trace_limits) -> None:
    """因超限被淘汰的 ID 与从未出现过的返回同一种清晰的未命中。"""
    trace_store.configure(max_traces=1)
    client.get("/", headers={HEADER: "tl-victim"})
    client.get("/", headers={HEADER: "tl-survivor"})
    body = client.get("/traces/tl-victim").json()
    assert body["found"] is False
    assert body["reason"] == "unknown_or_evicted"


def test_query_does_not_create_trace(client) -> None:
    """查询未命中的 ID 不会副作用般地开立空时间线。"""
    client.get("/traces/tl-ghost")
    again = client.get("/traces/tl-ghost").json()
    assert again["found"] is False
