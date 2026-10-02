"""关联 ID 测试：沿用/生成、并发不串号、后台任务保持、日志贯穿。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.main import app


def test_caller_correlation_id_is_reused(client: httpx.Client, logs: list[dict]) -> None:
    """调用方自带 X-Correlation-ID 时全程沿用，并原样回传。"""
    response = client.get("/whoami", headers={"X-Correlation-ID": "trace-abc-001"})

    assert response.status_code == 200
    assert response.headers["x-correlation-id"] == "trace-abc-001"
    assert response.json()["correlation_id"] == "trace-abc-001"
    assert response.json()["from_scope"] == "trace-abc-001"

    request_logs = [l for l in logs if l["correlation_id"] == "trace-abc-001"]
    assert request_logs, "没有任何日志携带该关联 ID"
    assert all(l["correlation_id"] == "trace-abc-001" for l in request_logs)


def test_correlation_id_generated_when_absent(client: httpx.Client) -> None:
    """调用方未提供时自动生成 32 位十六进制 ID 并回传。"""
    response = client.get("/whoami")

    cid = response.headers["x-correlation-id"]
    assert response.status_code == 200
    assert len(cid) == 32
    assert all(ch in "0123456789abcdef" for ch in cid)
    assert response.json()["correlation_id"] == cid


def test_generated_correlation_ids_are_unique(client: httpx.Client) -> None:
    """连续请求生成的关联 ID 互不相同。"""
    ids = {client.get("/whoami").headers["x-correlation-id"] for _ in range(20)}
    assert len(ids) == 20


@pytest.mark.parametrize("concurrency", [1, 8, 50])
def test_concurrent_requests_do_not_cross_talk(concurrency: int) -> None:
    """并发请求各自携带不同关联 ID，休眠醒来后仍只能读到自己的 ID。"""

    async def run() -> list[tuple[str, str, int]]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as ac:
            async def one(i: int) -> tuple[str, str, int]:
                cid = f"concurrent-cid-{i:03d}"
                r = await ac.get(
                    "/concurrent", params={"delay_ms": 20},
                    headers={"X-Correlation-ID": cid},
                )
                return cid, r.json()["correlation_id"], r.status_code

            return await asyncio.gather(*(one(i) for i in range(concurrency)))

    results = asyncio.run(run())
    assert len(results) == concurrency
    # 端点内部已断言“醒来后关联 ID 仍是自己的”，失败会返回 500
    for sent, got, status in results:
        assert status == 200
        assert sent == got, f"关联 ID 串号: 发送 {sent}，实际 {got}"
    assert len({got for _, got, _ in results}) == concurrency


def test_background_task_keeps_correlation_id(
    client: httpx.Client, logs: list[dict]
) -> None:
    """请求转后台任务后关联 ID 不丢失；后台结束后也不泄漏给后续请求。"""
    response = client.get("/background")
    cid = response.headers["x-correlation-id"]

    bg_logs = [l for l in logs if l.get("bg_correlation_id")]
    assert len(bg_logs) == 1
    assert bg_logs[0]["bg_correlation_id"] == cid
    assert bg_logs[0]["expected"] == cid
    assert bg_logs[0]["correlation_id"] == cid

    # 后续请求不应复用到上一个请求的关联 ID
    next_response = client.get("/whoami")
    assert next_response.headers["x-correlation-id"] != cid
    assert len(next_response.headers["x-correlation-id"]) == 32


def test_every_log_line_of_request_carries_cid(
    client: httpx.Client, logs: list[dict]
) -> None:
    """同一次请求产生的所有日志（业务日志 + 访问日志）都带关联 ID。"""
    cid = "per-line-cid"
    client.get("/whoami", headers={"X-Correlation-ID": cid})

    lines = [l for l in logs if l.get("correlation_id") == cid]
    assert len(lines) >= 2  # 业务 "whoami called" + 访问日志
    for line in lines:
        assert line["correlation_id"] == cid
