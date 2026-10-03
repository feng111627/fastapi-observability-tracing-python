"""事后可回溯时间线测试。

覆盖：
- 按关联 ID 查询时间线（结构、起止/耗时/状态码/错误类型、事件顺序与字段）；
- 查不到的两种情况：从未出现（404）与已淘汰（410），与正常结果可区分；
- 后台任务/并发交错：事件归到发起方 ID、真实顺序不乱序、不串号；
- 容量与保留时长淘汰边界（FIFO、TTL、墓碑再过期）；
- 任何入口（含 /healthz、/metrics、/traces 自身）都能查到时间线；
- 敏感信息在时间线中同样脱敏；高并发写入/查询/淘汰同时进行不串号。
"""

from __future__ import annotations

import concurrent.futures
import json
import threading

from app.tracing import TraceStore, trace_store

HEADER = "X-Correlation-ID"


# --------------------------------------------------------------------------- #
# 1. 时间线查询：结构与内容
# --------------------------------------------------------------------------- #
def test_timeline_found_after_response(client) -> None:
    """正常请求响应完成后仍可按关联 ID 取回完整时间线。"""
    resp = client.get("/items/42", headers={HEADER: "tl-found-1"})
    assert resp.status_code == 200

    found = client.get("/traces/tl-found-1")
    assert found.status_code == 200
    data = found.json()

    # 整条线的摘要
    assert data["correlation_id"] == "tl-found-1"
    assert data["method"] == "GET"
    assert data["path"] == "/items/42"
    assert data["status"] == "completed"
    assert data["status_code"] == 200
    assert data["error_type"] is None
    assert data["started_at"] is not None
    assert data["finished_at"] is not None
    assert data["finished_at"] >= data["started_at"]
    assert isinstance(data["duration_ms"], (int, float)) and data["duration_ms"] >= 0
    assert data["event_count"] == len(data["events"]) >= 3

    kinds = [e["kind"] for e in data["events"]]
    assert kinds[0] == "request_start"
    assert kinds[-1] == "request_end"
    assert "log" in kinds


def test_each_event_has_required_fields_and_is_ordered(client) -> None:
    """每条事件带时间点/相对耗时/级别/消息/来源，且按真实顺序（seq）排列。"""
    client.get("/items/9", headers={HEADER: "tl-fields-1"})
    data = client.get("/traces/tl-fields-1").json()

    seqs: list[int] = []
    for event in data["events"]:
        for field in (
            "seq", "timestamp", "elapsed_ms", "level", "message",
            "component", "kind", "fields",
        ):
            assert field in event, field
        assert isinstance(event["timestamp"], str) and "T" in event["timestamp"]
        assert isinstance(event["elapsed_ms"], (int, float))
        assert event["elapsed_ms"] >= 0
        assert event["level"] in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        assert event["message"]
        assert event["component"]  # 来自哪一块（middleware / app.routes ...）
        seqs.append(event["seq"])

    # 顺序必须严格递增（真实发生顺序，不乱序、不丢）
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    # 相对耗时单调不减
    elapsed = [e["elapsed_ms"] for e in data["events"]]
    assert elapsed == sorted(elapsed)


def test_timeline_captures_business_log_extra_fields(client) -> None:
    """业务日志通过 extra 传的结构化字段进入事件 fields。"""
    client.get("/items/abc", headers={HEADER: "tl-extra-1"})
    data = client.get("/traces/tl-extra-1").json()
    business = [e for e in data["events"] if e["component"] == "app.routes"]
    assert business and business[0]["fields"].get("item_id") == "abc"


def test_error_request_timeline_status_and_error_type(client) -> None:
    """500 请求的时间线标记 error 状态、状态码与错误类型，并含异常事件。"""
    resp = client.get("/fail", headers={HEADER: "tl-err-1"})
    assert resp.status_code == 500

    data = client.get("/traces/tl-err-1").json()
    assert data["status"] == "error"
    assert data["status_code"] == 500
    assert data["error_type"] == "app.routes.ServiceError"

    end = data["events"][-1]
    assert end["kind"] == "request_end"
    assert end["level"] == "ERROR"
    assert end["fields"]["error_type"] == "app.routes.ServiceError"
    # 异常日志事件带着异常类型/消息（完整 traceback 仍只在实时日志里）
    err_events = [e for e in data["events"] if e["fields"].get("exc_type")]
    assert err_events
    assert err_events[0]["fields"]["exc_type"] == "app.routes.ServiceError"
    assert "get_item service failed" in err_events[0]["fields"]["exc_message"]


def test_handled_4xx_is_completed_not_server_error(client) -> None:
    """4xx 已处理错误：时间线保留 400，但不算服务端 error 状态。"""
    resp = client.get("/fail-handled", headers={HEADER: "tl-400-1"})
    assert resp.status_code == 400
    data = client.get("/traces/tl-400-1").json()
    assert data["status_code"] == 400
    assert data["status"] == "completed"
    assert data["error_type"] is None
    assert data["events"][-1]["level"] == "WARNING"


