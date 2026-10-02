# FastAPI 本地可观测性示例（关联 ID / 结构化日志 / 指标 / 健康检查）

一个**不依赖任何外部服务或真实凭据**的本地可观测性样板：服务在本机跑起来后，
每个请求的来龙去脉（谁发起、经过哪里、耗时多少、是否出错、错在哪一层）
都能通过关联 ID、JSON 日志和进程内指标看清楚。

## 功能总览

| 能力 | 说明 |
| --- | --- |
| 请求关联 ID | 每个请求一个贯穿全流程的 ID；调用方自带则沿用，否则自动生成；通过响应头回传 |
| 上下文隔离 | 基于 `contextvars`，并发请求、后台任务、线程池依赖均不串号、不丢失 |
| 结构化日志 | 单行 JSON 输出到 stderr，统一字段，敏感头脱敏 |
| 异常链路 | 记录异常类型与完整因果链（`raise ... from ...` 的 `cause` 与隐式 `context`），每层带完整 traceback |
| 指标 | 请求总数、错误数（按异常类型/状态码区分）、耗时分布直方图、当前进行中请求数 |
| 健康检查 | `GET /health` |

## 目录结构

```
app/
  context.py        # 关联 ID 的 contextvars 上下文管理
  logging_setup.py  # JSON 格式化器、敏感头脱敏、异常链展开、日志初始化
  metrics.py        # 线程安全的进程内指标注册表
  middleware.py     # ASGI 中间件：注入关联 ID、访问日志、指标、后台任务上下文
  routes.py         # 健康检查、指标端点与演示端点
  main.py           # 应用组装
tests/              # 关联 ID / 日志异常链 / 指标并发 三组测试
```

## 快速开始

```bash
uv sync                       # 安装依赖（含 dev 组的 pytest）
uv run python main.py         # 或 uv run uvicorn app.main:app --reload
# 服务监听 http://127.0.0.1:8000
```

## 关联 ID 的生成与沿用规则

1. 读取请求头 **`X-Correlation-ID`**：
   - 调用方提供了非空值 → **原样沿用**（不做改写）；
   - 未提供或为空 → 自动生成一个 32 位十六进制的 UUID4（如 `9c14b21bc4854849bb36e442a4554690`）。
2. 关联 ID 写入 `contextvars.ContextVar`，该请求处理期间的**所有日志**
   （业务代码任意位置 `logging.getLogger(...)` 打出的日志）都会自动携带；
   同时放入 `request.state.correlation_id` 供端点直接读取。
3. 通过**响应头 `X-Correlation-ID`** 原样回传，调用方凭此把日志与请求对应起来。
4. 请求结束（无论成功或异常）后中间件会 reset 上下文，防止上下文复用
   （如线程池工作线程）时把上一个请求的 ID 泄漏给下一个请求。
5. **后台任务**（Starlette `BackgroundTask`，在响应发出后才执行）运行期间
   会重新绑定该请求的关联 ID，因此后台日志同样带正确 ID，执行结束后恢复现场。
6. 实现为纯 ASGI 中间件，每个 HTTP 请求在自己的 asyncio Task 中设置
   ContextVar：asyncio 并发天然隔离；`run_in_threadpool` 的同步依赖也会
   复制当前上下文，不会串号。

业务代码里取当前关联 ID：

```python
from app.context import get_correlation_id
cid = get_correlation_id()
```

## 日志

- 格式：**单行 JSON**（stderr），UTF-8，便于 `jq` 等工具解析；换行符会被转义，一条日志永远只有一行。
- 初始化：`app.main:create_app()` 调用 `logging_setup.setup_logging()`，
  并让 uvicorn 的日志传播到同一根 logger。

### 访问日志字段

每个请求结束产生一条 `logger=app.access` 的日志：

| 字段 | 含义 |
| --- | --- |
| `timestamp` | UTC ISO-8601 时间 |
| `level` | `INFO`(2xx) / `WARNING`(4xx) / `ERROR`(5xx 或未捕获异常) |
| `logger` | 日志记录器名（如 `app.access`、`app.demo`） |
| `message` | 人读消息 |
| `correlation_id` | 本请求关联 ID |
| `method` | HTTP 方法 |
| `path` | 路由模板（如 `/whoami`），拿不到时用原始路径 |
| `status_code` | HTTP 状态码 |
| `duration_ms` | 处理耗时（毫秒，浮点） |
| `request_headers` | 请求头（**敏感头已脱敏**） |
| `error_type` | 仅失败时：最外层异常的全限定类型名 |
| `exception` | 仅未捕获异常时：完整异常链（见下） |

业务日志用 `extra=` 传结构化字段即可，例如：

```python
logger.info("whoami called", extra={"from_scope": request.state.correlation_id})
```

### 异常链

`exception` 是一个数组，从最外层异常向内排列，每层含：

- `relation`：`root`（最外层）、`cause`（显式 `raise X from Y`）、
  `context`（处理 Y 时隐式抛出 X）；
- `type`：异常全限定名（`模块.类名`，如 `builtins.ValueError`）；
- `message`：异常消息；
- `traceback`：**该层自己的完整 traceback**，不是只有最外层一句话。

示例（`GET /boom`）：

