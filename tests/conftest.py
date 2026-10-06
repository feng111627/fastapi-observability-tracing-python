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


# 测试文件 -> 覆盖的业务域（用于结束时的汇总输出）
_AREA_BY_MODULE = {
    "test_correlation": "关联 ID（沿用/生成、并发与后台不串号）",
    "test_exception_chain": "异常链与日志（完整链路、脱敏、字段）",
    "test_metrics": "指标（并发计数、错误分类、直方图、进行中）",
    "test_tracing": "事后时间线（首尾记录、纯读查询、淘汰、未命中区分）",
}


def pytest_terminal_summary(terminalreporter) -> None:
    """会话结束时按业务域汇总各模块的用例数与通过情况。"""
    stats = terminalreporter.stats
    modules: dict[str, dict[str, int]] = {}
    for status, key in (("passed", "passed"), ("failed", "failed"),
                        ("error", "error"), ("skipped", "skipped")):
        for report in stats.get(status, []):
            module = report.nodeid.split("::")[0].rsplit("/", 1)[-1]
            module = module.removesuffix(".py")
            modules.setdefault(module, {}).setdefault(key, 0)
            modules[module][key] += 1
    print("\n===== 覆盖业务域汇总 =====", flush=True)
    for module, counts in sorted(modules.items()):
        area = _AREA_BY_MODULE.get(module, module)
        parts = "，".join(f"{k}={v}" for k, v in sorted(counts.items()))
        total = sum(counts.values())
        ok = counts.get("passed", 0)
        mark = "✔" if ok == total else "✗"
        print(f"{mark} {area}：{ok}/{total} 通过（{parts}）", flush=True)
