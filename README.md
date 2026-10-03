# FastAPI 本地可观测性示例（关联 ID / 结构化日志 / 指标 / 事后时间线）

一套**完全本地化、零外部依赖、零真实凭据**的请求可观测性能力：服务在本机
跑起来后，可以清楚看到每个请求的关联 ID、来龙去脉、耗时和出错情况。

- 请求关联：每个请求一个贯穿全流程的 **Correlation ID**，支持上游沿用，
  响应头可取回，异步/后台任务/并发不串号；
- 结构化日志：每条日志为**单行 JSON**，带关联 ID、方法、路径、状态码、
  耗时；异常输出**完整异常链**（含被 `raise ... from ...` 包装的根因）；
  鉴权头等敏感信息自动脱敏；
- 指标与健康检查：`/metrics` 输出请求总数、错误数（按状态码与异常类型
  分类）、耗时分布直方图、当前进行中请求数；`/healthz` 为健康检查；
  全部计数在锁保护下原子更新，高并发不丢不串；
- **事后可回溯时间线**：每个请求从入口到结束（含中途转出去的后台/异步
  工作、异常、最终结果）都归到自己的关联 ID 名下持久保存在进程内，
  请求响应完之后仍可按 ID 取回整条时间线复盘；存储有容量/保留时长
  上限并按最旧优先淘汰，支持长时间高频本地运行。

## 目录结构