```json
"exception": [
  {"relation": "root",  "type": "builtins.RuntimeError", "message": "服务初始化失败", "traceback": "..."},
  {"relation": "cause", "type": "builtins.ValueError",   "message": "配置项 timeout_ms 不是数字: 'abc'", "traceback": "..."}
]
```

### 敏感信息脱敏

以下请求头在日志中统一替换为 `[REDACTED]`（大小写不敏感）：
`Authorization`、`Proxy-Authorization`、`Cookie`、`Set-Cookie`、
`X-Api-Key`、`X-Auth-Token`。如需扩展，修改 `app/logging_setup.py` 的
`SENSITIVE_HEADERS`。脱敏在中间件记录访问日志时完成，密钥值不会进入任何日志。

## 指标

`GET /metrics` 返回进程内 JSON 快照（注册表见 `app/metrics.py`，
全部更新在 `threading.RLock` 保护下完成，多线程/asyncio 线程池并发不丢计数）：

| 指标 | 口径 |
| --- | --- |
| `requests_total` | 进程启动（或最近一次 reset）以来完成的请求总数 |
| `requests_by_status` / `requests_by_method` / `requests_by_path` | 按状态码 / 方法 / 路由模板的计数 |
| `errors_total` | 状态码 `>= 400` 的请求数 |
| `errors_by_status` | 按状态码（404/500…）区分的错误数 |
| `errors_by_type` | 按**异常类型**区分（如 `builtins.RuntimeError`、`builtins.ValueError`）；没有异常对象的错误记为 `HTTP <状态码>`（如 `HTTP 404`） |
| `latency_ms` | `count`（样本数）、`sum`（总耗时毫秒）、`buckets`（**累积**直方图，桶上界 5/10/25/50/100/250/500/1000/2500/5000/+Inf 毫秒） |
| `in_progress` | 当前正在处理、尚未结束的请求数（gauge；请求开始 +1、结束 -1，含异常路径） |

> 说明：`/metrics` 请求自身也会被计数。因此在 `/metrics` 响应生成的瞬间，
> 快照里的 `in_progress` 通常为 `1`，代表的正是这次查询本身；请求结束后即归 0。

## 端点一览

| 方法 路径 | 用途 |
| --- | --- |
| `GET /health` | 健康检查，返回 `{"status":"ok"}` |
| `GET /metrics` | 指标 JSON 快照 |
| `GET /` | 简单示例，回显关联 ID |
| `GET /whoami` | 回显关联 ID（ContextVar / request.state / 请求头），演示业务日志贯穿 |
| `GET /boom` | 抛出 `RuntimeError`（`from ValueError`），验证完整异常链 |
| `GET /flaky?fail=true|false` | 可控失败，构造不同异常类型的错误指标 |
| `GET /background` | 把工作转成后台任务，验证关联 ID 不丢失 |
| `GET /concurrent?delay_ms=20` | 有耗时的端点，用于并发关联 ID 与指标测试 |
| `GET /sensitive` | 验证鉴权头在日志中脱敏 |

## 本地验证方法

### 1. 手工验证（真实 HTTP 服务）

```bash
uv run uvicorn app.main:app --port 8000

# 沿用自带关联 ID（观察响应头与终端日志）
curl -i -H 'X-Correlation-ID: demo-trace-0001' \
     -H 'Authorization: Bearer secret123' \
     http://127.0.0.1:8000/sensitive

# 未带关联 ID → 自动生成并通过响应头回传
curl -i http://127.0.0.1:8000/whoami

# 异常链（终端日志里 exception 数组含 root RuntimeError 与 cause ValueError）
curl -i http://127.0.0.1:8000/boom

# 后台任务（日志出现 background task running 且关联 ID 与请求一致）
curl http://127.0.0.1:8000/background

# 指标
curl -s http://127.0.0.1:8000/metrics | python3 -m json.tool
```

用 `jq` 看单条日志示例：服务日志重定向到文件后，

```bash
grep '"path": "/boom"' server.log | tail -1 | jq '.exception[].type'
```

### 2. 自动化测试

```bash
uv run pytest
```

测试开启了 `-v`，并在每个用例前后打印中文的“开始用例 / 用例结果”，
可以清楚看到覆盖了哪些用例及各自通过情况。覆盖三类关键场景：

- **并发请求下关联 ID 不串号**（参数化 1/8/50 个并发请求，各自带不同 ID，
  端点休眠醒来后断言读到的仍是自己的 ID；另有后台任务保持 ID 与结束后不泄漏的用例）；
- **异常链被完整记录**（`raise from` 的 cause 链、隐式 context 链、
  每层 traceback 完整、异常类型字段正确）；
- **并发指标计数准确**（8/16 线程各数百次真实 HTTP 请求，断言请求总数、
  错误数、直方图样本数精确无丢失，`in_progress` 归零；另含异步并发进行中
  计数在飞行期间等于并发数的用例，以及注册表层面 16×1000 次的直接压测）。
- 此外覆盖敏感头脱敏、访问日志必备字段、4xx/5xx 日志级别、
  错误按异常类型区分、健康检查与指标端点。

所有能力均为进程内实现，无需数据库、消息队列、APM、OTLP collector
或任何真实凭据。
