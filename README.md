# FastAPI 本地可观测性示例（关联 ID / 结构化日志 / 指标）

一套**完全本地化、零外部依赖、零真实凭据**的请求可观测性能力：服务在本机
跑起来后，可以清楚看到每个请求的关联 ID、来龙去脉、耗时和出错情况。

- 请求关联：每个请求一个贯穿全流程的 **Correlation ID**，支持上游沿用，
  响应头可取回，异步/后台任务/并发不串号；
- 结构化日志：每条日志为**单行 JSON**，带关联 ID、方法、路径、状态码、
  耗时；异常输出**完整异常链**（含被 `raise ... from ...` 包装的根因）；
  鉴权头等敏感信息自动脱敏；
- 指标与健康检查：`/metrics` 输出请求总数、错误数（按状态码与异常类型
  分类）、耗时分布直方图、当前进行中请求数；`/healthz` 为健康检查；
  全部计数在锁保护下原子更新，高并发不丢不串。

## 目录结构

```
app/
  context.py         # Correlation ID：ContextVar 绑定/沿用/生成
  logging_setup.py   # JSON formatter、异常链展开、敏感头脱敏
  metrics.py         # 线程安全的进程内指标（计数/直方图/进行中）
  middleware.py      # 中间件：串联 ID、访问日志、指标、异常兜底
  routes.py          # 演示路由 + /healthz + /metrics
  main.py            # create_app 应用工厂
tests/
  test_correlation.py     # 并发不串号、后台任务不丢失等
  test_exception_chain.py # 异常链完整记录、脱敏、日志字段
  test_metrics.py         # 并发计数准确、错误分类、直方图等
```

## 快速开始

```bash
uv sync                       # 安装依赖（含 dev 测试依赖）
uv run python main.py         # 启动服务，监听 127.0.0.1:8000
# 或：uv run uvicorn app.main:app --port 8000
```

### 本地验证步骤

```bash
# 1) 健康检查
curl -s http://127.0.0.1:8000/healthz
# {"status":"ok"}

# 2) 自带关联 ID（被沿用并在响应头回传）
curl -s -D - -H 'X-Correlation-ID: my-trace-001' \
  http://127.0.0.1:8000/echo-correlation-id

# 3) 不带 ID（自动生成 32 位 uuid4 hex，响应头可取回）
curl -s -D - http://127.0.0.1:8000/echo-correlation-id

# 4) 触发三层异常链（500），服务端日志可见完整链与脱敏头
curl -s -H 'Authorization: Bearer secret-token' \
  http://127.0.0.1:8000/fail

# 5) 后台任务（响应后执行，日志仍带发起方 ID）
curl -s -X POST -H 'X-Correlation-ID: bg-1' \
  http://127.0.0.1:8000/background

# 6) 慢请求（观察耗时分布）与指标
curl -s 'http://127.0.0.1:8000/slow?ms=20'
curl -s http://127.0.0.1:8000/metrics | python -m json.tool
```

服务端控制台输出的是单行 JSON 日志，例如：

```json
{"timestamp":"2026-10-03T05:10:00","level":"INFO","logger":"app.access",
 "message":"request completed","correlation_id":"998ef07b...",
 "method":"GET","path":"/fail","status_code":500,"duration_ms":3.99,
 "error_type":"app.routes.ServiceError"}
```

## 关联 ID（Correlation ID）规则

- 请求头：`X-Correlation-ID`（常量见 `app.context.CORRELATION_ID_HEADER`）。
- **沿用**：入站头存在且 `strip()` 后非空 → 原样沿用，不做改写。
- **生成**：入站头缺失或为空白 → 生成 `uuid4().hex`（32 位十六进制）。
- **贯穿**：ID 绑定在 `contextvars.ContextVar` 上。asyncio 每个任务拷贝
  独立 context，FastAPI 的 `BackgroundTasks` 在线程池执行时同样拷贝当前
  context，因此：
  - 请求中途转后台任务，后台日志仍带**发起请求**的 ID；
  - 多请求高并发、交错 `await`、后台线程并发都不会串号或丢失。
- **取回**：
  - 所有业务响应（含 500）都带响应头 `X-Correlation-ID`；
  - 500 响应体也含 `correlation_id` 字段；
  - 代码内用 `app.context.get_correlation_id()` 随时获取。

## 日志字段

每条日志为单行 JSON，稳定字段如下：

