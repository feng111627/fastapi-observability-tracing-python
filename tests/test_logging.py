"""结构化日志测试：日志字段、异常链完整记录、敏感信息脱敏。"""

from __future__ import annotations

import json

import httpx


def _access_log(logs: list[dict], path: str) -> dict:
    matches = [
        l for l in logs
        if l.get("logger") == "app.access" and l.get("path") == path
    ]
    assert matches, f"缺少 {path} 的访问日志: {[l.get('path') for l in logs]}"
    return matches[-1]


def test_access_log_has_required_fields(
    client: httpx.Client, logs: list[dict]
) -> None:
    """访问日志必须带关联 ID、方法、路径、状态码、耗时，且可被 JSON 解析。"""
    cid = "log-fields-cid"
    response = client.get("/whoami", headers={"X-Correlation-ID": cid})
    assert response.status_code == 200

    line = _access_log(logs, "/whoami")
    for field in ("timestamp", "level", "correlation_id", "method", "path",
                  "status_code", "duration_ms"):
        assert field in line, f"缺少字段 {field}"
    assert line["correlation_id"] == cid
    assert line["method"] == "GET"
    assert line["path"] == "/whoami"
    assert line["status_code"] == 200
    assert isinstance(line["duration_ms"], (int, float))
    assert line["duration_ms"] >= 0


def test_full_exception_chain_is_logged(
    client: httpx.Client, logs: list[dict]
) -> None:
    """异常日志记录最外层类型 + 完整因果链（含 raise from 的里层原因）。"""
    cid = "chain-cid"
    response = client.get("/boom", headers={"X-Correlation-ID": cid})
    assert response.status_code == 500

    line = _access_log(logs, "/boom")
    assert line["level"] == "ERROR"
    assert line["status_code"] == 500
    assert line["correlation_id"] == cid
    assert line["error_type"] == "builtins.RuntimeError"

    chain = line["exception"]
    assert isinstance(chain, list)
    assert len(chain) == 2, f"异常链不完整: {[c['type'] for c in chain]}"

    outer, inner = chain
    assert outer["relation"] == "root"
    assert outer["type"] == "builtins.RuntimeError"
    assert outer["message"] == "服务初始化失败"
    assert "raise RuntimeError" in outer["traceback"]

    assert inner["relation"] == "cause"
    assert inner["type"] == "builtins.ValueError"
    assert "timeout_ms" in inner["message"]
    # 里层原因必须带自己的完整 traceback，不能只剩一句话
    assert "raise ValueError" in inner["traceback"]


def test_implicit_exception_context_is_logged(
    client: httpx.Client, logs: list[dict]
) -> None:
    """隐式上下文（处理 A 时又抛 B）同样要被完整记录。"""
    response = client.get("/flaky", params={"fail": "1"})
    assert response.status_code == 500

    line = _access_log(logs, "/flaky")
    chain = line["exception"]
    assert len(chain) >= 1
    assert chain[0]["type"] == "builtins.ValueError"
    assert "flaky 被要求失败" in chain[0]["message"]


def test_sensitive_headers_are_redacted(
    client: httpx.Client, logs: list[dict]
) -> None:
    """鉴权头不得明文出现在日志里，非敏感头保留原值。"""
    response = client.get(
        "/sensitive",
        headers={
            "Authorization": "Bearer super-secret-token",
            "X-Correlation-ID": "redact-cid",
        },
    )
    assert response.status_code == 200

    line = _access_log(logs, "/sensitive")
    logged = json.dumps(line, ensure_ascii=False)
    assert "super-secret-token" not in logged
    assert line["request_headers"]["authorization"] == "[REDACTED]"
    assert line["request_headers"]["x-correlation-id"] == "redact-cid"


def test_4xx_logged_as_warning_and_5xx_as_error(
    client: httpx.Client, logs: list[dict]
) -> None:
    """4xx 记 WARNING，5xx 记 ERROR，2xx 记 INFO。"""
    client.get("/health")
    client.get("/definitely-not-found")
    client.get("/boom")

    assert _access_log(logs, "/health")["level"] == "INFO"
    assert _access_log(logs, "/definitely-not-found")["level"] == "WARNING"
    assert _access_log(logs, "/boom")["level"] == "ERROR"


def test_logs_are_single_line_json(client: httpx.Client) -> None:
    """根 handler 输出的每条日志都是可解析的单行 JSON。"""
    import logging
    from io import StringIO

    stream = StringIO()
    handler = logging.StreamHandler(stream)
    from app.logging_setup import JsonLogFormatter, CorrelationIdFilter
    from app.context import _correlation_id_var
    handler.setFormatter(JsonLogFormatter())
    handler.addFilter(CorrelationIdFilter(_correlation_id_var))
    logger = logging.getLogger("app.jsonline-test")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.info("多行\n内容也应单行输出", extra={"k": "v"})
    logger.removeHandler(handler)

    raw = stream.getvalue().strip()
    assert raw.count("\n") == 0
    parsed = json.loads(raw)
    assert parsed["message"] == "多行\n内容也应单行输出"
    assert parsed["k"] == "v"
