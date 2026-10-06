# FastAPI 本地可观测性示例（关联 ID / 结构化日志 / 指标 / 事后时间线）

一套**完全本地化、零外部依赖、零真实凭据**的请求可观测性能力：服务在本机
跑起来后，可以清楚看到每个请求的关联 ID、来龙去脉、耗时和出错情况；
请求响应完之后，还能按关联 ID 把整条时间线取回复盘。

- 请求关联：每个请求一个贯穿全流程的 **Correlation ID**，支持上游沿用，
  响应头可取回，异步/后台任务/并发不串号；
- 结构化日志：每条日志为**单行 JSON**，带关联 ID、方法、路径、状态码、
  耗时；异常输出**完整异常链**（含被 `raise ... from ...` 包装的根因）；
  鉴权头等敏感信息自动脱敏；
- 指标与健康检查：`/metrics` 输出请求总数、错误数（按状态码与异常类型
  分类）、耗时分布直方图、当前进行中请求数；`/healthz` 为健康检查；
  全部计数在锁保护下原子更新，高并发不丢不串；
- **事后时间线**：每个请求从进来到结束（含中途转出的后台工作）产生的
  事件按关联 ID 归档，`GET /traces/{correlation_id}` 随时取回整条时间线；
  存储容量有界（条数/单线事件数/保留时长均可调），超限按时间先后淘汰
  最旧的，本地长时间高频跑内存不膨胀。

## 目录结构

```
app/
  context.py         # Correlation ID：ContextVar 绑定/沿用/生成
  logging_setup.py   # JSON formatter、异常链展开、敏感头脱敏、时间线归档 handler
  metrics.py         # 线程安全的进程内指标（计数/直方图/进行中）
  tracing.py         # 容量有界的事后时间线存储（按关联 ID 归档事件）
  middleware.py      # 中间件：串联 ID、时间线、访问日志、指标、异常兜底
  routes.py          # 演示路由 + /healthz + /metrics + /traces/{id}
  main.py            # create_app 应用工厂
tests/
  test_correlation.py     # 并发不串号、后台任务不丢失等
  test_exception_chain.py # 异常链完整记录、脱敏、日志字段
  test_metrics.py         # 并发计数准确、错误分类、直方图等
  test_tracing.py         # 时间线查询、后台/并发交错、容量淘汰、未命中
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

# 7) 事后取回某条请求的完整时间线（请求早已响应完也能查）。
#    查询是纯读取：哪怕带着同一个关联 ID 去查，也不会重置/覆盖原记录。
curl -s -H 'X-Correlation-ID: bg-1' http://127.0.0.1:8000/traces/bg-1 \
  | python -m json.tool
# 事件列表的第一条固定是 request started（入口）、request finished（结束）
# 标志请求收尾；即使某请求一条业务日志都没有，这两条骨架事件也始终在。

# 8) 查一个从未出现过的 ID：HTTP 200 + found:false + reason=unknown
curl -s http://127.0.0.1:8000/traces/never-existed
# {"found":false,"correlation_id":"never-existed","reason":"unknown"}

# 9) 查一个「到过但已被淘汰」的 ID：found:false + reason=evicted
#    （本地验证可把容量调小再观察：TRACE_STORE_MAX_TRACES=1 uv run python main.py，
#     依次打两个不同 ID 的请求后查第一个）
# {"found":false,"correlation_id":"...","reason":"evicted","evicted_by":"capacity"}
```

服务端控制台输出的每一行都是单行 JSON（含 uvicorn 自身的服务器日志，
已在启动时统一并入同一 JSON 管道；uvicorn 的明文访问行由中间件的结构化
访问日志取代），例如：

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

## 事后时间线（GET /traces/{correlation_id}，JSON）

**解决什么问题**：实时日志只能盯着当下翻，请求一多就翻不动；响应发出去
之后想复盘「当时这条请求到底经历了什么」、以及它中途转出去的后台活儿
跑到哪了，实时日志和指标都回答不了。时间线把每个请求从进来到结束产生
的事件按关联 ID 归档，事后随时整条取回。