def test_in_flight_timeline_visible_before_finish() -> None:
    """请求仍在处理中时，也能看到截至目前已发生的事件，状态为 in_flight。"""
    store = TraceStore(max_traces=10)
    store.begin("in-flight-1", "GET", "/slow")
    store.add_event("in-flight-1", "INFO", "working", "app.routes")

    status, payload = store.get_timeline("in-flight-1")
    assert status == "found"
    assert payload is not None
    assert payload["status"] == "in_flight"
    assert payload["status_code"] is None
    assert payload["finished_at"] is None
    assert payload["event_count"] == 2

    store.finish("in-flight-1", 200, 3.0, None)
    _, payload = store.get_timeline("in-flight-1")
    assert payload is not None and payload["status"] == "completed"


# --------------------------------------------------------------------------- #
# 2. 查不到：404（从未出现）/ 410（已淘汰）
# --------------------------------------------------------------------------- #
def test_unknown_correlation_id_returns_404(client) -> None:
    resp = client.get("/traces/there-is-no-such-id")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "trace_not_found"
    assert body["correlation_id"] == "there-is-no-such-id"


def test_evicted_correlation_id_returns_410(client) -> None:
    """超出容量被淘汰的 ID 返回 410，且与 404/200 明确可区分。"""
    trace_store.configure(max_traces=2)
    client.get("/", headers={HEADER: "cap-old-1"})
    client.get("/", headers={HEADER: "cap-old-2"})
    client.get("/", headers={HEADER: "cap-new-3"})  # 挤掉 cap-old-1

    # 直接走存储判定边界（HTTP 查询自身也会录制时间线，故不在极小容量下
    # 用 HTTP 断言“仍保留”，避免观测请求自身占槽造成干扰）
    assert trace_store.get_timeline("cap-old-1")[0] == "evicted"
    assert trace_store.get_timeline("cap-old-1-but-never")[0] == "not_found"
    assert trace_store.get_timeline("cap-new-3")[0] == "found"

    # HTTP 层：被淘汰返回 410 且是结构化 JSON
    gone = client.get("/traces/cap-old-2")
    assert gone.status_code == 410
    body = gone.json()
    assert body["error"] == "trace_evicted"
    assert body["correlation_id"] == "cap-old-2"

    # HTTP 层：从未出现返回 404
    assert client.get("/traces/cap-old-1-but-never").status_code == 404


def test_evicted_tombstone_expires_to_not_found() -> None:
    """墓碑也随 TTL 过期后，被淘汰 ID 最终与从未出现一样返回 404（但不报错）。"""
    store = TraceStore(max_traces=1, ttl_seconds=0.05, max_evicted=100)
    store.begin("will-expire", "GET", "/")
    store.finish("will-expire", 200, 1.0, None)
    store.begin("replacement", "GET", "/")  # 挤掉前者 -> 墓碑
    assert store.get_timeline("will-expire")[0] == "evicted"

    import time as _time

    _time.sleep(0.08)
    assert store.get_timeline("will-expire")[0] == "not_found"
    assert store.get_timeline("never")[0] == "not_found"


# --------------------------------------------------------------------------- #
# 3. 后台任务与并发交错
# --------------------------------------------------------------------------- #
def test_background_events_attached_to_origin_timeline(client) -> None:
    """响应后才执行的后台任务，事件仍挂在发起请求的时间线且排在结束事件之后。"""
    resp = client.post("/background", headers={HEADER: "bg-tl-1"})
    assert resp.status_code == 200

    data = client.get("/traces/bg-tl-1").json()
    messages = [e["message"] for e in data["events"]]
    assert "background job started" in messages
    assert "background job finished" in messages

    bg_events = [e for e in data["events"] if e["component"] == "app.background"]
    assert [e["fields"]["phase"] for e in bg_events] == ["start", "done"]
    # 后台事件全部属于发起方 ID（响应结束后才发生，elapsed 更大）
    end_idx = messages.index("request finished")
    for bg in bg_events:
        assert messages.index(bg["message"]) > end_idx
    assert bg_events[-1]["elapsed_ms"] >= data["duration_ms"]


