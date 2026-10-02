"""关联 ID 测试：沿用/生成规则、响应头回传、并发不串号、后台任务不丢失。"""

from __future__ import annotations

import concurrent.futures
import threading

from app.context import (
    get_correlation_id,
    new_correlation_id,
    resolve_correlation_id,
)

HEADER = "X-Correlation-ID"


def test_generate_when_absent(client) -> None:
    """调用方未带头时自动生成，并通过响应头可取回。"""
    resp = client.get("/echo-correlation-id")
    assert resp.status_code == 200
    cid = resp.headers.get(HEADER)
    assert cid, "响应头必须回传关联 ID"
    assert len(cid) == 32  # uuid4 hex
    assert resp.json()["correlation_id"] == cid


def test_reuse_inbound_id(client) -> None:
    """调用方自带 ID 时原样沿用，不重新生成。"""
    resp = client.get(
        "/echo-correlation-id", headers={HEADER: "upstream-trace-abc-1"}
    )
    assert resp.headers[HEADER] == "upstream-trace-abc-1"
    assert resp.json() == {
        "correlation_id": "upstream-trace-abc-1",
        "header_echo": "upstream-trace-abc-1",
    }


def test_blank_inbound_id_is_replaced(client) -> None:
    """入站头只有空白时视为缺失，重新生成。"""
    resp = client.get("/echo-correlation-id", headers={HEADER: "   "})
    cid = resp.headers[HEADER]
    assert len(cid) == 32


def test_error_response_also_carries_correlation_id(client) -> None:
    """500 响应体和响应头都能取回关联 ID，便于调用方上报问题。"""
    resp = client.get("/fail", headers={HEADER: "err-trace-77"})
    assert resp.status_code == 500
    assert resp.headers[HEADER] == "err-trace-77"
    assert resp.json()["correlation_id"] == "err-trace-77"


def test_all_log_lines_for_request_share_same_id(client, caplogs) -> None:
    """同一请求产生的所有日志（业务 + 访问日志）关联 ID 一致。"""
    client.get("/items/42", headers={HEADER: "log-trace-5"})
    related = [
        r for r in caplogs if r["correlation_id"] == "log-trace-5"
    ]
    assert len(related) >= 2  # fetching item + request completed
    assert any(r["message"] == "request completed" for r in related)
    assert all(r["correlation_id"] == "log-trace-5" for r in related)


def test_resolve_rules_unit() -> None:
    """直接验证沿用/生成的纯函数规则。"""
    assert resolve_correlation_id(" kept-id ") == "kept-id"
    assert resolve_correlation_id("") != ""  # 空串 -> 生成
    assert resolve_correlation_id(None) != ""
    a, b = new_correlation_id(), new_correlation_id()
    assert a != b and len(a) == 32


def test_concurrent_requests_do_not_mix_ids(client) -> None:
    """高频并发下每个请求只看到自己的关联 ID，绝不串号。

    使用线程池模拟多个并发调用方（TestClient 是同步客户端）；
    每个线程携带唯一 ID 请求 /slow 制造重叠窗口。
    """
    n = 40
    unique_ids = [f"concurrent-req-{i:03d}" for i in range(n)]
    errors: list[str] = []

    def hit(cid: str) -> tuple[str, str, str]:
        resp = client.get("/slow?ms=20", headers={HEADER: cid})
        body_id = resp.json()["correlation_id"]
        header_id = resp.headers[HEADER]
        return cid, body_id, header_id

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        for sent, body_id, header_id in pool.map(hit, unique_ids):
            if body_id != sent or header_id != sent:
                errors.append(f"串号: 发送={sent} 响应体={body_id} 响应头={header_id}")

    assert not errors, "\n".join(errors)


def test_contextvar_isolation_between_concurrent_tasks() -> None:
    """直接在 asyncio 任务级别验证 ContextVar 的隔离语义。"""
    import asyncio

    async def worker(cid: str) -> str:
        from app.context import bind_correlation_id, reset_correlation_id

        value, token = bind_correlation_id(cid)
        try:
            await asyncio.sleep(0.005)
            # 交错 await 后上下文仍应是本任务自己的值
            return get_correlation_id()
        finally:
            reset_correlation_id(token)

    async def scenario() -> None:
        ids = [f"ctx-{i}" for i in range(200)]
        results = await asyncio.gather(*(worker(i) for i in ids))
        assert results == ids

    asyncio.run(scenario())


def test_background_task_keeps_requester_correlation_id(client, caplogs) -> None:
    """请求转后台任务后，后台日志仍带发起方 ID，且不被其他请求污染。"""
    resp = client.post("/background", headers={HEADER: "bg-origin-1"})
    assert resp.status_code == 200
    assert resp.json()["correlation_id"] == "bg-origin-1"

    # 后台任务执行期间插入另一个请求，试图“串号”
    client.get("/echo-correlation-id", headers={HEADER: "bg-intruder-2"})

    # 等待后台任务（TestClient 会在退出 context 前 join，但这里显式等待）
    client.get("/slow?ms=120")

    bg_logs = [r for r in caplogs if r["logger"] == "app.background"]
    assert len(bg_logs) == 2, bg_logs
    assert all(r["correlation_id"] == "bg-origin-1" for r in bg_logs), bg_logs
    phases = {r["phase"]: r["correlation_id"] for r in bg_logs}
    assert phases == {"start": "bg-origin-1", "done": "bg-origin-1"}
    # 插入的请求没有串到后台任务上
    assert not any(
        r["correlation_id"] == "bg-intruder-2" and r["logger"] == "app.background"
        for r in caplogs
    )


def test_thread_local_background_jobs_under_concurrency(client) -> None:
    """多个后台任务并发时各自的线程上下文 ID 不互相覆盖。"""
    sent = [f"bg-concurrent-{i}" for i in range(10)]
    barrier = threading.Barrier(len(sent))

    # 直接在线程中模拟“拷贝 context 后执行后台工作”的语义
    import contextvars

    def job(cid: str) -> str:
        ctx = contextvars.copy_context()
        barrier.wait()

        def run() -> str:
            from app.context import bind_correlation_id

            value, _ = bind_correlation_id(cid)
            return get_correlation_id() if value == cid else "MIXED"

        return ctx.run(run)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(job, sent))
    assert results == sent