**记录什么**：请求进入时按关联 ID 开立时间线，并固定写入一条**入口骨架
事件**（`event_type=request_started`，来源 `logger=app.timeline`，带方法/
路径/时间点）；处理期间的每条业务日志（与 stdout 的 JSON 流**同一条管道**，
内容、脱敏、异常链完全一致）实时归档为事件；请求结束时落定汇总字段，并
固定写入一条**结束骨架事件**（`event_type=request_finished`，带最终状态码/
总耗时/错误类型）。因此任何请求——哪怕全程一条业务日志都没有（健康检查、
指标导出、纯计算的接口）——时间线里都有明确的首尾记录；走异常的请求也能
在结束事件与汇总字段里看到最终结果。响应发出后转出的后台工作产生的事件
仍会继续追加到发起请求的时间线上（排在结束事件之后，序号即真实顺序）。

**怎么查询**：`GET /traces/{correlation_id}`。查询是**纯读取**：命中就把
当时那一整份记录（全部事件 + 汇总）原样返回，绝不会因为查询请求自身携带
的关联 ID 而新建、重置或覆盖任何已有记录——带着同一个 ID 反复查，每次
拿到的内容一致。为此 `/traces/*` 查询入口自身不开立时间线（其余所有入口，
含 `/healthz`、`/metrics`，都照常记录）。快照在锁内一次性拷出，并发读写、
查询与淘汰同时发生时读到的也是一致的一个瞬间，不会半截、不会串号。

命中时（HTTP 200，`found: true`）：

| 字段 | 含义 |
| --- | --- |
| `started_at` / `ended_at` | 请求开始/结束的本地时间（ISO，毫秒） |
| `duration_ms` | 请求总耗时（毫秒） |
| `status_code` / `error_type` | 最终状态码 / 未处理异常类型全名 |
| `finished` | 请求是否已结束（结束后后台事件仍可能追加） |
| `truncated` | 事件数超单线上限被截断时为 `true` |
| `events[]` | 有序事件列表，每条含 `seq`（全局单调序号）、`timestamp`、`offset_ms`（相对开端的耗时）、`level`、`logger`（事件来自哪一块）、`message` 及该日志的全部结构化字段（含 `exc_chain`）。首/尾骨架事件额外带 `event_type`（`request_started` / `request_finished`），来源固定为 `logger=app.timeline` |

未命中时（HTTP 200，`found: false`）两种原因**明确区分**，都不是服务错误：

| 返回 | 含义 |
| --- | --- |
| `{"found": false, "reason": "unknown", ...}` | 这个 ID **从未到达过**本服务（或重启后已清零） |
| `{"found": false, "reason": "evicted", "evicted_by": "capacity" \| "ttl_expired", ...}` | 这个 ID **到过、但已被挤掉**：`capacity` 为条数超限淘汰，`ttl_expired` 为保留时长过期清理 |

未命中不会副作用般地开立空时间线，也不会伪装成一条空记录返回。

**顺序保证**：每条事件在锁内领取全局单调序号 `seq`，`offset_ms` 随之
单调不减；同一关联 ID 下并发或后台工作交错执行时，事件顺序即真实发生
顺序，不丢、不乱、不串号（每条事件自带 `correlation_id` 可核对）。

**容量与淘汰**（环境变量可调，默认值为本地长时间高频跑的合理上限）：

| 环境变量 | 默认 | 含义 |
| --- | --- | --- |
| `TRACE_STORE_MAX_TRACES` | `1000` | 最多保留多少条时间线，超出按开立先后淘汰最旧的 |
| `TRACE_STORE_MAX_EVENTS_PER_TRACE` | `200` | 单条时间线最多多少事件，超出停止追加并置 `truncated` |
| `TRACE_STORE_TTL_SECONDS` | `3600` | 保留时长（按最近活动时间计），过期即清理 |

淘汰只影响时间线存储本身：那条请求的实时日志照常输出、指标照常计数。
被淘汰/过期的关联 ID 会留下一条**有界墓碑**（数量随 `TRACE_STORE_MAX_TRACES`
有界），查询时据此区分「从未出现」与「到过但已被挤掉」；该 ID 若再次
到达则墓碑自动摘除。同一关联 ID 再次进入（如调用方重试复用同一 ID）视为
新请求，开立新的时间线。所有写入/查询/淘汰在单把 `threading.RLock` 内
完成，查询拿到的是一致快照，不会读到半截数据。

