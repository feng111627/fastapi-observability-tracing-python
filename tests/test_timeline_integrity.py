"""时间线完整性回归：首尾结构事件、查询纯读取、未命中原因区分、并发一致快照。

覆盖本轮修复的四类问题：
1. 没有任何业务日志的请求（/healthz、/metrics、只算东西的接口）也必须
   在时间线里留下固定的「请求进入 / 请求结束」记录；异常请求的最终结果
   同样可见；
2. 查询是纯读取：带着某条请求的关联 ID 去查它自己，不会新建、重置或
   覆盖已归档的时间线；
3. 「从未出现」与「已被淘汰」两种未命中返回可区分的原因；
4. 并发读写与容量淘汰同时发生时，读到的每份快照都完整一致。
"""

from __future__ import annotations

import concurrent.futures
import threading

import pytest

from app.tracing import (
    DEFAULT_MAX_EVENTS_PER_TRACE,
    DEFAULT_MAX_TRACES,
    DEFAULT_TTL_SECONDS,
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


def _assert_boundary_events(body: dict, cid: str) -> None:
    """校验一条时间线具备完整的首尾结构事件且整体有序。"""
    events = body["events"]
    assert len(events) >= 2, "至少应有「请求进入」与「请求结束」两条结构事件"

    first, last = events[0], events[-1]
    assert first["event_kind"] == "request_started"
    assert first["logger"] == "app.timeline"
    assert first["message"] == "request started"
    assert first["correlation_id"] == cid
    assert first["timestamp"] and first["seq"] >= 1
    assert first["offset_ms"] == 0

    assert last["event_kind"] == "request_finished"
    assert last["message"] == "request finished"
    assert last["correlation_id"] == cid
    assert last["status_code"] == body["status_code"]
    assert last["seq"] > first["seq"]
    assert last["offset_ms"] >= first["offset_ms"]

    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    offsets = [e["offset_ms"] for e in events]
    assert offsets == sorted(offsets)


# ---------- 首尾记录：没有业务日志的请求也有完整的入口与结束 ----------


def test_healthz_timeline_has_boundary_events(client) -> None:
    """健康检查全程无业务日志，时间线仍固定有入口与结束记录。"""
    client.get("/healthz", headers={HEADER: "bd-health"})
    body = client.get("/traces/bd-health").json()
    assert body["found"] is True
    assert body["finished"] is True
    assert body["status_code"] == 200
    _assert_boundary_events(body, "bd-health")
    # 该平台端点本身不打业务日志，时间线里恰好只有两条结构事件
    assert body["event_count"] == 2


def test_metrics_endpoint_timeline_has_boundary_events(client) -> None:
    """指标导出入口同样能在自己的时间线里看到首尾。"""
    client.get("/metrics", headers={HEADER: "bd-metrics"})
    body = client.get("/traces/bd-metrics").json()
    assert body["found"] is True
    assert body["path"] == "/metrics"
    _assert_boundary_events(body, "bd-metrics")


def test_compute_only_endpoint_has_boundary_events(client) -> None:
    """只算东西、不打业务日志的接口，首尾记录同样完整。"""
    client.get("/echo-correlation-id", headers={HEADER: "bd-echo"})
    body = client.get("/traces/bd-echo").json()
    assert body["found"] is True
    _assert_boundary_events(body, "bd-echo")


def test_exception_request_shows_final_result(client) -> None:
    """走异常的请求：结束记录里能看到最终状态码与错误类型。"""
    client.get("/fail", headers={HEADER: "bd-fail"})
    body = client.get("/traces/bd-fail").json()
    assert body["found"] is True
    _assert_boundary_events(body, "bd-fail")
    last = body["events"][-1]
    assert last["status_code"] == 500
    assert last["error_type"] == "app.routes.ServiceError"
    assert last["level"] == "ERROR"
    assert last["duration_ms"] is not None and last["duration_ms"] >= 0


# ---------- 查询是纯读取：不新建、不重置、不覆盖 ----------


def test_query_with_same_id_preserves_record(client) -> None:
    """带着某条请求自己的关联 ID 去查它：记录原样返回，绝不重置。"""
    client.get("/items/7", headers={HEADER: "self-ref"})
    first = client.get(
        "/traces/self-ref", headers={HEADER: "self-ref"}
    ).json()
    assert first["found"] is True
    assert first["status_code"] == 200
    assert first["duration_ms"] is not None
    messages = [e["message"] for e in first["events"]]
    assert "fetching item" in messages
    _assert_boundary_events(first, "self-ref")

    # 再查（仍带同一 ID），内容必须与第一次完全一致，没有任何丢失
    second = client.get(
        "/traces/self-ref", headers={HEADER: "self-ref"}
    ).json()
    assert second == first


def test_query_does_not_create_or_reset_any_trace(client) -> None:
    """查询请求自身的关联 ID 不会副作用般地开立空时间线。"""
    resp = client.get("/traces/ghost-target", headers={HEADER: "ghost-query"})
    assert resp.json()["found"] is False
    # 查询目标没被创建，查询自身的 ID 同样没有留下任何时间线
    assert trace_store.get("ghost-target") is None
    assert trace_store.get("ghost-query") is None
    again = client.get("/traces/ghost-target", headers={HEADER: "ghost-query"})
    assert again.json()["reason"] == "unknown"


def test_query_response_still_carries_correlation_id(client) -> None:
    """纯读取不影响关联 ID 的沿用与响应头回传规则。"""
    resp = client.get("/traces/whatever", headers={HEADER: "qid-1"})
    assert resp.headers[HEADER] == "qid-1"


# ---------- 两种未命中可区分 ----------


def test_miss_reasons_are_distinguishable(client, trace_limits) -> None:
    """从未出现 / 容量挤出 / 过期清理三种情况两两可区分，且都不是错误。"""
    # 从未出现
    unknown = client.get("/traces/miss-unknown")
    assert unknown.status_code == 200
    assert unknown.json()["found"] is False
    assert unknown.json()["reason"] == "unknown"
    assert "evicted_by" not in unknown.json()

    # 容量挤出
    trace_store.configure(max_traces=1)
    client.get("/", headers={HEADER: "miss-cap"})
    client.get("/", headers={HEADER: "miss-cap-other"})
    capacity = client.get("/traces/miss-cap").json()
    assert capacity["found"] is False
    assert capacity["reason"] == "evicted"
    assert capacity["evicted_by"] == "capacity"

    # 过期清理
    trace_store.configure(max_traces=DEFAULT_MAX_TRACES, ttl_seconds=0.05)
    client.get("/", headers={HEADER: "miss-ttl"})
    assert client.get("/traces/miss-ttl").json()["found"] is True
    import time

    time.sleep(0.1)
    ttl = client.get("/traces/miss-ttl").json()
    assert ttl["found"] is False
    assert ttl["reason"] == "evicted"
    assert ttl["evicted_by"] == "ttl"

    # 正常命中与两种未命中一眼区分
    client.get("/", headers={HEADER: "miss-hit"})
    hit = client.get("/traces/miss-hit").json()
    assert hit["found"] is True
    assert hit["events"]


# ---------- 并发读写与淘汰同时发生：快照一致 ----------


def test_consistent_snapshots_under_concurrent_io_and_eviction(
    client, trace_limits
) -> None:
    """并发写入、查询、容量淘汰同时进行：读到的每份快照都完整一致。"""
    trace_store.configure(max_traces=16)
    n_writers, reqs_per_writer = 6, 12
    stop_readers = threading.Event()
    failures: list[str] = []
    failures_lock = threading.Lock()

    def check_snapshot(cid: str, snap: dict | None) -> None:
        if snap is None:
            return
        try:
            assert snap["correlation_id"] == cid
            events = snap["events"]
            assert events, "快照不应是空时间线"
            assert events[0]["event_kind"] == "request_started"
            for e in events:
                assert e["correlation_id"] == cid, "串号"
            seqs = [e["seq"] for e in events]
            assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
            offsets = [e["offset_ms"] for e in events]
            assert offsets == sorted(offsets)
            if snap["finished"]:
                # 无后台工作的请求，结束时「请求结束」必是最后一条
                assert events[-1]["event_kind"] == "request_finished"
                assert events[-1]["status_code"] == snap["status_code"]
        except AssertionError as exc:  # 汇集失败而不是淹没在线程异常里
            with failures_lock:
                failures.append(f"{cid}: {exc}")

    def writer(wid: int) -> None:
        for i in range(reqs_per_writer):
            cid = f"cc-w{wid}-{i}"
            resp = client.get("/", headers={HEADER: cid})
            assert resp.status_code == 200

    def reader() -> None:
        n = 0
        while not stop_readers.is_set():
            cid = f"cc-w{n % n_writers}-{(n // n_writers) % reqs_per_writer}"
            check_snapshot(cid, trace_store.get(cid))
            # HTTP 查询自身不得产生/破坏任何时间线
            body = client.get(f"/traces/{cid}", headers={HEADER: cid}).json()
            if body["found"]:
                snap = dict(body)
                snap.pop("found")
                check_snapshot(cid, snap)
            else:
                assert body["reason"] in ("unknown", "evicted")
            n += 1

    readers = [threading.Thread(target=reader) for _ in range(3)]
    for t in readers:
        t.start()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_writers) as pool:
        list(pool.map(writer, range(n_writers)))
    stop_readers.set()
    for t in readers:
        t.join()

    assert not failures, f"并发下读到不一致快照: {failures[:5]}"