```
app/
  context.py         # Correlation ID：ContextVar 绑定/沿用/生成
  logging_setup.py   # JSON formatter、异常链展开、敏感头脱敏、时间线 handler
  metrics.py         # 线程安全的进程内指标（计数/直方图/进行中）
  tracing.py         # 有界、线程安全的请求时间线存储（录制/查询/淘汰）
  middleware.py      # 中间件：串联 ID、访问日志、指标、时间线、异常兜底
  routes.py          # 演示路由 + /healthz + /metrics + /traces
  main.py            # create_app 应用工厂（可经环境变量调容量）
tests/
  test_correlation.py     # 并发不串号、后台任务不丢失等
  test_exception_chain.py # 异常链完整记录、脱敏、日志字段
  test_metrics.py         # 并发计数准确、错误分类、直方图等
  test_timeline.py        # 时间线查询/后台交错/容量淘汰/查不到的区分等
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

# 7) 事后按关联 ID 取回整条时间线（请求早已响应完也能查）
curl -s -H 'X-Correlation-ID: my-trace-001' http://127.0.0.1:8000/ -o /dev/null
curl -s http://127.0.0.1:8000/traces/my-trace-001 | python -m json.tool
# 后台任务的时间线（响应结束后才产生的事件也挂在 bg-1 名下）
curl -s http://127.0.0.1:8000/traces/bg-1 | python -m json.tool

# 8) 查不到的两种情况（都返回结构化 JSON，不报错）
curl -s -w ' [%{http_code}]\n' http://127.0.0.1:8000/traces/never-existed  # 404
curl -s http://127.0.0.1:8000/traces/stats | python -m json.tool          # 容量/占用
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

## 事后可回溯时间线（GET /traces/{correlation_id}）

### 解决什么问题

实时日志只能“盯着屏幕翻”：请求一多就翻不过来，请求返回之后想复盘当时
发生了什么也找不到，中途转出去的后台活儿更是串不起来。时间线在既有
关联 ID 与结构化日志之上加了一层**按请求归档、事后可取回**的能力：

- 请求一进来（`request_start`）就开一条时间线，业务处理、中途转出去的
  后台/异步工作、抛出的异常（`request_end` 前的异常事件）、最终结果
  全部按**真实发生顺序**追加为事件；
- 请求**响应完成之后**仍可按关联 ID 取回，后台任务在响应后才产生的
  事件也继续挂在**发起请求**的 ID 名下（elapsed 更大，排在结束事件后）；
- 每条事件都能看到时间点（`timestamp`）、相对请求开始的耗时
  （`elapsed_ms`）、级别（`level`）、消息（`message`）、来自哪一块
  （`component`，如 `middleware` / `app.routes` / `app.access` /
  `app.background`）与业务 `extra` 结构化字段（`fields`）；
- 整条线还能看到开始 `started_at`、结束 `finished_at`、总耗时
  `duration_ms`、最终状态码 `status_code`、错误类型 `error_type` 与
  整体状态 `status`。

所有事件共享一个进程内**全局单调递增序号 `seq`**。即使同一关联 ID 下
事件由事件循环线程与后台任务线程池交错产生，事件的先后也严格反映真实
发生顺序（不依赖各线程时钟），不会乱序、不丢；事件按关联 ID 分桶存储，
绝不会把别的请求的事件混进来。

### 查询与三种返回

```bash
curl -s http://127.0.0.1:8000/traces/<correlation_id>
```

| 情况 | HTTP | 响应 |
| --- | --- | --- |
| 命中 | `200` | 完整时间线 JSON，`status` 为 `completed` / `error`（5xx）/ `in_flight`（请求仍在处理中，也能看到截至当前的事件） |
| 该 ID **从未出现过** | `404` | `{"error":"trace_not_found", ...}` |
| 曾存在但**已被淘汰** | `410 Gone` | `{"error":"trace_evicted", ...}` |

查不到时返回的是**结构化 JSON 而不是抛错**，且 404 与 410 用不同状态码
和 `error` 码明确区分，调用方可分别处理。另有
`GET /traces/stats` 返回存储容量与当前占用。

### 容量与淘汰

时间线是进程内内存态，定位为**本地开发/单机排障**，为避免请求一直打进来
导致内存无限增长，同时受两道上限约束：

| 配置 | 默认 | 含义 |
| --- | --- | --- |
| `TRACE_MAX_TRACES` | `1000` 条 | 最多保留多少条请求时间线 |
| `TRACE_TTL_SECONDS` | `3600` 秒（1 小时） | 单条时间线最长保留时长 |

超出任一上限即按**开始时间从最旧开始淘汰**（条数用 FIFO，TTL 惰性淘汰：
写入/查询时顺带清理过期项）。被淘汰的 ID 会进入一个有界（默认 4096）的
“墓碑”集合，以便在近期内仍能回答“这条曾经有过，但已被淘汰”（410）；
墓碑自身也随 TTL 与容量清理，过期后退化为与“从未出现”一致的 404。

可用环境变量调整后启动：

```bash
TRACE_MAX_TRACES=500 TRACE_TTL_SECONDS=1800 uv run python main.py
```

### 并发正确性与对既有能力的影响

- 共享结构全部在单个 `threading.RLock` 临界区内访问；查询返回的是
  **深拷贝快照**，高并发写入 / 查询 / 淘汰同时发生时读不到半截数据、
  不错乱、不串号。
- 时间线录制是**旁路**：写存储失败绝不影响请求；某条时间线被淘汰只影响
  事后查询，**该请求本来的实时 JSON 日志与指标照常输出，不受影响**。
- 时间线里的异常事件只保留轻量的异常类型/消息；完整 `exc_chain`
  traceback 仍只走实时日志，避免时间线膨胀。

### 与既有口径的兼容范围

- **关联 ID 沿用/生成规则不变**：入站 `X-Correlation-ID` 去空白非空即
  沿用，否则生成 `uuid4().hex`。
- **响应头回传不变**：所有响应（含 500、平台端点）都带
  `X-Correlation-ID`。
- **日志字段含义与指标口径不变**：新增的时间线不改任何既有 JSON 日志
  字段与 `/metrics` 计数口径。
- **入口覆盖范围扩大**：本轮之前 `/healthz`、`/metrics` 被整体排除在
  关联/埋点之外；现在**没有任何入口被整体排除**——打到任何入口
  （含 `/healthz`、`/metrics`、`/traces/*`）都会绑定/回传关联 ID 并留下
  可查询的时间线。为不污染既有指标，这些平台端点仍**不纳入业务指标与
  业务访问日志**（这是“排除范围”的收窄：从“整体排除”变成“仅不计数”）。
- **日志统一为机器可解析格式**：除 `app.*` 业务日志外，`uvicorn` /
  `uvicorn.error` 也被接管为同样的单行 JSON，`uvicorn.access` 的明文
  访问行被关闭（访问口径统一为结构化 `request completed`），因此按关联
  ID 聚合一次请求的日志时不会混进格式不一致的明文行。
- **敏感信息照旧脱敏**：进入时间线的异常请求头与实时日志使用同一份
  脱敏结果，原始密钥不会出现在时间线中。

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
4. **事后时间线（tests/test_timeline.py，23 个）**：
   - 查询：响应后可取回完整时间线；事件含时间点/相对耗时/级别/消息/
     来源且按 seq 严格有序；业务 extra 字段入线；500 标记 error +
     错误类型 + 异常事件；4xx 记 WARNING 但不算服务端错误；处理中
     （in_flight）也可见已发生事件；
   - 查不到：从未出现 → 404，已淘汰 → 410，墓碑随 TTL 退化为 404，
     三者互不混淆且都不抛错；
   - 后台/并发交错：后台事件挂在发起方 ID 名下并排在结束事件后；
     30 线程交错各时间线只含自身事件、seq 全局不乱不丢；写入/查询/
     淘汰同时高并发时快照完整自洽、容量不超限；后台任务不被插入请求
     串号；
   - 容量边界：条数 FIFO 精确淘汰最旧、TTL 惰性淘汰、淘汰不影响实时
     日志与指标、`/traces/stats` 反映容量；
   - 全入口覆盖：`/healthz`、`/metrics`、`/traces/*` 自身都能查到
     时间线（平台端点不进业务指标），自动生成的 ID 也可查；
   - 脱敏与统一格式：原始密钥不出现在时间线；uvicorn 日志均为 JSON、
     无明文 handler。

`-s` 输出形如：

```
▶ 开始用例: tests/test_metrics.py::test_concurrent_counter_accuracy
✔ 用例通过: tests/test_metrics.py::test_concurrent_counter_accuracy (0.010s)
```

## 设计说明

- 指标与时间线均为进程内内存态，重启即清零，定位是**本地开发/单机排障**，
  不需要 Prometheus、OTLP collector 或任何外部服务与凭据。
- 时间线存储有界：受 `TRACE_MAX_TRACES` 与 `TRACE_TTL_SECONDS` 双重约束，
  最旧优先淘汰 + 有界墓碑区分 404/410，长时间高频本地跑内存也不会无限增长。
- 时间线是实时日志/指标的**旁路**：即使回溯存储出问题或时间线被淘汰，
  请求的实时处理、JSON 日志和指标都不受影响。
- `/healthz`、`/metrics` 与 `/traces/*` 不进业务指标/业务访问日志（避免
  观测端点自我污染），但**都**绑定关联 ID、回传响应头并录制时间线——
  不再有任何入口被整体排除在这套能力之外。
