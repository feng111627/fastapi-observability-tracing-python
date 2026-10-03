"""pytest 公共夹具与测试进度输出。

- ``caplogs``：捕获本应用产生的结构化 JSON 日志，实时解析成 dict 列表，
  便于断言关联 ID、异常链、脱敏等；
- ``client``：基于 ``TestClient`` 的 FastAPI 客户端，每个用例前重置
  全局指标，保证用例互不干扰；
- 每个用例开始/通过/失败时输出一行进度，``pytest -s`` 时可以直观看到
  覆盖了哪些用例及各自结果（PASSED/FAILED 汇总由 pytest ``-v`` 提供）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.logging_setup import CorrelationFilter, JsonFormatter
from app.main import app
from app.metrics import metrics
from app.tracing import trace_store


class _JsonCaptureHandler(logging.Handler):
    """产生日志时立即格式化为 JSON 并解析进内存列表。"""

    def __init__(self) -> None:
        super().__init__()
        self.addFilter(CorrelationFilter())
        self.setFormatter(JsonFormatter())
        self.records: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(json.loads(self.format(record)))


@pytest.fixture
def caplogs() -> Iterator[list[dict]]:
    """返回一个实时填充的日志记录列表（用例体内即可读取）。"""
    handler = _JsonCaptureHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield handler.records
    finally:
        root.removeHandler(handler)


@pytest.fixture
def client() -> Iterator[TestClient]:
    metrics.reset()
    trace_store.reset()
    # 测试期间静默控制台 JSON 流（只压 StreamHandler；TraceCaptureHandler
    # 等内存态 handler 保持生效，时间线归档不受静默影响）。
    root = logging.getLogger()
    saved_levels = [
        (h, h.level) for h in root.handlers if isinstance(h, logging.StreamHandler)
    ]
    for h, _ in saved_levels:
        h.setLevel(logging.CRITICAL + 1)
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for h, level in saved_levels:
            h.setLevel(level)


def pytest_runtest_setup(item: pytest.Item) -> None:
    print(f"\n▶ 开始用例: {item.nodeid}", flush=True)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call":
        if report.passed:
            print(
                f"✔ 用例通过: {item.nodeid} ({report.duration:.3f}s)",
                flush=True,
            )
        elif report.failed:
            print(f"✗ 用例失败: {item.nodeid}", flush=True)