def test_concurrent_interleaving_keeps_order_and_isolation(client) -> None:
    """多个请求交错执行：每个时间线只含自己的事件，seq 全局不乱、不丢。

    不用 Barrier（线程池 worker 数小于任务数时 Barrier 会死锁）；
    每个请求都打 /slow 让出事件循环，制造自然交错窗口。
    """
    n = 30

    def hit(i: int) -> str:
        cid = f"interleave-{i:03d}"
        client.get("/slow?ms=15", headers={HEADER: cid})
        return cid

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        cids = list(pool.map(hit, range(n)))

    for cid in cids:
        data = client.get(f"/traces/{cid}").json()
        seqs = [e["seq"] for e in data["events"]]
        assert seqs == sorted(seqs), f"{cid} 事件乱序"
        assert len(seqs) == len(set(seqs)), f"{cid} 事件丢失/重复"
        # 时间线里所有事件都来自这条请求本身：没有别的 ID 的业务内容
        # （事件本身按桶存储；通过业务消息特征验证不串号）
        messages = " ".join(e["message"] for e in data["events"])
        for other in cids:
            if other != cid:
                assert other not in messages
        assert data["status_code"] == 200


def test_store_concurrent_writes_queries_and_eviction() -> None:
    """写入/查询/淘汰同时高并发发生：读不到半截数据、不错乱、不串号。"""
    store = TraceStore(max_traces=50, ttl_seconds=3600)
    stop = threading.Event()
    errors: list[str] = []

    def writer(wid: int) -> None:
        for i in range(300):
            cid = f"w{wid}-e{i}"
            store.begin(cid, "GET", "/work")
            for _ in range(3):
                store.add_event(cid, "INFO", "step", "app.routes")
            store.finish(cid, 200, 0.5, None)

    def reader() -> None:
        i = 0
        while not stop.is_set():
            cid = f"w{i % 8}-e{i % 50}"
            try:
                status, payload = store.get_timeline(cid)
                if status == "found":
                    # 快照必须完整自洽、可 JSON 序列化（无半截数据）
                    assert payload["event_count"] == len(payload["events"])
                    seqs = [e["seq"] for e in payload["events"]]
                    assert seqs == sorted(seqs)
                    json.dumps(payload, default=str)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"reader: {exc!r}")
            i += 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(writer, w) for w in range(8)]
        rf = pool.submit(reader)
        for f in futures:
            f.result()
        stop.set()
        rf.result()

    assert not errors, errors
    # 容量始终被严格约束
    assert store.stats()["stored_traces"] <= 50


def test_background_events_not_mixed_with_intruder(client) -> None:
    """后台任务执行期间插入别的请求，后台事件不会串到插入请求的时间线上。"""
    client.post("/background", headers={HEADER: "bg-origin-x"})
    client.get("/echo-correlation-id", headers={HEADER: "bg-intruder-x"})
    client.get("/slow?ms=120")  # 等后台任务跑完

    origin = client.get("/traces/bg-origin-x").json()
    intruder = client.get("/traces/bg-intruder-x").json()

    assert any(
        e["component"] == "app.background" for e in origin["events"]
    )
    assert not any(
        e["component"] == "app.background" for e in intruder["events"]
    )
    # 插入者的时间线里只应有自己这一条请求的入口/出口
    assert intruder["path"] == "/echo-correlation-id"


# --------------------------------------------------------------------------- #
# 4. 容量 / TTL 淘汰边界
# --------------------------------------------------------------------------- #
def test_capacity_evicts_oldest_first_unit() -> None:
    """精确边界：容量为 N 时保留最新 N 条，最旧的先淘汰。"""
    store = TraceStore(max_traces=3)
    for i in range(5):
        store.begin(f"c{i}", "GET", "/")
        store.finish(f"c{i}", 200, 1.0, None)

    assert store.stats()["stored_traces"] == 3
    assert store.get_timeline("c0")[0] == "evicted"
    assert store.get_timeline("c1")[0] == "evicted"
    for kept in ("c2", "c3", "c4"):
        assert store.get_timeline(kept)[0] == "found"


def test_ttl_expires_old_traces_unit() -> None:
    """超过保留时长的时间线在下次写入/查询时被惰性淘汰。"""
    store = TraceStore(max_traces=100, ttl_seconds=0.05)
    store.begin("stale", "GET", "/")
    store.finish("stale", 200, 1.0, None)

    import time as _time

    _time.sleep(0.08)
    # 新写入触发惰性清理
    store.begin("fresh", "GET", "/")
    assert store.get_timeline("stale")[0] == "evicted"
    assert store.get_timeline("fresh")[0] == "found"


def test_eviction_does_not_touch_metrics_or_realtime_logs(client, caplogs) -> None:
    """被淘汰只影响事后查询：实时日志仍在，指标口径不受影响。"""
    trace_store.configure(max_traces=1)
    client.get("/items/1", headers={HEADER: "evict-metrics-1"})
    client.get("/items/2", headers={HEADER: "evict-metrics-2"})  # 挤掉前者

    # 时间线查不到了
    assert client.get("/traces/evict-metrics-1").status_code == 410
    # 但实时结构化日志仍完整保留（caplogs 独立于 trace_store）
    ids = {r["correlation_id"] for r in caplogs}
    assert "evict-metrics-1" in ids
    # 指标照样统计了两个请求，不受淘汰影响
    snap = client.get("/metrics").json()
    assert snap["requests_total"] == 2