| 字段 | 含义 |
| --- | --- |
| `timestamp` | 本地时间 `YYYY-MM-DDTHH:MM:SS` |
| `level` | 日志级别（INFO/WARNING/ERROR） |
| `logger` | logger 名称（如 `app.access`、`app.background`） |
| `message` | 日志消息 |
| `correlation_id` | 当前请求的关联 ID |
| `method` | 请求方法（仅请求上下文内有值） |
| `path` | 路由模板（如 `/items/{item_id}`） |
| `status_code` | HTTP 状态码（访问日志） |
| `duration_ms` | 请求耗时，毫秒（访问日志） |
| `error_type` | 未处理异常的类型全名（仅出错时） |
| `exc_chain` | 异常链数组（见下） |
| `request_headers` | 异常时记录的**脱敏后**请求头 |

`exc_chain` 为「最外层 → 最内层根因」的数组，每项含 `type`（异常类型
全名）、`message`、`traceback`；同时支持显式链（`raise X from Y` →
`__cause__`）与隐式链（except 内再抛 → `__context__`），不会只剩最外层
一句话。

### 敏感信息脱敏

`app/logging_setup.py` 的 `mask_headers` 对以下请求头（大小写不敏感）统一
替换为 `***`：`Authorization`、`Proxy-Authorization`、`Cookie`、
`Set-Cookie`、`X-Api-Key`、`X-Auth-Token`。脱敏在中间件进入时完成，
原始值不会进入日志。

## 指标口径（GET /metrics，JSON）

| 指标 | 口径 |
| --- | --- |
| `requests_total` | 进程启动/重置后完成的请求总数（不含 `/healthz`、`/metrics` 本身） |
| `requests_by_route` | 按 `METHOD 路由模板` 聚合的请求数（避免高基数标签） |
| `errors_total_by_status` | 状态码 **>= 500** 的请求数，按状态码分类（如 `{"500": n}`） |
| `errors_total_by_type` | 未处理异常数，按异常类型全名分类（如 `app.routes.ServiceError`） |
| `inflight_requests` | 当前进行中请求数（进入 +1，结束/异常 -1，不会为负） |
| `latency_seconds_all` | 全局耗时直方图 + `sum`/`count` |
| `latency_seconds_by_route` | 按路由模板的耗时直方图 |

直方图桶边界（秒）：`0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0,
2.5, 5.0, 10.0`；`cumulative_counts[i]` 为 `<= buckets[i]` 的累计样本数，
`le_inf` 为 `+Inf` 桶（总样本数）。4xx（业务已处理错误）计入请求总数但
**不**计入服务端错误。

并发正确性：所有计数在单个 `threading.RLock` 保护的临界区内原子更新，
事件循环线程与后台任务线程池并发写入不丢数、不串错。

健康检查 `GET /healthz` 固定返回 `{"status":"ok"}`，不依赖任何下游，且
不纳入业务指标。

## 运行测试

```bash
uv run pytest -s          # -s 显示每个用例的开始/通过进度，-v 由 pyproject 内置
```

测试覆盖三类核心场景：

1. **并发请求下关联 ID 不串号**：40 个线程携带不同 ID 并发请求 `/slow`；
   200 个 asyncio 任务交错 await；后台任务执行期间插入其他请求验证 ID
   不被覆盖；多线程 context 拷贝语义验证。
2. **异常链完整记录**：`/fail` 的 `LookupError → DatabaseError →
   ServiceError` 三层链（类型/消息/各层 traceback）均被记录；隐式链
   展开；鉴权头/API key/Cookie 脱敏且原始值不出现在任何日志中；访问日志
   必填字段齐全。
3. **并发指标计数准确**：8 线程 × 500 次混合成功/错误写入，总数/错误数/
   直方图计数精确一致；真实 HTTP 端到端并发计数；慢请求重叠期间
   `inflight_requests` 可观测到 >= 2 且最终归零。

`-s` 输出形如：

```
▶ 开始用例: tests/test_metrics.py::test_concurrent_counter_accuracy
✔ 用例通过: tests/test_metrics.py::test_concurrent_counter_accuracy (0.010s)
```

## 设计说明

- 指标为进程内内存态，重启即清零，定位是**本地开发/单机排障**，不需要
  Prometheus、OTLP collector 或任何外部服务与凭据。
- `/healthz` 与 `/metrics` 不经过业务埋点（避免探针对指标的污染）。
