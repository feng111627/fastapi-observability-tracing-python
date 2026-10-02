"""异常链完整记录、敏感头脱敏、访问日志字段的测试。"""

from __future__ import annotations

from app.logging_setup import (
    SENSITIVE_HEADERS,
    flatten_exception_chain,
    mask_headers,
)


def _find_error_log(caplogs, message: str = "unhandled exception while processing request"):
    matches = [r for r in caplogs if r["message"] == message]
    assert matches, f"未找到异常日志，实际日志: {[r['message'] for r in caplogs]}"
    return matches[-1]


def test_three_layer_exception_chain_is_logged(client, caplogs) -> None:
    """/fail 抛出 LookupError -> DatabaseError -> ServiceError，
    日志必须完整包含三层（含类型、消息、traceback），而不只是最外层。"""
    resp = client.get("/fail", headers={"X-Correlation-ID": "chain-1"})
    assert resp.status_code == 500

    err = _find_error_log(caplogs)
    chain = err["exc_chain"]
    types = [c["type"] for c in chain]

    assert types == [
        "app.routes.ServiceError",
        "app.routes.DatabaseError",
        "builtins.LookupError",
    ], types
    # 根因消息必须可见（最容易丢失的信息）
    assert chain[-1]["message"] == "row id=42 not found in local demo store"
    assert "failed to load item" == chain[1]["message"]
    assert "get_item service failed" == chain[0]["message"]
    # 每一层都有独立 traceback，且包含该异常的短类名
    for link in chain:
        assert "Traceback" in link["traceback"]
        short_name = link["type"].rsplit(".", 1)[-1]
        assert short_name in link["traceback"]
    # 异常日志本身也带关联 ID 与请求信息
    assert err["correlation_id"] == "chain-1"
    assert err["method"] == "GET"
    assert err["path"] == "/fail"
    assert err["level"] == "ERROR"


def test_access_log_has_required_fields_and_duration(client, caplogs) -> None:
    """访问日志必须带：关联 ID、method、path、status_code、耗时。"""
    client.get("/items/7", headers={"X-Correlation-ID": "access-1"})
    done = [r for r in caplogs if r["message"] == "request completed"]
    assert len(done) == 1
    rec = done[0]
    for field in (
        "timestamp",
        "level",
        "correlation_id",
        "method",
        "path",
        "status_code",
        "duration_ms",
    ):
        assert field in rec, field
    assert rec["correlation_id"] == "access-1"
    assert rec["method"] == "GET"
    assert rec["path"] == "/items/{item_id}"  # 路由模板而非 /items/7
    assert rec["status_code"] == 200
    assert isinstance(rec["duration_ms"], (int, float))
    assert rec["duration_ms"] >= 0


def test_error_access_log_status_and_error_type(client, caplogs) -> None:
    """500 请求的访问日志标记状态码与错误类型。"""
    client.get("/fail")
    rec = [r for r in caplogs if r["message"] == "request completed"][-1]
    assert rec["status_code"] == 500
    assert rec["error_type"] == "app.routes.ServiceError"


def test_sensitive_headers_are_masked(client, caplogs) -> None:
    """鉴权头等敏感头在日志里必须脱敏，原始值不得出现。"""
    secret = "Bearer super-secret-token-value"
    resp = client.get(
        "/fail",
        headers={
            "Authorization": secret,
            "X-Api-Key": "ak-123456",
            "Cookie": "session=abcdef",
            "X-Correlation-ID": "mask-1",
        },
    )
    assert resp.status_code == 500
    err = _find_error_log(caplogs)
    logged_headers = err["request_headers"]

    assert logged_headers["authorization"] == "***"
    assert logged_headers["x-api-key"] == "***"
    assert logged_headers["cookie"] == "***"
    # 非敏感头保留
    assert logged_headers["x-correlation-id"] == "mask-1"

    raw = str(caplogs)
    assert secret not in raw
    assert "super-secret-token-value" not in raw
    assert "ak-123456" not in raw
    assert "session=abcdef" not in raw


def test_mask_headers_does_not_mutate_input() -> None:
    """脱敏返回副本，不修改原始请求头字典。"""
    original = {"Authorization": "Bearer x", "Accept": "application/json"}
    masked = mask_headers(original)
    assert original["Authorization"] == "Bearer x"
    assert masked["Authorization"] == "***"
    assert masked["Accept"] == "application/json"


def test_sensitive_header_set_covers_common_auth_headers() -> None:
    assert {
        "authorization",
        "proxy-authorization",
        "cookie",
        "x-api-key",
        "x-auth-token",
    } <= SENSITIVE_HEADERS


def test_flatten_implicit_exception_context() -> None:
    """隐式异常链（except 中 raise 新异常但未用 from）也要完整保留。"""
    try:
        try:
            raise TimeoutError("upstream timeout")
        except TimeoutError:
            raise RuntimeError("wrapper without from")
    except RuntimeError as exc:
        chain = flatten_exception_chain(exc)

    assert [c["type"] for c in chain] == [
        "builtins.RuntimeError",
        "builtins.TimeoutError",
    ]
    assert chain[-1]["message"] == "upstream timeout"


def test_handled_exception_logged_with_chain_but_status_400(client, caplogs) -> None:
    """业务内处理掉的异常记录 WARNING + 异常链，访问日志状态码仍是 400。"""
    resp = client.get("/fail-handled")
    assert resp.status_code == 400

    warning = [r for r in caplogs if r["message"] == "handled business error"]
    assert len(warning) == 1
    assert warning[0]["level"] == "WARNING"
    assert warning[0]["exc_chain"][0]["type"] == "builtins.ValueError"

    access = [r for r in caplogs if r["message"] == "request completed"][0]
    assert access["status_code"] == 400
    # 4xx 不算服务端错误，中间件不标记 error_type
    assert access["error_type"] is None