def test_stats_endpoint_reports_capacity(client) -> None:
    trace_store.configure(max_traces=7, ttl_seconds=120.0)
    stats = client.get("/traces/stats").json()
    assert stats["max_traces"] == 7
    assert stats["ttl_seconds"] == 120.0
    assert stats["stored_traces"] >= 1


# --------------------------------------------------------------------------- #
# 5. 任何入口都不被排除
# --------------------------------------------------------------------------- #
def test_healthz_has_correlation_id_and_timeline(client) -> None:
    """/healthz 不再被整体排除：响应头回传 ID，事后能查到时间线。"""
    resp = client.get("/healthz", headers={HEADER: "plat-health-1"})
    assert resp.status_code == 200
    assert resp.headers[HEADER] == "plat-health-1"

    data = client.get("/traces/plat-health-1")
    assert data.status_code == 200
    tl = data.json()
    assert tl["path"] == "/healthz"
    assert tl["status_code"] == 200
    assert [e["kind"] for e in tl["events"]][0] == "request_start"
    assert tl["events"][-1]["kind"] == "request_end"


def test_metrics_endpoint_has_timeline_but_not_counted_in_metrics(client) -> None:
    """兼容口径：/metrics 自身有时间线，但仍不计入业务指标。"""
    resp = client.get("/metrics", headers={HEADER: "plat-metrics-1"})
    assert resp.status_code == 200
    assert resp.headers[HEADER] == "plat-metrics-1"

    tl = client.get("/traces/plat-metrics-1").json()
    assert tl["path"] == "/metrics"
    assert tl["status_code"] == 200

    snap = client.get("/metrics").json()
    # /healthz、/metrics、/traces/* 都不进业务指标
    assert snap["requests_total"] == 0


def test_trace_endpoint_itself_is_traced(client) -> None:
    """查询时间线的请求本身也留下时间线（自举验证，不被排除）。"""
    resp = client.get("/traces/any-id-at-all", headers={HEADER: "self-trace-1"})
    assert resp.headers[HEADER] == "self-trace-1"

    tl = client.get("/traces/self-trace-1").json()
    assert tl["path"] == "/traces/any-id-at-all"
    assert tl["status_code"] == 404  # 查询目标不存在
    assert tl["events"][-1]["kind"] == "request_end"
    assert tl["events"][-1]["fields"]["status_code"] == 404


def test_auto_generated_id_is_also_queryable(client) -> None:
    """不带入站头自动生成的 ID，取回后同样能查到时间线。"""
    resp = client.get("/")
    cid = resp.headers[HEADER]
    assert len(cid) == 32
    got = client.get(f"/traces/{cid}")
    assert got.status_code == 200
    assert got.json()["correlation_id"] == cid


# --------------------------------------------------------------------------- #
# 6. 脱敏与统一 JSON 格式
# --------------------------------------------------------------------------- #
def test_sensitive_values_never_appear_in_timeline(client) -> None:
    """时间线事件中出现的异常请求头也必须是脱敏后的，原始密钥不得出现。"""
    secret = "Bearer timeline-secret-value"
    client.get(
        "/fail",
        headers={
            "Authorization": secret,
            "X-Api-Key": "ak-tl-999",
            HEADER: "tl-mask-1",
        },
    )
    raw = client.get("/traces/tl-mask-1").content.decode()
    assert "timeline-secret-value" not in raw
    assert "ak-tl-999" not in raw
    data = json.loads(raw)
    err_headers = None
    for event in data["events"]:
        if "request_headers" in event["fields"]:
            err_headers = event["fields"]["request_headers"]
    assert err_headers is not None
    assert err_headers["authorization"] == "***"
    assert err_headers["x-api-key"] == "***"


def test_all_log_output_is_single_line_json() -> None:
    """框架日志（uvicorn.*）同样被接管为 JSON，不再有明文 handler。"""
    import logging
    from logging import StreamHandler as _SH

    from app.logging_setup import JsonFormatter, configure_logging

    configure_logging(logging.INFO)

    def _our_handlers(name: str):
        # 只看我们自己配置的 StreamHandler（pytest 会另挂捕获用 handler）
        return [
            h for h in logging.getLogger(name).handlers if type(h) is _SH
        ]

    for name in ("uvicorn", "uvicorn.error"):
        handlers = _our_handlers(name)
        assert handlers, f"{name} 缺少 JSON handler"
        assert all(isinstance(h.formatter, JsonFormatter) for h in handlers)
        assert logging.getLogger(name).propagate is False
    # uvicorn 自带明文访问日志被关闭（访问口径统一为结构化 request completed）
    assert logging.getLogger("uvicorn.access").disabled is True
