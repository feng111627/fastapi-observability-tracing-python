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
    # 写入的 n_threads*n_events 条事件 + begin 固定写入的入口骨架事件
    total = n_threads * n_events + 1
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


# ---------- 查不到的情况（两种未命中可区分） ----------


def test_unknown_id_returns_distinguishable_not_found(client) -> None:
    """从未出现过的 ID：HTTP 200 + found:false + reason=unknown。"""
    resp = client.get("/traces/never-existed")
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is False
    assert body["correlation_id"] == "never-existed"
    assert body["reason"] == "unknown"
    assert "evicted_by" not in body
    assert "events" not in body  # 不伪装成一条空时间线


def test_evicted_id_returns_distinguishable_not_found(client, trace_limits) -> None:
    """容量超限被淘汰的 ID：found:false + reason=evicted，与从未出现可区分。"""
    trace_store.configure(max_traces=1)
    client.get("/", headers={HEADER: "tl-victim"})
    client.get("/", headers={HEADER: "tl-survivor"})
    body = client.get("/traces/tl-victim").json()
    assert body["found"] is False
    assert body["reason"] == "evicted"
    assert body["evicted_by"] == "capacity"
    # 与从未出现过的返回一眼可区分
    assert client.get("/traces/never-existed").json()["reason"] == "unknown"


def test_ttl_expired_id_returns_evicted_not_found(client, trace_limits) -> None:
    """保留时长过期被清理的 ID：reason=evicted 且 evicted_by=ttl_expired。"""
    trace_store.configure(ttl_seconds=0.05)
    client.get("/", headers={HEADER: "tl-ttl-gone"})
    assert client.get("/traces/tl-ttl-gone").json()["found"] is True
    time.sleep(0.1)
    body = client.get("/traces/tl-ttl-gone").json()
    assert body["found"] is False
    assert body["reason"] == "evicted"
    assert body["evicted_by"] == "ttl_expired"


# ---------- 首/尾骨架事件（无业务日志的请求也有完整时间线） ----------


def test_timeline_has_entry_and_exit_without_business_logs(client) -> None:
    """一条业务日志都没有的接口，时间线也有明确的入口与结束记录。"""
    resp = client.get("/echo-correlation-id", headers={HEADER: "tl-quiet"})
    assert resp.status_code == 200

    body = client.get("/traces/tl-quiet").json()
    assert body["found"] is True
    events = body["events"]
    # 首 + 访问日志（业务入口的固定一条）+ 尾，无任何业务日志
    assert body["event_count"] == 3
    assert [e["message"] for e in events] == [
        "request started",
        "request completed",
        "request finished",
    ]

    entry, exit_ = events[0], events[-1]
    # 入口记录：时间点、来源、方法、路径齐全，且排在最前
    assert entry["event_type"] == "request_started"
    assert entry["message"] == "request started"
    assert entry["logger"] == "app.timeline"
    assert entry["timestamp"] and entry["offset_ms"] == 0.0
    assert entry["method"] == "GET" and entry["path"] == "/echo-correlation-id"
    # 结束记录：能看到最终结果（状态码/耗时），序号在入口之后
    assert exit_["event_type"] == "request_finished"
    assert exit_["message"] == "request finished"
    assert exit_["logger"] == "app.timeline"
    assert exit_["status_code"] == 200
    assert exit_["duration_ms"] is not None and exit_["duration_ms"] >= 0
    assert exit_["seq"] > entry["seq"]


def test_platform_endpoints_have_entry_and_exit_events(client) -> None:
    """健康检查/指标导出这类平台入口同样留下首尾记录。"""
    client.get("/healthz", headers={HEADER: "tl-h"})
    client.get("/metrics", headers={HEADER: "tl-m"})
    for cid, path in (("tl-h", "/healthz"), ("tl-m", "/metrics")):
        body = client.get(f"/traces/{cid}").json()
        assert body["found"] is True, cid
        types = [e["event_type"] for e in body["events"]]
        assert types[0] == "request_started" and types[-1] == "request_finished"
        assert body["events"][0]["path"] == path
        assert body["status_code"] == 200