**覆盖范围与兼容**：关联 ID 绑定与响应头回传覆盖**所有入口**；时间线
记录覆盖除 `/traces/*` 查询入口外的所有入口（含 `/healthz`、`/metrics`），
打到任何入口的请求都能查到自己带首尾记录的时间线。`/traces/*` 是纯读取
入口，自身不入时间线，保证「查时间线」永远不会改动任何已记录的时间线。
`/healthz`、`/metrics`、`/traces/*` 仍不纳入业务指标与访问日志——关联
ID 沿用/生成规则、响应头回传方式、日志字段含义、指标口径与上一轮完全
一致。

## 运行测试

```bash
uv run pytest -s          # -s 显示每个用例的开始/通过进度，-v 由 pyproject 内置
```

测试覆盖四类核心场景：

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
4. **事后时间线**：响应结束后按关联 ID 取回完整时间线（汇总字段 + 有序
   事件）；无业务日志的请求（含 `/healthz`、`/metrics`）也有固定的入口/
   结束骨架事件；500 请求的时间线含完整异常链、错误类型与结束记录；
   带同一关联 ID 查自己不会重置/覆盖原记录、查询入口自身不留时间线；
   后台任务事件归入发起请求且排在访问日志之后；20 路并发请求时间线互不
   混入；8 线程 × 50 事件同线写入不丢不重、序号连续；并发读写与小容量
   淘汰同时发生时每份快照自洽（事件数/序号/相对耗时/关联 ID 一致）；
   容量超限按开立先后淘汰最旧、单线事件数截断、TTL 过期清理、淘汰后
   迟到事件不复活半截时间线、被淘汰不影响指标；复用同一 ID 开新线；
   未命中区分 `unknown`（从未出现）与 `evicted`（容量/TTL 淘汰）且查询
   无副作用。

`-s` 输出形如（结尾附按业务域的覆盖汇总，可直观看出覆盖了哪些业务及
各自通过情况）：

```
▶ 开始用例: tests/test_metrics.py::test_concurrent_counter_accuracy
✔ 用例通过: tests/test_metrics.py::test_concurrent_counter_accuracy (0.010s)
...
===== 覆盖业务域汇总 =====
✔ 关联 ID（沿用/生成、并发与后台不串号）：10/10 通过（passed=10）
✔ 异常链与日志（完整链路、脱敏、字段）：8/8 通过（passed=8）
✔ 指标（并发计数、错误分类、直方图、进行中）：10/10 通过（passed=10）
✔ 事后时间线（首尾记录、纯读查询、淘汰、未命中区分）：21/21 通过（passed=21）
```

## 设计说明

- 指标与时间线均为进程内内存态，重启即清零，定位是**本地开发/单机排障**，
  不需要 Prometheus、OTLP collector 或任何外部服务与凭据。
- 时间线存储容量有界（见上「容量与淘汰」），本地长时间高频请求下内存
  不无限增长；淘汰按时间先后作用于最旧的时间线。
- `/healthz`、`/metrics`、`/traces/*` 不经过业务埋点（避免探针与查询
  流量对指标的污染）；其中 `/healthz`、`/metrics` 的关联 ID 与时间线
  照常记录，`/traces/*` 作为纯读取入口自身不入时间线，保证查询永不
  改动已记录的内容。
- 时间线事件与 stdout 的 JSON 日志共用同一 filter/formatter 管道，因此
  按关联 ID 聚合一次请求的日志时，时间线里的内容与实时输出格式一致，
  不会混入格式不一致的明文行；敏感头脱敏也在进入管道前完成。
- uvicorn 自身日志在应用导入时与启动钩子中两次统一进 JSON 管道
  （`unify_uvicorn_logging`，覆盖 `uvicorn` CLI 与 `python main.py`
  两种启动方式）；`uvicorn.access` 的明文访问行关闭，由中间件的结构化
  访问日志（带关联 ID）取代。
