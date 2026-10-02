"""pytest 公共夹具。

- 每个用例前重置指标注册表，保证用例间隔离；
- 捕获 app 命名空间下的 JSON 日志（即时解析），供断言关联 ID / 异常链；
- 用例开始/结束打印中文标题与结果，方便从输出中看到覆盖了哪些用例。
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.context import _correlation_id_var
from app.logging_setup import CorrelationIdFilter, JsonLogFormatter
from app.main import app
from app.metrics import registry


class _JsonCapturingHandler(logging.Handler):
    """日志产生时立即解析成 dict 存入列表。"""

    def __init__(self, sink: list[dict]) -> None:
        super().__init__()
        self._sink = sink
        self.setFormatter(JsonLogFormatter())
        self.addFilter(CorrelationIdFilter(_correlation_id_var))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._sink.append(json.loads(self.format(record)))
        except Exception:  # pragma: no cover - 捕获逻辑不应影响用例
            self.handleError(record)


@pytest.fixture
def client() -> TestClient:
    """TestClient：500 以响应形式返回（不向上抛），便于断言。"""
    registry.reset()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def logs() -> list[dict]:
    """捕获 ``app`` 命名空间下的结构化日志（含 app.access / app.demo）。"""
    captured: list[dict] = []
    handler = _JsonCapturingHandler(captured)
    app_logger = logging.getLogger("app")
    app_logger.addHandler(handler)
    app_logger.setLevel(logging.DEBUG)
    try:
        yield captured
    finally:
        app_logger.removeHandler(handler)


def pytest_runtest_logstart(nodeid: str, location) -> None:
    print(f"\n===== 开始用例: {nodeid} =====")


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when == "call":
        outcome = {"passed": "通过", "failed": "失败", "skipped": "跳过"}.get(
            report.outcome, report.outcome
        )
        print(f"----- 用例结果: {outcome} -----")