def test_failed_request_timeline_shows_final_result(client) -> None:
    """走异常的请求：结束记录与汇总里都能看到最终状态码与错误类型。"""
    client.get("/fail", headers={HEADER: "tl-err"})
    body = client.get("/traces/tl-err").json()
    assert body["found"] is True
    assert body["status_code"] == 500
    assert body["error_type"] == "app.routes.ServiceError"
    exit_events = [
        e for e in body["events"] if e.get("event_type") == "request_finished"
    ]
    assert len(exit_events) == 1
    assert exit_events[0]["status_code"] == 500
    assert exit_events[0]["error_type"] == "app.routes.ServiceError"


# ---------- 查询是纯读取：查自己不破坏原记录 ----------


def test_query_with_same_id_does_not_reset_timeline(client) -> None:
    """带着同一个关联 ID 去查它自己的时间线：原记录原样返回、后续可查。"""
    client.get("/items/7", headers={HEADER: "tl-self"})
    before = client.get("/traces/tl-self", headers={HEADER: "tl-self"}).json()
    assert before["found"] is True
    assert before["status_code"] == 200
    assert before["duration_ms"] is not None
    messages = [e["message"] for e in before["events"]]
    assert "fetching item" in messages and "request completed" in messages

    # 再查（仍带同一 ID）：内容必须和第一次查回来的一模一样，没被重置
    again = client.get("/traces/tl-self", headers={HEADER: "tl-self"}).json()
    assert again == before


def test_query_endpoint_itself_leaves_no_timeline(client) -> None:
    """查询入口自身不入时间线：带新 ID 的查询不会副作用地开立记录。"""
    client.get("/traces/whatever", headers={HEADER: "tl-query-only"})
    body = client.get("/traces/tl-query-only").json()
    assert body["found"] is False
    assert body["reason"] == "unknown"


def test_query_does_not_create_trace(client) -> None:
    """查询未命中的 ID 不会副作用般地开立空时间线。"""
    client.get("/traces/tl-ghost")
    again = client.get("/traces/tl-ghost").json()
    assert again["found"] is False


# ---------- 并发读写与淘汰同时发生的一致性 ----------


def test_consistent_snapshots_under_concurrent_io_and_eviction(
    client, trace_limits
) -> None:
    """并发请求 + 并发查询 + 小容量淘汰同时发生：读到的每份快照都完整自洽。"""
    trace_store.configure(max_traces=8)
    errors: list[str] = []
    stop = threading.Event()

    def writer(worker: int) -> None:
        for i in range(30):
            if stop.is_set():
                return
            cid = f"tl-mix-{worker}-{i}"
            resp = client.get("/slow", params={"ms": 1}, headers={HEADER: cid})
            if resp.status_code != 200:
                errors.append(f"request {cid} -> {resp.status_code}")

    def reader(worker: int) -> None:
        for i in range(30):
            if stop.is_set():
                return
            cid = f"tl-mix-{worker}-{i}"
            body = client.get(f"/traces/{cid}").json()
            if body["found"]:
                # 快照自洽：事件数与列表等长、序号严格递增、相对耗时不减、
                # 每条事件的关联 ID 都属于该时间线（不串号）
                events = body["events"]
                if body["event_count"] != len(events):
                    errors.append(f"{cid}: event_count 与列表长度不符")
                seqs = [e["seq"] for e in events]
                if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
                    errors.append(f"{cid}: 序号不递增或有重复")
                offsets = [e["offset_ms"] for e in events]
                if offsets != sorted(offsets):
                    errors.append(f"{cid}: offset_ms 非单调")
                if any(e["correlation_id"] != cid for e in events):
                    errors.append(f"{cid}: 混入其他请求的事件")
                # 已结束的时间线必须带结束骨架事件
                if body["finished"] and not any(
                    e.get("event_type") == "request_finished" for e in events
                ):
                    errors.append(f"{cid}: 已结束但缺结束记录")
            elif body["reason"] not in ("unknown", "evicted"):
                errors.append(f"{cid}: 非法未命中原因 {body['reason']}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        futures = []
        for w in range(4):
            futures.append(pool.submit(writer, w))
            futures.append(pool.submit(reader, w))
        for f in futures:
            f.result()
    stop.set()
    assert not errors, errors[:5]
